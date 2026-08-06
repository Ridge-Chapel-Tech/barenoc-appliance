#!/bin/bash
# AI Technician: change a Wi-Fi SSID's passphrase (WPA2-PSK).
# Usage: unifi_set_ssid_password.sh <ssid> <passphrase>
set -u
SSID="$1"
PASS="$2"

python3 - "$SSID" "$PASS" <<'PYEOF'
import sys, json, ssl, urllib.request, urllib.error
from urllib.parse import quote

ssid, password = sys.argv[1], sys.argv[2]

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


if not ssid or not (8 <= len(password) <= 63):
    print(json.dumps({"error": "ssid required and password must be 8-63 characters"}))
    sys.exit(1)

creds = load_creds()
login, err = call("POST", "/api/v1/auth/login",
                  {"username": creds.get("username", ""), "password": creds.get("password", "")})
if err or not login:
    print(json.dumps({"error": f"API login failed: {err}"}))
    sys.exit(1)
tok = login.get("access_token", "")

data, err = call("POST", f"/api/v1/unifi/wlans/{quote(ssid, safe='')}/password",
                 data={"password": password}, token=tok)
if err:
    print(json.dumps({"error": err}))
    sys.exit(1)
print(json.dumps(data))
PYEOF
