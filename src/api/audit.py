import os
import json
import hashlib
import datetime
from typing import Optional


def generate_event_id() -> str:
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    return f"evt_{ts}"


def compute_hash(data: dict, previous_hash: Optional[str] = None) -> str:
    raw = json.dumps(data, sort_keys=True) + (previous_hash or "0")
    return hashlib.sha256(raw.encode()).hexdigest()


def log_event(
    db_session,
    event_type: str,
    actor: str,
    data: dict,
    ticket_id: Optional[str] = None,
) -> dict:
    """
    Write an immutable audit log entry.
    Returns the event dict for reference.
    """
    from models import AuditLog

    # Get previous hash for chain
    prev = (
        db_session.query(AuditLog)
        .order_by(AuditLog.id.desc())
        .first()
    )
    previous_hash = prev.sha256_hash if prev else None

    event = AuditLog(
        event_id=generate_event_id(),
        event_type=event_type,
        ticket_id=ticket_id,
        actor=actor,
        data=data,
        previous_hash=previous_hash,
        sha256_hash=compute_hash(data, previous_hash),
    )
    db_session.add(event)
    db_session.commit()
    return event
