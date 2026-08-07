#!/bin/bash
# apply_patch.sh — check for available system updates on ANY Linux flavor.
# Detects the target's package manager (apt / dnf / yum / apk / zypper) and
# runs the read-only "what can be updated" check. NEVER installs anything.
# Usage: apply_patch.sh <target_ip> [patch_id] [ssh_user] [ssh_key_path]

TARGET="$1"
PATCH_ID="${2:-latest}"
SSH_USER="${3:-barenoc}"
SSH_KEY="${4:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"

if [ -z "$TARGET" ]; then
  echo '{"error": "Usage: apply_patch.sh <target> [patch_id]", "success": false}'
  exit 1
fi

if [ ! -f "$SSH_KEY" ]; then
  echo "{\"error\": \"SSH key not found: $SSH_KEY\", \"success\": false}"
  exit 1
fi

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR -o ConnectTimeout=10)

# Detect the package manager in one remote round trip.
PM=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$TARGET" \
  'if command -v apt-get >/dev/null 2>&1; then echo apt
   elif command -v dnf >/dev/null 2>&1; then echo dnf
   elif command -v yum >/dev/null 2>&1; then echo yum
   elif command -v apk >/dev/null 2>&1; then echo apk
   elif command -v zypper >/dev/null 2>&1; then echo zypper
   else echo unknown; fi' 2>&1)
RC=$?
if [ $RC -ne 0 ]; then
  echo "{\"success\": false, \"error\": \"SSH to $TARGET failed: $PM\", \"target\": \"$TARGET\"}"
  exit $RC
fi
PM=$(echo "$PM" | tail -1 | tr -d '[:space:]')

case "$PM" in
  apt)
    # No stderr suppression: a denied sudo (missing sudoers entry) must
    # surface as an error, never as an empty success.
    if [ "$SSH_USER" = "root" ]; then
      REMOTE_CMD='apt update -qq && apt list --upgradable'
    else
      REMOTE_CMD='sudo -n apt update -qq && sudo -n apt list --upgradable'
    fi
    ;;
  dnf)
    # check-update: 0 = clean, 100 = updates available, other = error.
    if [ "$SSH_USER" = "root" ]; then
      REMOTE_CMD='dnf check-update -q; r=$?; if [ "$r" -eq 100 ]; then echo "(updates available)"; exit 0; elif [ "$r" -eq 0 ]; then exit 0; else exit "$r"; fi'
    else
      REMOTE_CMD='sudo -n dnf check-update -q; r=$?; if [ "$r" -eq 100 ]; then echo "(updates available)"; exit 0; elif [ "$r" -eq 0 ]; then exit 0; else exit "$r"; fi'
    fi
    ;;
  yum)
    if [ "$SSH_USER" = "root" ]; then
      REMOTE_CMD='yum check-update -q; r=$?; if [ "$r" -eq 100 ]; then echo "(updates available)"; exit 0; elif [ "$r" -eq 0 ]; then exit 0; else exit "$r"; fi'
    else
      REMOTE_CMD='sudo -n yum check-update -q; r=$?; if [ "$r" -eq 100 ]; then echo "(updates available)"; exit 0; elif [ "$r" -eq 0 ]; then exit 0; else exit "$r"; fi'
    fi
    ;;
  apk)
    if [ "$SSH_USER" = "root" ]; then
      REMOTE_CMD='apk update -q && apk list -u'
    else
      REMOTE_CMD='sudo -n apk update -q && sudo -n apk list -u'
    fi
    ;;
  zypper)
    if [ "$SSH_USER" = "root" ]; then
      REMOTE_CMD='zypper list-updates'
    else
      REMOTE_CMD='sudo -n zypper list-updates'
    fi
    ;;
  *)
    echo "{\"success\": false, \"error\": \"Unsupported package manager on $TARGET\", \"target\": \"$TARGET\"}"
    exit 1
    ;;
esac

RESULT=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$TARGET" "$REMOTE_CMD" 2>&1)
EXIT_CODE=$?

# Best-effort "updates available?" heuristic per manager.
AVAILABLE="false"
case "$PM" in
  apt)   [ "$(echo "$RESULT" | grep -vc '^Listing')" -gt 0 ] && AVAILABLE="true" ;;
  dnf|yum) echo "$RESULT" | grep -q 'updates available\|^[A-Za-z0-9_.+-]' && AVAILABLE="true" ;;
  apk)   [ -n "$(echo "$RESULT" | grep -v '^WARNING\|^fetch' | grep -v '^$')" ] && AVAILABLE="true" ;;
  zypper) [ -n "$(echo "$RESULT" | grep -v '^S\|^Repository\|^Loading\|^$')" ] && AVAILABLE="true" ;;
esac

if [ $EXIT_CODE -eq 0 ]; then
  ENCODED=$(echo "$RESULT" | base64 -w 0)
  echo "{\"success\": true, \"action\": \"check_patches\", \"target\": \"$TARGET\", \"package_manager\": \"$PM\", \"updates_available\": $AVAILABLE, \"updates_b64\": \"$ENCODED\"}"
else
  echo "{\"success\": false, \"error\": \"$RESULT\", \"target\": \"$TARGET\"}"
fi

exit $EXIT_CODE
