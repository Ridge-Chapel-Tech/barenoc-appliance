"""Device-revoke integrity sweep — HTTP surface.

The engine lives in src/api/revoke_integrity.py (state + audit + email); this
module is the HTTP surface. The SCHEDULER calls POST /sweep each cycle — the
engine runs in the API container (DB + models + emailer), the scheduler only
triggers it, same pattern as Service Checks / Network Optimization.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import require_any_role
from database import get_db
import revoke_integrity

router = APIRouter(prefix="/api/v1/revoke-integrity", tags=["revoke-integrity"])


@router.post("/sweep")
def sweep(db: Session = Depends(get_db),
          user=Depends(require_any_role("admin", "agent"))):
    """Scheduler-facing: run one revoke-integrity pass. The engine uses its
    own session; this dependency's session is only used for auth."""
    return revoke_integrity.run_sweep()
