#!/bin/bash
# Write the appliance's tailscale status for the api container to read
# (volumes/remote_access/self.json, mounted read-only into barenoc-api).
OUT=/opt/barenoc/volumes/remote_access/self.json
TS=/usr/bin/tailscale
mkdir -p "$(dirname "$OUT")"
if [ -x "$TS" ] && "$TS" status --json > /tmp/ts.json 2>/dev/null; then
  python3 - "$OUT" <<'PYEOF'
import json, sys
try:
    d = json.load(open("/tmp/ts.json"))
    peers = d.get("Peer") or {}
    out = {
        "online": d.get("BackendState") == "Running",
        "hostname": d.get("Self", {}).get("HostName"),
        "tailscale_ip": (d.get("Self", {}).get("TailscaleIPs") or [None])[0],
        "node_key": d.get("Self", {}).get("ID"),
        "peers": [{"name": n.get("HostName"), "ip": (n.get("TailscaleIPs") or [None])[0],
                   "online": bool(n.get("Online"))} for n in peers.values()],
    }
    json.dump(out, open(sys.argv[1], "w"), indent=1)
except Exception as e:
    json.dump({"error": str(e)}, open(sys.argv[1], "w"))
PYEOF
else
  echo '{"online": false, "error": "tailscale not running"}' > "$OUT"
fi
chmod 644 "$OUT"
