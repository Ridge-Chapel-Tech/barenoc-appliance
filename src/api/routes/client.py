"""BareNOC chat client — download/version service.

Serves the desktop chat client (packaged with BareNOC) for download from the
logged-in web portal. Packages are generated on the fly from the synced
client bundle on the appliance (/opt/barenoc/client), versioned to match the
appliance (see version.py).
"""

import io
import os
import tarfile
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from auth import get_current_user
from routes.chat import require_chat_enabled
from version import APP_VERSION, RELEASE_CHANNEL

router = APIRouter(prefix="/api/v1/client", tags=["client"])

CLIENT_DIR = os.getenv("CLIENT_DIR", "/opt/barenoc/client")

# Platform packages: what gets bundled + the launcher file to add
PLATFORMS = {
    "linux":   {"label": "Linux",   "ext": "tar.gz", "launcher": None},
    "windows": {"label": "Windows", "ext": "zip",    "launcher": "run.bat"},
    "macos":   {"label": "macOS",   "ext": "zip",    "launcher": "run.command"},
}

LAUNCHERS = {
    "run.bat": "@echo off\r\ncd /d %~dp0\r\npython barenoc_chat.py %*\r\n",
    "run.command": "#!/bin/bash\ncd \"$(dirname \"$0\")\"\npython3 barenoc_chat.py \"$@\"\n",
}

CLIENT_FILES = ("barenoc_chat.py", "bnapi.py", "bnui.py", "README.md", "install.sh")


def _client_files() -> dict:
    """Return {filename: bytes} from the synced client bundle."""
    if not os.path.isdir(CLIENT_DIR):
        return {}
    out = {}
    for name in CLIENT_FILES:
        path = os.path.join(CLIENT_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                out[name] = f.read()
    return out


def _archive(platform: str) -> tuple[bytes, str]:
    files = _client_files()
    if not files:
        raise HTTPException(status_code=503, detail="Client bundle not installed on appliance")

    launcher = PLATFORMS[platform]["launcher"]
    if launcher:
        files[launcher] = LAUNCHERS[launcher].encode()
    if "install.sh" in files:
        files["install.sh"] = b"#!/bin/bash\n" + files["install.sh"].split(b"\n", 1)[1] \
            if b"#!/bin/bash" not in files["install.sh"][:20] else files["install.sh"]

    filename = f"barenoc-chat-{APP_VERSION}-{platform}.{PLATFORMS[platform]['ext']}"

    if platform == "linux":
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in files.items():
                info = tarfile.TarInfo(f"barenoc-chat/{name}")
                info.size = len(data)
                info.mtime = int(datetime.utcnow().timestamp())
                if name == "install.sh":
                    info.mode = 0o755
                tf.addfile(info, io.BytesIO(data))
        return buf.getvalue(), filename

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zi = zipfile.ZipInfo(f"barenoc-chat/{name}", datetime.now().timetuple()[:6])
            zf.writestr(zi, data)
    return buf.getvalue(), filename


@router.get("")
def client_info(user=Depends(require_chat_enabled)):
    """Client metadata (version + download URLs). 403 when the desktop chat
    feature is disabled in Settings → General."""
    return {
        "name": "BareNOC Chat",
        "version": APP_VERSION,
        "channel": RELEASE_CHANNEL,
        "platforms": {
            p: {
                "label": meta["label"],
                "filename": f"barenoc-chat-{APP_VERSION}-{p}.{meta['ext']}",
                "url": f"/api/v1/client/download?platform={p}",
                "size_hint": "~50 KB",
            }
            for p, meta in PLATFORMS.items()
        },
        "portal_page": "/downloads",
    }


@router.get("/download")
def client_download(platform: str = "linux", user=Depends(require_chat_enabled)):
    if platform not in PLATFORMS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown platform '{platform}'. Choose: {', '.join(PLATFORMS)}")
    data, filename = _archive(platform)
    media = "application/gzip" if platform == "linux" else "application/zip"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
