# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — builds a standalone BareNOC Chat binary for the
# CURRENT platform (PyInstaller cannot cross-compile):
#   Linux:   dist/barenoc-chat          (single-file executable)
#   Windows: dist/BareNOC Chat.exe      (single-file, windowed)
#   macOS:   dist/BareNOC Chat.app      (.app bundle, onedir for fast start)
#
# Run via client/build.sh (Linux/macOS) or client/build.bat (Windows).

import platform

APP_NAME = "BareNOC Chat"
APP_ID = "com.barenoc.chat"
VERSION = "0.1.0"

is_win = platform.system() == "Windows"
is_mac = platform.system() == "Darwin"

icon = None
if is_win:
    icon = "barenoc-chat.ico"
elif is_mac:
    icon = "barenoc-chat.icns"

# the window icon PNG is bundled too — bnui resolves it via sys._MEIPASS
# when frozen, or next to the source files when run from a checkout.
datas = [("barenoc-chat.png", ".")]

a = Analysis(
    ["barenoc_chat.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "numpy"],   # nothing should pull these in; keep the bundle lean
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if is_mac:
    # onedir .app bundle (classic layout — faster cold start than onefile)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="barenoc-chat",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="barenoc-chat")
    app = BUNDLE(
        coll,
        name="BareNOC Chat.app",
        icon=icon,
        bundle_identifier=APP_ID,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
            "NSPrincipalClass": "NSApplication",
        },
    )
else:
    # single-file executable (Windows: .exe; Linux: plain binary)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="BareNOC Chat.exe" if is_win else "barenoc-chat",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        icon=icon,
        disable_windowed_traceback=False,
    )
