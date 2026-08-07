"""Device-facing API — authenticated by the device's certificate (mTLS).

nginx terminates TLS for /api/v1/device/*, REQUIRES a valid client certificate
(issued by the BareNOC CA root), and passes the certificate subject to this
router via the X-SSL-Client-CN header (nginx strips any client-supplied
X-SSL-* headers, so the CN is trusted). The CN resolves to a device record,
which is how adoption "links": the first successful call proves the device
holds a valid cert for its identity.
"""

import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models import Device
from step_ca import device_cn

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


@router.post("/report")
async def device_report(request: Request, db: Session = Depends(get_db)):
    """The device's heartbeat/status report. First successful call LINKS the
    device to its inventory record (adoption completes). The cert proves the
    device owns the identity; revocation is instant at this layer (a revoked
    record is rejected even if the cert is still within its TTL)."""
    # Optional payload: the device can report its hostname on each beat.
    hostname = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            hostname = str(body.get("hostname") or "").strip()
    except Exception:
        pass
    cn = _client_cn(request)
    device = db.query(Device).filter(Device.cert_cn == cn).first()
    if not device:
        # Allow the CN->record mapping to be verified via the device name too
        # (cert_cn is set on the link; before that the record may be found by
        #  the canonical CN of its name).
        name = cn[len("device-"):]
        device = (db.query(Device)
                  .filter(Device.name == name)
                  .order_by(Device.id.desc()).first())
    if not device:
        # SELF-REGISTRATION: a valid cert from the BareNOC CA is proof of
        # identity — create the record (this is how the /onboard portal adopts
        # a workstation: enroll a cert, and the first report links it).
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
    if hostname and not device.hostname:
        device.hostname = hostname[:120]
    if device.adoption_status != "linked":
        device.adoption_status = "linked"
        device.adoption_method = "cert"
        device.cert_cn = cn
        device.cert_enrolled_at = device.cert_enrolled_at or now
        device.claimed = True
    device.cert_last_seen = now
    device.last_seen = now
    device.status = "online"
    db.commit()
    return {"ok": True, "device": device.name, "cn": cn,
            "adopted": device.adoption_status, "method": device.adoption_method}
