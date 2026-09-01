"""Firmware management engine (UniFi-only v1).

Autonomy-aware firmware upgrades for the controller's managed gear (APs →
switches → gateway LAST), running ONLY inside maintenance windows, one device
at a time, verify-then-next, halt-on-failure, with rollback + physical
escalation.

Design locked by the user (2026-08-18):
  * UniFi-only v1 (vendor-agnostic framework later). Managed gear = gateway /
    switches / APs from the controller.
  * Upgrade order by risk: APs → switches → gateway LAST.
  * Gateway INCLUDED (NOT manual-only) — the product point: BareNOC exists to
    fix the never-patched home router. Gateway upgrades get the most
    conservative treatment.
  * Maintenance windows: local-time low-impact windows (default 03:00),
    one-time + recurring (reuse the updates-schedule-v2 local-time machinery).
    Firmware is PRE-STAGED before the window; upgrades only run inside one.
  * Execution: one device at a time → verify (version bumped AND device
    returned + informing) → next. Any failure = halt the run + alert.
  * Approval (autonomy matrix):
      autonomous — auto-run inside the window; one-at-a-time verified; NOTIFY
                   "action pending" (non-blocking — proceed even with no
                   response); escalate on failure.
      balanced   — auto for APs/switches inside the window; gateway requires
                   explicit approval; rest identical.
      strict     — every upgrade individually approved; nothing auto.
      off        — opt out (setup toggle; default = follow the autonomy profile).
  * Physical escalation: upgrade + rollback both fail → P1 ticket + email with
    device details + a runbook. Never limp silently.
  * Pending-action queue (FEED for roles-and-chat-context): approvals +
    escalations are persisted as actionable pending items with role visibility.

The engine runs as a daemon thread in the API container (like the alert /
telemetry engines) — it holds DB sessions, the UniFi client, and multi-minute
verify waits, so it must live in the api process, not the scheduler. The
scheduler is deliberately NOT involved; this keeps the long-lived state machine
in one place and restart-safe (in-flight rows are persisted).

OPEN ITEMS (flagged for the policy discussion, see the handoff):
  * Default autonomy when neither FIRMWARE_AUTONOMY nor LLM_POLICY_PROFILE is
    set is "balanced" (auto APs/switches, gateway approval). This is the sane
    home-router default but should be confirmed.
  * Balanced vs Strict semantics are still being defined upstream; this matrix
    is the first concrete consumer and should be ratified as the canonical one.
"""

import datetime
import logging
import os
import threading
import time

from database import SessionLocal
from models import (Device, DeviceFirmware, FirmwareUpgrade,
                    MaintenanceWindow, PendingAction, Ticket)
from emailer import send_email, get_recipients, alert_html
from schemas import generate_ticket_id
from audit import log_event
from change_log import record

# Reuse the updates-schedule-v2 local-time machinery (zoneinfo + .env TZ).
from routes.updates import _local_now, _parse_local_dt

logger = logging.getLogger("barenoc-firmware")

# ── tunables (prod via .env; tests drive the clock explicitly) ─────────────
STAGE_STAGING_S = 60         # pre-stage settle before the apply command
VERIFY_TIMEOUT_S = 900       # max wait for version bump + inform after apply
ROLLBACK_VERIFY_TIMEOUT_S = 300
PRESTAGE_LEAD_MIN = 60       # pre-stage when the next window starts within this
ENGINE_POLL_S = 20

FW_AUTONOMIES = ("autonomous", "balanced", "strict", "off")
RISK_ORDER = {"ap": 0, "switch": 1, "gateway": 2}
INFLIGHT_STATUSES = ("staging", "upgrading", "verifying", "rolling_back")

RUNBOOK = (
    "Physical-assistance runbook for {name} ({mac}):\n"
    "1. Locate the device (AP = ceiling/wall, switch = rack, gateway = the router box).\n"
    "2. For an AP or switch: press and HOLD the reset button for ~10 s until the LED "
    "flashes, then release. The device factory-resets and re-adopts.\n"
    "3. For a gateway (UCG/UDM/UXG): do NOT hold reset unless the UniFi UI is "
    "unreachable. First power-cycle (unplug 30 s, replug). If it still won't inform, "
    "hold reset ~10 s (factory reset), then restore from the UniFi backup in the "
    "controller.\n"
    "4. Verify the device re-appears ONLINE in the UniFi controller (or the BareNOC "
    "Devices page) and that its firmware shows {to_version} (or {from_version} if "
    "rolled back).\n"
    "5. Reply here or resolve the pending escalation once the device is recovered.\n"
    "Do NOT leave the device unreachable — a half-upgraded device may drop its "
    "uplink and take clients offline."
)


def _read_env() -> dict:
    """Hot-read /opt/barenoc/.env (file first, process env fallback)."""
    env = {}
    path = "/opt/barenoc/.env"
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except Exception:
        pass
    for key in ("FIRMWARE_AUTONOMY", "FIRMWARE_TECH_VISIBILITY",
                "LLM_POLICY_PROFILE", "TZ"):
        if key not in env and key in os.environ:
            env[key] = os.environ[key]
    return env


def _env_bool(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def effective_autonomy(env: dict = None) -> str:
    """The autonomy profile that governs firmware upgrades.

    FIRMWARE_AUTONOMY (the firmware-specific opt-out/override) wins when set to
    a known value; otherwise follow the autonomy profile. Default: balanced
    (auto APs/switches, gateway approval) — flagged as an open question.
    """
    env = env if env is not None else _read_env()
    fw = (env.get("FIRMWARE_AUTONOMY") or "").strip().lower()
    if fw in FW_AUTONOMIES:
        return fw
    profile = (env.get("LLM_POLICY_PROFILE") or "").strip().lower()
    if profile in ("autonomous", "balanced", "strict"):
        return profile
    return "balanced"


def technician_visibility_enabled(env: dict = None) -> bool:
    env = env if env is not None else _read_env()
    return _env_bool(env.get("FIRMWARE_TECH_VISIBILITY"))


def approval_decision(effective: str, device_type: str):
    """Encode the autonomy matrix. Returns (decision, required_role_or_None).

    decision in {"auto", "approval", "disabled"}.
    required_role is the minimum role that may act on a blocking approval
    ("admin" for gateways always; "technician" for non-gateway strict items).
    """
    dt = (device_type or "").lower()
    if effective == "off":
        return ("disabled", None)
    if effective == "autonomous":
        return ("auto", None)
    if effective == "balanced":
        if dt == "gateway":
            return ("approval", "admin")
        return ("auto", None)
    if effective == "strict":
        return ("approval", "admin" if dt == "gateway" else "technician")
    # Unknown profile — be conservative.
    return ("approval", "admin")


def window_active(window: MaintenanceWindow, now: datetime.datetime = None) -> bool:
    """True when ``now`` (naive LOCAL wall-clock) is inside the window."""
    now = now or _local_now()
    if not window.enabled:
        return False
    dur = max(1, int(window.duration_minutes or 60))
    if window.mode == "onetime":
        if not window.when:
            return False
        try:
            start = _parse_local_dt(window.when)
        except Exception:
            return False
        end = start + datetime.timedelta(minutes=dur)
        return start <= now < end
    # recurring: day (daily | 0-6, 0=Sunday) + hour, LOCAL
    day = (window.day or "daily")
    if day != "daily":
        try:
            sun0 = (now.weekday() + 1) % 7
            if sun0 != int(day):
                return False
        except (TypeError, ValueError):
            return False
    start_min = int(window.hour or 3) * 60
    now_min = now.hour * 60 + now.minute
    end_min = start_min + dur
    if end_min > 1440:  # wraps midnight
        return now_min >= start_min or now_min < (end_min - 1440)
    return start_min <= now_min < end_min


def _next_window_start(db, now: datetime.datetime):
    """Earliest future window start (naive local) among enabled windows, or None."""
    now = now or _local_now()
    best = None
    for w in db.query(MaintenanceWindow).filter(MaintenanceWindow.enabled.is_(True)).all():
        start = _window_start(w, now)
        if start is not None and (best is None or start < best):
            best = start
    return best


def _window_start(w: MaintenanceWindow, now: datetime.datetime):
    """The next start instant (naive local) of this window at/after ``now``."""
    now = now or _local_now()
    if w.mode == "onetime":
        try:
            start = _parse_local_dt(w.when)
        except Exception:
            return None
        return start if start >= now else None
    day = (w.day or "daily")
    hh = int(w.hour or 3)
    for offset in range(0, 8):  # look up to a week ahead
        candidate = (now + datetime.timedelta(days=offset)).replace(
            hour=hh, minute=0, second=0, microsecond=0)
        if day != "daily":
            try:
                if (candidate.weekday() + 1) % 7 != int(day):
                    continue
            except (TypeError, ValueError):
                continue
        if candidate >= now:
            return candidate
    return None


def _utc(dt: datetime.datetime) -> datetime.datetime:
    """Naive LOCAL wall-clock -> naive UTC (DST-safe via the appliance TZ).

    The SQLite datetime columns are NAIVE (like the rest of the codebase), so the
    engine stores + compares naive-UTC values everywhere. A naive input is
    treated as LOCAL wall-clock and converted; an aware input is normalized to
    UTC and stripped.
    """
    if dt is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    from routes.updates import _local_to_utc
    return _local_to_utc(dt).replace(tzinfo=None)


def refresh_inventory(db, client) -> int:
    """Pull stat/device firmware fields into DeviceFirmware (upsert by MAC).
    Returns the number of managed-device rows upserted."""
    devices = client.get_devices()
    seen = set()
    for d in devices:
        mac = (d.get("mac") or "").strip()
        if not mac:
            continue
        seen.add(mac)
        row = db.query(DeviceFirmware).filter(DeviceFirmware.mac_address == mac).first()
        dev = db.query(Device).filter(Device.mac_address == mac).first()
        if row is None:
            row = DeviceFirmware(mac_address=mac)
            db.add(row)
        row.device_id = dev.id if dev else None
        row.name = d.get("name") or row.name
        row.device_type = d.get("type") or row.device_type or "unknown"
        row.model = d.get("model") or row.model
        row.ip = d.get("ip") or row.ip
        row.current_version = d.get("version") or row.current_version or ""
        # The controller's previous/available may legitimately be empty (up to
        # date / never upgraded); only overwrite when the field is present so a
        # transient partial read doesn't wipe a real value.
        if d.get("previous_version"):
            row.previous_version = d["previous_version"]
        if d.get("available_version"):
            row.available_version = d["available_version"]
        row.upgradeable = bool(d.get("upgradeable"))
        row.online = (d.get("status") == "online")
    # Managed gear the controller no longer reports is offline (the U6-Mesh
    # case: stat/device drops it while it's down).
    for row in db.query(DeviceFirmware).all():
        if row.mac_address not in seen:
            row.online = False
    db.commit()
    return len(devices)


def _risk_key(device_type: str) -> int:
    return RISK_ORDER.get((device_type or "").lower(), 3)


def _next_candidate(db):
    """The next device to upgrade (risk order), or None.

    A pending ESCALATION halts the whole run (any failure = halt). Pending or
    deferred APPROVALS only block their own device. Approved items no longer
    block. In-flight upgrades block their own device.
    """
    halted = db.query(PendingAction).filter(
        PendingAction.kind == "escalation",
        PendingAction.status == "pending",
    ).count() > 0
    if halted:
        return None
    blocked = {p.mac_address for p in db.query(PendingAction).filter(
        PendingAction.kind == "approval",
        PendingAction.status.in_(("pending", "deferred")),
    ).all()}
    inflight = {u.mac_address for u in db.query(FirmwareUpgrade).filter(
        FirmwareUpgrade.status.in_(INFLIGHT_STATUSES)).all()}
    rows = db.query(DeviceFirmware).filter(
        DeviceFirmware.upgradeable.is_(True),
        DeviceFirmware.available_version.isnot(None),
        DeviceFirmware.available_version != "",
        DeviceFirmware.current_version != DeviceFirmware.available_version,
    ).all()
    candidates = [d for d in rows
                  if d.mac_address not in blocked
                  and d.mac_address not in inflight]
    candidates.sort(key=lambda d: (_risk_key(d.device_type), d.mac_address))
    return candidates[0] if candidates else None


def _notify_pending(db, pa: PendingAction, now, proceeding: bool = False) -> None:
    """In-app = the PendingAction row itself; email the alert channel when a
    transport is configured (send_email is best-effort and no-ops without one)."""
    try:
        recipients = get_recipients("alerts")
        if not recipients:
            return
        action = "proceeding automatically" if proceeding else "awaiting approval"
        rows = [
            ("Device", f"{pa.device_name} ({pa.device_type})"),
            ("Firmware", f"{pa.firmware_from} → {pa.firmware_to}"),
            ("Status", action),
            ("Action", "review the pending-actions queue on the System → Firmware page"),
        ]
        subject = ("BareNOC firmware: action pending — "
                   f"{pa.device_name} {pa.firmware_from} → {pa.firmware_to}")
        send_email(recipients, subject,
                   body_html=alert_html("Firmware action pending", rows))
    except Exception:
        logger.exception("firmware pending-email failed")


def _ensure_approval(db, dev: DeviceFirmware, required_role: str, now) -> PendingAction:
    """Create (or return) the blocking approval for one device. Dedups on the
    same MAC + pending state so repeated ticks never spam."""
    existing = db.query(PendingAction).filter(
        PendingAction.kind == "approval",
        PendingAction.mac_address == dev.mac_address,
        PendingAction.status.in_(("pending", "deferred")),
    ).first()
    if existing:
        return existing
    pa = PendingAction(
        kind="approval",
        title=f"Firmware upgrade: {dev.name} ({dev.device_type})",
        detail=(f"Approve {dev.name} ({dev.mac_address}) to upgrade from "
                f"{dev.current_version} to {dev.available_version} inside the "
                f"next maintenance window."),
        device_id=dev.device_id,
        mac_address=dev.mac_address,
        device_name=dev.name,
        device_type=dev.device_type,
        firmware_from=dev.current_version,
        firmware_to=dev.available_version,
        status="pending",
        auto=False,
        required_role=required_role,
        extra={"window": "next available"},
    )
    db.add(pa)
    db.commit()
    _notify_pending(db, pa, now)
    log_event(db, "firmware_approval_pending", "system", {
        "device": dev.name, "mac": dev.mac_address,
        "from": dev.current_version, "to": dev.available_version,
        "required_role": required_role,
    })
    return pa


def _start_upgrade(db, client, dev: DeviceFirmware, window, now,
                   triggered_by: str = "auto") -> FirmwareUpgrade:
    """Begin an upgrade run for one device: record a non-blocking auto notice
    (kind=approval, auto=True, status=approved) + the in-flight FirmwareUpgrade
    row, and notify 'action pending — proceeding'."""
    pa = PendingAction(
        kind="approval",
        title=f"Firmware upgrade (auto): {dev.name} ({dev.device_type})",
        detail=(f"{dev.name} will upgrade from {dev.current_version} to "
                f"{dev.available_version} — proceeding without waiting for a "
                f"response (autonomy policy)."),
        device_id=dev.device_id,
        mac_address=dev.mac_address,
        device_name=dev.name,
        device_type=dev.device_type,
        firmware_from=dev.current_version,
        firmware_to=dev.available_version,
        status="approved",
        auto=True,
        required_role="technician",
        resolved_by="system",
        resolved_at=_utc(now),
        extra={"window": window.name if window else ""},
    )
    db.add(pa)
    u = FirmwareUpgrade(
        device_id=dev.device_id,
        mac_address=dev.mac_address,
        device_name=dev.name,
        device_type=dev.device_type,
        from_version=dev.current_version,
        to_version=dev.available_version,
        window_id=window.id if window else None,
        status="staging",
        triggered_by=triggered_by,
        started_at=_utc(now),
    )
    db.add(u)
    db.commit()
    _notify_pending(db, pa, now, proceeding=True)
    log_event(db, "firmware_upgrade_started", "system", {
        "device": dev.name, "mac": dev.mac_address, "type": dev.device_type,
        "from": dev.current_version, "to": dev.available_version,
        "window": window.name if window else None, "triggered_by": triggered_by,
    })
    return u


def _transition(db, u: FirmwareUpgrade, new_status: str, now_utc,
                deadline_seconds=None) -> None:
    """Record the time spent in the current stage, then move to the next."""
    if u.stage_started_at:
        u.durations = dict(u.durations or {})
        elapsed = max(0, round((now_utc - u.stage_started_at).total_seconds()))
        u.durations[u.status] = u.durations.get(u.status, 0) + elapsed
    u.status = new_status
    u.stage_started_at = now_utc
    u.stage_deadline = (now_utc + datetime.timedelta(seconds=deadline_seconds)
                        if deadline_seconds else None)


def _finish(db, u: FirmwareUpgrade, status: str, now, error=None) -> None:
    now_utc = _utc(now)
    if u.stage_started_at:
        u.durations = dict(u.durations or {})
        elapsed = max(0, round((now_utc - u.stage_started_at).total_seconds()))
        u.durations[u.status] = u.durations.get(u.status, 0) + elapsed
    if u.started_at:
        u.durations = dict(u.durations or {})
        u.durations["total"] = max(0, round((now_utc - u.started_at).total_seconds()))
    u.status = status
    u.finished_at = now_utc
    u.error = error
    db.commit()


def _verify_device(client, mac: str, target_version: str):
    """(ok, err). True only when the device's version bumped AND it is back
    online/informing (state 1)."""
    d = client.get_device(mac)
    if not d:
        return False, "device not present on the controller"
    if (d.get("version") or "") != target_version:
        return False, f"version {d.get('version')!r} != expected {target_version!r}"
    if int(d.get("state") or 0) != 1:
        return False, "device offline / not informing"
    return True, ""


def _set_device_result(db, u: FirmwareUpgrade, result: str, now, error=None) -> None:
    row = db.query(DeviceFirmware).filter(DeviceFirmware.mac_address == u.mac_address).first()
    if not row:
        return
    row.last_result = result
    row.last_error = error
    row.last_upgrade_at = _utc(now)
    if result == "success":
        row.current_version = u.to_version
        row.previous_version = u.from_version
        row.available_version = ""
        row.upgradeable = False
        row.prestaged_version = ""
    elif result == "rolled_back":
        row.prestaged_version = ""
        row.last_error = error or row.last_error
    db.commit()


def _escalate(db, u: FirmwareUpgrade, now, severity: str, title: str,
              detail: str, runbook: str = None) -> None:
    """Create a blocking escalation (halts the run) + a ticket + an email.

    severity: "P1" (physical — upgrade AND rollback failed) or "P2"
    (single failure — halt + alert, device still up)."""
    existing = db.query(PendingAction).filter(
        PendingAction.kind == "escalation",
        PendingAction.mac_address == u.mac_address,
        PendingAction.status == "pending",
    ).first()
    pa = existing
    if pa is None:
        pa = PendingAction(
            kind="escalation",
            title=title,
            detail=detail,
            device_id=u.device_id,
            mac_address=u.mac_address,
            device_name=u.device_name,
            device_type=u.device_type,
            firmware_from=u.from_version,
            firmware_to=u.to_version,
            status="pending",
            required_role="admin",
            extra={"severity": severity, "runbook": runbook or ""},
        )
        db.add(pa)
    else:
        pa.extra = dict(pa.extra or {})
        pa.extra["severity"] = severity
        pa.extra["runbook"] = runbook or pa.extra.get("runbook", "")
    ticket = Ticket(
        ticket_id=generate_ticket_id(),
        title=title,
        description=detail,
        priority=severity,
        status="open",
        source="auto",
        assigned_to="system",
        target_device_id=u.device_id,
    )
    db.add(ticket)
    db.commit()
    try:
        recipients = get_recipients("alerts")
        if recipients:
            rows = [
                ("Device", f"{u.device_name} ({u.device_type})"),
                ("MAC", u.mac_address or "—"),
                ("Firmware", f"{u.from_version} → {u.to_version}"),
                ("Priority", severity),
                ("Detail", detail),
            ]
            send_email(recipients, f"[{severity}] BareNOC: {title}",
                       body_html=alert_html(title, rows),
                       body_text=detail)
    except Exception:
        logger.exception("firmware escalation email failed")
    log_event(db, "firmware_escalation", "system", {
        "device": u.device_name, "mac": u.mac_address,
        "severity": severity, "ticket_id": ticket.ticket_id,
    }, ticket.ticket_id)
    logger.error("Firmware escalation (%s): %s — %s", severity, u.device_name, title)


def _physical_runbook(u: FirmwareUpgrade) -> str:
    return RUNBOOK.format(name=u.device_name, mac=u.mac_address,
                          to_version=u.to_version, from_version=u.from_version)


def _escalate_failure(db, u: FirmwareUpgrade, now, reason: str) -> None:
    """Single failure: halt the run + alert (P2). Device is presumed still up."""
    title = f"Firmware upgrade failed: {u.device_name}"
    detail = (f"{u.device_name} ({u.mac_address}) failed to upgrade from "
              f"{u.from_version} to {u.to_version}: {reason}. The run is halted "
              f"pending review.")
    _escalate(db, u, now, "P2", title, detail)


def _escalate_physical(db, u: FirmwareUpgrade, now) -> None:
    """Double failure (upgrade + rollback both failed): P1 + physical runbook."""
    runbook = _physical_runbook(u)
    title = f"Firmware recovery failed: {u.device_name} needs physical assistance"
    detail = (f"{u.device_name} ({u.mac_address}) did not recover after the "
              f"upgrade to {u.to_version} AND the rollback to {u.from_version} "
              f"both failed. The device may be unreachable.\n\n{runbook}")
    _escalate(db, u, now, "P1", title, detail, runbook=runbook)


def advance_inflight(db, client, u: FirmwareUpgrade, now) -> None:
    """Advance one in-flight upgrade through its state machine. Called every
    tick regardless of the window (a half-finished upgrade must finish or roll
    back — only STARTING new upgrades is window-gated)."""
    now_utc = _utc(now)

    if u.status == "staging":
        if u.stage_started_at is None:
            # Pre-stage the firmware (device downloads first). Skip the cache
            # call when the device already pre-staged this exact version.
            dev = db.query(DeviceFirmware).filter(
                DeviceFirmware.mac_address == u.mac_address).first()
            already_staged = bool(dev and dev.prestaged_version == u.to_version)
            if not already_staged:
                if not client.cache_firmware(u.mac_address):
                    _finish(db, u, "failed", now, "pre-stage (cache) command failed")
                    _set_device_result(db, u, "failed", now, "pre-stage failed")
                    _escalate_failure(db, u, now, "pre-stage (cache) command failed")
                    return
                if dev:
                    dev.prestaged_version = u.to_version
                    db.commit()
            u.stage_started_at = now_utc
            u.stage_deadline = now_utc + datetime.timedelta(seconds=STAGE_STAGING_S)
            db.commit()
            return
        if now_utc < u.stage_deadline:
            db.commit()
            return
        # Apply the firmware.
        if not client.upgrade_device(u.mac_address, u.to_version):
            _finish(db, u, "failed", now, "upgrade command failed")
            _set_device_result(db, u, "failed", now, "upgrade command failed")
            _escalate_failure(db, u, now, "upgrade command failed")
            return
        _transition(db, u, "verifying", now_utc, VERIFY_TIMEOUT_S)
        u.verify_attempts = 0
        db.commit()
        return

    if u.status == "verifying":
        ok, err = _verify_device(client, u.mac_address, u.to_version)
        if ok:
            _finish(db, u, "success", now)
            _set_device_result(db, u, "success", now)
            log_event(db, "firmware_upgrade_success", "system", {
                "device": u.device_name, "mac": u.mac_address,
                "from": u.from_version, "to": u.to_version,
            })
            record(db, event_type="firmware_updated", actor="system",
                   asset=u.device_name,
                   summary=f"Firmware updated on {u.device_name}",
                   detail=f"{u.from_version} → {u.to_version}",
                   links={"mac": u.mac_address, "device_id": u.device_id,
                          "from": u.from_version, "to": u.to_version})
            return
        u.verify_attempts = (u.verify_attempts or 0) + 1
        if now_utc < u.stage_deadline:
            db.commit()
            return  # still waiting for the device to come back
        # Verify timed out → attempt rollback.
        if not u.rollback_attempted:
            u.rollback_attempted = True
            if not client.rollback_device(u.mac_address, u.from_version):
                _finish(db, u, "failed", now, "rollback command failed")
                _set_device_result(db, u, "failed", now, "rollback command failed")
                _escalate_physical(db, u, now)
                return
            _transition(db, u, "rolling_back", now_utc, ROLLBACK_VERIFY_TIMEOUT_S)
            db.commit()
            return
        # Rollback already attempted and the device still hasn't recovered.
        _finish(db, u, "failed", now, err or "device did not recover after rollback")
        _set_device_result(db, u, "failed", now, err or "device did not recover")
        _escalate_physical(db, u, now)
        return

    if u.status == "rolling_back":
        ok, err = _verify_device(client, u.mac_address, u.from_version)
        if ok:
            _finish(db, u, "rolled_back", now)
            _set_device_result(db, u, "rolled_back", now, err or "verify failed, rolled back")
            log_event(db, "firmware_upgrade_rolled_back", "system", {
                "device": u.device_name, "mac": u.mac_address,
                "from": u.from_version, "to": u.to_version,
            })
            return
        if now_utc < u.stage_deadline:
            db.commit()
            return
        _finish(db, u, "failed", now, "rollback did not recover the device: " + (err or ""))
        _set_device_result(db, u, "failed", now, "rollback did not recover")
        _escalate_physical(db, u, now)
        return


def _prestage_due(db, client, now, env=None) -> int:
    """Pre-stage (cache) upgradeable devices when the next window starts within
    PRESTAGE_LEAD_MIN minutes — 'firmware is PRE-STAGED before the window'."""
    nxt = _next_window_start(db, now)
    if nxt is None:
        return 0
    lead = datetime.timedelta(minutes=PRESTAGE_LEAD_MIN)
    if not (now <= nxt <= now + lead):
        return 0
    done = 0
    for dev in db.query(DeviceFirmware).filter(
        DeviceFirmware.upgradeable.is_(True),
        DeviceFirmware.available_version.isnot(None),
        DeviceFirmware.available_version != "",
        DeviceFirmware.current_version != DeviceFirmware.available_version,
    ).all():
        if dev.prestaged_version == dev.available_version:
            continue
        if client.cache_firmware(dev.mac_address):
            dev.prestaged_version = dev.available_version
            done += 1
    db.commit()
    if done:
        logger.info("Firmware pre-staged %d device(s) for the next window", done)
    return done


def engine_tick(db, client, now: datetime.datetime = None, env: dict = None) -> dict:
    """One pass of the firmware engine. Idempotent + restart-safe.

    Order:
      1. advance any in-flight upgrade to a terminal state (window-independent);
      2. if autonomy is off, stop;
      3. if no maintenance window is active, pre-stage if one is imminent, stop;
      4. otherwise pick the next device in risk order and either start it (auto)
         or ensure a blocking approval exists (balanced-gateway / strict).
    """
    now = now or _local_now()
    env = env if env is not None else _read_env()

    inflight = db.query(FirmwareUpgrade).filter(
        FirmwareUpgrade.status.in_(INFLIGHT_STATUSES)).order_by(
        FirmwareUpgrade.id.desc()).first()
    if inflight:
        advance_inflight(db, client, inflight, now)
        return {"status": "advancing", "upgrade_id": inflight.id,
                "stage": inflight.status}

    effective = effective_autonomy(env)
    if effective == "off":
        return {"status": "off"}

    active_window = next(
        (w for w in db.query(MaintenanceWindow).filter(
            MaintenanceWindow.enabled.is_(True)).all()
         if window_active(w, now)), None)
    if active_window is None:
        staged = _prestage_due(db, client, now, env)
        return {"status": "no_window", "prestaged": staged}

    refresh_inventory(db, client)

    candidate = _next_candidate(db)
    if candidate is None:
        return {"status": "nothing_due", "window": active_window.name}

    decision, required_role = approval_decision(effective, candidate.device_type)
    if decision == "disabled":
        return {"status": "disabled"}
    if decision == "approval":
        # An approval this device+firmware already granted → proceed (the row
        # stays as the audit record; don't re-ask every tick).
        approved = db.query(PendingAction).filter(
            PendingAction.kind == "approval",
            PendingAction.mac_address == candidate.mac_address,
            PendingAction.firmware_to == candidate.available_version,
            PendingAction.status == "approved",
        ).first()
        if approved:
            u = _start_upgrade(db, client, candidate, active_window, now,
                               triggered_by="approval")
            return {"status": "started", "device": candidate.mac_address,
                    "upgrade_id": u.id}
        pa = _ensure_approval(db, candidate, required_role, now)
        return {"status": "awaiting_approval", "device": candidate.mac_address,
                "pending_action_id": pa.id}
    u = _start_upgrade(db, client, candidate, active_window, now, triggered_by="auto")
    return {"status": "started", "device": candidate.mac_address,
            "upgrade_id": u.id}


# ── background thread ───────────────────────────────────────────────────────

def _client_from_env():
    """Build a UniFiClient from the shared config (reuse unifi_sync's wiring)."""
    from routes.unifi_sync import _get_unifi_config, _auth_ready, _unifi_client
    cfg = _get_unifi_config()
    if not _auth_ready(cfg):
        return None
    return _unifi_client(cfg)


class FirmwareEngine(threading.Thread):
    def __init__(self, poll_seconds: int = ENGINE_POLL_S):
        super().__init__(daemon=True, name="firmware-engine")
        self.poll_seconds = poll_seconds
        self._client = None

    def run(self):
        logger.info("Firmware engine started")
        while True:
            try:
                # Only talk to the controller when there's a reason: an
                # in-flight upgrade to finish, or at least one enabled
                # maintenance window to act within. Otherwise the thread stays
                # quiet (no UniFi login churn on boxes without firmware set up).
                db = SessionLocal()
                try:
                    inflight = db.query(FirmwareUpgrade).filter(
                        FirmwareUpgrade.status.in_(INFLIGHT_STATUSES)).first()
                    nwindows = db.query(MaintenanceWindow).filter(
                        MaintenanceWindow.enabled.is_(True)).count()
                finally:
                    db.close()
                if inflight is not None or nwindows > 0:
                    client = _client_from_env()
                    if client is not None and client.login():
                        db = SessionLocal()
                        try:
                            engine_tick(db, client)
                        finally:
                            db.close()
                    else:
                        logger.debug("Firmware engine: UniFi unavailable/login failed")
            except Exception:
                logger.exception("Firmware engine cycle error")
            time.sleep(self.poll_seconds)


_engine = None


def start_firmware_engine():
    global _engine
    if _engine is None:
        _engine = FirmwareEngine()
        _engine.start()
    return _engine
