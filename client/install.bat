@echo off
REM BareNOC Chat installer for Windows (Python 3.8+ with tkinter).
REM Installs to %LOCALAPPDATA%\BareNOC + Start Menu and Desktop shortcuts.
setlocal
set "SRC=%~dp0"
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "DST=%LOCALAPPDATA%\BareNOC"

echo === BareNOC Chat installer (Windows) ===

REM -- locate python (prefer pythonw so the GUI has no console window) --
set "PY="
where pythonw >nul 2>nul && set "PY=pythonw"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [ERROR] Python 3 not found.
  echo         Install it from https://www.python.org/downloads/
  echo         (tick "Add python.exe to PATH" during setup; tkinter is included).
  exit /b 1
)

REM -- copy app files --
mkdir "%DST%" 2>nul
copy /Y "%SRC%bnapi.py" "%DST%" >nul
copy /Y "%SRC%bnui.py" "%DST%" >nul
copy /Y "%SRC%barenoc_chat.py" "%DST%" >nul
copy /Y "%SRC%barenoc-chat.ico" "%DST%" >nul

REM -- verify tkinter --
"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] tkinter is missing from this Python install.
  echo         Use the python.org installer (it includes tkinter).
  exit /b 1
)

REM -- launcher (passes arguments through, e.g. --server URL) --
> "%DST%\barenoc-chat.bat" (
  echo @echo off
  echo where pythonw ^>nul 2^>nul
  echo if not errorlevel 1 ^(
  echo   start "" pythonw "%DST%\barenoc_chat.py" %%*
  echo   exit /b 0
  echo ^)
  echo python "%DST%\barenoc_chat.py" %%*
)

REM -- Start Menu + Desktop shortcuts --
powershell -NoProfile -ExecutionPolicy Bypass -File "%SRC%install-shortcuts.ps1" -AppDir "%DST%"
if errorlevel 1 echo [WARN] Shortcut creation failed - launch via "%DST%\barenoc-chat.bat"

echo.
echo Installed to: %DST%
echo Launch via:   Start Menu ^> "BareNOC Chat"  ^(or run barenoc-chat.bat^)
echo Uninstall:    delete the "BareNOC" folder under %%LOCALAPPDATA%% + the 2 shortcuts
endlocal
