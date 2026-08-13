import os
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from typing import Optional
from datetime import datetime
from database import get_db
from models import Device, User
from schemas import DeviceCreate, DeviceUpdate, DeviceResponse
from auth import get_current_user, get_access_context, require_role
from crypto import encrypt, decrypt

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

logger = logging.getLogger("devices")

DEFAULT_GROUP = "default"


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
def device_control_key(ctx: dict = Depends(get_access_context)):
    """The appliance's device-control SSH keypair (operator+).

    The public half goes on the device's authorized_keys; the private half is
    what the Credentials modal stores so the runner can SSH in.
    """
    from auth import require_any_role
    require_any_role("operator", "admin")(ctx["user"])
    from control_key import ensure_control_key
    return ensure_control_key()


@router.post("/snmp-sweep-results")
def snmp_sweep_results(body: dict, db: Session = Depends(get_db),
                       user: User = Depends(require_role("agent"))):
    """Ingest the SNMP discovery sweep (agent callback). Creates/updates
    unclaimed device records with the identity SNMP gear announces."""
    found = (body or {}).get("found") or []
    added = updated = 0
    for hit in found:
        ip = (hit or {}).get("ip") or ""
        if not ip:
            continue
        name = (hit.get("sysname") or "").strip() or None
        vendor = (hit.get("vendor") or "").strip() or None
        desc = (hit.get("sysdescr") or "").strip()
        d = db.query(Device).filter(Device.ip_address == ip).first()
        if d:
            d.vendor = d.vendor or vendor
            if name and (not d.name or d.name.startswith("discovered-") or d.name == "unknown"):
                d.name = name
            if d.device_type == "unknown" and desc:
                d.device_type = _guess_snmp_type(desc)
            updated += 1
        else:
            d = Device(name=name or f"discovered-{ip.replace('.', '-')}",
                       ip_address=ip, device_type=_guess_snmp_type(desc),
                       vendor=vendor, status="unknown", claimed=False,
                       tags=["snmp-discovered"])
            db.add(d)
            added += 1
    db.commit()
    return {"status": "ok", "added": added, "updated": updated, "count": len(found)}


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
    if ctx["user"].role == "tenant":
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
    # short-lived cert from the internal CA and reports over mTLS).
    _controlled_cond = or_(
        Device.ssh_key_fingerprint.isnot(None),
        and_(Device.unifi_managed.is_(True), Device.claimed.is_(True)),
        and_(Device.adoption_status == "linked", Device.claimed.is_(True)),
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
    return resp


def _get_checked(db: Session, device_id: int, ctx: dict) -> Device:
    """Fetch a device and enforce group-based access."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if ctx["user"].role == "tenant" and device.owner_id != ctx["user"].id:
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
        status="pending",
        claimed=device_data.claimed if device_data.claimed is not None else True,
        device_group=group,
        owner_id=ctx["user"].id if ctx["user"].role == "tenant" else None,
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

    device.name = config.name
    device.hostname = config.hostname or config.name
    device.device_type = config.device_type
    device.vendor = config.vendor
    device.model = config.model
    device.tags = config.tags
    device.claimed = True
    device.status = "pending"
    device.device_group = group
    if ctx["user"].role == "tenant":
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
        # Multi-VLAN discovery: DISCOVERY_SUBNETS is a comma list of CIDRs
        # (e.g. 192.168.4.0/24,192.168.8.0/24). Legacy DISCOVERY_SUBNET
        # (a bare 3-octet prefix) still works. Defaults to the management LAN.
        raw = env.get("DISCOVERY_SUBNETS") or env.get("DISCOVERY_SUBNET") or "192.168.0.0/24"
        subnets = [s.strip() for s in raw.split(",") if s.strip()]
        max_per_subnet = 50
        try:
            max_per_subnet = max(10, min(int(env.get("DISCOVERY_MAX_HOSTS_PER_SUBNET") or "50"), 254))
        except (TypeError, ValueError):
            max_per_subnet = 50
        discovered = 0
        for subnet in subnets:
            if "/" not in subnet and subnet.count(".") == 3:
                subnet = subnet + "/24"
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
        sweep = {
            "ticket_id": f"snmp-sweep-{datetime.utcnow().strftime('%M%S')}",
            "action": "snmp_sweep",
            "target": ",".join(str(n) for n in subnets),
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
        else:
            setattr(device, field, value)

    device.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(device)
    resp = DeviceResponse.model_validate(device).model_dump()
    resp["snmp_configured"] = bool(device.snmp_community)
    resp["ssh_configured"] = bool(device.ssh_key_fingerprint)
    return resp


@router.delete("/{device_id}", status_code=204)
def delete_device(device_id: int, db: Session = Depends(get_db), ctx: dict = Depends(get_access_context)):
    device = _get_checked(db, device_id, ctx)
    # Detach tickets that reference this device (ON DELETE SET NULL behavior)
    from models import Ticket
    db.query(Ticket).filter(Ticket.target_device_id == device_id).update(
        {Ticket.target_device_id: None}
    )
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
    require_any_role("operator", "admin", "agent")(ctx["user"])
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
    if ctx["user"].role != "tenant":
        require_any_role("operator", "admin", "agent")(ctx["user"])
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
    if ctx["user"].role != "tenant":
        require_any_role("operator", "admin", "agent")(ctx["user"])
    from audit import log_event
    device = _get_checked(db, device_id, ctx)
    device.adoption_status = "revoked"
    db.commit()
    log_event(db, "device_adopt_revoke", ctx["user"].username, {
        "device_id": device.id, "device": device.name, "cn": device.cert_cn})
    return _adoption_brief(device)


@router.get("/{device_id}/credentials")
def get_device_credentials(device_id: int, db: Session = Depends(get_db), ctx: dict = Depends(get_access_context)):
    """Return decrypted credentials for agent use. Admin or agent identity only."""
    from auth import require_any_role
    require_any_role("admin", "agent")(ctx["user"])
    device = _get_checked(db, device_id, ctx)
    result = {}
    if device.snmp_community:
        result["snmp_community"] = decrypt(device.snmp_community)
    if device.ssh_user:
        result["ssh_user"] = device.ssh_user
    if device.ssh_key_fingerprint:
        key_file = _ssh_key_path(device.name)
        if os.path.exists(key_file):
            with open(key_file) as f:
                result["ssh_key"] = decrypt(f.read())
    return result
