import os
import json
import hashlib
import datetime
from typing import Optional

from audit_catalog import _looks_secret

# Volume-honesty guard (the 2 vCPU/4 GB baseline): each audit event's data
# payload must stay small — no nested blobs. enforce_payload_limits trims
# oversized payloads in place before they are written/hashed.
MAX_EVENT_DATA_BYTES = 512  # hard ceiling; new events should stay <= ~300 B


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


def _redact_value(value):
    """Never let a secret value land in the audit log (defense-in-depth on
    top of the call sites' own scrubbing)."""
    return "[redacted]"


def _sanitize(data):
    """Deep-copy `data`, redacting any secret-looking keys and truncating
    long strings. Returns the sanitized payload."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if _looks_secret(k):
                out[k] = _redact_value(v)
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(data, list):
        return [_sanitize(v) for v in data]
    if isinstance(data, str):
        return data[:400]
    return data


def enforce_payload_limits(data: dict) -> dict:
    """Redact secret fields and keep the payload under MAX_EVENT_DATA_BYTES.

    Applied before hashing/storing so the hash chain stays consistent with
    whatever is actually persisted. The caller's reference is not mutated —
    the sanitized copy is returned.
    """
    try:
        clean = _sanitize(data)
        raw = json.dumps(clean, sort_keys=True, default=str)
        if len(raw.encode("utf-8")) > MAX_EVENT_DATA_BYTES:
            # Coarse but safe: replace the whole data blob with a compact
            # summary — an oversized audit payload is worse than a terse one.
            return {"truncated": True, "note": "payload over size limit"}
        return clean
    except Exception:
        return {"error": "payload sanitize failed"}


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

    # Volume-honesty guard: scrub secrets + keep payload small before hashing.
    safe_data = enforce_payload_limits(data or {})

    event = AuditLog(
        event_id=generate_event_id(),
        event_type=event_type,
        ticket_id=ticket_id,
        actor=actor,
        data=safe_data,
        previous_hash=previous_hash,
        sha256_hash=compute_hash(safe_data, previous_hash),
    )
    db_session.add(event)
    db_session.commit()
    return event


def pseudonymize_audit_log(db_session, keep_days: int) -> dict:
    """Compliance retention pass (08-27, the locked model): audit EVENTS are
    kept forever, but personal identifiers (actor, IPs, usernames, emails in
    the data payload) are blanked once a row is older than keep_days. Runs as
    a documented one-time pass: the aged segment is rewritten and RE-CHAINED
    (row order + previous_hash pointers preserved; each row's hash recomputed
    from its data), then a `retention.pseudonymized` event records it — so
    verify_chain stays green for the current state.

    keep_days <= 0 = never pseudonymize (home/indefinite)."""
    from models import AuditLog
    import datetime as _dt
    out = {"pseudonymized": 0, "rechained": 0, "event": None, "note": ""}
    if keep_days is None or keep_days <= 0:
        out["note"] = "no window configured (indefinite)"
        return out
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=keep_days)
    _PERSONAL = {"ip", "username", "email", "actor", "from", "to", "client_ip",
                 "user", "reporter"}
    rows = (db_session.query(AuditLog)
            .filter(AuditLog.timestamp < cutoff)
            .order_by(AuditLog.id.asc()).all())
    first_edited = None
    for r in rows:
        data = dict(r.data or {})
        changed = False
        if r.actor:
            r.actor = "[redacted]"
            changed = True
        for k in list(data):
            if k.lower() in _PERSONAL and data[k]:
                data[k] = "[redacted]"
                changed = True
        if changed:
            r.data = data
            if first_edited is None:
                first_edited = r.id
            out["pseudonymized"] += 1
    if first_edited is not None:
        # re-chain from the first edited row forward
        after = (db_session.query(AuditLog)
                 .filter(AuditLog.id >= first_edited)
                 .order_by(AuditLog.id.asc()).all())
        prev_hash = None
        for i, r in enumerate(after):
            if i == 0:
                prev_hash = (db_session.query(AuditLog)
                             .filter(AuditLog.id < first_edited)
                             .order_by(AuditLog.id.desc()).first())
                prev_hash = prev_hash.sha256_hash if prev_hash else None
            r.previous_hash = prev_hash
            r.sha256_hash = compute_hash(r.data, prev_hash)
            prev_hash = r.sha256_hash
            out["rechained"] += 1
        db_session.commit()
        ev = log_event(db_session, "retention.pseudonymized", "system",
                       {"window_days": keep_days,
                        "rows": out["pseudonymized"],
                        "rechained": out["rechained"]}, None)
        out["event"] = ev.event_id if ev else None
    else:
        db_session.commit()
    return out


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
