"""Device-facing API — authenticated by the device's certificate (mTLS).

nginx terminates TLS for /api/v1/device/*, REQUIRES a valid client certificate
(issued by the BareNOC CA root), and passes the certificate subject to this
router via the X-SSL-Client-CN header (nginx strips any client-supplied
X-SSL-* headers, so the CN is trusted). The CN resolves to a device record,
which is how adoption "links": the first successful call proves the device
holds a valid cert for its identity.
"""

import datetime
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db
from models import Device
from step_ca import device_cn
import network_scope

router = APIRouter(prefix="/api/v1/device", tags=["device"])

_CN_RE = re.compile(r"^device-[A-Za-z0-9._-]{1,110}$")


def _client_cn(request: Request) -> str:
    """CN of the verified client cert (set by nginx from the validated
    $ssl_client_s_dn — nginx strips client-supplied X-SSL-* headers)."""
    dn = request.headers.get("x-ssl-client-dn") or ""
    cn = ""
    for part in dn.split(","):
        if part.strip().startswith("CN="):
            cn = part.strip()[3:]
            break
    if not _CN_RE.match(cn):
        raise HTTPException(status_code=403, detail="missing or invalid device certificate identity")
    return cn


def _json_text(value):
    """Serialize a value to JSON text, or None if it can't be represented."""
    if value is None:
        return None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


def _report_ips(request: Request, body: dict) -> list:
    """IPs this report carries: the agent-reported facts.ips (and a bare
    `ip`, if present) plus the request's real client IP. Deduped, in order."""
    ips = []
    raw_ips = body.get("ips") or []
    if isinstance(raw_ips, str):
        raw_ips = [raw_ips]
    for v in raw_ips:
        if isinstance(v, str) and v.strip():
            ips.append(v.strip())
    if isinstance(body.get("ip"), str) and body.get("ip").strip():
        ips.append(body["ip"].strip())
    rip = (request.headers.get("x-real-ip") or "").strip()
    if rip and rip not in ips:
        ips.append(rip)
    return ips


def _report_macs(body: dict) -> list:
    """MACs this report carries (the agent-reported facts.macs)."""
    macs = []
    raw_macs = body.get("macs") or []
    if isinstance(raw_macs, str):
        raw_macs = [raw_macs]
    for v in raw_macs:
        if isinstance(v, str) and v.strip():
            macs.append(v.strip())
    return macs


def _refresh_ip(device: Device, ips: list) -> None:
    """Fill the device IP from the report when the record has none usable.

    Never overwrites an existing discovery IP (that IP is what matched the box
    in the first place); never writes a CGNAT/Tailscale overlay address into
    inventory.
    """
    if device.ip_address and device.ip_address != "0.0.0.0":
        return
    for ip in ips:
        if not ip or ip == "0.0.0.0":
            continue
        if network_scope.is_tunnel_or_cgnat(ip):
            continue
        device.ip_address = ip
        return


def _find_unclaimed_match(db: Session, ips: list, macs: list) -> "Device | None":
    """Fallback adoption target for device-dedupe: an UNCLAIMED, not-yet-
    linked/revoked discovery record sharing an IP or MAC with this report.

    When multiple unclaimed records match, the most recently seen wins
    (deterministic tie-break: highest id). Never returns a claimed/linked
    record (a different identity) or a revoked one.
    """
    if not ips and not macs:
        return None
    conds = []
    if ips:
        conds.append(Device.ip_address.in_(ips))
    if macs:
        conds.append(func.lower(Device.mac_address).in_([m.lower() for m in macs]))
    if not conds:
        return None
    status_ok = or_(Device.adoption_status.is_(None),
                    Device.adoption_status.notin_(["linked", "revoked"]))
    return (db.query(Device)
            .filter(Device.claimed.is_(False), status_ok, or_(*conds))
            .order_by(Device.last_seen.is_(None),
                      Device.last_seen.desc(),
                      Device.id.desc())
            .first())


def resolve_device(db: Session, cn: str, ips: "list | None" = None,
                   macs: "list | None" = None):
    """Resolve a cert CN to its device record (link order):

      1. exact cert_cn match (the linked identity);
      2. name match (the canonical CN of the record's name — pre-link);
      3. IP/MAC fallback: an unclaimed discovery record for the same box
         (device-dedupe — adopt-in-place instead of a duplicate).

    Returns (device, adopted_in_place). `adopted_in_place` is True only when
    the record came from the fallback (the caller must adopt it in place, not
    self-register a duplicate).
    """
    device = db.query(Device).filter(Device.cert_cn == cn).first()
    if device:
        return device, False
    name = cn[len("device-"):]
    device = (db.query(Device)
              .filter(Device.name == name)
              .order_by(Device.id.desc()).first())
    if device:
        return device, False
    candidate = _find_unclaimed_match(db, ips or [], macs or [])
    if candidate is not None:
        return candidate, True
    return None, False


@router.post("/report")
async def device_report(request: Request, db: Session = Depends(get_db)):
    """The device's heartbeat/status report. First successful call LINKS the
    device to its inventory record (adoption completes). The cert proves the
    device owns the identity; revocation is instant at this layer (a revoked
    record is rejected even if the cert is still within its TTL).

    Two report shapes are accepted, distinguished by the body:
      * plain cert heartbeat (no agent fields) — links/keeps method="cert",
        exactly as before (backward compatible, path untouched);
      * NOC_Agent self-report (agent_version or adoption_method=="agent") —
        links/keeps method="agent" and stores agent_version + capabilities +
        the facts object.
    """
    body = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        pass
    hostname = str(body.get("hostname") or "").strip()
    agent_version = str(body.get("agent_version") or "").strip()
    adoption_method = str(body.get("adoption_method") or "").strip().lower()
    is_agent = bool(agent_version) or adoption_method == "agent"
    cn = _client_cn(request)
    ips = _report_ips(request, body)
    macs = _report_macs(body)
    device, adopted_in_place = resolve_device(db, cn, ips=ips, macs=macs)
    if not device:
        # SELF-REGISTRATION: a valid cert from the BareNOC CA is proof of
        # identity — create the record (this is how the /onboard portal adopts
        # a workstation: enroll a cert, and the first report links it). Only
        # reached when there is no cert_cn/name match AND no unclaimed IP/MAC
        # discovery record to adopt in place (device-dedupe).
        name = cn[len("device-"):][:120]
        ip = (request.headers.get("x-real-ip") or "").strip()
        device = Device(name=name, ip_address=ip or "0.0.0.0",
                        device_type="workstation", claimed=True,
                        status="online", tags=["self-onboarded"],
                        hostname=hostname[:120] or None)
        db.add(device)
    if device.adoption_status == "revoked":
        raise HTTPException(status_code=403, detail="device adoption revoked")
    now = datetime.datetime.utcnow()
    if adopted_in_place:
        # Dedupe adopt-in-place: refresh the discovery record's hostname + IP
        # from the report while KEEPING its discovery metadata (name, MAC,
        # device_type). The block below then links it exactly as it would a
        # self-registered record.
        if hostname:
            device.hostname = hostname[:120]
        _refresh_ip(device, ips)
    elif hostname and not device.hostname:
        device.hostname = hostname[:120]
    if device.adoption_status != "linked":
        device.adoption_status = "linked"
        device.adoption_method = "agent" if is_agent else "cert"
        device.cert_cn = cn
        device.cert_enrolled_at = device.cert_enrolled_at or now
        device.claimed = True
        # Cert adoption authorizes the appliance's CONTROL key on the device
        # (the /onboard script adds its public half to authorized_keys + enables
        # sshd) — pair it by storing the control key as this device's SSH
        # credential, making it immediately SSH-controllable by the agent.
        # Agent adoption does NOT provision SSH credentials: the agent IS the
        # control path (no stored SSH secrets on the appliance).
        if not is_agent and not device.ssh_key_fingerprint:
            try:
                from control_key import ensure_control_key
                from routes.devices import _store_ssh_key
                device.ssh_user = device.ssh_user or "barenoc"
                device.ssh_key_fingerprint = _store_ssh_key(
                    device.name, ensure_control_key()["private_key"])
            except Exception:
                pass
    if is_agent:
        # Every agent report refreshes the method + version + capabilities and
        # the latest facts (design §4 / §12: a cert-adopted device flips to
        # method="agent" on its first agent report).
        device.adoption_method = "agent"
        if agent_version:
            device.agent_version = agent_version[:64]
        if body.get("agent_capabilities") is not None:
            device.agent_capabilities = _json_text(body.get("agent_capabilities"))
        device.facts_json = _json_text({
            "hostname": body.get("hostname"),
            "os": body.get("os"),
            "kernel": body.get("kernel"),
            "macs": body.get("macs"),
            "ips": body.get("ips"),
            "uptime_s": body.get("uptime_s"),
            "disk_free_gb": body.get("disk_free_gb"),
        })
    device.cert_last_seen = now
    device.last_seen = now
    device.status = "online"
    db.commit()
    return {"ok": True, "device": device.name, "cn": cn,
            "adopted": device.adoption_status, "method": device.adoption_method}
