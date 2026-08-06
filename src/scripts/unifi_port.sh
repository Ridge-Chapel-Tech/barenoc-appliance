#!/bin/bash
# UniFi port VLAN assignment — approved agent action.
# Usage: unifi_port.sh <switch_mac> <port_idx> <tagged_csv> [native] [dry_run]
#   switch_mac: e.g. aa:bb:cc:dd:ee:01   port_idx: 7
#   tagged_csv: "Storage,vlan-4" or network IDs (comma separated)
#   native:     network name/id for the untagged VLAN, or "-" to keep current
#   dry_run:    "true" to preview without writing
# Delegates to the BareNOC API (which owns the UniFi credentials).
set -u

SW="$1"
PORT="$2"
TAGGED="$3"
NATIVE="${4:--}"
DRY="${5:-false}"

python3 - "$SW" "$PORT" "$TAGGED" "$NATIVE" "$DRY" <<'PYEOF'
import sys, json, ssl, urllib.request, urllib.error

sw, port, tagged_csv, native, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

import re
if not re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", sw):
    print(json.dumps({"error": "target must be a switch MAC address, got " + repr(sw) +
                       " - use the switch MAC (from the device list), not a name or IP"}))
    sys.exit(1)

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def call(method, path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        "https://localhost" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=20, context=ctx)
        return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode()).get("detail", str(e))
        except Exception:
            return None, str(e)
    except Exception as e:
        return None, str(e)


def load_creds():
    """Read the agent service-account credentials (0600, pi-agent-owned)."""
    creds = {}
    try:
        with open("/opt/barenoc/agent/credentials") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    creds[k.strip()] = v.strip()
    except Exception as e:
        print(json.dumps({"error": f"cannot read agent credentials: {e}"}))
        sys.exit(1)
    return creds


creds = load_creds()
login, err = call("POST", "/api/v1/auth/login",
                  {"username": creds.get("username", ""), "password": creds.get("password", "")})
if err or not login:
    print(json.dumps({"error": f"API login failed: {err}"}))
    sys.exit(1)
tok = login.get("access_token", "")

tagged = [x.strip() for x in tagged_csv.split(",") if x.strip()]
body = {"tagged": tagged, "dry_run": (dry == "true")}
if native and native != "-":
    body["native"] = native

res, err = call("POST", f"/api/v1/unifi/ports/{sw}/{port}/vlans", body, token=tok)
if err:
    print(json.dumps({"error": err}))
    sys.exit(1)
print(json.dumps(res))
PYEOF
