"""Branding — serve the uploaded customer logo (public, no auth).

The logo file itself is public branding (rendered in <img> tags which cannot
send Authorization headers). The upload/delete endpoints live in settings.py
and require an admin token.
"""

import os
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/api/v1/branding", tags=["branding"])

BRANDING_DIR = "/opt/barenoc/volumes/branding"


def _current_logo_name() -> str:
    """Return the BRANDING_LOGO value from the .env file, if any."""
    try:
        with open("/opt/barenoc/.env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BRANDING_LOGO="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.getenv("BRANDING_LOGO", "")


@router.get("/logo")
def get_logo():
    """Serve the current customer logo, or 404 if none is set."""
    name = _current_logo_name()
    if not name:
        return JSONResponse({"detail": "No logo configured"}, status_code=404)
    # Guard against path traversal — only serve files in the branding dir
    if os.path.basename(name) != name:
        return JSONResponse({"detail": "Invalid logo name"}, status_code=400)
    path = os.path.join(BRANDING_DIR, name)
    if not os.path.isfile(path):
        return JSONResponse({"detail": "Logo file missing"}, status_code=404)
    return FileResponse(path)
