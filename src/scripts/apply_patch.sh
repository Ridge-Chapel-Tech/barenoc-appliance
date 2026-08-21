#!/bin/bash
# apply_patch.sh — check for available system updates on ANY Linux flavor,
# across ALL update sources (OS package manager + flatpak + firmware + snap +
# rpm-ostree). Read-only: NEVER installs anything.
# Usage: apply_patch.sh <target_ip> [patch_id] [ssh_user] [ssh_key_path]

TARGET="$1"
PATCH_ID="${2:-latest}"
SSH_USER="${3:-barenoc}"
SSH_KEY="${4:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check_updates_multi.sh"

if [ -z "$TARGET" ]; then
  echo '{"error": "Usage: apply_patch.sh <target> [patch_id]", "success": false}'
  exit 1
fi

# Target is an IP/hostname from the runner's resolved device record; reject
# anything that could smuggle shell into the remote command line.
case "$TARGET" in
  *[!A-Za-z0-9._:-]*)
    echo "{\"error\": \"Invalid target: $TARGET\", \"success\": false}"
    exit 1
    ;;
esac

if [ ! -f "$SSH_KEY" ]; then
  echo "{\"error\": \"SSH key not found: $SSH_KEY\", \"success\": false}"
  exit 1
fi

if [ ! -f "$CHECK_SCRIPT" ]; then
  echo "{\"error\": \"check_updates_multi.sh not found next to apply_patch.sh\", \"success\": false}"
  exit 1
fi

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR -o ConnectTimeout=10)

# The multi-source check runs ON the target (stdin → `bash -s`, target as $1);
# stdout carries the machine-readable JSON report, stderr the ssh diagnostics.
ERR_FILE=$(mktemp /tmp/apply_patch_ssh_err.XXXXXX)
RESULT=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$TARGET" "bash -s" "$TARGET" < "$CHECK_SCRIPT" 2>"$ERR_FILE")
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ] && [ -z "$RESULT" ]; then
  ERR=$(head -c 300 "$ERR_FILE" 2>/dev/null | tr '\n' ' ')
  rm -f "$ERR_FILE"
  echo "{\"success\": false, \"error\": \"SSH to $TARGET failed: $ERR\", \"target\": \"$TARGET\"}"
  exit $EXIT_CODE
fi
rm -f "$ERR_FILE"

if [ -z "$RESULT" ]; then
  echo "{\"success\": false, \"error\": \"no output from update check on $TARGET\", \"target\": \"$TARGET\"}"
  exit 1
fi

# The remote script already emitted the per-source report (success/action/
# target/package_manager/sources/total/updates_available/updates_b64). Relay it.
printf '%s\n' "$RESULT"
exit $EXIT_CODE
