#!/bin/bash
# UniFi network (VLAN) creation — approved agent write action.
# Usage: unifi_network_create.sh <name> <vlan> [subnet] [dhcp_true_false]
#   name:  e.g. "IoT"          vlan: e.g. 12 (1-4094)
#   subnet: CIDR like 192.168.12.1/24 (optional; defaults to 192.168.<vlan>.1/24)
#   dhcp:  "true" (default) or "false"
# Delegates to the BareNOC API (which owns the UniFi credentials).
set -u

NAME="${1:-}"
VLAN="${2:-}"
SUBNET="${3:-}"
DHCP="${4:-true}"

python3 - "$NAME" "$VLAN" "$SUBNET" "$DHCP" <<'PYEOF'
import sys, json, ssl, urllib.request, urllib.error

name, vlan, subnet, dhcp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

if not name:
    print(json.dumps({"error": "network name is required (params.name)"}))
    sys.exit(1)
try:
    vlan = int(vlan)
except (TypeError, ValueError):
    print(json.dumps({"error": "vlan must be an integer, got " + repr(vlan)}))
    sys.exit(1)
if not 1 <= vlan <= 4094:
    print(json.dumps({"error": "vlan must be 1-4094, got " + str(vlan)}))
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

body = {"name": name, "vlan": vlan, "dhcp": (dhcp.lower() in ("1", "true", "yes", "on"))}
if subnet:
    body["subnet"] = subnet

res, err = call("POST", "/api/v1/unifi/networks", body, token=tok)
if err:
    print(json.dumps({"error": err}))
    sys.exit(1)
print(json.dumps(res))
PYEOF
