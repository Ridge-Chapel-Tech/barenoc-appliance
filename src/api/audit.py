import os
import json
import hashlib
import datetime
from typing import Optional


def _audit_enabled() -> bool:
    """Compliance control: audit log on/off (hot-read from .env). When off,
    log_event is a no-op so nothing is recorded (the viewer shows empty)."""
    try:
        from llm_providers import read_env_file
        env = read_env_file() or {}
        return str(env.get("AUDIT_LOG_ENABLED") or "true").strip().lower() \
            not in ("0", "false", "no", "off")
    except Exception:
        return True


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
):
    """
    Write an immutable audit log entry.
    Returns the event dict for reference (None when audit logging is off).
    """
    if not _audit_enabled():
        return None
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


def verify_chain(db_session) -> dict:
    """Recompute the hash chain over the whole audit log.

    Returns {ok, count, broken_at, first_ts, last_ts, error}. Each row must
    hash correctly against its own data + recorded previous_hash, and each
    row's previous_hash must equal the previous row's sha256_hash.
    """
    from models import AuditLog
    rows = db_session.query(AuditLog).order_by(AuditLog.id.asc()).all()
    out = {"ok": True, "count": len(rows), "broken_at": None,
           "first_ts": None, "last_ts": None, "error": None}
    if not rows:
        return out
    out["first_ts"] = rows[0].timestamp.isoformat() if rows[0].timestamp else None
    out["last_ts"] = rows[-1].timestamp.isoformat() if rows[-1].timestamp else None
    prev_hash = None
    for i, r in enumerate(rows):
        if i == 0:
            if r.previous_hash is not None and r.previous_hash != "":
                out["ok"] = False
                out["broken_at"] = r.id
                out["error"] = "first row has a previous_hash"
                return out
        else:
            if (r.previous_hash or "") != (prev_hash or ""):
                out["ok"] = False
                out["broken_at"] = r.id
                out["error"] = "previous_hash does not chain to the prior row"
                return out
        if compute_hash(r.data, r.previous_hash) != r.sha256_hash:
            out["ok"] = False
            out["broken_at"] = r.id
            out["error"] = "sha256_hash does not match the row data"
            return out
        prev_hash = r.sha256_hash
    return out
