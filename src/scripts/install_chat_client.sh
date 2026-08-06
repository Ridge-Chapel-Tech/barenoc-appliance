#!/bin/bash
# Remotely install the BareNOC chat client on an onboarded device (Linux first).
# Usage: install_chat_client.sh <target_ip> [ssh_user] [ssh_key_path]
#
# Packages the client bundle from the appliance, copies it to the device over
# SSH, installs python3-tk via the device's package manager, and wires the
# barenoc-chat launcher. Returns JSON.

TARGET="$1"
SSH_USER="${2:-root}"
SSH_KEY="${3:-/opt/barenoc/volumes/secrets/ssh/id_ed25519}"
CLIENT_DIR="/opt/barenoc/client"

if [ -z "$TARGET" ]; then
  echo '{"error": "No target specified", "success": false}'
  exit 1
fi
if [ ! -d "$CLIENT_DIR" ]; then
  echo '{"error": "Client bundle missing on appliance (/opt/barenoc/client)", "success": false}'
  exit 1
fi
if [ ! -f "$SSH_KEY" ]; then
  echo "{\"error\": \"SSH key not found: $SSH_KEY\", \"success\": false}"
  exit 1
fi

SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10"

# Detect the remote OS — Linux targets only for now
OS=$(ssh $SSH_OPTS "$SSH_USER@$TARGET" "uname -s" 2>/dev/null | tr -d '\r\n')
if [ "$OS" != "Linux" ]; then
  echo "{\"success\": false, \"error\": \"Remote install currently supports Linux targets (got: ${OS:-unreachable})\", \"target\": \"$TARGET\"}"
  exit 1
fi

# Package the client bundle
TARBALL=$(mktemp /tmp/barenoc-chat-XXXXXX.tar.gz)
tar czf "$TARBALL" -C "$CLIENT_DIR" --exclude __pycache__ . 2>/dev/null

# Write the remote install script
RINSTALL=$(mktemp /tmp/barenoc-remote-install-XXXXXX.sh)
cat > "$RINSTALL" <<'REMOTE_SCRIPT'
#!/bin/bash
set -e
PM=""
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get install -y -qq python3-tk >/dev/null 2>&1 || true
  PM="apt"
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y -q python3-tkinter >/dev/null 2>&1 || true
  PM="dnf"
elif command -v pacman >/dev/null 2>&1; then
  sudo pacman -S --noconfirm tk >/dev/null 2>&1 || true
  PM="pacman"
else
  echo "no apt/dnf/pacman available on target"
  exit 2
fi
DEST="$HOME/.local/share/barenoc-chat"
mkdir -p "$DEST"
tar xzf /tmp/barenoc-chat.tar.gz -C "$DEST"
chmod +x "$DEST/install.sh" 2>/dev/null || true
"$DEST/install.sh" >/dev/null 2>&1 || true
echo "BareNOC chat client installed to $DEST (package manager: $PM)"
REMOTE_SCRIPT
chmod +x "$RINSTALL"

scp $SSH_OPTS "$TARBALL" "$SSH_USER@$TARGET:/tmp/barenoc-chat.tar.gz" >/dev/null 2>&1
scp $SSH_OPTS "$RINSTALL" "$SSH_USER@$TARGET:/tmp/barenoc-install.sh" >/dev/null 2>&1
REMOTE=$(ssh $SSH_OPTS "$SSH_USER@$TARGET" "bash /tmp/barenoc-install.sh" 2>&1)
RC=$?

rm -f "$TARBALL" "$RINSTALL"

if [ $RC -eq 0 ] && echo "$REMOTE" | grep -q installed; then
  echo "{\"success\": true, \"target\": \"$TARGET\", \"install\": \"$(echo "$REMOTE" | tail -1)\"}"
else
  echo "{\"success\": false, \"error\": \"$(echo "$REMOTE" | tail -3 | tr '\n' ' ')\", \"target\": \"$TARGET\"}"
fi
