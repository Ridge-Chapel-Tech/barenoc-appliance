#!/usr/bin/env bash
# Build a standalone BareNOC Chat binary with PyInstaller.
# Runs on Linux or macOS. Windows: use build.bat.
# Usage:  ./build.sh          (builds for the current OS)
# Output: dist/barenoc-chat (Linux) or dist/BareNOC Chat.app (macOS)
set -euo pipefail
cd "$(dirname "$0")"

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) echo "Use build.bat on Windows (PyInstaller cannot cross-compile)." >&2; exit 1 ;;
esac

if [ ! -d .venv-build ]; then
  echo "==> creating build venv"
  python3 -m venv .venv-build
fi
echo "==> ensuring pyinstaller"
.venv-build/bin/pip install -q --upgrade pip
.venv-build/bin/pip install -q pyinstaller

echo "==> running pyinstaller (can take a minute)"
.venv-build/bin/pyinstaller --noconfirm --clean barenoc_chat.spec

echo
echo "==> done:"
case "$(uname -s)" in
  Linux)  echo "    dist/barenoc-chat        (single-file executable, no Python needed)" ;;
  Darwin) echo "    dist/BareNOC Chat.app    (drag to /Applications)" ;;
esac
