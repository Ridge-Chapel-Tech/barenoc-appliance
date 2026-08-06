#!/bin/bash
# AI Technician: list managed UniFi network devices with health/uptime fields
# (read-only). Optional filters (combinable): type (ap|switch|gateway), status
# (online|offline).
# Usage: unifi_devices.sh [type] [status]
set -u
FILTER_TYPE="${1:-}"
FILTER_STATUS="${2:-}"

python3 - "$FILTER_TYPE" "$FILTER_STATUS" <<'PYEOF'
import sys, json, ssl, urllib.request, urllib.error

ftype, fstatus = (sys.argv[1] if len(sys.argv) > 1 else ""), (sys.argv[2] if len(sys.argv) > 2 else "")

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

params = []
if ftype:
    params.append("device_type=" + ftype)
if fstatus:
    params.append("status=" + fstatus)
qs = ("?" + "&".join(params)) if params else ""
data, err = call("GET", "/api/v1/unifi/devices" + qs, token=tok)
if err:
    print(json.dumps({"error": err}))
    sys.exit(1)
print(json.dumps(data))
PYEOF
