#!/bin/bash
# device_ssh.sh <ip> [command...]
# First-class endpoint control: SSH to a managed device using its STORED
# (decrypted) control key. Logs in as the agent service account, resolves the
# device by IP, fetches GET /devices/{id}/credentials (the API decrypts the
# Fernet-encrypted key server-side — never touch the key files on disk),
# writes a 0600 temp key, and runs ssh. No command -> interactive shell.
#
# This is the intended control path for the autonomous agent (pi tasks) and
# for ad-hoc troubleshooting — same resolution the runner uses for actions.
#
# Examples:
#   device_ssh.sh 192.168.10.141 'hostname && uname -a'
#   device_ssh.sh 192.168.10.141 'sudo -n dnf check-update'
set -e

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: device_ssh.sh <ip> [command...]" >&2
  exit 1
fi
shift

API=https://localhost/api/v1
CREDS=$(cat /opt/barenoc/agent/credentials 2>/dev/null || true)
U=$(echo "$CREDS" | sed -n 's/^username=//p' | tr -d '[:space:]')
P=$(echo "$CREDS" | sed -n 's/^password=//p' | tr -d '[:space:]')
if [ -z "$U" ] || [ -z "$P" ]; then
  echo "device_ssh: agent credentials not found (/opt/barenoc/agent/credentials)" >&2
  exit 1
fi

TOKEN=$(curl -sk -X POST "$API/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"$P\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

ID=$(curl -sk "$API/devices?controlled=true" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); devs=d.get('devices',[]); print(next((str(x['id']) for x in devs if x.get('ip_address')=='$TARGET'), ''))")
if [ -z "$ID" ]; then
  echo "device_ssh: no controlled device at $TARGET" >&2
  exit 1
fi

CRED=$(curl -sk "$API/devices/$ID/credentials" -H "Authorization: Bearer $TOKEN")
USER=$(echo "$CRED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("ssh_user") or "barenoc")')
KEY=$(echo "$CRED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("ssh_key") or "")')
if [ -z "$KEY" ]; then
  echo "device_ssh: no stored SSH key for $TARGET" >&2
  exit 1
fi

TMP=$(mktemp /tmp/device-ssh-XXXXXX.key)
printf '%s\n' "$KEY" > "$TMP"   # ssh-keygen needs the trailing newline
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT

SSH_OPTS=(-i "$TMP" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
if [ "$#" -gt 0 ]; then
  exec ssh "${SSH_OPTS[@]}" "$USER@$TARGET" "$@"
else
  exec ssh "${SSH_OPTS[@]}" "$USER@$TARGET"
fi
