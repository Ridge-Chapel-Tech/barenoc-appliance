#!/bin/bash
# nmap fingerprinting of a single host — TCP service/version scan + MAC vendor
# + reverse-DNS hostname + SSH-banner OS identification + TTL fallback.
# Read-only network probes; nothing is written to the target. Runs as the
# restricted pi-agent user (nmap -sT/-sV need no root; reverse DNS + banner
# grabs are unauthenticated).
#
# Usage: fingerprint.sh <ip>
# Outputs JSON:
#   {"ip": "...", "mac": "...", "vendor": "...", "hostname": "...", "ttl": N,
#    "os": "...", "os_reason": "ssh_banner|service|ttl",
#    "ssh_banner": "...", "open_ports": [...], "count": N}

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
# Read-only TCP connect scan: top 100 ports + service/version detection + reverse DNS.
nmap -sT -Pn -sV -R --top-ports 100 --open --host-timeout 45s -oX "$XML" "$IP" >/dev/null 2>&1

TTL=""
TTL=$(ping -c 1 -W 2 "$IP" 2>/dev/null | sed -n 's/.*ttl=\([0-9]*\).*/\1/p' | head -1)

# SSH banner — OpenSSH announces the exact distribution ("SSH-2.0-OpenSSH_9.6p1
# Ubuntu-3ubuntu13.5"); macOS uses LibreSSL, Windows says OpenSSH_for_Windows.
SSH_BANNER=""
if [ -n "$(nmap -p 22 --open -Pn -oG - "$IP" 2>/dev/null | grep -o '22/open' )" ]; then
  SSH_BANNER=$(timeout 5 bash -c "exec 3<>/dev/tcp/$IP/22 && IFS= read -r line <&3 && echo \"\$line\"" 2>/dev/null | head -1)
fi

RESULT=$(TTL="$TTL" SSH_BANNER="$SSH_BANNER" python3 - "$XML" <<'PYEOF'
import sys, json, os, socket, xml.etree.ElementTree as ET

xml_path = sys.argv[1]
ttl_raw = os.environ.get("TTL", "")
ttl = int(ttl_raw) if ttl_raw.isdigit() else None
ssh_banner = os.environ.get("SSH_BANNER", "")

out = {"ip": "", "mac": "", "vendor": "", "hostname": "", "ttl": ttl,
       "os": "", "os_reason": "", "ssh_banner": ssh_banner,
       "open_ports": [], "count": 0}

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

# Reverse-DNS fallback if nmap didn't resolve it
if not out["hostname"]:
    try:
        out["hostname"] = socket.gethostbyaddr(out.get("ip") or "")[0]
    except Exception:
        pass


def os_from_ssh_banner(b):
    """SSH banners identify the OS precisely."""
    b = b.strip()
    if not b.startswith("SSH-2.0-"):
        return ""
    body = b[len("SSH-2.0-"):]
    if "OpenSSH_for_Windows" in body:
        return "Windows (OpenSSH)"
    if "LibreSSL" in body:
        return "macOS (OpenSSH + LibreSSL)"
    # OpenSSH_9.6p1 Ubuntu-3ubuntu13.5  /  OpenSSH_9.6p1 Debian-4  /  Fedora
    parts = body.split()
    if len(parts) >= 2 and parts[1]:
        distro = parts[1]
        if "Ubuntu" in distro: return "Ubuntu Linux"
        if "Debian" in distro: return "Debian Linux"
        if "Fedora" in distro or "fc" in distro: return "Fedora Linux"
        if "Arch" in distro: return "Arch Linux"
        if "openSUSE" in distro or "SUSE" in distro: return "openSUSE"
        if "FreeBSD" in distro: return "FreeBSD"
        if "Raspbian" in distro: return "Raspberry Pi OS (Debian)"
        return distro
    return "Unix/Linux (SSH)"


def guess_os(out, ttl):
    ports = {p["port"] for p in out["open_ports"]}
    if out.get("ssh_banner"):
        os_name = os_from_ssh_banner(out["ssh_banner"])
        if os_name:
            out["os_reason"] = "ssh_banner"
            return os_name
    if 445 in ports or 139 in ports:
        out["os_reason"] = "service"
        return "Windows (SMB ports open)"
    if 9100 in ports:
        out["os_reason"] = "service"
        return "Printer (raw print port 9100)"
    if 5000 in ports or 5001 in ports:
        out["os_reason"] = "service"
        return "NAS / media server (DSM 5000/5001)"
    if 161 in ports and (80 in ports or 443 in ports):
        out["os_reason"] = "service"
        return "Managed network device (SNMP + web UI)"
    if 22 in ports:
        out["os_reason"] = "service"
        return "Unix/Linux (SSH open)" if (ttl and ttl <= 64) else "Unix-like (SSH open)"
    if ttl:
        out["os_reason"] = "ttl"
        if ttl > 100:
            return "Windows (TTL %d)" % ttl
        if ttl <= 64:
            return "Unix/Linux (TTL %d)" % ttl
        return "Network device (TTL %d)" % ttl
    out["os_reason"] = "ttl"
    return "Unknown"


out["os"] = guess_os(out, ttl)
print(json.dumps(out))
PYEOF
)
rm -f "$XML"
echo "$RESULT"
