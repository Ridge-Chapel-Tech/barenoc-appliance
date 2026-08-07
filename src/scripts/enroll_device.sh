#!/bin/bash
# enroll_device.sh — adopt a Linux device with a step-ca certificate (Phase F).
#
# Usage: enroll_device.sh <target_ip> <ssh_user> <ssh_key_path> [ttl]
#
# What it does (all over SSH, no internet needed on the device):
#   1. Logs into the BareNOC API (agent credentials) and mints a one-time
#      enrollment token for the device (POST /devices/<id>/adopt/cert).
#   2. Ships the step-cli binary (copied from the step-ca container) to the
#      device via scp.
#   3. On the device: installs step-cli, bootstraps the CA (by fingerprint),
#      enrolls the short-lived certificate with the token.
#   4. Installs a heartbeat script (renew cert when due + POST the mTLS
#      report) — the device is an adopted member, linked on first report.
set -u

TARGET="${1:-}"
SSH_USER="${2:-}"
SSH_KEY="${3:-}"
TTL="${4:-600}"

API_URL="${BARENOC_API_URL:-https://localhost}"
CA_URL="${STEPCA_URL:-https://stepca.barenoc.local:8443}"

fail() { echo "{\"error\": \"$*\"}" >&2; exit 1; }
[ -n "$TARGET" ] && [ -n "$SSH_USER" ] && [ -n "$SSH_KEY" ] || fail "usage: enroll_device.sh <target_ip> <ssh_user> <ssh_key_path> [ttl]"

python3 - "$TARGET" "$TTL" "$API_URL" <<'PYEOF' > /tmp/enroll-out.json 2>/dev/null || fail "could not mint enrollment token"
import sys, json, ssl, urllib.request, urllib.error

target, ttl, api = sys.argv[1], sys.argv[2], sys.argv[3]
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def call(method, path, data=None, token=None):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(api + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=20, context=ctx)
        return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        try: return None, json.loads(e.read().decode()).get("detail", str(e))
        except Exception: return None, str(e)
    except Exception as e:
        return None, str(e)

creds = {}
for line in open("/opt/barenoc/agent/credentials"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("="); creds[k.strip()] = v.strip()

login, err = call("POST", "/api/v1/auth/login", {"username": creds.get("username", ""), "password": creds.get("password", "")})
if err or not login: sys.exit("api login failed: %s" % err)
tok = login.get("access_token", "")

devs, err = call("GET", f"/api/v1/devices?ip={target}", token=tok)
if err: sys.exit("device lookup failed: %s" % err)
dev = None
for d in (devs or {}).get("devices", []):
    if d.get("ip_address") == target: dev = d; break
if not dev: sys.exit("no device with ip " + target)

res, err = call("POST", f"/api/v1/devices/{dev['id']}/adopt/cert", {"ttl": int(ttl)}, token=tok)
if err: sys.exit("adopt/cert failed: %s" % err)
print(json.dumps({"device_id": dev["id"], "cn": res["cn"], "token": res["token"],
                  "ca_url": res["ca_url"], "ca_fingerprint": res["ca_fingerprint"]}))
PYEOF
[ -s /tmp/enroll-out.json ] || fail "no enrollment data"

CN=$(python3 -c "import json;print(json.load(open('/tmp/enroll-out.json'))['cn'])")
TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/enroll-out.json'))['token'])")
CA_FP=$(python3 -c "import json;print(json.load(open('/tmp/enroll-out.json'))['ca_fingerprint'])")

# the device reaches the appliance at the same host as the CA, on 443
REPORT_HOST="${CA_URL#https://}"; REPORT_HOST="${REPORT_HOST%%:*}"
REPORT_URL="https://${REPORT_HOST}/api/v1/device/report"

docker cp barenoc-step-ca:/usr/local/bin/step /tmp/step 2>/dev/null || fail "cannot copy step-cli from the step-ca container"
scp -q -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new /tmp/step "$SSH_USER@$TARGET:/tmp/step" || fail "scp step-cli failed"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$SSH_USER@$TARGET" \
    bash -s -- "$CN" "$TOKEN" "$CA_URL" "$CA_FP" "$REPORT_URL" <<'REMOTE'
set -u
CN="$1"; TOKEN="$2"; CA_URL="$3"; CA_FP="$4"; REPORT_URL="$5"
install -m 0755 /tmp/step /usr/local/bin/step || { echo "install failed (need root?)"; exit 1; }
export STEPPATH=/root/.step
step ca bootstrap --ca-url "$CA_URL" --fingerprint "$CA_FP" < /dev/null >/dev/null 2>&1 || true
step ca certificate "$CN" /etc/barenoc-device.crt /etc/barenoc-device.key --token "$TOKEN" || { echo "enroll failed"; exit 1; }
chmod 600 /etc/barenoc-device.key
cat > /usr/local/bin/barenoc-device-heartbeat.sh <<HEART
#!/bin/bash
# Renew the device cert when it's within its renewal window, then report in.
/usr/local/bin/step ca renew /etc/barenoc-device.crt /etc/barenoc-device.key 2>/dev/null || true
curl -sk --cert /etc/barenoc-device.crt --key /etc/barenoc-device.key -X POST "$REPORT_URL" >/dev/null 2>&1 || true
HEART
chmod +x /usr/local/bin/barenoc-device-heartbeat.sh
( crontab -l 2>/dev/null | grep -v barenoc-device-heartbeat; echo "*/10 * * * * /usr/local/bin/barenoc-device-heartbeat.sh" ) | crontab -
echo "enrolled $CN; heartbeat cron installed (renew + mTLS report every 10 min)"
REMOTE
REMOTE_EXIT=$?
[ $REMOTE_EXIT -eq 0 ] || fail "remote enrollment failed (exit $REMOTE_EXIT)"
echo "{\"ok\": true, \"device\": \"$CN\", \"enrolled\": true, \"note\": \"cert + heartbeat installed; device links on first mTLS report\"}"
rm -f /tmp/enroll-out.json /tmp/step
