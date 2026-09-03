import os
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from typing import Optional
from datetime import datetime, timedelta
from database import get_db
from models import Device, User, DeviceJob, is_customer
from schemas import DeviceCreate, DeviceUpdate, DeviceResponse, generate_ticket_id
from auth import get_current_user, get_access_context, require_role
from crypto import encrypt, decrypt
from audit import log_event
from change_log import record
from action_validator import (
    effective_channels, suggest_from_fingerprint,
    CHANNELS,
)
import network_scope
import discovery

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

logger = logging.getLogger("devices")

DEFAULT_GROUP = "default"


def _actor_name(user) -> str:
    """Robust actor name (real auth users carry username; test fakes may not)."""
    return (getattr(user, "username", None)
            or getattr(user, "name", None)
            or str(getattr(user, "role", "unknown")))


def _log_credential_access(db, actor: str, device: Device,
                           credential_type: str, action: str,
                           device_id: int = None):
    """Audit the highest-value compliance event on a box that holds network
    credentials: a stored SSH/SNMP secret was decrypted/fetched for use.
    Records actor, device, type, action — NEVER the secret itself."""
    log_event(db, "credential_access", actor, {
        "device_id": device_id if device_id is not None else (device.id if device else None),
        "device_name": device.name if device else None,
        "credential_type": credential_type,
        "action": action,
    })


def _device_channels(device: Device) -> list:
    """The device's effective control channels (derived ∪ explicit).
    device_adoption_model.md §8."""
    agent_connected = (device.adoption_method == "agent"
                       or bool(getattr(device, "agent_version", None)))
    return effective_channels(
        ssh_configured=bool(device.ssh_key_fingerprint),
        snmp_configured=bool(device.snmp_community),
        unifi_managed=bool(device.unifi_managed),
        agent_connected=agent_connected,
        explicit=device.channels,
    )


def device_readiness(device: Device, now: "datetime | None" = None,
                     max_age_minutes: int = 10) -> dict:
    """Post-adoption readiness report for an agent-adopted endpoint.

    A cert-linked device is NOT ready until the NOC_Agent has actually
    reported in (agent_version + host facts) and is still alive (recent
    last_seen + cert_last_seen — the agent polls jobs over mTLS). The
    endpoint installer (agent_install.sh) enrolls a cert; this is the
    appliance-side check that the agent came up and is talking back.
    """
    now = now or datetime.utcnow()
    max_age = timedelta(minutes=int(max_age_minutes))
    checks = {}

    def _chk(key, ok, detail=""):
        checks[key] = {"ok": bool(ok), "detail": str(detail)}

    _chk("adopted", device.adoption_status == "linked",
         device.adoption_status or "none")
    _chk("agent_channel", device.adoption_method == "agent",
         device.adoption_method or "none")
    _chk("agent_reported", bool(device.agent_version), device.agent_version or "")
    _chk("facts_reported", bool(device.facts_json),
         "present" if device.facts_json else "")

    def _age(dt):
        return (now - dt) if dt is not None else None

    ls = _age(device.last_seen)
    cs = _age(device.cert_last_seen)
    _chk("online", device.status == "online", device.status or "")
    _chk("recent_last_seen", ls is not None and ls <= max_age,
         f"{int(ls.total_seconds())}s ago" if ls is not None else "never")
    _chk("recent_cert_seen", cs is not None and cs <= max_age,
         f"{int(cs.total_seconds())}s ago" if cs is not None else "never")

    missing = [k for k, v in checks.items() if not v["ok"]]
    return {"ready": not missing, "device_id": device.id, "name": device.name,
            "max_age_minutes": int(max_age_minutes), "checks": checks,
            "missing": missing}


def _normalize_channels(value) -> list:
    """Validate + canonicalize an explicit channel list (empty -> [])."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out = []
    for c in (value or []):
        if c in CHANNELS and c not in out:
            out.append(c)
    return out


# F8 Windows health/cleanup schedule — per-device, canonical shape.
_WIN_SCHEDULE_HEALTH_MODES = ("off", "daily", "weekly")
_WIN_SCHEDULE_CLEANUP_MODES = ("off", "on_request", "low_usage")


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return lo if n < lo else (hi if n > hi else n)


def _normalize_windows_schedule(value) -> dict:
    """Validate + canonicalize a per-device Windows health/cleanup schedule
    dict. Unknown keys are dropped; bad modes/ints fall back to safe defaults
    (off). Returns the canonical schedule ({} = all off), never None.

    Canonical shape (documented in models.Device.windows_health_schedule):
      health: off|daily|weekly        (health_hour local, health_day 0=Sun..6)
      cleanup: off|on_request|low_usage
      cleanup_window_start/end: local hours for the low-usage window
      last_health_run / last_cleanup_run: scheduler-managed period keys
      (local date for daily/low_usage, ISO week for weekly)
    """
    if not isinstance(value, dict):
        return {}
    out = {}
    health = str(value.get("health") or "off").strip().lower()
    out["health"] = health if health in _WIN_SCHEDULE_HEALTH_MODES else "off"
    cleanup = str(value.get("cleanup") or "off").strip().lower()
    out["cleanup"] = cleanup if cleanup in _WIN_SCHEDULE_CLEANUP_MODES else "off"
    out["health_hour"] = _clamp_int(value.get("health_hour"), 0, 23, 3)
    out["health_day"] = _clamp_int(value.get("health_day"), 0, 6, 0)
    out["cleanup_hour"] = _clamp_int(value.get("cleanup_hour"), 0, 23, 3)
    out["cleanup_window_start"] = _clamp_int(value.get("cleanup_window_start"), 0, 23, 2)
    out["cleanup_window_end"] = _clamp_int(value.get("cleanup_window_end"), 0, 23, 5)
    for k in ("last_health_run", "last_cleanup_run"):
        v = value.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def device_groups() -> list:
    """Valid device groups — mirror Pocket ID groups (env-configurable)."""
    raw = os.getenv("DEVICE_GROUPS", "device-core,device-edge")
    groups = [g.strip() for g in raw.split(",") if g.strip()]
    return [DEFAULT_GROUP] + groups


def _group_ok(ctx: dict, device_group: str) -> bool:
    """Can this user access a device in the given group?
    Admin always; 'default' (ungrouped) open to all; otherwise the user must
    hold the matching Pocket ID group claim."""
    if ctx["user"].role == "admin":
        return True
    g = device_group or DEFAULT_GROUP
    if g == DEFAULT_GROUP:
        return True
    return g in (ctx.get("groups") or [])


def _validate_group(group: Optional[str]) -> str:
    g = (group or DEFAULT_GROUP).strip()
    if g not in device_groups():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown device group '{g}'. Valid: {', '.join(device_groups())}",
        )
    return g


@router.get("/control-key")
def device_control_key(db: Session = Depends(get_db),
                      ctx: dict = Depends(get_access_context)):
    """The appliance's device-control SSH keypair (operator+).

    The public half goes on the device's authorized_keys; the private half is
    what the Credentials modal stores so the runner can SSH in.
    """
    from auth import require_any_role
    require_any_role("technician", "operator", "admin")(ctx["user"])
    from control_key import ensure_control_key
    _log_credential_access(db, _actor_name(ctx["user"]), None, "ssh",
                           "fetch_control_key", device_id=None)
    return ensure_control_key()


@router.post("/snmp-sweep-results")
def snmp_sweep_results(body: dict, db: Session = Depends(get_db),
                       user: User = Depends(require_role("agent"))):
    """Ingest the SNMP discovery sweep (agent callback). Match-before-insert +
    self-exclusion via discovery.upsert_discovered — a repeated sweep UPDATEs
    the same record (by MAC then IP) instead of INSERTing duplicates, and the
    appliance's own identity is never recorded."""
    found = (body or {}).get("found") or []
    added = updated = folded = skipped_self = skipped_claimed = 0
    for hit in found:
        ip = (hit or {}).get("ip") or ""
        if not ip:
            continue
        if network_scope.is_tunnel_or_cgnat(ip):
            continue  # never inventory CGNAT/Tailscale overlay addresses
        name = (hit.get("sysname") or "").strip() or None
        vendor = (hit.get("vendor") or "").strip() or None
        desc = (hit.get("sysdescr") or "").strip()
        outcome, _d = discovery.upsert_discovered(
            db, ip=ip, name=name, vendor=vendor,
            device_type=_guess_snmp_type(desc) if desc else None,
            status="unknown", claimed=False, tags=["snmp-discovered"],
            source="snmp-sweep")
        if outcome == "added":
            added += 1
        elif outcome == "updated":
            updated += 1
        elif outcome == "folded":
            folded += 1
        elif outcome == "skipped_self":
            skipped_self += 1
        else:
            skipped_claimed += 1
    db.commit()
    return {"status": "ok", "added": added, "updated": updated,
            "folded": folded,
            "skipped_self": skipped_self, "skipped_claimed": skipped_claimed,
            "count": len(found)}


@router.post("/discover-results")
def discover_results(body: dict, db: Session = Depends(get_db),
                     user: User = Depends(require_role("agent"))):
    """Ingest ping/discover-sweep finds (runner callback). Match-before-insert +
    self-exclusion via discovery.upsert_discovered — repeated scans of the same
    host yield ONE record (by MAC then IP), and the appliance is never added."""
    found = (body or {}).get("found") or []
    added = updated = folded = skipped_self = skipped_claimed = 0
    for hit in found:
        ip = (hit or {}).get("ip") or ""
        mac = (hit or {}).get("mac") or None
        if not ip and not mac:
            continue
        if ip and network_scope.is_tunnel_or_cgnat(ip):
            continue  # never inventory CGNAT/Tailscale overlay addresses
        name = (hit or {}).get("name") or (f"discovered-{ip.replace('.', '-')}" if ip else None)
        outcome, _d = discovery.upsert_discovered(
            db, mac=mac, ip=ip or None, name=name, hostname=(hit or {}).get("hostname"),
            device_type="unknown", status="online", claimed=False,
            tags=["discovered"], source="ping-sweep")
        if outcome == "added":
            added += 1
        elif outcome == "updated":
            updated += 1
        elif outcome == "folded":
            folded += 1
        elif outcome == "skipped_self":
            skipped_self += 1
        else:
            skipped_claimed += 1
    db.commit()
    return {"status": "ok", "added": added, "updated": updated,
            "folded": folded,
            "skipped_self": skipped_self, "skipped_claimed": skipped_claimed,
            "count": len(found)}


def _guess_snmp_type(desc: str) -> str:
    d = desc.lower()
    if any(k in d for k in ("router", "gateway", "udm", "ucg")):
        return "gateway"
    if any(k in d for k in ("switch", "ubiquiti networks")):
        return "switch"
    if any(k in d for k in ("access point", "ubiquiti", "unifi")):
        return "ap"
    if any(k in d for k in ("printer", "laserjet", "deskjet")):
        return "printer"
    if any(k in d for k in ("nas", "synology", "qnap")):
        return "nas"
    if "linux" in d or "ubuntu" in d or "debian" in d:
        return "server"
    return "unknown"


@router.get("/groups")
def list_groups(ctx: dict = Depends(get_access_context)):
    """Valid device groups (mirrors Pocket ID groups)."""
    return {"groups": device_groups(), "default": DEFAULT_GROUP}


@router.get("")
def list_devices(
    device_type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    claimed: Optional[bool] = None,
    controlled: Optional[bool] = None,
    group: Optional[str] = None,
    seen_within: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    ctx: dict = Depends(get_access_context),
):
    q = db.query(Device)
    if is_customer(ctx["user"]):
        # Tenants see only the devices they own (adopted themselves).
        q = q.filter(Device.owner_id == ctx["user"].id)
    if device_type:
        q = q.filter(Device.device_type == device_type)
    if status:
        q = q.filter(Device.status == status)
    if claimed is not None:
        q = q.filter(Device.claimed == claimed)
    # 'controlled' = BareNOC has admin control and can run actions on the
    # device: via SSH credentials, OR via the UniFi controller for adopted
    # UniFi-managed gear (unifi_managed + claimed), OR via certificate
    # adoption (adoption_status == 'linked' — the device holds a valid
    # short-lived cert from the internal CA and reports over mTLS), OR via the
    # NOC_Agent channel (adoption_method == 'agent'). vendor_api-only devices
    # are folded into the filter when the channels column ships SQL membership
    # (follow-up); they still surface 'channels' in the response.
    _controlled_cond = or_(
        Device.ssh_key_fingerprint.isnot(None),
        and_(Device.unifi_managed.is_(True), Device.claimed.is_(True)),
        and_(Device.adoption_status == "linked", Device.claimed.is_(True)),
        and_(Device.adoption_method == "agent", Device.claimed.is_(True)),
    )
    if controlled is True:
        q = q.filter(_controlled_cond)
    elif controlled is False:
        q = q.filter(and_(
            Device.ssh_key_fingerprint.is_(None),
            or_(Device.unifi_managed.isnot(True), Device.claimed.isnot(True)),
            or_(Device.adoption_status != "linked", Device.claimed.isnot(True)),
        ))
    if group:
        q = q.filter(Device.device_group == group)
    # Group-based access: non-admins only see devices in groups they hold
    # (plus 'default'/ungrouped devices).
    if ctx["user"].role != "admin":
        held = ctx.get("groups") or []
        q = q.filter(or_(Device.device_group.in_([DEFAULT_GROUP, ""]),
                         Device.device_group.in_(held)))
    if seen_within:
        from datetime import datetime, timedelta
        q = q.filter(Device.last_seen >= datetime.utcnow() - timedelta(days=seen_within))
    if search:
        like = f"%{search}%"
        q = q.filter(
            Device.name.ilike(like) |
            Device.ip_address.ilike(like) |
            Device.hostname.ilike(like) |
            Device.vendor.ilike(like)
        )
    total = q.count()
    devices = q.order_by(Device.name).offset(offset).limit(limit).all()
    result_devices = []
    for d in devices:
        r = DeviceResponse.model_validate(d).model_dump()
        r["snmp_configured"] = bool(d.snmp_community)
        r["ssh_configured"] = bool(d.ssh_key_fingerprint)
        r["unifi_managed"] = bool(d.unifi_managed)
        r["channels"] = _device_channels(d)
        result_devices.append(r)
    return {
        "devices": result_devices,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{device_id}")
def get_device(device_id: int, db: Session = Depends(get_db), ctx: dict = Depends(get_access_context)):
    device = _get_checked(db, device_id, ctx)
    resp = DeviceResponse.model_validate(device).model_dump()
    resp["snmp_configured"] = bool(device.snmp_community)
    resp["ssh_configured"] = bool(device.ssh_key_fingerprint)
    resp["channels"] = _device_channels(device)
    return resp


@router.get("/{device_id}/readiness")
def get_device_readiness(device_id: int,
                         max_age_minutes: int = Query(10, ge=1, le=1440),
                         db: Session = Depends(get_db),
                         ctx: dict = Depends(get_access_context)):
    """Appliance-side post-adoption readiness report for a device.

    Verifies a just-adopted NOC_Agent endpoint actually came up: adopted via
    the agent channel, reported agent_version + host facts, and is alive
    (recent last_seen + cert_last_seen). ``max_age_minutes`` tunes the liveness
    window (default 10).
    """
    device = _get_checked(db, device_id, ctx)
    return device_readiness(device, max_age_minutes=max_age_minutes)


def _get_checked(db: Session, device_id: int, ctx: dict) -> Device:
    """Fetch a device and enforce group-based access."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if is_customer(ctx["user"]) and device.owner_id != ctx["user"].id:
        raise HTTPException(status_code=404, detail="Device not found")
    if not _group_ok(ctx, device.device_group):
        raise HTTPException(
            status_code=403,
            detail=f"You don't have access to devices in group "
                   f"'{device.device_group or DEFAULT_GROUP}'",
        )
    return device


@router.post("", response_model=DeviceResponse, status_code=201)
def create_device(
    device_data: DeviceCreate,
    db: Session = Depends(get_db),
    ctx: dict = Depends(get_access_context),
):
    group = _validate_group(device_data.device_group)
    # Non-admins may only add devices into groups they hold
    if not _group_ok(ctx, group):
        raise HTTPException(status_code=403,
                            detail=f"You don't have access to device group '{group}'")
    if network_scope.is_tunnel_or_cgnat(device_data.ip_address):
        raise HTTPException(status_code=400,
                            detail="100.64.0.0/10 is CGNAT/Tailscale space — it can never be a device record.")
    # SELF-EXCLUSION: the appliance itself is never a device record (manual
    # add OR discovery ping-sweep backstop).
    if discovery.is_self_identity(ip=device_data.ip_address,
                                  mac=device_data.mac_address,
                                  name=device_data.name,
                                  hostname=device_data.hostname):
        raise HTTPException(status_code=400,
                            detail="the appliance itself is never a device record (self-exclusion)")
    # Check for duplicate IP
    existing = db.query(Device).filter(Device.ip_address == device_data.ip_address).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Device with IP {device_data.ip_address} already exists")

    device = Device(
        name=device_data.name,
        hostname=device_data.hostname or device_data.name,
        ip_address=device_data.ip_address,
        device_type=device_data.device_type,
        vendor=device_data.vendor,
        model=device_data.model,
        mac_address=device_data.mac_address,
        tags=device_data.tags,
        channels=_normalize_channels(device_data.channels),
        windows_health_schedule=_normalize_windows_schedule(device_data.windows_health_schedule),
        status="pending",
        claimed=device_data.claimed if device_data.claimed is not None else True,
        device_group=group,
        owner_id=ctx["user"].id if is_customer(ctx["user"]) else None,
    )
    db.add(device)
    # Encrypt credentials before storing
    if device_data.snmp_community:
        device.snmp_community = encrypt(device_data.snmp_community)
    if device_data.ssh_key:
        # SSH keys are stored encrypted in the secrets dir
        device.ssh_key_fingerprint = _store_ssh_key(device_data.name, device_data.ssh_key)

    device.last_seen = datetime.utcnow()
    db.commit()
    db.refresh(device)
    resp = DeviceResponse.model_validate(device).model_dump()
    resp["snmp_configured"] = bool(device.snmp_community)
    resp["ssh_configured"] = bool(device.ssh_key_fingerprint)
    resp["channels"] = _device_channels(device)
    return resp


@router.post("/{device_id}/claim")
def claim_device(
    device_id: int,
    config: DeviceCreate,
    db: Session = Depends(get_db),
    ctx: dict = Depends(get_access_context),
):
    """Claim a discovered (unclaimed) device with configuration."""
    device = _get_checked(db, device_id, ctx)
    group = _validate_group(config.device_group)
    if not _group_ok(ctx, group):
        raise HTTPException(status_code=403,
                            detail=f"You don't have access to device group '{group}'")
    if network_scope.is_tunnel_or_cgnat(device.ip_address):
        raise HTTPException(status_code=400,
                            detail="100.64.0.0/10 is CGNAT/Tailscale space — it can never be claimed or adopted.")

    device.name = config.name
    device.hostname = config.hostname or config.name
    device.device_type = config.device_type
    device.vendor = config.vendor
    device.model = config.model
    device.tags = config.tags
    device.claimed = True
    device.status = "pending"
    device.device_group = group
    if config.channels:
        device.channels = _normalize_channels(config.channels)
    if is_customer(ctx["user"]):
        # Tenant adoption: the device belongs to them (their view only).
        device.owner_id = ctx["user"].id
    device.updated_at = datetime.utcnow()

    if config.snmp_community:
        device.snmp_community = encrypt(config.snmp_community)
    if config.ssh_user:
        device.ssh_user = config.ssh_user
    if config.ssh_key:
        # Claim with control: persist the encrypted SSH private key so the
        # agent's SSH actions can actually control the device.
        device.ssh_key_fingerprint = _store_ssh_key(device.name, config.ssh_key)

    db.commit()
    record(db, event_type="device_config_changed", actor=_actor_name(ctx["user"]),
           asset=device.name,
           summary=f"Claimed device {device.name}",
           detail=f"Ownership/control configured (type {device.device_type or 'unknown'})",
           links={"device_id": device.id})

    # Queue connectivity verification
    import json as _json
    job = {
        "ticket_id": f"claim-verify-{device_id}-{datetime.utcnow().strftime('%H%M%S')}",
        "action": "ping_test",
        "target": device.ip_address,
        "params": {},
        "reason": f"Verification after claiming {device.name}",
        "confidence": 1.0,
        "created_at": datetime.utcnow().isoformat(),
        "source": "claim",
        "_callback": {"type": "verify_device", "device_id": device_id},
    }
    jpath = f"/opt/barenoc/jobs/incoming/claim-verify-{device_id}.json"
    with open(jpath, "w") as f:
        _json.dump(job, f, indent=2)

    resp = DeviceResponse.model_validate(device).model_dump()
    resp["snmp_configured"] = bool(device.snmp_community)
    resp["ssh_configured"] = bool(device.ssh_key_fingerprint)
    resp["channels"] = _device_channels(device)
    return resp


@router.post("/{device_id}/verify")
def verify_device(device_id: int, db: Session = Depends(get_db), ctx: dict = Depends(get_access_context)):
    """Queue a ping job for the Pi Agent to verify connectivity."""
    device = _get_checked(db, device_id, ctx)

    # Write a job file for the Pi Agent
    import json
    job = {
        "ticket_id": f"verify-{device_id}-{datetime.utcnow().strftime('%H%M%S')}",
        "action": "ping_test",
        "target": device.ip_address,
        "params": {},
        "reason": f"Connectivity verification for {device.name}",
        "confidence": 1.0,
        "created_at": datetime.utcnow().isoformat(),
        "source": "api-verify",
        "_callback": {"type": "verify_device", "device_id": device_id},
    }
    job_path = f"/opt/barenoc/jobs/incoming/verify-{device_id}.json"
    with open(job_path, "w") as f:
        json.dump(job, f, indent=2)

    device.status = "pending"
    db.commit()
    return {"device_id": device_id, "status": "pending", "message": "Verification job queued"}


# ── NOC_Agent update actions (Part B) ──────────────────────────────────────
# "Check for updates" + "Apply updates" on the Devices page for agent-managed
# devices. Enqueues a device_jobs row; the NOC_Agent pulls + executes it on its
# next poll (routes/device_agent transport), and the result reports back to
# jobs/result. apply_updates is confirm-gated (body.confirm must be true).
_AGENT_UPDATE_ACTIONS = {"check_updates", "apply_updates"}


def _is_agent_managed(device: Device) -> bool:
    """The NOC_Agent channel is present (the update capability lives there)."""
    return bool(device.adoption_method == "agent" or device.agent_version)


def _agent_job_brief(job: DeviceJob) -> dict:
    return {
        "id": str(job.id),
        "action": job.action,
        "params": job.params,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "result": job.result_json,
    }


@router.get("/{device_id}/agent-jobs")
def list_agent_jobs(device_id: int, db: Session = Depends(get_db),
                    ctx: dict = Depends(get_access_context)):
    """Recent NOC_Agent jobs + results for a device (Devices-page result
    reporting: the check list, the apply outcome, reboot-required hint)."""
    device = _get_checked(db, device_id, ctx)
    jobs = (db.query(DeviceJob)
            .filter(DeviceJob.device_id == device.id)
            .order_by(DeviceJob.id.desc())
            .limit(5).all())
    return {"device_id": device.id, "agent_managed": _is_agent_managed(device),
            "jobs": [_agent_job_brief(j) for j in jobs]}


@router.post("/{device_id}/agent-job")
def enqueue_agent_update_job(device_id: int, body: dict,
                             db: Session = Depends(get_db),
                             ctx: dict = Depends(get_access_context)):
    """Enqueue check_updates or apply_updates for an AGENT-managed device.

    apply_updates writes to the endpoint OS: the Devices page prompts the
    operator first and sends confirm=true (the SAME gate as reboot).
    """
    device = _get_checked(db, device_id, ctx)
    action = str((body or {}).get("action") or "").strip()
    if action not in _AGENT_UPDATE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {sorted(_AGENT_UPDATE_ACTIONS)}")
    if not _is_agent_managed(device):
        raise HTTPException(
            status_code=400,
            detail="updates require an agent-managed device (NOC_Agent channel)")
    params = {}
    if action == "apply_updates":
        if not (body or {}).get("confirm"):
            raise HTTPException(status_code=400,
                                detail="apply_updates requires confirm=true")
        params["confirm"] = True

    from routes.device_agent import enqueue_job
    job = enqueue_job(db, device.id, action, params, ttl_seconds=1800)
    log_event(db, "job_created", _actor_name(ctx["user"]), {
        "device_id": device.id,
        "device": device.name,
        "action": action,
        "job_id": str(job.id),
        "via": "devices-page",
    })
    return {"device_id": device.id, "job": _agent_job_brief(job)}


@router.post("/discover")
def discover_devices(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Queue ping sweep jobs for the Pi Agent."""
    import json, threading

    def _run_discovery():
        """Run discovery in background thread."""
        import os, ipaddress
        from database import SessionLocal
        from llm_providers import read_env_file
        env = read_env_file()
        s = SessionLocal()
        try:
            existing_ips = set(row[0] for row in s.query(Device.ip_address).all())
        finally:
            s.close()
        # Self-exclusion: never even ping the appliance's own IPs.
        excl = discovery.self_exclusion(env)
        # Multi-VLAN discovery: DISCOVERY_SUBNETS is a comma list of CIDRs
        # (e.g. 10.0.4.0/24,10.0.8.0/24). Legacy DISCOVERY_SUBNET
        # (a bare 3-octet prefix) still works. Default: derive the LAN from
        # APPLIANCE_IP (installer + Settings always know it) — the old
        # 192.168.0.0/24 hard default made "Scan Network" scan the wrong
        # network on fresh installs (nothing was ever found).
        raw = env.get("DISCOVERY_SUBNETS") or env.get("DISCOVERY_SUBNET")
        if not raw:
            aip = (env.get("APPLIANCE_IP") or "").strip()
            raw = ".".join(aip.split(".")[:3]) + ".0/24" if aip.count(".") == 3 else "192.168.0.0/24"
        subnets = [s.strip() for s in raw.split(",") if s.strip()]
        # Normalize legacy bare 3-octet prefixes to /24 so the ping loop AND
        # the SNMP sweep target both see valid CIDRs.
        normalized = [(s + "/24") if ("/" not in s and s.count(".") == 3) else s
                      for s in subnets]
        max_per_subnet = 50
        try:
            max_per_subnet = max(10, min(int(env.get("DISCOVERY_MAX_HOSTS_PER_SUBNET") or "50"), 254))
        except (TypeError, ValueError):
            max_per_subnet = 50
        discovered = 0
        for subnet in normalized:
            try:
                net = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                logger.warning(f"Discovery: skipping bad subnet {subnet}")
                continue
            if net.prefixlen < 24:
                logger.warning(f"Discovery: {subnet} is wider than /24 — skipping (safety)")
                continue
            count = 0
            for ip in net.hosts():
                ip = str(ip)
                if network_scope.is_tunnel_or_cgnat(ip):
                    continue  # CGNAT/Tailscale overlay — never a scan target
                if discovery.is_self_ip(ip, excl):
                    continue  # appliance's own IP — never a scan target
                if ip in existing_ips or count >= max_per_subnet:
                    continue
                job = {
                    "ticket_id": f"disc-{ip.replace('.', '-')}-{datetime.utcnow().strftime('%M%S')}",
                    "action": "ping_test",
                    "target": ip,
                    "params": {},
                    "reason": "Discovery scan",
                    "confidence": 1.0,
                    "created_at": datetime.utcnow().isoformat(),
                    "source": "discovery",
                    "_callback": {"type": "discover_add", "ip": ip},
                }
                jpath = f"/opt/barenoc/jobs/incoming/discover-{ip.replace('.', '-')}.json"
                try:
                    with open(jpath, "w") as f:
                        json.dump(job, f, indent=2)
                    discovered += 1
                    count += 1
                except Exception:
                    pass
        # SNMP sweep pass: probe the scanned subnets for SNMP gear (routers,
        # switches, APs, printers identify themselves). Runs as an agent job.
        # CGNAT/Tailscale ranges are dropped from the sweep target entirely.
        sweep_subnets = [n for n in normalized
                         if not network_scope.subnet_overlaps_tunnel(n)]
        sweep = {
            "ticket_id": f"snmp-sweep-{datetime.utcnow().strftime('%M%S')}",
            "action": "snmp_sweep",
            "target": ",".join(sweep_subnets),
            "params": {"community": env.get("DISCOVERY_SNMP_COMMUNITY", "public")},
            "reason": "SNMP discovery sweep",
            "confidence": 1.0,
            "created_at": datetime.utcnow().isoformat(),
            "source": "discovery",
            "_callback": {"type": "snmp_store"},
        }
        try:
            with open("/opt/barenoc/jobs/incoming/snmp-sweep.json", "w") as f:
                json.dump(sweep, f, indent=2)
        except Exception:
            pass
        logger.info(f"Discovery queued {discovered} ping jobs + 1 SNMP sweep")

    threading.Thread(target=_run_discovery, daemon=True).start()
    return {"status": "discovery_started", "message": "Scanning network in background. Refresh devices in a minute."}


def _ssh_key_path(name: str) -> str:
    """Where a device's encrypted SSH private key lives (by device name)."""
    safe = name.replace(' ', '_')
    return f"/opt/barenoc/volumes/secrets/ssh/{safe}.key"


def _store_ssh_key(name: str, key_content: str) -> str:
    """Encrypt + persist a device's SSH private key and return the fingerprint.
    No-op when the key is empty."""
    key_path = _ssh_key_path(name)
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    # ssh-keygen rejects keys without the trailing newline (OpenSSL 3.0) —
    # normalize here so stored keys are always loadable.
    if not key_content.endswith("\n"):
        key_content += "\n"
    with open(key_path, "w") as f:
        f.write(encrypt(key_content))
    return f"encrypted:{name}"


def _apply_fingerprint_suggestion(device: Device, fp: dict) -> None:
    """Suggest type + channels from a fingerprint result and persist the
    recommendation for the UI (device_adoption_model.md §2/§4). Advisory only:
    fills device_type when unknown and stores the ranked recommendation under
    fingerprint.suggestion — it never removes channels or creds."""
    if not fp or fp.get("error"):
        return
    sug = suggest_from_fingerprint(fp)
    if (not device.device_type or device.device_type == "unknown") and sug.get("device_type"):
        device.device_type = sug["device_type"]
    device.fingerprint = dict(fp)
    device.fingerprint["suggestion"] = sug


def _queue_fingerprint_job(db: Session, device: Device) -> bool:
    """Write a fingerprint job file for the Pi Agent. Returns True if queued."""
    import json
    if not device.ip_address:
        return False
    ts = datetime.utcnow().strftime("%M%S")
    job = {
        "ticket_id": f"fp-{device.id}-{ts}",
        "action": "fingerprint_device",
        "target": device.ip_address,
        "params": {},
        "reason": "Fingerprint unclaimed device",
        "confidence": 1.0,
        "created_at": datetime.utcnow().isoformat(),
        "source": "fingerprint",
        "_callback": {"type": "fingerprint_store", "device_id": device.id},
    }
    jpath = f"/opt/barenoc/jobs/incoming/fp-{device.id}-{ts}.json"
    try:
        with open(jpath, "w") as f:
            json.dump(job, f, indent=2)
        return True
    except Exception:
        return False


@router.post("/{device_id}/fingerprint")
def fingerprint_device(device_id: int, db: Session = Depends(get_db),
                       ctx: dict = Depends(get_access_context)):
    """Queue an nmap fingerprint job for one device (admin/operator)."""
    device = _get_checked(db, device_id, ctx)
    if _queue_fingerprint_job(db, device):
        return {"status": "queued", "message": f"Fingerprinting {device.ip_address} — refresh in ~1 min"}
    return {"status": "error", "message": "Could not queue fingerprint job"}


@router.post("/fingerprint/unclaimed")
def fingerprint_unclaimed(db: Session = Depends(get_db),
                          ctx: dict = Depends(get_access_context)):
    """Queue nmap fingerprint jobs for every unclaimed device."""
    import threading

    def _run():
        # Use a fresh session — the request-scoped one is closed by FastAPI
        # once the response is sent, so it must not be touched in a thread.
        from database import SessionLocal
        s = SessionLocal()
        try:
            devices = s.query(Device).filter(Device.claimed == False).all()  # noqa: E712
            queued = 0
            for d in devices:
                if _queue_fingerprint_job(s, d):
                    queued += 1
            logger.info(f"Fingerprint: queued {queued} jobs for {len(devices)} unclaimed devices")
        finally:
            s.close()

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "message": "Fingerprinting unclaimed devices in background"}


@router.patch("/{device_id}", response_model=DeviceResponse)
def update_device(
    device_id: int,
    update: DeviceUpdate,
    db: Session = Depends(get_db),
    ctx: dict = Depends(get_access_context),
):
    device = _get_checked(db, device_id, ctx)
    data = update.model_dump(exclude_unset=True)
    if "device_group" in data:
        data["device_group"] = _validate_group(data["device_group"])
        if not _group_ok(ctx, data["device_group"]):
            raise HTTPException(status_code=403,
                                detail=f"You don't have access to device group '{data['device_group']}'")

    for field, value in data.items():
        if field == "snmp_community":
            device.snmp_community = encrypt(value) if value else None
        elif field == "ssh_user":
            device.ssh_user = value or None
        elif field == "ssh_key" and value:
            device.ssh_key_fingerprint = _store_ssh_key(device.name, value)
        elif field == "channels":
            device.channels = _normalize_channels(value)
        elif field == "windows_health_schedule":
            device.windows_health_schedule = _normalize_windows_schedule(value)
        elif field == "fingerprint" and isinstance(value, dict):
            _apply_fingerprint_suggestion(device, value)
        else:
            setattr(device, field, value)

    device.updated_at = datetime.utcnow()
    db.commit()
    record(db, event_type="device_config_changed", actor=_actor_name(ctx["user"]),
           asset=device.name,
           summary=f"Updated device {device.name}",
           detail=f"Changed fields: {', '.join(sorted(data.keys()))}",
           links={"device_id": device.id})
    db.refresh(device)
    resp = DeviceResponse.model_validate(device).model_dump()
    resp["snmp_configured"] = bool(device.snmp_community)
    resp["ssh_configured"] = bool(device.ssh_key_fingerprint)
    resp["channels"] = _device_channels(device)
    return resp


@router.put("/{device_id}/windows-schedule")
def set_windows_schedule(device_id: int, body: dict, db: Session = Depends(get_db),
                         ctx: dict = Depends(get_access_context)):
    """Set/clear the per-device Windows health/cleanup schedule (F8).

    Body is the schedule dict (same shape as Device.windows_health_schedule):
      {"health": "off"|"daily"|"weekly", "health_hour": 3, "health_day": 0,
       "cleanup": "off"|"on_request"|"low_usage", "cleanup_hour": 3,
       "cleanup_window_start": 2, "cleanup_window_end": 5}
    Returns the normalized schedule actually stored (never a passthrough of
    arbitrary keys)."""
    device = _get_checked(db, device_id, ctx)
    sched = _normalize_windows_schedule(body or {})
    device.windows_health_schedule = sched
    device.updated_at = datetime.utcnow()
    db.commit()
    record(db, event_type="device_config_changed", actor=_actor_name(ctx["user"]),
           asset=device.name,
           summary=f"Updated Windows health schedule for {device.name}",
           detail=f"windows_health_schedule={json.dumps(sched)}",
           links={"device_id": device.id})
    return {"device_id": device.id, "windows_health_schedule": sched}


@router.post("/{device_id}/windows-health-ticket")
def create_windows_health_ticket(device_id: int, body: dict,
                                 db: Session = Depends(get_db),
                                 ctx: dict = Depends(get_access_context)):
    """Create a source=auto ticket for a scheduled Windows health/cleanup run
    (F8) and return its ticket_id so the scheduler can enqueue the job file.

    The device owner is the submitter, so the report lands in their ticket
    list; source=auto keeps the worker from re-interpreting the ticket.
    Body: {"kind": "health"|"cleanup"} (default health)."""
    from models import Ticket
    from auth import require_any_role
    require_any_role("technician", "operator", "admin", "agent")(ctx["user"])
    device = _get_checked(db, device_id, ctx)
    kind = str(body.get("kind") or "health").strip().lower()
    if kind not in ("health", "cleanup"):
        raise HTTPException(status_code=400, detail="kind must be 'health' or 'cleanup'")
    label = "health check" if kind == "health" else "cleanup"
    ticket = Ticket(
        ticket_id=generate_ticket_id(),
        title=f"Windows {label}: {device.name}",
        description=(f"Scheduled Windows {label} for {device.name} "
                     f"({device.ip_address}). The report is posted here "
                     f"automatically when the run finishes."),
        priority="P4",
        status="open",
        source="auto",
        submitter_id=device.owner_id,
        target_device_id=device.id,
        assigned_to="system",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    log_event(db, "ticket_created", _actor_name(ctx["user"]), {
        "ticket_id": ticket.ticket_id, "device_id": device.id,
        "source": "windows-health-schedule", "kind": kind,
    }, ticket.ticket_id)
    return {"ticket_id": ticket.ticket_id, "device_id": device.id}


@router.patch("/{device_id}/windows-schedule/mark-run")
def mark_windows_schedule_run(device_id: int, body: dict,
                              db: Session = Depends(get_db),
                              ctx: dict = Depends(get_access_context)):
    """Scheduler-internal: record a health/cleanup run period key (no
    change-log noise). Body: {"health": "<period key>"} and/or
    {"cleanup": "<period key>"}. The period key is a local date
    (daily/low_usage) or ISO week (weekly)."""
    from auth import require_any_role
    require_any_role("technician", "operator", "admin", "agent")(ctx["user"])
    device = _get_checked(db, device_id, ctx)
    sched = dict(device.windows_health_schedule or {})
    if isinstance(body.get("health"), str) and body["health"].strip():
        sched["last_health_run"] = body["health"].strip()
    if isinstance(body.get("cleanup"), str) and body["cleanup"].strip():
        sched["last_cleanup_run"] = body["cleanup"].strip()
    device.windows_health_schedule = _normalize_windows_schedule(sched)
    device.updated_at = datetime.utcnow()
    db.commit()
    return {"device_id": device.id, "windows_health_schedule": device.windows_health_schedule}


@router.delete("/{device_id}", status_code=204)
def delete_device(device_id: int, db: Session = Depends(get_db), ctx: dict = Depends(get_access_context)):
    device = _get_checked(db, device_id, ctx)
    # Detach tickets that reference this device (ON DELETE SET NULL behavior)
    from models import Ticket
    db.query(Ticket).filter(Ticket.target_device_id == device_id).update(
        {Ticket.target_device_id: None}
    )
    # Service checks: a monitor targeting this device is disabled (its target
    # can no longer resolve) instead of leaving a dangling FK that breaks the
    # DELETE below. The service-checks engine also has a self-heal for any
    # dangling reference created out-of-band.
    from models import ServiceMonitor
    db.query(ServiceMonitor).filter(
        ServiceMonitor.target_device_id == device_id).update({
            ServiceMonitor.target_device_id: None,
            ServiceMonitor.enabled: False,
            ServiceMonitor.last_error: "target device was deleted — monitor disabled",
        })
    db.delete(device)
    db.commit()
    return None


def _adoption_brief(device: Device) -> dict:
    return {
        "status": device.adoption_status or "none",
        "method": device.adoption_method or "none",
        "cert_cn": device.cert_cn,
        "cert_serial": device.cert_serial,
        "cert_enrolled_at": device.cert_enrolled_at.isoformat() if device.cert_enrolled_at else None,
        "cert_last_seen": device.cert_last_seen.isoformat() if device.cert_last_seen else None,
    }


@router.get("/{device_id}/adoption")
def device_adoption_status(device_id: int, db: Session = Depends(get_db),
                           ctx: dict = Depends(get_access_context)):
    """Adoption status for a device (none/enrolling/linked/revoked + method)."""
    from auth import require_any_role
    require_any_role("technician", "operator", "admin", "agent")(ctx["user"])
    device = _get_checked(db, device_id, ctx)
    return _adoption_brief(device)


@router.post("/{device_id}/adopt/cert")
def adopt_with_cert(device_id: int, body: dict = None, db: Session = Depends(get_db),
                    ctx: dict = Depends(get_access_context)):
    """Adopt a device with a certificate (step-ca).

    Mints a one-time enrollment token; the device uses it with step-cli to get
    its short-lived cert, then its first /api/v1/device/report call links it
    (adoption completes). Body: {"ttl": 600 (seconds, optional)}.
    """
    from auth import require_any_role
    if not is_customer(ctx["user"]):
        require_any_role("technician", "operator", "admin", "agent")(ctx["user"])
    # Tenants adopt their own devices only — _get_checked enforces ownership.
    from audit import log_event
    device = _get_checked(db, device_id, ctx)
    if device.adoption_status == "revoked":
        raise HTTPException(status_code=400, detail="device adoption is revoked")
    from step_ca import device_cn, mint_token, root_fingerprint
    cn = device_cn(device.name)
    ttl = int((body or {}).get("ttl") or 600)
    ttl = max(60, min(ttl, 3600))
    token = mint_token(cn, ttl=ttl)
    device.adoption_status = "enrolling"
    device.adoption_method = "cert"
    device.cert_cn = cn
    db.commit()
    log_event(db, "device_adopt_start", ctx["user"].username, {
        "device_id": device.id, "device": device.name, "cn": cn, "ttl": ttl})
    record(db, event_type="device_adopted", actor=_actor_name(ctx["user"]),
           asset=device.name,
           summary=f"Certificate adoption started for {device.name}",
           detail=f"Cert CN {cn} (enrollment token minted, ttl {ttl}s)",
           links={"device_id": device.id, "cn": cn})
    return {
        "status": "enrolling",
        "cn": cn,
        "token": token,
        "ttl": ttl,
        "ca_url": "https://stepca.barenoc.local:8443",
        "ca_fingerprint": root_fingerprint(),
        "note": "On the device: step ca bootstrap --ca-url <ca_url> --fingerprint <fp>; step ca certificate <cn> cert.crt cert.key --token <token>; then POST https://<appliance>/api/v1/device/report with the client cert.",
    }


@router.post("/{device_id}/adopt/revoke")
def revoke_adoption(device_id: int, db: Session = Depends(get_db),
                    ctx: dict = Depends(get_access_context)):
    """Revoke adoption: the device is de-trusted immediately at the API layer
    (its report calls 403) and its short-TTL cert expires shortly after."""
    from auth import require_any_role
    if not is_customer(ctx["user"]):
        require_any_role("technician", "operator", "admin", "agent")(ctx["user"])
    from audit import log_event
    device = _get_checked(db, device_id, ctx)
    device.adoption_status = "revoked"
    db.commit()
    log_event(db, "device_adopt_revoke", ctx["user"].username, {
        "device_id": device.id, "device": device.name, "cn": device.cert_cn})
    record(db, event_type="device_revoked", actor=_actor_name(ctx["user"]),
           asset=device.name,
           summary=f"Revoked adoption for {device.name}",
           detail=f"Cert CN {device.cert_cn or 'n/a'}",
           links={"device_id": device.id, "cn": device.cert_cn})
    return _adoption_brief(device)


def _repoint_device_refs(db: Session, from_id: int, to_id: int) -> None:
    """Re-point every FK that referenced the duplicate onto the survivor, so
    the FK-enforced DELETE below can proceed (PRAGMA foreign_keys=ON)."""
    from models import (Ticket, DeviceJob, ServiceMonitor, DeviceFirmware,
                        FirmwareUpgrade, PendingAction, Metric, Finding,
                        LinkEpisode, StarlinkEpisode)
    db.query(Ticket).filter(Ticket.target_device_id == from_id).update(
        {Ticket.target_device_id: to_id}, synchronize_session=False)
    db.query(DeviceJob).filter(DeviceJob.device_id == from_id).update(
        {DeviceJob.device_id: to_id}, synchronize_session=False)
    db.query(ServiceMonitor).filter(ServiceMonitor.target_device_id == from_id).update(
        {ServiceMonitor.target_device_id: to_id}, synchronize_session=False)
    db.query(DeviceFirmware).filter(DeviceFirmware.device_id == from_id).update(
        {DeviceFirmware.device_id: to_id}, synchronize_session=False)
    db.query(FirmwareUpgrade).filter(FirmwareUpgrade.device_id == from_id).update(
        {FirmwareUpgrade.device_id: to_id}, synchronize_session=False)
    db.query(PendingAction).filter(PendingAction.device_id == from_id).update(
        {PendingAction.device_id: to_id}, synchronize_session=False)
    db.query(Metric).filter(Metric.device_id == from_id).update(
        {Metric.device_id: to_id}, synchronize_session=False)
    db.query(Finding).filter(Finding.device_id == from_id).update(
        {Finding.device_id: to_id}, synchronize_session=False)
    # Unique-constrained episode rows: drop the duplicate-side row when the
    # survivor already owns that key (one physical box → one episode row).
    for ep in db.query(LinkEpisode).filter(LinkEpisode.device_id == from_id).all():
        clash = db.query(LinkEpisode).filter(
            LinkEpisode.device_id == to_id,
            LinkEpisode.interface == ep.interface).first()
        if clash is not None:
            db.delete(ep)
        else:
            ep.device_id = to_id
    for ep in db.query(StarlinkEpisode).filter(StarlinkEpisode.device_id == from_id).all():
        clash = db.query(StarlinkEpisode).filter(
            StarlinkEpisode.device_id == to_id).first()
        if clash is not None:
            db.delete(ep)
        else:
            ep.device_id = to_id


def merge_duplicates(db: Session, keep_id: int, duplicate_id: int,
                     actor: str, reason: str = "device-dedupe merge") -> dict:
    """Merge two records for the same physical box (device-dedupe).

    `keep_id` is the DISCOVERY record (survives — its name/MAC/device_type are
    preserved); `duplicate_id` is the duplicate whose adoption identity is
    transferred onto the survivor and then deleted. Audited as
    `device_merged` (from_id, into_id). This is the admin cleanup shape for
    pre-existing dupes (e.g. Plex id 12 + id 46); the report path now prevents
    them (adopt-in-place).
    """
    keep = db.query(Device).filter(Device.id == keep_id).first()
    dup = db.query(Device).filter(Device.id == duplicate_id).first()
    if keep is None:
        raise HTTPException(status_code=404, detail=f"Device {keep_id} not found")
    if dup is None:
        raise HTTPException(status_code=404, detail=f"Device {duplicate_id} not found")
    if keep.id == dup.id:
        raise HTTPException(status_code=400, detail="Cannot merge a device with itself")
    if keep.adoption_status == "revoked":
        raise HTTPException(status_code=400, detail="Refusing to merge into a revoked record")
    # Never overwrite a record already linked to a DIFFERENT cert identity.
    if (keep.adoption_status == "linked" and keep.cert_cn and dup.cert_cn
            and keep.cert_cn != dup.cert_cn):
        raise HTTPException(
            status_code=409,
            detail="Target device is already linked to a different certificate identity")

    # Adopt the survivor with the duplicate's identity (the reverse of the
    # report-path fix: here we fold a pre-existing duplicate back together).
    for field in ("cert_cn", "cert_serial", "cert_enrolled_at", "cert_last_seen",
                  "adoption_status", "adoption_method", "agent_version",
                  "agent_capabilities", "facts_json"):
        value = getattr(dup, field)
        if value not in (None, ""):
            setattr(keep, field, value)
    keep.claimed = bool(keep.claimed or dup.claimed)
    if dup.status and dup.status not in ("unknown", "unclaimed"):
        keep.status = dup.status
    if not keep.hostname and dup.hostname:
        keep.hostname = dup.hostname
    if (not keep.ip_address or keep.ip_address == "0.0.0.0") and dup.ip_address:
        keep.ip_address = dup.ip_address
    if not keep.owner_id and dup.owner_id:
        keep.owner_id = dup.owner_id
    # Preserve any control channel only the duplicate had (the discovery record
    # stays canonical for MAC/device_type/name).
    for field in ("ssh_user", "ssh_key_fingerprint", "snmp_community",
                  "channels", "vendor", "model"):
        if getattr(keep, field) in (None, "", []) \
                and getattr(dup, field) not in (None, "", []):
            setattr(keep, field, getattr(dup, field))
    keep.updated_at = datetime.utcnow()

    _repoint_device_refs(db, dup.id, keep.id)
    db.delete(dup)
    log_event(db, "device_merged", actor, {
        "from_id": duplicate_id, "into_id": keep_id, "reason": reason})
    return {
        "ok": True,
        "merged": {"from_id": duplicate_id, "into_id": keep_id},
        "device": {"id": keep.id, "name": keep.name, "cert_cn": keep.cert_cn,
                   "adoption_status": keep.adoption_status,
                   "adoption_method": keep.adoption_method,
                   "claimed": keep.claimed},
    }


@router.post("/merge-duplicates")
def merge_duplicates_route(body: dict = None, db: Session = Depends(get_db),
                           ctx: dict = Depends(get_access_context)):
    """Admin cleanup for pre-existing same-box duplicates (device-dedupe).

    Body: {"keep_id": <discovery record>, "duplicate_id": <dupe to fold in>}
    (aliases into_id/from_id accepted). Adopts the discovery record with the
    duplicate's identity and deletes the duplicate; audited `device_merged`.
    """
    from auth import require_any_role
    require_any_role("admin")(ctx["user"])
    body = body or {}

    def _int(*keys):
        for k in keys:
            v = body.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"Invalid {k}")
        return None

    keep_id = _int("keep_id", "into_id")
    duplicate_id = _int("duplicate_id", "from_id")
    if keep_id is None or duplicate_id is None:
        raise HTTPException(status_code=400,
                            detail="keep_id and duplicate_id are required")
    return merge_duplicates(db, keep_id, duplicate_id, _actor_name(ctx["user"]),
                            reason=body.get("reason") or "device-dedupe merge")


@router.get("/{device_id}/credentials")
def get_device_credentials(device_id: int, db: Session = Depends(get_db), ctx: dict = Depends(get_access_context)):
    """Return decrypted credentials for agent use. Admin or agent identity only."""
    from auth import require_any_role
    require_any_role("admin", "agent")(ctx["user"])
    device = _get_checked(db, device_id, ctx)
    # SELF-PROTECTION: never hand the agent credentials for the appliance
    # itself — the AI must not be able to SSH into its own host (scoped sudo
    # includes shutdown/reboot). Matched against APPLIANCE_IP + the .local aliases.
    import routes.settings as _s
    env = _s._read_env_file()
    self_ips = [x.strip() for x in str(env.get("APPLIANCE_IP", "")).split(",") if x.strip()]
    hay = " ".join([str(device.name or ""), str(device.ip_address or ""),
                     str(device.hostname or "")]).lower()
    if any(s in hay for s in self_ips) or any(
            s in hay for s in ("127.0.0.1", "localhost", "bareNOC.local", "app.barenoc.com", "bareNOC")):
        raise HTTPException(status_code=403,
                            detail="The appliance itself is never a management target (self-protection).")
    result = {}
    actor = _actor_name(ctx["user"])
    if device.snmp_community:
        result["snmp_community"] = decrypt(device.snmp_community)
        _log_credential_access(db, actor, device, "snmp", "decrypt")
    if device.ssh_user:
        result["ssh_user"] = device.ssh_user
    if device.ssh_key_fingerprint:
        key_file = _ssh_key_path(device.name)
        if os.path.exists(key_file):
            with open(key_file) as f:
                result["ssh_key"] = decrypt(f.read())
            _log_credential_access(db, actor, device, "ssh", "decrypt")
    return result
