#!/bin/bash
# snmp_sweep.sh — SNMP discovery sweep: find gear (routers/switches/APs/
# printers/NAS) that answers SNMP and identify it. Runs after the ping sweep
# so we probe a short list (fast), but works standalone too.
#
# Usage: snmp_sweep.sh <cidr-list> [community]
#   cidr-list: comma-separated subnets, e.g. 10.0.4.0/24,10.0.8.0/24
# Outputs JSON: {"found": [{"ip","sysname","sysdescr","sysobjectid","vendor"}],
#                "count": N, "community": "..."}
set -u

TARGETS="${1:-}"
COMMUNITY="${2:-public}"
if [ -z "$TARGETS" ]; then
  echo '{"error": "no subnets given", "found": [], "count": 0}'
  exit 1
fi

# find SNMP-open hosts per subnet (UDP 161) — fast: no service versioning.
# 100.64.0.0/10 (CGNAT + Tailscale overlay) is never a valid gear identity.
HOSTS=$(mktemp)
for net in $(echo "$TARGETS" | tr ',' ' '); do
  nmap -sU -p 161 --open -Pn --host-timeout 20s --exclude 100.64.0.0/10 \
    -oG - "$net" 2>/dev/null \
    | awk '/161\/open/{print $2}' >> "$HOSTS"
done

RESULT=$(python3 - "$COMMUNITY" "$HOSTS" <<'PYEOF'
import subprocess, sys, json, re

community, hosts_file = sys.argv[1], sys.argv[2]
ips = []
try:
    with open(hosts_file) as f:
        ips = [l.strip() for l in f if l.strip()]
except Exception:
    pass

def _cgnat(ip):
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if a.version != 4:
        return False
    b = a.packed
    return b[0] == 100 and 64 <= b[1] <= 127

out = []
for ip in ips:
    if _cgnat(ip):
        continue
    def oid(oid_):
        try:
            r = subprocess.run(["snmpget", "-v2c", "-c", community, "-t", "3", "-r", "1",
                                "-Oqv", ip, oid_], capture_output=True, text=True, timeout=8)
            return r.stdout.strip()
        except Exception:
            return ""
    sysname = oid(".1.3.6.1.2.1.1.5.0")
    sysdescr = oid(".1.3.6.1.2.1.1.1.0")
    sysobj = oid(".1.3.6.1.2.1.1.2.0")
    if not sysname and not sysdescr:
        continue
    # vendor guess from sysObjectID enterprise number (1.3.6.1.4.1.<enterprise>)
    vendor = ""
    m = re.search(r"1\.3\.6\.1\.4\.1\.(\d+)", sysobj)
    if m:
        ent = m.group(1)
        vendor = {
            "9": "Cisco", "14988": "MikroTik", "2636": "Juniper", "11": "Hewlett-Packard",
            "8072": "Net-SNMP (Linux)", "171": "Synology", "236": "Hikvision",
            "11863": "Dahua", "6527": "Fortinet", "25506": "Huawei", "4413": "Brocade",
            "17163": "Proxmox", "343": "Dell", "674": "Netgear", "4526": "ZyXEL",
            "28614": "TP-Link", "41112": "Ubiquiti", "161": "Cisco-SMB",
        }.get(ent, f"enterprise {ent}")
    out.append({"ip": ip, "sysname": sysname, "sysdescr": sysdescr[:200],
                "sysobjectid": sysobj, "vendor": vendor})

print(json.dumps({"found": out, "count": len(out), "community": community}))
PYEOF
)
rm -f "$HOSTS"
echo "$RESULT"
