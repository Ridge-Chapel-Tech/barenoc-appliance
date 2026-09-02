#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC — post-adoption agent readiness check (NOC_Agent endpoints).
#
# After an endpoint installs NOC_Agent (agent_install.sh) and its first report
# links the device (method="agent"), run this ON the appliance to confirm the
# device is actually READY — the agent reported in (version + host facts) and
# is still alive (recent last_seen + cert_last_seen = the agent is polling
# jobs over mTLS) — not just cert-linked with no agent heartbeat.
#
#   sudo bash /opt/barenoc/scripts/verify_agent_adoption.sh <device_name|id> [max_age_minutes]
#
# Exits non-zero when any check fails (loud, never silent).
# ═══════════════════════════════════════════════════════════════════════════
set -u

DEVICE="${1:-}"
MAX_AGE="${2:-10}"
API="${BARENOC_API_URL:-https://127.0.0.1}"
CREDS_FILE="/opt/barenoc/agent/credentials"

[ -n "$DEVICE" ] || { echo "usage: $0 <device_name|device_id> [max_age_minutes]" >&2; exit 2; }

# Agent login — the credential FILE must agree with the DB (same contract as
# verify_agent_provision.sh). A missing/mismatched credential file surfaces
# here instead of an opaque 401 from the readiness endpoint.
if [[ -f "$CREDS_FILE" ]]; then
  USERNAME="$(grep -E '^username=' "$CREDS_FILE" | head -1 | cut -d= -f2-)"
  PASSWORD="$(grep -E '^password=' "$CREDS_FILE" | head -1 | cut -d= -f2-)"
  TOKEN="$(curl -sk -X POST "$API/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)"
else
  echo "✗ agent credentials file MISSING: $CREDS_FILE" >&2
  exit 1
fi
[ -n "$TOKEN" ] || { echo "✗ agent login failed — credentials file and DB are out of sync" >&2; exit 1; }

# Resolve a device name to an id via the devices search; a numeric arg is an id.
if echo "$DEVICE" | grep -qE '^[0-9]+$'; then
  DEV_ID="$DEVICE"
else
  DEV_ID="$(curl -sk -G "$API/api/v1/devices" \
    --data-urlencode "search=$DEVICE" \
    --data-urlencode "limit=5" \
    -H "Authorization: Bearer $TOKEN" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin).get("devices",[]);print(d[0]["id"] if d else "")' 2>/dev/null)"
  [ -n "$DEV_ID" ] || { echo "✗ device not found: $DEVICE" >&2; exit 1; }
fi

REPORT="$(curl -sk "$API/api/v1/devices/$DEV_ID/readiness?max_age_minutes=$MAX_AGE" \
  -H "Authorization: Bearer $TOKEN")"

echo "$REPORT" | python3 -c '
import sys, json
try:
    r = json.load(sys.stdin)
except Exception:
    print("✗ readiness endpoint returned no usable JSON (auth? device id?)")
    sys.exit(1)
print("device: %s (id %s)" % (r.get("name"), r.get("device_id")))
for k, v in sorted((r.get("checks") or {}).items()):
    mark = "✓" if v.get("ok") else "✗"
    print("  %s %s: %s" % (mark, k, v.get("detail")))
missing = r.get("missing") or []
if r.get("ready"):
    print("==> agent adoption readiness: READY")
    sys.exit(0)
print("==> agent adoption readiness: NOT READY (missing: %s)" % ", ".join(missing))
sys.exit(1)
'
RC=$?
if [[ "$RC" -ne 0 && -z "$REPORT" ]]; then
  echo "✗ readiness check failed — empty response from $API" >&2
fi
exit "$RC"
