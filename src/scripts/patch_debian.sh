#!/bin/bash
# Safe patch script - applies approved patches via SSH
# Usage: patch_debian.sh <target_ip> <patch_id> [ssh_user] [ssh_key_path]

TARGET="$1"
PATCH_ID="$2"
SSH_USER="${3:-root}"
SSH_KEY="${4:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"

if [ -z "$TARGET" ] || [ -z "$PATCH_ID" ]; then
  echo '{"error": "Usage: patch_debian.sh <target> <patch_id>", "success": false}'
  exit 1
fi

if [ ! -f "$SSH_KEY" ]; then
  echo "{\"error\": \"SSH key not found: $SSH_KEY\", \"success\": false}"
  exit 1
fi

# Run apt update and upgrade
RESULT=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  "$SSH_USER@$TARGET" "apt update -qq && apt list --upgradable 2>/dev/null" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo "{\"success\": true, \"action\": \"check_patches\", \"target\": \"$TARGET\", \"result\": \"Updates checked\"}"
else
  echo "{\"success\": false, \"error\": \"$RESULT\", \"target\": \"$TARGET\"}"
fi

exit $EXIT_CODE
