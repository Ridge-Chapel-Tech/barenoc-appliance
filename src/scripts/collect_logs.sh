#!/bin/bash
# Safe log collection script
# Usage: collect_logs.sh <target_ip> [lines] [ssh_user] [ssh_key_path]

TARGET="$1"
LINES="${2:-50}"
SSH_USER="${3:-root}"
SSH_KEY="${4:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"

if [ -z "$TARGET" ]; then
  echo '{"error": "No target specified", "success": false}'
  exit 1
fi

if [ ! -f "$SSH_KEY" ]; then
  echo "{\"error\": \"SSH key not found: $SSH_KEY\", \"success\": false}"
  exit 1
fi

LOGS=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  "$SSH_USER@$TARGET" "journalctl --no-pager -n $LINES 2>/dev/null | tail -$LINES" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  # Base64 encode to preserve formatting in JSON
  ENCODED=$(echo "$LOGS" | base64 -w 0)
  echo "{\"success\": true, \"target\": \"$TARGET\", \"lines\": $LINES, \"logs_b64\": \"$ENCODED\"}"
else
  echo "{\"success\": false, \"error\": \"$LOGS\", \"target\": \"$TARGET\"}"
fi

exit $EXIT_CODE
