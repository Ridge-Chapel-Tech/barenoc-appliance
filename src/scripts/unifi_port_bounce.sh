#!/bin/bash
# AI Technician: cycle a UniFi switch port (disable -> 2s -> enable).
# Usage: unifi_port_bounce.sh <switch_mac> <port_idx>
set -u
SW="$1"; PORT="$2"

python3 - "$SW" "$PORT" <<'PYEOF'
import sys, json, ssl, urllib.request, urllib.error

sw, port = sys.argv[1], sys.argv[2]

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
        r = urllib.request.urlopen(req, timeout=30, context=ctx)
        return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode()).get("detail", str(e))
        except Exception:
            return None, str(e)
    except Exception as e:
        return None, str(e)


def load_creds():
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

data, err = call("POST", f"/api/v1/unifi/ports/{sw}/{port}/bounce", token=tok)
if err:
    print(json.dumps({"error": err}))
    sys.exit(1)
print(json.dumps(data))
PYEOF
