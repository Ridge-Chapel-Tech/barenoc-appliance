#!/bin/bash
# Functional test for tailscale_remote_support.sh::manage_support_key (08-26).
# Runs in a temp HOME — never touches the real authorized_keys.
set -euo pipefail
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export SUPPORT_KEY_FILE="$TMP/support-ssh.pub"
export SUPPORT_SSH_DIR="$TMP/.ssh"
export SUPPORT_AUTHKEYS="$TMP/.ssh/authorized_keys"
echo "ssh-ed25519 AAAATESTKEY bareNOC support <support@barenoc.com>" > "$SUPPORT_KEY_FILE"
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/src/scripts/tailscale_remote_support.sh"

# Pre-existing owner key must survive untouched.
mkdir -p "$SUPPORT_SSH_DIR"
printf 'ssh-ed25519 AAAAMYOWNKEY user@example\n' > "$SUPPORT_AUTHKEYS"

bash -c "source $SCRIPT; manage_support_key enable"
grep -q 'AAAATESTKEY' "$SUPPORT_AUTHKEYS" || { echo "FAIL: support key not added"; exit 1; }
grep -q 'AAAAMYOWNKEY' "$SUPPORT_AUTHKEYS" || { echo "FAIL: owner key lost"; exit 1; }
[ "$(grep -c 'AAAATESTKEY' "$SUPPORT_AUTHKEYS")" = "1" ] || { echo "FAIL: duplicated support key"; exit 1; }

# Idempotent enable.
bash -c "source $SCRIPT; manage_support_key enable"
[ "$(grep -c 'AAAATESTKEY' "$SUPPORT_AUTHKEYS")" = "1" ] || { echo "FAIL: enable not idempotent"; exit 1; }

bash -c "source $SCRIPT; manage_support_key disable"
grep -q 'AAAATESTKEY' "$SUPPORT_AUTHKEYS" && { echo "FAIL: support key not removed"; exit 1; }
grep -q 'AAAAMYOWNKEY' "$SUPPORT_AUTHKEYS" || { echo "FAIL: owner key removed with support key"; exit 1; }

echo "all support-ssh tests passed"
