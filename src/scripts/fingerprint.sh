#!/bin/bash
# nmap fingerprinting of a single host — TCP service/version scan + MAC vendor
# + TTL-based OS guess. Read-only network probes; nothing is written to the
# target. Runs as the restricted pi-agent user (nmap -sT/-sV need no root).
#
# Usage: fingerprint.sh <ip>
# Outputs JSON:
#   {"ip": "...", "mac": "...", "vendor": "...", "hostname": "...", "ttl": N,
#    "os_guess": "...", "open_ports": [{"port":N,"protocol":"tcp","service":"...",
#                                      "product":"...","version":"..."}], "count": N}

IP="$1"
if ! [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "{\"error\": \"Invalid IP '$IP'\", \"count\": 0}"
  exit 1
fi

if ! command -v nmap >/dev/null 2>&1; then
  echo "{\"error\": \"nmap not installed on the agent host (sudo apt install nmap)\", \"count\": 0}"
  exit 1
fi

XML=$(mktemp)
# Read-only TCP connect scan: top 100 ports + service/version detection.
# -Pn skips host discovery (caller already confirmed the host is alive).
nmap -sT -Pn -sV --top-ports 100 --open --host-timeout 45s -oX "$XML" "$IP" >/dev/null 2>&1

TTL=""
TTL=$(ping -c 1 -W 2 "$IP" 2>/dev/null | sed -n 's/.*ttl=\([0-9]*\).*/\1/p' | head -1)

RESULT=$(TTL="$TTL" python3 - "$XML" <<'PYEOF'
import sys, json, os, xml.etree.ElementTree as ET

xml_path = sys.argv[1]
ttl_raw = os.environ.get("TTL", "")
ttl = int(ttl_raw) if ttl_raw.isdigit() else None

out = {"ip": "", "mac": "", "vendor": "", "hostname": "", "ttl": ttl,
       "os_guess": "", "open_ports": [], "count": 0}

try:
    root = ET.parse(xml_path).getroot()
    host = root.find("host")
    if host is not None:
        for a in host.findall("address"):
            if a.get("addrtype") == "mac":
                out["mac"] = a.get("addr", "")
                out["vendor"] = a.get("vendor", "")
            elif a.get("addrtype") == "ipv4" and not out["ip"]:
                out["ip"] = a.get("addr", "")
        hn = host.find("hostnames/hostname")
        if hn is not None:
            out["hostname"] = hn.get("name", "")
        for p in host.findall("ports/port"):
            st = p.find("state")
            if st is None or st.get("state") != "open":
                continue
            svc = p.find("service")
            port = {"port": int(p.get("portid", 0)), "protocol": p.get("protocol", "tcp"),
                    "service": "", "product": "", "version": ""}
            if svc is not None:
                port["service"] = svc.get("name", "")
                port["product"] = svc.get("product", "")
                port["version"] = svc.get("version", "")
            out["open_ports"].append(port)
except Exception:
    pass

out["count"] = len(out["open_ports"])


def guess_os(out, ttl):
    ports = {p["port"] for p in out["open_ports"]}
    if 445 in ports or 139 in ports:
        return "Windows workstation/server (SMB ports open)"
    if 9100 in ports:
        return "Printer (raw print port 9100)"
    if 5000 in ports or 5001 in ports:
        return "Synology NAS or media server (DSM 5000/5001)"
    if 161 in ports and (80 in ports or 443 in ports):
        return "Managed network device (SNMP + web UI)"
    if 22 in ports:
        base = "Unix/Linux" if (ttl and ttl <= 64) else "Unix-like"
        return base + " (SSH open)"
    if 53 in ports:
        return "DNS server / router (port 53)"
    if 80 in ports or 443 in ports:
        return "Web/management device (router, AP, switch, or NAS)"
    if ttl:
        if ttl > 100:
            return "Windows (TTL %d)" % ttl
        if ttl <= 64:
            return "Unix/Linux (TTL %d)" % ttl
        return "Network device (TTL %d)" % ttl
    return "Unknown"


out["os_guess"] = guess_os(out, ttl)
print(json.dumps(out))
PYEOF
)
rm -f "$XML"
echo "$RESULT"
