"""Uplink / ISP read surface — the endpoint behind the Devices card.

  GET  /api/v1/uplink/status   (readonly+)

Read-only; returns the vendor-agnostic uplink payload (Starlink dish →
UniFi gateway WAN → egress probe fallback). Admin + non-admin staff can read
it (readonly tier and above) — it never accepts writes.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import require_role
from database import get_db
from models import User
import uplink

router = APIRouter(prefix="/api/v1/uplink", tags=["uplink"])


@router.get("/status")
def status(db: Session = Depends(get_db),
           user: User = Depends(require_role("readonly"))):
    return uplink.uplink_status(db)
