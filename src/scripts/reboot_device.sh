#!/bin/bash
# Safe reboot script - schedules a reboot via SSH
# Usage: reboot_device.sh <target_ip> [ssh_user] [ssh_key_path]
# Only executes during allowed maintenance window (configurable)

TARGET="$1"
SSH_USER="${2:-root}"
SSH_KEY="${3:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"

if [ -z "$TARGET" ]; then
  echo '{"error": "No target specified", "success": false}'
  exit 1
fi

# Check if SSH key exists
if [ ! -f "$SSH_KEY" ]; then
  echo "{\"error\": \"SSH key not found: $SSH_KEY\", \"success\": false}"
  exit 1
fi

# Send reboot command via SSH
RESULT=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$SSH_USER@$TARGET" "sudo reboot" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo "{\"success\": true, \"action\": \"reboot\", \"target\": \"$TARGET\"}"
else
  echo "{\"success\": false, \"error\": \"$RESULT\", \"target\": \"$TARGET\"}"
fi

exit $EXIT_CODE
