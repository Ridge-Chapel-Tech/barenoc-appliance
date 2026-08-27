"""Starlink link-health read surface — the endpoint behind the Devices
'Uplink / ISP' card's dish stats.

  GET  /api/v1/starlink/status   (operator+)

Returns the collector's config + the dish device + the latest starlink.*
metrics (the honest last samples), the episode health state, any open
degradation/outage ticket, and a mini trend (last hour) for the two headline
signals (ping + downlink throughput). Gaps show as "no data" rather than
fabricated points.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import require_role
from database import get_db
from models import User
import starlink

router = APIRouter(prefix="/api/v1/starlink", tags=["starlink"])


@router.get("/status")
def status(db: Session = Depends(get_db),
           user: User = Depends(require_role("operator"))):
    return starlink.status_payload(db)
