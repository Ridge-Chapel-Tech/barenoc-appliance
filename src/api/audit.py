import os
import json
import hashlib
import secrets
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
    # Microsecond timestamps alone can collide when concurrent writers fire in
    # the same microsecond (event_id is UNIQUE). A short random suffix keeps
    # the id collision-free within the 32-char column.
    return f"evt_{ts}_{secrets.token_hex(3)}"


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


def _begin_write_transaction(db_session):
    """Serialize the read-then-write that follows against other SQLite writers.

    ``BEGIN IMMEDIATE`` takes SQLite's RESERVED lock up front so the previous-
    hash READ and the INSERT commit as one atomic step — a second writer can't
    read the same tail hash and fork the chain. If the caller's session is
    already inside a write transaction (a prior flush/DML already holds the
    lock), SQLite raises "cannot start a transaction within a transaction":
    that existing transaction already serializes us, so only that error is
    swallowed. Any other error (e.g. a busy database) propagates.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    try:
        db_session.execute(text("BEGIN IMMEDIATE"))
    except OperationalError as e:
        if "cannot start a transaction" not in str(e):
            raise


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

    # Atomic chain append: acquire the write lock BEFORE reading the tail so
    # two concurrent writers can't both read the same previous_hash and fork.
    try:
        _begin_write_transaction(db_session)
    except Exception:
        db_session.rollback()
        raise

    try:
        # Get previous hash for chain (inside the write transaction).
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
    except Exception:
        db_session.rollback()
        raise
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
        # re-chain from the first edited row forward (same logic as
        # repair_chain, scoped to the aged segment's tail).
        after = (db_session.query(AuditLog)
                 .filter(AuditLog.id >= first_edited)
                 .order_by(AuditLog.id.asc()).all())
        anchor = (db_session.query(AuditLog)
                  .filter(AuditLog.id < first_edited)
                  .order_by(AuditLog.id.desc()).first())
        _rechain(db_session, after,
                 anchor_prev=anchor.sha256_hash if anchor else None)
        out["rechained"] = len(after)
        db_session.commit()
        ev = log_event(db_session, "retention.pseudonymized", "system",
                       {"window_days": keep_days,
                        "rows": out["pseudonymized"],
                        "rechained": out["rechained"]}, None)
        out["event"] = ev.event_id if ev else None
    else:
        db_session.commit()
    return out


def _compact_ids(ids) -> str:
    """Collapse a sorted list of int ids into a compact range string so the
    chain.repaired payload stays small even when a whole tail is re-chained
    (e.g. [8502, 8503, 8504, 13601] -> "8502-8504,13601")."""
    if not ids:
        return ""
    parts = []
    start = prev = ids[0]
    for x in ids[1:]:
        if x == prev + 1:
            prev = x
        else:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = x
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _rechain(db_session, rows, anchor_prev=None):
    """Recompute previous_hash + sha256_hash for `rows` (already in id order)
    into one consistent chain anchored at `anchor_prev` (the sha256_hash of the
    row preceding the first row in `rows`, or None for the very first row).

    Only rows whose pointer/hash actually differs are rewritten; returns the
    list of ids that changed. Does NOT commit or log — the caller owns the
    transaction and the audit event.
    """
    changed = []
    prev_hash = anchor_prev
    for r in rows:
        new_sha = compute_hash(r.data, prev_hash)
        if ((r.previous_hash or None) != (prev_hash or None)
                or r.sha256_hash != new_sha):
            r.previous_hash = prev_hash
            r.sha256_hash = new_sha
            changed.append(r.id)
        prev_hash = new_sha
    return changed


def repair_chain(db_session) -> dict:
    """Re-chain the whole audit log in id order, fixing any forks the
    pre-atomic log_event writer left behind (two rows sharing one
    previous_hash). Idempotent: a green chain is a no-op that records no
    event. When rows ARE fixed it records a `chain.repaired` audit event
    (ids + count) so the repair itself is audited.

    Gate-runs this once on test then prod after the atomic-write fix lands;
    do NOT run it on a live DB casually.
    """
    from models import AuditLog
    out = {"repaired": 0, "fixed_ids": [], "event": None, "note": ""}
    try:
        _begin_write_transaction(db_session)
    except Exception:
        db_session.rollback()
        raise
    try:
        rows = db_session.query(AuditLog).order_by(AuditLog.id.asc()).all()
        if not rows:
            out["note"] = "empty log"
            db_session.commit()
            return out
        changed = _rechain(db_session, rows, anchor_prev=None)
        if changed:
            db_session.commit()
            out["repaired"] = len(changed)
            out["fixed_ids"] = changed
            ev = log_event(db_session, "chain.repaired", "system", {
                "count": len(changed),
                "ids": _compact_ids(changed),
            })
            out["event"] = ev.event_id if ev else None
        else:
            out["note"] = "chain already consistent"
            db_session.commit()
    except Exception:
        db_session.rollback()
        raise
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
