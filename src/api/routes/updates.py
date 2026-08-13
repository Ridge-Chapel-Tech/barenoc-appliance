"""Updates — the appliance's self-update machinery (free & open, beta).

- The update CHECK is pure api-side: installed version (version.APP_VERSION)
  vs the public manifest at barenoc.com. Updates are NOT gated by any key —
  BareNOC is free and open; paid support is the only thing that's separate.
- The APPLY runs on the HOST as root via a systemd .path unit watching a
  trigger file (update_request.json / rollback_request.json) written here.

Auth: operator/admin (UI) and agent (the scheduler's scheduled updates).
"""

import datetime
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import require_any_role
from database import get_db
from models import User
from routes.settings import _read_env_file

router = APIRouter(prefix="/api/v1/updates", tags=["updates"])

STATUS_DIR = "/opt/barenoc/volumes/update_status"
STATUS_FILE = os.path.join(STATUS_DIR, "status.json")
UPDATE_REQ = os.path.join(STATUS_DIR, "update_request.json")
ROLLBACK_REQ = os.path.join(STATUS_DIR, "rollback_request.json")
SCHEDULE_FILE = os.path.join(STATUS_DIR, "update_schedule.conf")

MANIFEST_URL = os.getenv(
    "UPDATE_MANIFEST_URL", "https://barenoc.com/downloads/versions.json")


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _fetch_json(url: str, timeout: int = 6) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "bareNOC-update/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _current_version() -> str:
    try:
        import version
        return version.APP_VERSION
    except Exception:
        return "unknown"


def _read_status() -> dict:
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_status(status: dict):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass


def _update_access(env: dict) -> dict:
    """Updates are free & open — no activation key (kept as a stable status
    shape so the UI can render the open state)."""
    return {"valid": True, "open": True, "key_set": True, "revoked": False,
            "reason": "", "note": "free & open (beta)"}


class ScheduleBody(BaseModel):
    enabled: bool
    day: str            # "daily" or 0-6 (0=Sunday)
    hour: int           # 0-23


def _run_check() -> dict:
    env = _read_env_file()
    access = _update_access(env)
    cur = _current_version()
    status = {
        "checked_at": _now(),
        "current": cur,
        "latest": cur,
        "kind": "",
        "available": False,
        "changelog": "",
        "tarball": "",
        "checksum": "",
        "update_access": access,
        "manifest_error": "",
    }
    try:
        m = _fetch_json(MANIFEST_URL + "?v=" + datetime.date.today().isoformat())
        latest = str(m.get("version") or cur)
        status.update({
            "latest": latest,
            "kind": m.get("kind", ""),
            "changelog": m.get("changelog", ""),
            "tarball": (m.get("assets") or {}).get("tarball", ""),
            "checksum": (m.get("assets") or {}).get("checksums", ""),
            "available": latest != cur,
        })
    except Exception as e:
        status["manifest_error"] = str(e)
    _write_status(status)
    return status


@router.get("/status")
def update_status(user: User = Depends(require_any_role("operator", "admin", "agent"))):
    status = _read_status()
    # always include the live installed version + schedule even pre-check
    status.setdefault("current", _current_version())
    status.setdefault("update_access", _update_access(_read_env_file()))
    status.setdefault("checked_at", "")
    status["schedule"] = _read_schedule()
    last = {}
    try:
        with open(os.path.join(STATUS_DIR, "update_result.json")) as f:
            last = json.load(f)
    except Exception:
        pass
    status["last_update"] = last
    return status


@router.post("/check")
def update_check(user: User = Depends(require_any_role("operator", "admin", "agent"))):
    return _run_check()


@router.post("/now")
def update_now(user: User = Depends(require_any_role("operator", "admin", "agent"))):
    status = _read_status() or _run_check()
    if not status.get("available"):
        raise HTTPException(400, "already up to date (or the manifest is unreachable)")
    payload = {"version": status.get("latest"), "kind": status.get("kind"),
               "tarball": status.get("tarball"), "checksums": status.get("checksum"),
               "requested_at": _now(), "snapshot": True}
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(UPDATE_REQ, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        raise HTTPException(500, f"could not queue the update: {e}")
    return {"status": "accepted",
            "note": f"updating to {payload['version']} in the background — watch the Updates card"}


@router.post("/rollback")
def update_rollback(user: User = Depends(require_any_role("operator", "admin", "agent"))):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(ROLLBACK_REQ, "w") as f:
            json.dump({"requested_at": _now()}, f, indent=2)
    except Exception as e:
        raise HTTPException(500, f"could not queue the rollback: {e}")
    return {"status": "accepted", "note": "rolling back to the previous release in the background"}


def _read_schedule() -> dict:
    conf = {"enabled": False, "day": "daily", "hour": 2}
    try:
        with open(SCHEDULE_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    conf[k.strip()] = v.strip()
    except Exception:
        pass
    try:
        conf["enabled"] = conf.get("enabled") in ("true", "1", "yes")
        conf["hour"] = int(conf.get("hour", 2))
    except Exception:
        pass
    return conf


@router.get("/schedule")
def get_schedule(user: User = Depends(require_any_role("operator", "admin", "agent"))):
    return _read_schedule()


@router.post("/schedule")
def set_schedule(body: ScheduleBody,
                 user: User = Depends(require_any_role("operator", "admin", "agent"))):
    if body.hour < 0 or body.hour > 23:
        raise HTTPException(422, "hour must be 0-23")
    if body.day != "daily":
        try:
            day = int(body.day)
        except ValueError:
            raise HTTPException(422, "day must be 'daily' or 0-6")
        if day < 0 or day > 6:
            raise HTTPException(422, "day must be 'daily' or 0-6")
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(SCHEDULE_FILE, "w") as f:
            f.write("# BareNOC update schedule (Settings → Updates; the scheduler applies it)\n")
            f.write(f"enabled={'true' if body.enabled else 'false'}\n")
            f.write(f"day={body.day}\n")
            f.write(f"hour={body.hour}\n")
    except Exception as e:
        raise HTTPException(500, f"could not save the schedule: {e}")
    return {"status": "ok", "schedule": _read_schedule()}
