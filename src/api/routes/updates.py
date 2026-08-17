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
        with open(os.path.join(STATUS_DIR, "status.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_progress() -> dict:
    """Read the host self-update service's progress file (stages/pct/message)."""
    try:
        with open(os.path.join(STATUS_DIR, "progress.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_update_result() -> dict:
    """The last in-app update/rollback result (persisted by the host service)."""
    try:
        with open(os.path.join(STATUS_DIR, "update_result.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _confirmed_progress(progress: dict) -> dict:
    """Annotate the host service's progress so the card never re-renders a
    stale completion banner.

    A terminal 'done' stage is a COMPLETED event: the self-update script writes
    it only after the health check passes, so the running version has already
    flipped — the version flip + health check IS the confirmation. Any 'done'
    (whether the completed version is the running one or a since-superseded
    older one) should render the steady 'up to date' state, not a permanent
    'Complete 100%' banner.

    We annotate (add ``confirmed``) rather than delete the file: the scheduler's
    notify watcher reads the same status and emails each terminal transition
    ONCE using a persisted key — the raw ``stage`` must stay visible to it so
    that once-email keeps firing. A terminal 'failed' (still actionable) and
    in-flight stages are left untouched.
    """
    progress = dict(progress or {})
    if (progress.get("stage") or "") == "done":
        progress["confirmed"] = True
    return progress


def _write_status(status: dict):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(os.path.join(STATUS_DIR, "status.json"), "w") as f:
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
    live = _current_version()
    # The live installed version ALWAYS wins — a stale check result (status.json
    # was written when an older build was installed) must never be shown as the
    # current version. Keep the rest of the shape the UI already uses.
    stored_current = status.get("current", "")
    status["current"] = live
    status.setdefault("update_access", _update_access(_read_env_file()))
    # Stable shape before any check has run: the UI (dashboard banner) reads
    # latest/available and must never see them missing (08-17 gate fix on #26).
    status.setdefault("latest", "")
    status.setdefault("available", False)
    status.setdefault("manifest_error", "")
    status.setdefault("checked_at", "")
    # Signal when the persisted check predates the running build so the UI can
    # refresh it on load (checked_at/latest/available would otherwise stay stale
    # too — the .d auto-check-on-load only ran when checked_at was missing).
    status["check_stale"] = bool(stored_current != live or not status.get("checked_at"))
    status["schedule"] = _read_schedule()
    last = _read_update_result()
    status["last_update"] = last
    status["progress"] = _confirmed_progress(_read_progress())
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


@router.post("/notify")
def update_notify(body: dict = None,
                  user: User = Depends(require_any_role("operator", "admin", "agent"))):
    """Email the terminal update state (called by the scheduler when the
    host service's progress flips to done/failed). Best-effort; never raises.
    Reuses the alert channel (ALERT_RECIPIENTS) with a rendered HTML table.
    """
    body = body or {}
    stage = str(body.get("stage") or "").strip()
    message = str(body.get("message") or "").strip()
    version = str(body.get("version") or "").strip() or _current_version()
    if stage not in ("done", "failed"):
        raise HTTPException(status_code=400, detail="stage must be done or failed")
    try:
        from emailer import alert_html, send_email
        from llm_providers import read_env_file as _env
        recipients = (_env().get("ALERT_RECIPIENTS") or "").strip() or None
        if not recipients:
            return {"status": "ok", "notified": False, "note": "no ALERT_RECIPIENTS configured"}
        ok = stage == "done"
        rows = [
            ["Appliance", "BareNOC"],
            ["Version", version],
            ["Outcome", "✅ update complete" if ok else "❌ update failed"],
            ["Detail", message or ("—" if ok else "see the Updates card")],
        ]
        subject = (
            f"BareNOC appliance updated to {version}"
            if ok else f"BareNOC update FAILED ({version})"
        )
        body_html = alert_html(subject, rows)
        result, err = send_email(recipients, subject, body_html=body_html)
        return {"status": "ok", "notified": result, "error": err}
    except Exception as e:
        return {"status": "ok", "notified": False, "error": str(e)}


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
