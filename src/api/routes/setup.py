"""First-run setup wizard — /api/v1/setup/status + /complete.

The wizard walks a fresh install through the minimal config (one sitting):
LLM key, timezone, site name + alert email, autonomy profile, backups,
first device, and the shareable chat URL. Steps reuse the standard settings
endpoints; this module reports what's still missing and marks SETUP_COMPLETE.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from auth import require_role
from database import get_db
from models import Device, User
from routes.settings import _read_backup_conf, _read_env_file, _write_env_file

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])


def _status_dict(env: dict, request: Request) -> dict:
    llm_configured = any(
        k.startswith("LLM_PROVIDER_") and k.endswith("_API_KEY") and env.get(k)
        for k in env)
    backups = _read_backup_conf()
    base = str(request.base_url).rstrip("/")
    return {
        "complete": str(env.get("SETUP_COMPLETE", "")).strip().lower()
        in ("1", "true", "yes"),
        "site_name": env.get("CUSTOMER_NAME") or "BareNOC",
        "steps": {
            # first login created the admin — the caller of this API is admin
            "account": True,
            "llm": llm_configured,
            "timezone": bool((env.get("TZ") or "").strip()),
            "site_name": bool((env.get("CUSTOMER_NAME") or "").strip()),
            "email": bool((env.get("ALERT_EMAIL") or "").strip()),
            "autonomy": bool((env.get("LLM_POLICY_PROFILE") or "").strip()),
            "backups": str(backups.get("USB_BACKUP_ENABLED", "")).lower()
            in ("1", "true", "yes"),
        },
        "chat_url": f"{base}/chat",
        "devices_count": 0,
    }


@router.get("/status")
def setup_status(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_role("admin"))):
    s = _status_dict(_read_env_file(), request)
    s["devices_count"] = db.query(Device).count()
    return s


@router.post("/complete")
def setup_complete(request: Request, user: User = Depends(require_role("admin"))):
    env = _read_env_file()
    env["SETUP_COMPLETE"] = "true"
    _write_env_file(env)
    return {"status": "ok", "complete": True}
