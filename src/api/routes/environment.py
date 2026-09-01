"""Knowledge-layer L1 query surface (read-only).

Exposes the environment digest (for the agent runner's sysctx) and the full
normalized device record (for the optimizer + report). No writes, no secrets:
``environment_state`` never returns SNMP communities / SSH keys / credentials.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth import get_access_context, require_any_role
import environment_state

router = APIRouter(prefix="/api/v1/environment", tags=["environment"])
logger = logging.getLogger("environment")

# The agent service account (role "agent") fetches the digest for the runner's
# sysctx; tech tier + admin get it for the UI/report.
_ENV_ROLES = ("admin", "technician", "operator", "agent")


def _authorize(ctx: dict):
    """Digest + device state are environment-inventory reads — tech tier and
    the agent service identity only (customers stay out)."""
    require_any_role(*_ENV_ROLES)(ctx["user"])
    return ctx["user"]


@router.get("/summary")
def environment_summary(db: Session = Depends(get_db),
                        ctx: dict = Depends(get_access_context)):
    """The compact environment digest (inventory/capability/control highlights
    + unknown-device flags), with a pre-rendered ``text`` block the sysctx
    builder appends."""
    _authorize(ctx)
    return environment_state.summarize_environment(db)


@router.get("/devices/{device_id}")
def environment_device(device_id: int, db: Session = Depends(get_db),
                       ctx: dict = Depends(get_access_context)):
    """The full normalized record for one device (optimizer + report)."""
    _authorize(ctx)
    rec = environment_state.device_state(db, device_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return rec
