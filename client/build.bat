@echo off
REM Build a standalone BareNOC Chat.exe with PyInstaller (Windows only).
REM Usage: build.bat
setlocal
cd /d "%~dp0"

if not exist .venv-build (
  echo ==^> creating build venv
  python -m venv .venv-build
)
echo ==^> ensuring pyinstaller
.venv-build\Scripts\python -m pip install -q --upgrade pip
.venv-build\Scripts\python -m pip install -q pyinstaller

echo ==^> running pyinstaller (can take a minute)
.venv-build\Scripts\pyinstaller --noconfirm --clean barenoc_chat.spec

echo.
echo ==^> done: dist\BareNOC Chat.exe  (single-file, no Python needed)
endlocal
