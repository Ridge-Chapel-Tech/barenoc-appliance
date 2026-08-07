#!/bin/bash
# Safe log collection script
# Usage: collect_logs.sh <target_ip> [lines] [ssh_user] [ssh_key_path]

TARGET="$1"
LINES="${2:-50}"
SSH_USER="${3:-barenoc}"
SSH_KEY="${4:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"

if [ -z "$TARGET" ]; then
  echo '{"error": "No target specified", "success": false}'
  exit 1
fi

if [ ! -f "$SSH_KEY" ]; then
  echo "{\"error\": \"SSH key not found: $SSH_KEY\", \"success\": false}"
  exit 1
fi

# Non-root control users (the dedicated barenoc account) read the SYSTEM
# journal via the scoped sudo entry (journalctl is in the sudoers command
# list). Root connects directly. sudo -n keeps a missing sudoers entry from
# hanging the job on a password prompt. macOS has no journald — use log(1).
if [ "$SSH_USER" = "root" ]; then
  REMOTE_UNAME_CMD="uname -s"
else
  REMOTE_UNAME_CMD="sudo -n uname -s"
fi
UNAME=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR -o ConnectTimeout=10 "$SSH_USER@$TARGET" "$REMOTE_UNAME_CMD" 2>&1)
UNAME=$(echo "$UNAME" | tail -1 | tr -d '[:space:]')

if [ "$UNAME" = "Darwin" ]; then
  # macOS: unified log, last N lines (system + app).
  if [ "$SSH_USER" = "root" ]; then
    REMOTE_CMD="log show --last 1h --style compact 2>/dev/null | tail -$LINES"
  else
    REMOTE_CMD="sudo -n log show --last 1h --style compact 2>/dev/null | tail -$LINES"
  fi
elif [ "$SSH_USER" = "root" ]; then
  REMOTE_CMD="journalctl --no-pager -n $LINES"
else
  REMOTE_CMD="sudo -n journalctl --no-pager -n $LINES"
fi

LOGS=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR -o ConnectTimeout=10 "$SSH_USER@$TARGET" "$REMOTE_CMD" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  # Base64 encode to preserve formatting in JSON
  ENCODED=$(echo "$LOGS" | base64 -w 0)
  echo "{\"success\": true, \"target\": \"$TARGET\", \"lines\": $LINES, \"logs_b64\": \"$ENCODED\"}"
else
  echo "{\"success\": false, \"error\": \"$LOGS\", \"target\": \"$TARGET\"}"
fi

exit $EXIT_CODE
