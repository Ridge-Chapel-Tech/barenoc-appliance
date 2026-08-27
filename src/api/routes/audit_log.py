"""Audit log viewer + export + hash-chain verify (compliance controls).

Admin-only. The audit trail is immutable (hash-chained) — this router exposes
the viewer, a chain-integrity check, and a JSON export an auditor can keep.
"""

import json

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from auth import require_role
from database import get_db
from models import AuditLog, User
from audit import verify_chain, log_event

router = APIRouter(prefix="/api/v1/audit-log", tags=["audit-log"])


def _serialize(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "event_type": row.event_type,
        "ticket_id": row.ticket_id,
        "actor": row.actor,
        "data": row.data or {},
        "previous_hash": row.previous_hash,
        "sha256_hash": row.sha256_hash,
    }


@router.get("")
def list_audit(limit: int = 200, offset: int = 0, event_type: str = None,
               actor: str = None, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    """Paginated audit rows, newest first (optional event_type / actor filter)."""
    q = db.query(AuditLog)
    if event_type:
        q = q.filter(AuditLog.event_type == event_type)
    if actor:
        q = q.filter(AuditLog.actor == actor)
    total = q.count()
    rows = q.order_by(AuditLog.id.desc()).offset(offset).limit(min(limit, 1000)).all()
    return {"total": total, "rows": [_serialize(r) for r in rows],
            "chain": verify_chain(db)}


@router.get("/verify")
def verify(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    """Recompute the hash chain and report integrity."""
    return verify_chain(db)


@router.get("/export")
def export(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    """Export the full audit log (JSON) + chain-verify summary."""
    import datetime as _dt
    fname = f"barenoc-audit-{_dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    # Export event: who pulled the audit-log export, when (compliance). The
    # export itself is recorded first so the emitted rows/chain include it.
    actor = getattr(user, "username", None) or str(getattr(user, "role", "admin"))
    log_event(db, "export_download", actor, {"kind": "audit_log"})
    rows = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
    body = {
        "schema_version": 1,
        "exported_at": _dt.datetime.utcnow().isoformat() + "Z",
        "chain": verify_chain(db),
        "rows": [_serialize(r) for r in rows],
    }
    return Response(
        content=json.dumps(body, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
