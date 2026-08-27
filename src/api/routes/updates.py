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
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import require_any_role
from database import get_db
from models import User
from routes.settings import _read_env_file
from audit import log_event

router = APIRouter(prefix="/api/v1/updates", tags=["updates"])

STATUS_DIR = "/opt/barenoc/volumes/update_status"
UPDATE_REQ = os.path.join(STATUS_DIR, "update_request.json")
ROLLBACK_REQ = os.path.join(STATUS_DIR, "rollback_request.json")
SCHEDULE_FILE = os.path.join(STATUS_DIR, "update_schedule.conf")

# Owner "what's changed" note (post-auto-update, 2026-08-25): after an update
# actually APPLIES a new version, tell the owner what happened — a short
# friendly summary (2–4 bullets) + a link to the full GitHub Release changelog.
# Notification only (never a dashboard card); best-effort; one note per applied
# version (version-keyed marker); silently skipped when no recipients configured.
CHANGELOG_URL_TMPL = ("https://github.com/Ridge-Chapel-Tech/"
                      "barenoc-appliance/releases/tag/v{version}")
GITHUB_RELEASE_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)")

# Auto-update is ON by default (2026-08-25 user directive). Single source of
# truth for the default schedule: a weekly maintenance window — Sunday
# (day 0, the scheduler's 0=Sunday semantics) at 03:00 LOCAL time (appliance
# TZ). Safe because releases >= v2026.08.25.a are MANDATORY-SIG: the apply
# verifies a detached GPG signature before touching anything (fail-closed).
DEFAULT_UPDATE_SCHEDULE = {
    "mode": "recurring",
    "enabled": True,
    "day": "0",    # 0=Sunday..6=Saturday (matches netopt's weekly default)
    "hour": 3,     # 03:00 local
    "when": "",
    "fired": "",
}

MANIFEST_URL = os.getenv(
    "UPDATE_MANIFEST_URL", "https://barenoc.com/downloads/versions.json")


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _appliance_tz() -> str:
    """The appliance's LOCAL timezone (TZ from .env) — the schedule is
    wall-clock in THIS timezone, not the container's UTC. Falls back to UTC
    when TZ is unset or invalid (the same source alerting's _local_now uses)."""
    try:
        tz = (_read_env_file().get("TZ") or os.environ.get("TZ") or "").strip()
    except Exception:
        tz = ""
    return tz or "UTC"


def _zone(tz_name: str = None):
    try:
        return ZoneInfo(tz_name or _appliance_tz())
    except Exception:
        return ZoneInfo("UTC")


def _local_now(tz_name: str = None) -> datetime.datetime:
    """NAIVE wall-clock now in the appliance TZ (safe for hour/day compares)."""
    return datetime.datetime.now(_zone(tz_name)).replace(tzinfo=None)


def _local_to_utc(dt: datetime.datetime, tz_name: str = None) -> datetime.datetime:
    """Convert a NAIVE local wall-clock datetime to an AWARE UTC datetime.
    DST-safe: zoneinfo attaches the correct offset for that local instant."""
    return dt.replace(tzinfo=_zone(tz_name)).astimezone(datetime.timezone.utc)


def _utc_to_local(dt: datetime.datetime, tz_name: str = None) -> datetime.datetime:
    """Convert an aware UTC datetime to NAIVE local wall-clock time."""
    tz = _zone(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(tz).replace(tzinfo=None)


def _parse_local_dt(s: str) -> datetime.datetime:
    """Parse a local wall-clock datetime ('YYYY-MM-DDTHH:MM[:SS]' or
    'YYYY-MM-DD HH:MM[:SS]'). Returns a NAIVE datetime; raises on bad input."""
    s = (s or "").strip()
    if not s:
        raise ValueError("empty datetime")
    norm = s.replace(" ", "T")
    if len(norm) == 16:  # datetime-local input: YYYY-MM-DDTHH:MM
        dt = datetime.datetime.strptime(norm, "%Y-%m-%dT%H:%M")
    else:
        dt = datetime.datetime.fromisoformat(norm)
    return dt.replace(tzinfo=None)


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


def _read_verify_result() -> dict:
    """The last post-update verification result (written by
    verify_post_update.sh)."""
    try:
        with open(os.path.join(STATUS_DIR, "verify_post_update.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _auto_report_enabled() -> bool:
    """AUTO_REPORT_POST_UPDATE gates the automatic post-update bug report.
    Default ON (the 08-20 user directive: after every update, auto-report a
    real failure); the knob lets an operator turn it off. The endpoint still
    refuses to report anything but a genuine terminal failure."""
    try:
        raw = (_read_env_file().get("AUTO_REPORT_POST_UPDATE") or "true").strip().lower()
    except Exception:
        raw = "true"
    return raw not in ("0", "false", "no", "off")


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
    mode: str = "recurring"   # "recurring" | "onetime"
    day: str = "daily"        # recurring only: "daily" or 0-6 (0=Sunday)
    hour: int = 2             # recurring only: 0-23 (LOCAL time)
    when: str = ""            # onetime only: local "YYYY-MM-DDTHH:MM"


def _check_is_stale(checked_at, hours=None):
    """True when the last check is older than the staleness window (default
    UPDATES_CHECK_STALE_HOURS, 6 h). Never errors on a bad/missing timestamp —
    a missing timestamp is handled by the caller as stale anyway."""
    if not checked_at:
        return True
    try:
        h = hours if hours is not None else int(
            (_read_env_file().get("UPDATES_CHECK_STALE_HOURS") or "6"))
        if h <= 0:
            return False
        ts = datetime.datetime.fromisoformat(str(checked_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - ts
        return age.total_seconds() > h * 3600
    except Exception:
        return True


def _version_gt(a: str, b: str) -> bool:
    """CalVer ordering: 2026.08.17.b > 2026.08.17.a > 2026.08.16.i. True only
    when a is genuinely NEWER than b — a downgrade or equal is NOT an available
    update (08-17: a stale manifest briefly showed .a as available while
    running .b, inverting the Updates banner)."""
    import re as _re
    def _parts(v: str):
        m = _re.match(r"^(\d{4})\.(\d{2})\.(\d{2})(?:\.([a-z]))?$", str(v or "").strip())
        if not m:
            return None
        y, mo, d, s = m.groups()
        return (int(y), int(mo), int(d), ord(s or "`"))
    pa, pb = _parts(a), _parts(b)
    return bool(pa and pb and pa > pb)


def _release_changelog_url(version: str) -> str:
    """The GitHub Release changelog URL for a version — the release asset URL
    versions.json already carries (status.json's `changelog`), falling back to
    the canonical template when no check has run yet."""
    url = (_read_status().get("changelog") or "").strip()
    return url or CHANGELOG_URL_TMPL.format(version=version)


def _github_release_api_url(changelog_url: str) -> str:
    """Map a github.com release page URL to its API endpoint; '' when the URL
    doesn't match the expected shape."""
    m = GITHUB_RELEASE_RE.match((changelog_url or "").strip())
    if not m:
        return ""
    owner, repo, tag = m.groups()
    return f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"


def _fetch_release_body(version: str) -> str:
    """Best-effort fetch of the sanitized GitHub Release body (the release
    workflow writes the CHANGELOG section straight into the release notes).
    Returns '' on ANY failure — the note must never block the update."""
    api_url = _github_release_api_url(_release_changelog_url(version))
    if not api_url:
        api_url = ("https://api.github.com/repos/Ridge-Chapel-Tech/"
                   f"barenoc-appliance/releases/tags/v{version}")
    try:
        import urllib.request
        req = urllib.request.Request(api_url, headers={
            "User-Agent": "bareNOC-update/1",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        return str(data.get("body") or "").strip()
    except Exception:
        return ""


def _release_bullets(body: str, limit: int = 4) -> list:
    """Lift up to `limit` cleaned bullet lines from a markdown release body.
    Skips section headers; strips inline markdown formatting and truncates."""
    out = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "|")):
            continue
        if line.startswith(("-", "*", "+")) and len(line) > 1 and line[1] in " \t":
            text = line[1:].strip()
        else:
            m = re.match(r"^\d+[.)]\s+(.*)$", line)
            if not m:
                continue
            text = m.group(1).strip()
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = text.strip()
        if not text:
            continue
        if len(text) > 240:
            text = text[:240].rstrip() + "…"
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _whats_new_summary(version: str) -> dict:
    """Build the owner note's 'what's new' content: 2–4 summary bullets (from
    the sanitized release body) + the full changelog link. Falls back to a
    'see the changelog' summary when the body is unreachable/empty. Never
    raises."""
    body = _fetch_release_body(version)
    bullets = _release_bullets(body, limit=4)
    if len(bullets) < 2:
        bullets = []
    return {
        "version": version,
        "date": _local_now().strftime("%Y-%m-%d"),
        "bullets": bullets,
        "changelog_url": _release_changelog_url(version),
    }


def _whats_new_marker() -> str:
    """The version-keyed marker path (derived from STATUS_DIR so tests can
    patch STATUS_DIR without touching the real volume)."""
    return os.path.join(STATUS_DIR, "whats_new_notified.key")


def _whats_new_sent() -> str:
    """The last version the what's-new note was sent for ('' when never)."""
    try:
        with open(_whats_new_marker()) as f:
            return f.read().strip()
    except Exception:
        return ""


def _whats_new_already_sent(version: str) -> bool:
    return _whats_new_sent() == (version or "").strip()


def _mark_whats_new_sent(version: str):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(_whats_new_marker(), "w") as f:
            f.write((version or "").strip())
    except Exception:
        pass


def _whats_new_html(summary: dict) -> str:
    import html as _html
    ver = _html.escape(str(summary.get("version") or ""))
    date_str = _html.escape(str(summary.get("date") or ""))
    bullets = summary.get("bullets") or []
    link = summary.get("changelog_url") or ""
    parts = [
        f"<h2 style='color:#1d4ed8;margin:0 0 12px'>BareNOC updated to v{ver}</h2>",
        f"<p style='margin:0 0 12px;color:#333'>Your appliance updated itself "
        f"on {date_str}. Here's what's new:</p>",
    ]
    if bullets:
        parts.append("<ul style='margin:0 0 12px;padding-left:20px'>")
        for b in bullets:
            parts.append(f"<li style='margin:4px 0'>{_html.escape(b)}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p style='margin:0 0 12px;color:#333'>See the changelog "
                     "for what's new in this release.</p>")
    if link:
        parts.append(
            f"<p style='margin:0 0 16px'><a href='{_html.escape(link, quote=True)}'>"
            f"Full changelog</a></p>")
    parts.append("<p style='color:#888;font-size:12px;margin-top:16px'>— BareNOC</p>")
    return "".join(parts)


def _whats_new_text(summary: dict) -> str:
    ver = str(summary.get("version") or "")
    date_str = str(summary.get("date") or "")
    bullets = summary.get("bullets") or []
    link = summary.get("changelog_url") or ""
    lines = [f"BareNOC updated to v{ver}",
             f"Your appliance updated itself on {date_str}. Here's what's new:"]
    if bullets:
        lines.extend(f"  • {b}" for b in bullets)
    else:
        lines.append("See the changelog for what's new in this release.")
    if link:
        lines.append(f"Full changelog: {link}")
    return "\n".join(lines)


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
        "signature": "",
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
            "signature": (m.get("assets") or {}).get("signature", ""),
            "available": _version_gt(latest, cur),
        })
    except Exception as e:
        status["manifest_error"] = str(e)
    _write_status(status)
    return status


@router.get("/status")
def update_status(user: User = Depends(require_any_role("technician", "operator", "admin", "agent"))):
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
    status.setdefault("signature", "")
    # Signal when the persisted check is stale so the UI refreshes it on load:
    # (a) the persisted check predates the running build (fresh deploy), or
    # (b) the check is simply OLD — a stable build on the same version must
    # still discover new releases within a few hours (08-18: a box on .b
    # stopped ever re-checking because stored_current == live forever).
    status["check_stale"] = bool(
        stored_current != live
        or not status.get("checked_at")
        or _check_is_stale(status.get("checked_at"))
    )
    status["schedule"] = _read_schedule()
    last = _read_update_result()
    status["last_update"] = last
    status["progress"] = _confirmed_progress(_read_progress())
    return status


@router.post("/check")
def update_check(user: User = Depends(require_any_role("technician", "operator", "admin", "agent"))):
    return _run_check()


@router.post("/now")
def update_now(user: User = Depends(require_any_role("technician", "operator", "admin", "agent"))):
    status = _read_status() or _run_check()
    if not status.get("available"):
        raise HTTPException(400, "already up to date (or the manifest is unreachable)")
    payload = {"version": status.get("latest"), "kind": status.get("kind"),
               "tarball": status.get("tarball"), "checksums": status.get("checksum"),
               "signature": status.get("signature"),
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
def update_rollback(user: User = Depends(require_any_role("technician", "operator", "admin", "agent"))):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(ROLLBACK_REQ, "w") as f:
            json.dump({"requested_at": _now()}, f, indent=2)
    except Exception as e:
        raise HTTPException(500, f"could not queue the rollback: {e}")
    return {"status": "accepted", "note": "rolling back to the previous release in the background"}


def _read_schedule() -> dict:
    conf = {"enabled": False, "mode": "recurring", "day": "daily",
            "hour": 2, "when": "", "fired": "", "timezone": _appliance_tz()}
    try:
        with open(os.path.join(STATUS_DIR, "update_schedule.conf")) as f:
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
    # Backward compatible: a conf written by the old enabled/day/hour format has
    # no `mode` — treat it as recurring (now in LOCAL time).
    if conf.get("mode") not in ("recurring", "onetime"):
        conf["mode"] = "recurring"
    conf["timezone"] = _appliance_tz()
    return conf


def _write_schedule(conf: dict):
    """Persist the canonical schedule conf (mode/enabled/day/hour/when/fired)."""
    mode = conf.get("mode") or "recurring"
    enabled = conf.get("enabled")
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("true", "1", "yes")
    else:
        enabled = bool(enabled)
    day = conf.get("day")
    day = "daily" if day in (None, "") else str(day)
    try:
        hour = int(conf.get("hour", 2))
    except (TypeError, ValueError):
        hour = 2
    when = (conf.get("when") or "").strip()
    fired = (conf.get("fired") or "").strip()
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(os.path.join(STATUS_DIR, "update_schedule.conf"), "w") as f:
            f.write("# BareNOC update schedule (System → Updates; the scheduler applies it)\n")
            f.write(f"mode={mode}\n")
            f.write(f"enabled={'true' if enabled else 'false'}\n")
            f.write(f"day={day}\n")
            f.write(f"hour={hour}\n")
            f.write(f"when={when}\n")
            f.write(f"fired={fired}\n")
    except Exception as e:
        raise HTTPException(500, f"could not save the schedule: {e}")


def ensure_default_update_schedule(db: Session = None, actor: str = "system") -> dict:
    """Write the DEFAULT_UPDATE_SCHEDULE ONCE — only when update_schedule.conf
    does not exist. The conf file's existence (enabled OR explicitly disabled)
    is the permanent opt-out marker: no future release can flip it back.

    Used by two paths (idempotent, never overwrites):
      - fresh installs: the setup wizard's completion sweep, and
      - upgrades: the API-startup migration for boxes that never configured
        anything (loompafoo's class).

    Returns {"written": bool, "reason": str} — never raises (a startup
    migration must not take the API down)."""
    path = os.path.join(STATUS_DIR, "update_schedule.conf")
    if os.path.exists(path):
        return {"written": False, "reason": "schedule conf already exists (preserved)"}
    try:
        _write_schedule(dict(DEFAULT_UPDATE_SCHEDULE))
        if db is not None:
            log_event(db, "update_schedule_change", actor, {
                "action": "default_created",
                "mode": DEFAULT_UPDATE_SCHEDULE["mode"],
                "enabled": bool(DEFAULT_UPDATE_SCHEDULE["enabled"]),
            })
        return {"written": True, "reason": "wrote the default auto-update schedule"}
    except Exception as e:
        return {"written": False, "reason": f"could not write the default schedule: {e}"}


@router.post("/notify")
def update_notify(body: dict = None,
                  user: User = Depends(require_any_role("technician", "operator", "admin", "agent"))):
    """Email the terminal update state (called by the scheduler when the
    host service's progress flips to done/failed). Best-effort; never raises.

    On a successful update that actually APPLIES a new version, the 'done'
    note becomes the owner's "what changed" note — a short friendly summary
    (2–4 bullets lifted from the sanitized GitHub Release body) + a link to
    the full changelog. One note per applied version; a rollback keeps the
    plain "updated" email; a duplicate version is silently skipped.
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
            return {"status": "ok", "notified": False,
                    "note": "no ALERT_RECIPIENTS configured"}

        if stage == "failed":
            rows = [
                ["Appliance", "BareNOC"],
                ["Version", version],
                ["Outcome", "❌ update failed"],
                ["Detail", message or "see the Updates card"],
            ]
            subject = f"BareNOC update FAILED ({version})"
            body_html = alert_html(subject, rows)
            result, err = send_email(recipients, subject, body_html=body_html)
            return {"status": "ok", "notified": result, "error": err}

        # stage == done. Only a genuine version upgrade gets the what's-new
        # note — a rollback reports action=rollback and keeps the plain email.
        last = _read_update_result()
        if str(last.get("action") or "").strip() == "rollback":
            rows = [
                ["Appliance", "BareNOC"],
                ["Version", version],
                ["Outcome", "✅ update complete"],
                ["Detail", message or "—"],
            ]
            subject = f"BareNOC appliance updated to {version}"
            body_html = alert_html(subject, rows)
            result, err = send_email(recipients, subject, body_html=body_html)
            return {"status": "ok", "notified": result, "error": err}

        if _whats_new_already_sent(version):
            return {"status": "ok", "notified": False,
                    "note": f"what's-new note already sent for {version}"}

        summary = _whats_new_summary(version)
        subject = f"BareNOC updated to v{version} — here's what's new"
        result, err = send_email(recipients, subject,
                                 body_html=_whats_new_html(summary),
                                 body_text=_whats_new_text(summary))
        if result:
            _mark_whats_new_sent(version)
        return {"status": "ok", "notified": result, "error": err,
                "whats_new": True}
    except Exception as e:
        return {"status": "ok", "notified": False, "error": str(e)}


@router.post("/auto-report")
def update_auto_report(body: dict = None,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_any_role("agent", "technician", "operator", "admin"))):
    """File a bug through the in-app Submit-Report path when a post-update
    check failed AND AUTO_REPORT_POST_UPDATE is enabled.

    Called by the scheduler after the host self-update service reaches a
    terminal 'failed' stage (a failed/rolled-back update, or a failed
    post-update verification). Only REAL failures are reported — a healthy
    update never calls this. The report ships the stage + evidence as the
    comment and the full redacted support bundle as the attachment.
    """
    body = body or {}
    if not _auto_report_enabled():
        return {"reported": False, "note": "AUTO_REPORT_POST_UPDATE is disabled"}

    stage = str(body.get("stage") or "").strip()
    message = str(body.get("message") or "").strip()
    version = str(body.get("version") or _current_version()).strip()

    # Confirm a real failure from persisted state (never fabricate one).
    prog = _read_progress()
    if not stage:
        stage = str(prog.get("stage") or "").strip()
    if not message:
        message = str(prog.get("message") or "").strip()
    if stage != "failed":
        return {"reported": False, "note": f"no reportable failure (stage={stage!r})"}

    result = _read_update_result()
    verify = _read_verify_result()
    evidence = {
        "progress": {k: prog.get(k) for k in ("stage", "pct", "message", "at")},
        "update_result": result,
        "verify_post_update": verify,
    }

    comment = (
        "Post-update verification failed.\n\n"
        f"stage: {stage}\n"
        f"version: {version}\n"
        f"message: {message or '(none)'}\n\n"
        "evidence:\n" + json.dumps(evidence, indent=2, default=str)
    )

    system_user = SimpleNamespace(username="barenoc-auto-report",
                                 display_name="BareNOC appliance")
    try:
        import report_submit
        from routes import support as _support
        bundle = _support.build_bundle(comment, db, system_user)
        out = report_submit.submit_report(comment, system_user, bundle=bundle,
                                          bundle_filename="barenoc-support.md",
                                          flagged=False)
    except RuntimeError as e:
        return {"reported": False, "error": str(e)}
    return {"reported": True, **out}


@router.get("/schedule")
def get_schedule(user: User = Depends(require_any_role("technician", "operator", "admin", "agent"))):
    return _read_schedule()


def _audit_schedule_change(db, actor, data):
    """Audit an update-schedule change. `db` may be None in direct unit
    calls (tests pass db=None); FastAPI always injects a real session."""
    if db is None:
        return
    log_event(db, "update_schedule_change", actor, data)


@router.post("/schedule")
def set_schedule(body: ScheduleBody,
                 user: User = Depends(require_any_role("technician", "operator", "admin", "agent")),
                 db: Session = Depends(get_db)):
    mode = (body.mode or "recurring").strip().lower()
    if mode not in ("recurring", "onetime"):
        raise HTTPException(422, "mode must be 'recurring' or 'onetime'")
    day = str(body.day or "daily").strip()
    hour = int(body.hour)
    when = (body.when or "").strip()

    if mode == "recurring":
        if hour < 0 or hour > 23:
            raise HTTPException(422, "hour must be 0-23")
        if day != "daily":
            try:
                day = int(day)
            except ValueError:
                raise HTTPException(422, "day must be 'daily' or 0-6")
            if day < 0 or day > 6:
                raise HTTPException(422, "day must be 'daily' or 0-6")
        when = ""
    else:  # onetime — a LOCAL datetime in the future
        try:
            when_dt = _parse_local_dt(when)
        except Exception:
            raise HTTPException(422, "when must be a local datetime like 'YYYY-MM-DDTHH:MM'")
        if when_dt <= _local_now():
            raise HTTPException(422, "when must be in the future (local time)")
        when = when_dt.strftime("%Y-%m-%dT%H:%M")  # canonical, no seconds
        day = "daily"

    _write_schedule({
        "mode": mode,
        "enabled": bool(body.enabled),
        "day": day,
        "hour": hour,
        "when": when,
        "fired": "",   # (re)arming clears any previous fire
    })
    _audit_schedule_change(db, user.username, {
        "action": "set",
        "mode": mode,
        "enabled": bool(body.enabled),
        "day": str(day),
        "hour": hour,
        "when": when,
    })
    return {"status": "ok", "schedule": _read_schedule()}


@router.post("/schedule/complete")
def complete_schedule(user: User = Depends(require_any_role("technician", "operator", "admin", "agent")),
                      db: Session = Depends(get_db)):
    """Mark a one-time schedule as fired and disable it (persisted — survives
    restart). Called by the scheduler AFTER it has actually queued the update."""
    conf = _read_schedule()
    if conf.get("mode") == "onetime":
        conf["fired"] = _now()
        conf["enabled"] = False
        _write_schedule(conf)
        _audit_schedule_change(db, user.username, {
            "action": "complete", "mode": "onetime", "enabled": False,
            "when": conf.get("when", ""),
        })
    return {"status": "ok", "schedule": _read_schedule()}


@router.post("/schedule/cancel")
def cancel_schedule(user: User = Depends(require_any_role("technician", "operator", "admin", "agent")),
                    db: Session = Depends(get_db)):
    """Cancel the current schedule: disable it and clear any one-time when/fired."""
    conf = _read_schedule()
    conf["enabled"] = False
    conf["when"] = ""
    conf["fired"] = ""
    _write_schedule(conf)
    _audit_schedule_change(db, user.username, {
        "action": "cancel", "mode": conf.get("mode", "recurring"),
        "enabled": False,
    })
    return {"status": "ok", "schedule": _read_schedule()}
