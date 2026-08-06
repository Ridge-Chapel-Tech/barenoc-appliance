#!/usr/bin/env bash
# BareNOC Chat installer — creates ~/.local/bin/barenoc-chat + desktop entry.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons"

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

cp "$DIR/barenoc-chat.png" "$ICON_DIR/barenoc-chat.png"

LAUNCHER="$BIN_DIR/barenoc-chat"
printf '#!/usr/bin/env bash\nexec python3 "%s/barenoc_chat.py" "$@"\n' "$DIR" > "$LAUNCHER"
chmod +x "$LAUNCHER"

# tkinter check with per-distro install hints
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "[WARN] tkinter is not installed — the GUI won't start until it is."
  if   command -v apt-get >/dev/null 2>&1; then echo "       Debian/Ubuntu: sudo apt install python3-tk"
  elif command -v dnf     >/dev/null 2>&1; then echo "       Fedora:       sudo dnf install python3-tkinter"
  elif command -v pacman  >/dev/null 2>&1; then echo "       Arch:         sudo pacman -S tk"
  elif command -v zypper  >/dev/null 2>&1; then echo "       openSUSE:     sudo zypper install python3-tk"
  fi
fi

cat > "$APP_DIR/barenoc-chat.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BareNOC Chat
Comment=Legacy AIM-style chat client for the BareNOC queue manager
Exec=$LAUNCHER
Icon=barenoc-chat
Terminal=false
Categories=Network;Chat;
StartupNotify=true
StartupWMClass=BareNOC-Chat
EOF

echo "Installed: $LAUNCHER"
echo "Run with:  barenoc-chat"
