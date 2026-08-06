#!/bin/bash
# BareNOC Chat installer for macOS — builds a proper .app bundle in ~/Applications.
# Double-click to run (or: bash install.command). Needs python.org Python 3.8+.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/BareNOC Chat.app"
VERSION="0.1.0"

echo "=== BareNOC Chat installer (macOS) ==="

# tkinter check (python.org builds include it; the bare CLT python may not)
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "[ERROR] tkinter is not available."
  echo "        Install Python from https://www.python.org/downloads/macos/ (tkinter included)."
  exit 1
fi

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DIR/barenoc_chat.py" "$DIR/bnui.py" "$DIR/bnapi.py" "$APP/Contents/Resources/"
cp "$DIR/barenoc-chat.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/MacOS/barenoc-chat" <<EOF
#!/bin/bash
exec python3 "$APP/Contents/Resources/barenoc_chat.py" "\$@"
EOF
chmod +x "$APP/Contents/MacOS/barenoc-chat"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>               <string>BareNOC Chat</string>
  <key>CFBundleDisplayName</key>        <string>BareNOC Chat</string>
  <key>CFBundleIdentifier</key>         <string>com.barenoc.chat</string>
  <key>CFBundleVersion</key>            <string>0.1.0</string>
  <key>CFBundleShortVersionString</key> <string>0.1.0</string>
  <key>CFBundleExecutable</key>         <string>barenoc-chat</string>
  <key>CFBundleIconFile</key>           <string>AppIcon</string>
  <key>CFBundlePackageType</key>        <string>APPL</string>
  <key>LSMinimumSystemVersion</key>     <string>10.13</string>
  <key>NSHighResolutionCapable</key>    <true/>
  <key>NSPrincipalClass</key>           <string>NSApplication</string>
</dict>
</plist>
PLIST

# Ad-hoc sign so newer macOS (esp. arm64) runs it without complaints.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "Installed: $APP"
echo "Run with:  open \"$APP\"   (or double-click in ~/Applications)"
