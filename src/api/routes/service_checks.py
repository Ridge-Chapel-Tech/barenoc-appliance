"""Service Checks — admin CRUD + test-now + the scheduler's poll hook.

The check ENGINE lives in src/api/service_checks.py (state machine + probes);
this module is the HTTP surface. The SCHEDULER calls POST /poll each cycle —
the engine runs in the API container (DB + models + emailer), the scheduler
only triggers it, same pattern as Network Optimization.
"""

import logging
import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import require_role, require_any_role
from database import get_db
from models import Device, ServiceMonitor, ServiceCheckEpisode, Ticket
from audit import log_event
from worknotes import add_note
import service_checks

logger = logging.getLogger("barenoc.service-checks")

router = APIRouter(prefix="/api/v1/service-checks", tags=["service-checks"])

CHECK_TYPES = service_checks.CHECK_TYPES


# ── validation ──────────────────────────────────────────────────────────────

def _clean_params(check_type: str, params: dict) -> dict:
    out = {}
    if check_type == "tcp":
        try:
            port = int(params.get("port"))
        except (TypeError, ValueError):
            raise HTTPException(400, "TCP checks need a port (1-65535)")
        if not 1 <= port <= 65535:
            raise HTTPException(400, "port must be 1-65535")
        out["port"] = port
    elif check_type == "http":
        path = str(params.get("path") or "/").strip()
        if not path:
            path = "/"
        if not path.startswith("/"):
            path = "/" + path
        out["path"] = path
        try:
            expected = int(params.get("expected_status") or 200)
        except (TypeError, ValueError):
            raise HTTPException(400, "expected_status must be a number")
        if not 100 <= expected <= 599:
            raise HTTPException(400, "expected_status must be 100-599")
        out["expected_status"] = expected
        out["body_contains"] = str(params.get("body_contains") or "")
        out["https"] = bool(params.get("https"))
    return out


def _validate(data: dict, db: Session, partial: bool = False) -> dict:
    """Validate + normalize a monitor payload. Presence-aware: only keys that
    appear in `data` are returned (so a partial update never clobbers fields)."""
    out = {}

    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        if len(name) > 128:
            raise HTTPException(400, "name must be ≤ 128 characters")
        out["name"] = name

    if "check_type" in data:
        check_type = str(data.get("check_type") or "ping").strip().lower()
        if check_type not in CHECK_TYPES:
            raise HTTPException(400, f"check_type must be one of {', '.join(CHECK_TYPES)}")
        out["check_type"] = check_type
    else:
        check_type = None

    if "target" in data:
        out["target"] = str(data.get("target") or "").strip() or None

    if "target_device_id" in data:
        raw = data.get("target_device_id")
        if raw in (None, "", 0):
            out["target_device_id"] = None
        else:
            try:
                target_device_id = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(400, "target_device_id must be a device id")
            if db.get(Device, target_device_id) is None:
                raise HTTPException(404, "linked device not found")
            out["target_device_id"] = target_device_id

    # Full create: at least one of target / target_device_id must resolve.
    if not partial and "target" not in data and "target_device_id" not in data:
        raise HTTPException(400, "set a target (host/IP) or link a device")
    if not partial:
        target = out.get("target")
        target_device_id = out.get("target_device_id")
        if not target and not target_device_id:
            raise HTTPException(400, "set a target (host/IP) or link a device")

    if "params" in data:
        ct = out.get("check_type") or check_type
        out["params"] = _clean_params(ct, data.get("params") or {})

    def _int_field(key, lo, hi):
        if key not in data:
            return
        v = data.get(key)
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a number")
        if not lo <= iv <= hi:
            raise HTTPException(400, f"{key} must be {lo}-{hi}")
        out[key] = iv

    _int_field("interval_min", 1, 1440)
    _int_field("fail_threshold", 1, 100)
    _int_field("recovery_ok", 1, 100)
    if "notify" in data:
        out["notify"] = bool(data["notify"])
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    return out


def _serialize(session: Session, m: ServiceMonitor) -> dict:
    target = service_checks.resolve_target(session, m)
    device = session.get(Device, m.target_device_id) if m.target_device_id else None
    ep = session.query(ServiceCheckEpisode).filter(
        ServiceCheckEpisode.monitor_id == m.id).first()
    ticket = None
    if ep and ep.ticket_id:
        ticket = session.query(Ticket).filter(Ticket.ticket_id == ep.ticket_id).first()
    return {
        "id": m.id,
        "name": m.name,
        "check_type": m.check_type,
        "target": m.target or "",
        "target_device_id": m.target_device_id,
        "target_device_name": (device.name if device else None),
        "resolved_target": target or None,
        "params": m.params or {},
        "interval_min": m.interval_min,
        "fail_threshold": m.fail_threshold,
        "recovery_ok": m.recovery_ok,
        "notify": bool(m.notify),
        "enabled": bool(m.enabled),
        "last_status": m.last_status or "unknown",
        "last_check_at": m.last_check_at.isoformat() if m.last_check_at else None,
        "last_error": m.last_error,
        "fail_streak": m.fail_streak or 0,
        "ok_streak": m.ok_streak or 0,
        "open_ticket": ({"ticket_id": ticket.ticket_id, "priority": ticket.priority,
                         "status": ticket.status, "title": ticket.title}
                        if ticket else None),
    }


# ── endpoints ───────────────────────────────────────────────────────────────

@router.get("")
def list_monitors(db: Session = Depends(get_db),
                  user=Depends(require_role("admin"))):
    rows = db.query(ServiceMonitor).order_by(ServiceMonitor.id.asc()).all()
    return {"monitors": [_serialize(db, m) for m in rows]}


@router.post("", status_code=201)
def create_monitor(data: dict, db: Session = Depends(get_db),
                   user=Depends(require_role("admin"))):
    v = _validate(data, db)
    # For create, apply the engine defaults when the form left them unset.
    cfg = service_checks.service_check_config()
    m = ServiceMonitor(
        name=v["name"], check_type=v.get("check_type", "ping"),
        target=v.get("target") or "", target_device_id=v.get("target_device_id"),
        params=v.get("params") or {},
        interval_min=v.get("interval_min", cfg["default_interval_min"]),
        fail_threshold=v.get("fail_threshold", cfg["default_fail_threshold"]),
        recovery_ok=v.get("recovery_ok", cfg["default_recovery_ok"]),
        notify=v.get("notify", True), enabled=v.get("enabled", True),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    log_event(db, "service_check_created", user.username, {
        "monitor_id": m.id, "monitor": m.name, "check_type": m.check_type,
    })
    return _serialize(db, m)


@router.get("/{monitor_id}")
def get_monitor(monitor_id: int, db: Session = Depends(get_db),
                user=Depends(require_role("admin"))):
    m = db.get(ServiceMonitor, monitor_id)
    if not m:
        raise HTTPException(404, "monitor not found")
    return _serialize(db, m)


@router.put("/{monitor_id}")
def update_monitor(monitor_id: int, data: dict, db: Session = Depends(get_db),
                   user=Depends(require_role("admin"))):
    m = db.get(ServiceMonitor, monitor_id)
    if not m:
        raise HTTPException(404, "monitor not found")
    v = _validate(data, db, partial=True)
    changed = []
    if "name" in v:
        m.name = v["name"]; changed.append("name")
    if "check_type" in v:
        m.check_type = v["check_type"]; changed.append("check_type")
    if "target" in v:
        m.target = v["target"] or ""; changed.append("target")
    if "target_device_id" in v:
        m.target_device_id = v["target_device_id"]; changed.append("target_device_id")
    if "params" in v:
        m.params = v["params"]; changed.append("params")
    if "interval_min" in v:
        m.interval_min = v["interval_min"]; changed.append("interval_min")
    if "fail_threshold" in v:
        m.fail_threshold = v["fail_threshold"]; changed.append("fail_threshold")
    if "recovery_ok" in v:
        m.recovery_ok = v["recovery_ok"]; changed.append("recovery_ok")
    if "notify" in v:
        m.notify = v["notify"]; changed.append("notify")
    if "enabled" in v:
        m.enabled = v["enabled"]; changed.append("enabled")
    if changed:
        db.commit()
        log_event(db, "service_check_updated", user.username, {
            "monitor_id": m.id, "monitor": m.name, "fields": sorted(set(changed)),
        })
    return _serialize(db, m)


@router.delete("/{monitor_id}")
def delete_monitor(monitor_id: int, db: Session = Depends(get_db),
                   user=Depends(require_role("admin"))):
    m = db.get(ServiceMonitor, monitor_id)
    if not m:
        raise HTTPException(404, "monitor not found")
    ep = db.query(ServiceCheckEpisode).filter(
        ServiceCheckEpisode.monitor_id == monitor_id).first()
    if ep:
        ticket = db.query(Ticket).filter(Ticket.ticket_id == ep.ticket_id).first() \
            if ep.ticket_id else None
        if ticket and ticket.status in ("open", "in_progress"):
            add_note(ticket, "service_check_deleted",
                     "Monitor deleted — closing the open service-check ticket.")
            ticket.status = "closed"
            ticket.resolution = "Monitor deleted"
            ticket.resolved_at = datetime.datetime.utcnow()
        db.delete(ep)
    name = m.name
    db.delete(m)
    db.commit()
    log_event(db, "service_check_deleted", user.username, {
        "monitor_id": monitor_id, "monitor": name,
    })
    return {"status": "ok"}


@router.post("/{monitor_id}/test")
def test_monitor(monitor_id: int, db: Session = Depends(get_db),
                 user=Depends(require_role("admin"))):
    """Run one probe NOW without touching state (no streaks, no tickets)."""
    m = db.get(ServiceMonitor, monitor_id)
    if not m:
        raise HTTPException(404, "monitor not found")
    target = service_checks.resolve_target(db, m)
    if not target:
        return {"status": "error", "detail": "no target (set a host/IP or link a device)"}
    ok, detail = service_checks.run_probe(m, target)
    return {"status": "up" if ok else "down", "detail": detail,
            "target": target, "check_type": m.check_type}


@router.post("/poll")
def poll(db: Session = Depends(get_db),
         user=Depends(require_any_role("admin", "agent"))):
    """Scheduler-facing: run one full service-check pass. The engine uses its
    own session; this dependency's session is only used for auth."""
    engine = service_checks.ServiceCheckEngine()
    summary = engine.check()
    return {"status": "ok", **summary}
