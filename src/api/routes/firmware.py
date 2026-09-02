"""Firmware management routes — System → Firmware (inventory, windows, queue,
history) + the pending-action API the roles-and-chat-context worker consumes.

Role visibility (mirrors firmware.py):
  * admin        — sees + acts on every pending item.
  * technician   — (operator today, a real technician role later) sees + acts
                   on NON-admin items only when FIRMWARE_TECH_VISIBILITY is on;
                   gateway approvals are admin-only regardless (required_role).
"""

import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user, require_role, require_any_role
from database import get_db
from models import (User, DeviceFirmware, FirmwareUpgrade, MaintenanceWindow,
                    PendingAction)
from audit import log_event
from routes.updates import _parse_local_dt, _local_now, _appliance_tz

import firmware

router = APIRouter(prefix="/api/v1/firmware", tags=["firmware"])

_ACTABLE_ROLES = ("admin", "operator", "technician")


def _can_act(user: User, item: PendingAction) -> bool:
    """May this user act on (approve/defer/escalate/resolve) the item?"""
    if user.role == "admin":
        return True
    if (item.required_role or "") == "admin":
        return False  # gateway approval admin-only regardless
    if user.role in _ACTABLE_ROLES and item.required_role != "admin":
        return firmware.technician_visibility_enabled()
    return False


def _can_see(user: User, item: PendingAction) -> bool:
    if user.role == "admin":
        return True
    if (item.required_role or "") == "admin":
        return False
    if user.role in _ACTABLE_ROLES:
        return firmware.technician_visibility_enabled()
    return False


def _action_dict(a: PendingAction) -> dict:
    return {
        "id": a.id,
        "kind": a.kind,
        "title": a.title,
        "detail": a.detail,
        "device_id": a.device_id,
        "mac_address": a.mac_address,
        "device_name": a.device_name,
        "device_type": a.device_type,
        "firmware_from": a.firmware_from,
        "firmware_to": a.firmware_to,
        "status": a.status,
        "auto": a.auto,
        "required_role": a.required_role,
        "resolved_by": a.resolved_by,
        "resolved_note": a.resolved_note,
        "metadata": a.extra or {},
        "severity": (a.extra or {}).get("severity", ""),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
    }


def _window_dict(w: MaintenanceWindow) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "mode": w.mode,
        "day": w.day,
        "hour": w.hour,
        "duration_minutes": w.duration_minutes,
        "when": w.when,
        "enabled": w.enabled,
        "timezone": w.timezone or _appliance_tz(),
        "active_now": firmware.window_active(w),
        "created_by": w.created_by,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


def _firmware_dict(f: DeviceFirmware) -> dict:
    return {
        "id": f.id,
        "device_id": f.device_id,
        "mac_address": f.mac_address,
        "name": f.name,
        "device_type": f.device_type,
        "model": f.model,
        "ip": f.ip,
        "current_version": f.current_version,
        "previous_version": f.previous_version,
        "available_version": f.available_version,
        "upgradeable": f.upgradeable,
        "online": f.online,
        "prestaged": bool(f.prestaged_version and f.prestaged_version == f.available_version),
        "last_result": f.last_result,
        "last_error": f.last_error,
        "last_upgrade_at": f.last_upgrade_at.isoformat() if f.last_upgrade_at else None,
    }


def _upgrade_dict(u: FirmwareUpgrade) -> dict:
    return {
        "id": u.id,
        "device_id": u.device_id,
        "mac_address": u.mac_address,
        "device_name": u.device_name,
        "device_type": u.device_type,
        "from_version": u.from_version,
        "to_version": u.to_version,
        "window_id": u.window_id,
        "status": u.status,
        "verify_attempts": u.verify_attempts,
        "rollback_attempted": u.rollback_attempted,
        "durations": u.durations or {},
        "error": u.error,
        "triggered_by": u.triggered_by,
        "started_at": u.started_at.isoformat() if u.started_at else None,
        "finished_at": u.finished_at.isoformat() if u.finished_at else None,
    }


# ── managed-service snapshot ───────────────────────────────────────────────

@router.get("/service")
def service_status(db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Managed-service snapshot: what the agent is doing for firmware right now.
    Read-only rollup of the engine's tables (inventory, windows, pending,
    history) — the "firmware as a service, not a button" surface the
    System → Firmware header panel (and later Juniper) present."""
    return firmware.service_summary(
        db, unifi_configured=firmware._client_from_env() is not None)


# ── inventory ───────────────────────────────────────────────────────────────

@router.get("/inventory")
def inventory(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(DeviceFirmware).order_by(DeviceFirmware.device_type,
                                             DeviceFirmware.name).all()
    items = [_firmware_dict(r) for r in rows]
    return {
        "devices": items,
        "total": len(items),
        "upgradeable": sum(1 for i in items if i["upgradeable"]),
        "autonomy": firmware.effective_autonomy(),
        "tech_visibility": firmware.technician_visibility_enabled(),
    }


@router.post("/refresh")
def refresh(db: Session = Depends(get_db),
            user: User = Depends(require_any_role("admin", "agent"))):
    client = firmware._client_from_env()
    if client is None:
        raise HTTPException(status_code=400, detail="UniFi authentication not configured")
    if not client.login():
        raise HTTPException(status_code=502,
                            detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    n = firmware.refresh_inventory(db, client)
    return {"status": "ok", "devices": n}


# ── maintenance windows ─────────────────────────────────────────────────────

@router.get("/windows")
def list_windows(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(MaintenanceWindow).order_by(MaintenanceWindow.id).all()
    return {"windows": [_window_dict(w) for w in rows],
            "timezone": _appliance_tz()}


@router.post("/windows")
def create_window(body: dict, db: Session = Depends(get_db),
                  user: User = Depends(require_role("admin"))):
    name = str((body or {}).get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    mode = str((body or {}).get("mode", "recurring")).strip().lower()
    if mode not in ("recurring", "onetime"):
        raise HTTPException(status_code=400, detail="mode must be recurring or onetime")
    day = str((body or {}).get("day", "daily")).strip() or "daily"
    try:
        hour = int((body or {}).get("hour", 3))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="hour must be 0-23")
    try:
        duration = int((body or {}).get("duration_minutes", 60))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="duration_minutes must be minutes")
    if not 1 <= duration <= 1440:
        raise HTTPException(status_code=400, detail="duration_minutes must be 1-1440")
    when = ""
    if mode == "recurring":
        if hour < 0 or hour > 23:
            raise HTTPException(status_code=400, detail="hour must be 0-23")
        if day != "daily":
            try:
                day = int(day)
            except ValueError:
                raise HTTPException(status_code=400, detail="day must be 'daily' or 0-6")
            if not 0 <= day <= 6:
                raise HTTPException(status_code=400, detail="day must be 'daily' or 0-6")
            day = str(day)
    else:
        try:
            when_dt = _parse_local_dt(str((body or {}).get("when", "")))
        except Exception:
            raise HTTPException(status_code=400,
                                detail="when must be a local datetime like 'YYYY-MM-DDTHH:MM'")
        when = when_dt.strftime("%Y-%m-%dT%H:%M")
        day = "daily"
    enabled = bool((body or {}).get("enabled", True))
    w = MaintenanceWindow(
        name=name, mode=mode, day=day, hour=hour,
        duration_minutes=duration, when=when, enabled=enabled,
        timezone=_appliance_tz(), created_by=user.username,
    )
    db.add(w)
    db.commit()
    log_event(db, "firmware_window_created", user.username,
              {"name": name, "mode": mode, "day": day, "hour": hour,
               "when": when, "duration_minutes": duration})
    return {"status": "ok", "window": _window_dict(w)}


@router.post("/windows/{window_id}/toggle")
def toggle_window(window_id: int, body: dict = None, db: Session = Depends(get_db),
                  user: User = Depends(require_role("admin"))):
    w = db.query(MaintenanceWindow).get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="window not found")
    if "enabled" in (body or {}):
        w.enabled = bool(body["enabled"])
    else:
        w.enabled = not w.enabled
    db.commit()
    return {"status": "ok", "window": _window_dict(w)}


@router.delete("/windows/{window_id}")
def delete_window(window_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require_role("admin"))):
    w = db.query(MaintenanceWindow).get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="window not found")
    db.delete(w)
    db.commit()
    log_event(db, "firmware_window_deleted", user.username, {"name": w.name})
    return {"status": "ok"}


# ── pending-action queue (approvals + escalations) ─────────────────────────

@router.get("/pending")
def pending_actions(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(PendingAction).order_by(PendingAction.created_at.desc()).all()
    items = []
    for a in rows:
        d = _action_dict(a)
        d["visible"] = _can_see(user, a)
        d["can_act"] = _can_act(user, a)
        items.append(d)
    visible = [i for i in items if i["visible"]]
    return {
        "items": visible,
        "total": len(visible),
        "all_count": len(items),
        "tech_visibility": firmware.technician_visibility_enabled(),
    }


def _get_pending(db, item_id: int) -> PendingAction:
    item = db.query(PendingAction).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="pending action not found")
    return item


@router.post("/pending/{item_id}/approve")
def approve_pending(item_id: int, body: dict = None, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    item = _get_pending(db, item_id)
    if item.kind != "approval":
        raise HTTPException(status_code=400, detail="only approvals can be approved")
    if not _can_act(user, item):
        raise HTTPException(status_code=403, detail="insufficient permissions")
    if item.status not in ("pending", "deferred"):
        raise HTTPException(status_code=400, detail=f"item is already {item.status}")
    item.status = "approved"
    item.resolved_by = user.username
    item.resolved_at = datetime.datetime.utcnow()
    item.resolved_note = str((body or {}).get("note", "")).strip() or "approved"
    db.commit()
    log_event(db, "firmware_approval_approved", user.username,
              {"item_id": item.id, "device": item.device_name})
    return {"status": "ok", "item": _action_dict(item)}


@router.post("/pending/{item_id}/defer")
def defer_pending(item_id: int, body: dict = None, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    item = _get_pending(db, item_id)
    if item.kind != "approval":
        raise HTTPException(status_code=400, detail="only approvals can be deferred")
    if not _can_act(user, item):
        raise HTTPException(status_code=403, detail="insufficient permissions")
    item.status = "deferred"
    item.resolved_note = str((body or {}).get("note", "")).strip() or "deferred"
    db.commit()
    log_event(db, "firmware_approval_deferred", user.username,
              {"item_id": item.id, "device": item.device_name})
    return {"status": "ok", "item": _action_dict(item)}


@router.post("/pending/{item_id}/escalate")
def escalate_pending(item_id: int, body: dict = None, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Promote an approval to a blocking escalation (P1, admin-only) — e.g. the
    operator wants a human to look at it. Halts the run until resolved."""
    item = _get_pending(db, item_id)
    if not _can_act(user, item):
        raise HTTPException(status_code=403, detail="insufficient permissions")
    note = str((body or {}).get("note", "")).strip() or "escalated by " + user.username
    item.kind = "escalation"
    item.required_role = "admin"
    item.status = "pending"
    item.resolved_at = None
    item.resolved_by = None
    item.extra = dict(item.extra or {})
    item.extra["severity"] = "P1"
    item.extra["escalated_by"] = user.username
    item.detail = (item.detail or "") + f"\nEscalated: {note}"
    db.commit()
    log_event(db, "firmware_approval_escalated", user.username,
              {"item_id": item.id, "device": item.device_name})
    return {"status": "ok", "item": _action_dict(item)}


@router.post("/pending/{item_id}/resolve")
def resolve_pending(item_id: int, body: dict = None, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Resolve any pending/deferred item (escalation resolve = halt cleared;
    approval resolve = rejected/abandoned)."""
    item = _get_pending(db, item_id)
    if not _can_act(user, item):
        raise HTTPException(status_code=403, detail="insufficient permissions")
    if item.status == "resolved":
        raise HTTPException(status_code=400, detail="item is already resolved")
    item.status = "resolved"
    item.resolved_by = user.username
    item.resolved_at = datetime.datetime.utcnow()
    item.resolved_note = str((body or {}).get("note", "")).strip() or "resolved"
    db.commit()
    log_event(db, "firmware_pending_resolved", user.username,
              {"item_id": item.id, "device": item.device_name, "kind": item.kind})
    return {"status": "ok", "item": _action_dict(item)}


# ── history ────────────────────────────────────────────────────────────────

@router.get("/history")
def history(limit: int = 50, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    rows = (db.query(FirmwareUpgrade)
            .order_by(FirmwareUpgrade.id.desc())
            .limit(min(max(1, limit), 200)).all())
    return {"upgrades": [_upgrade_dict(u) for u in rows]}


# ── manual engine tick (test + manual-trigger surface) ─────────────────────

@router.post("/tick")
def tick(db: Session = Depends(get_db),
         user: User = Depends(require_any_role("admin", "agent"))):
    client = firmware._client_from_env()
    if client is None:
        return {"status": "off", "note": "UniFi authentication not configured"}
    if not client.login():
        raise HTTPException(status_code=502,
                            detail=f"Could not log in to UniFi controller: {client.last_error or 'unknown'}")
    return firmware.engine_tick(db, client)
