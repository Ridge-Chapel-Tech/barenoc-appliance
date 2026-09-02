"""Change log — append-only operational/business history (L2, 2026-08-31).

Complements (never replaces) the hash-chained ``audit.py`` record: the audit
log is the tamper-evident SECURITY record; this is the READABLE "what changed
and why" history for the customer and the agent. It feeds the optimizer (no
re-recommendation of what's been fixed/declined) and is the "historical
documentation" the customer can keep and the agent can read to never redo work.

Design rules (locked by `briefs/change-log.md`):

  * Append-only: there is no update/delete surface in the API.
  * NOT hash-chained (that's the audit log's job) — this is a history, not a
    tamper record.
  * Every write is BEST-EFFORT: ``record()`` swallows every failure so a
    change-log problem can never break the operation that triggered it.
  * One event record: ``{at, actor (user/agent/system), event_type, asset,
    summary (one line), detail (technical), links (ticket/finding ids),
    customer_visible}``.
  * Two views: ``customer`` (customer_visible events, one-line readable, no
    technical detail) and ``agent`` (full detail + links).

This module is pure stdlib at import time (no FastAPI/SQLAlchemy) so every
capture point can import it freely; the ORM model is imported lazily inside
``record()``.
"""

import datetime

# The event types (extensible — unknown types are still recorded so a future
# producer never crashes against an older build).
EVENT_TYPES = (
    "device_adopted",
    "device_revoked",
    "device_config_changed",
    "firmware_updated",
    "finding_resolved",
    "finding_declined",
    "finding_actioned",
    "ticket_closed",
    "provisioned",
    "settings_changed",
    "device_sighting_folded",
)

# Default customer visibility per event type. The customer view is the one-line
# "your network changed" history; technical detail (config/credential/settings)
# stays agent-only by default, and a call site may still override explicitly.
CUSTOMER_VISIBLE = {
    "device_adopted": True,
    "device_revoked": True,
    "device_config_changed": True,
    "firmware_updated": True,
    "finding_resolved": True,
    "finding_declined": True,
    "finding_actioned": False,   # technical: a finding got a fix ticket
    "ticket_closed": True,
    "provisioned": True,
    "settings_changed": False,   # appliance-level, technical (override at the call site when it matters)
    "device_sighting_folded": False,  # technical: housekeeping dedupe of a randomized-MAC sighting
}

MAX_SUMMARY = 512
MAX_DETAIL = 4000
MAX_ACTOR = 64
MAX_ASSET = 256
MAX_EVENT_TYPE = 32


def _clean_summary(event_type: str, summary) -> str:
    s = str(summary or "").strip()
    if not s:
        s = str(event_type).replace("_", " ")
    return s[:MAX_SUMMARY]


def record(db, *, event_type, actor, asset="", summary="", detail="",
           links=None, customer_visible=None):
    """Append one change-log entry. NEVER raises — a failure here must not
    break the main operation (best-effort by design).

    ``db`` is a SQLAlchemy session. This function commits its own row (and,
    transitively, any pending changes the caller left uncommitted — the call
    sites place it after their own commit, so this is harmless). On any error
    it rolls back and returns None.
    """
    try:
        et = str(event_type or "").strip()[:MAX_EVENT_TYPE] or "unknown"
        if links is None:
            links = {}
        if customer_visible is None:
            customer_visible = CUSTOMER_VISIBLE.get(et, False)

        from models import ChangeLogEntry  # lazy import (avoids import cycles)

        entry = ChangeLogEntry(
            actor=str(actor or "system").strip()[:MAX_ACTOR] or "system",
            event_type=et,
            asset=(str(asset or "").strip()[:MAX_ASSET] or None),
            summary=_clean_summary(et, summary),
            detail=(str(detail or "").strip()[:MAX_DETAIL] or None),
            links=links if isinstance(links, dict) else {},
            customer_visible=bool(customer_visible),
        )
        db.add(entry)
        db.commit()
        return entry
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None


# ── read path (the two views + the download artifact) ──────────────────────

def capture_finding_resolutions(db, ticket_id, actor):
    """Record ``finding_resolved`` for every NetOpt finding whose fix ticket
    just closed (the finding-update path that links a finding key to its
    ticket). Best-effort — never raises."""
    try:
        from models import Finding
        findings = db.query(Finding).filter(
            Finding.fix_ticket_id == ticket_id).all()
        for f in findings:
            record(db, event_type="finding_resolved", actor=actor,
                   asset=f.finding_key,
                   summary=f"Finding {f.finding_key} resolved ({ticket_id})",
                   detail=(f.title or f.finding_key),
                   links={"finding_key": f.finding_key, "finding_id": f.id,
                          "ticket_id": ticket_id, "run_id": f.run_id},
                   customer_visible=True)
    except Exception:
        pass


def query_entries(db, view="agent", limit=200, offset=0):
    """Return (total, rows) newest-first. ``view`` filters customer vs agent."""
    from models import ChangeLogEntry
    q = db.query(ChangeLogEntry)
    if view == "customer":
        q = q.filter(ChangeLogEntry.customer_visible.is_(True))
    total = q.count()
    rows = (q.order_by(ChangeLogEntry.id.desc())
            .offset(offset).limit(limit).all())
    return total, rows


def _iso(ts) -> str:
    if not ts:
        return ""
    try:
        return ts.isoformat() + "Z" if ts.tzinfo is None else ts.isoformat()
    except Exception:
        return str(ts)


def entry_dict(entry, view="agent") -> dict:
    """One event as a JSON-friendly dict. The customer view omits technical
    detail + links (one-line readable only)."""
    out = {
        "at": _iso(entry.timestamp),
        "actor": entry.actor,
        "event_type": entry.event_type,
        "asset": entry.asset,
        "summary": entry.summary,
    }
    if view == "agent":
        out["detail"] = entry.detail
        out["links"] = entry.links or {}
        out["customer_visible"] = bool(entry.customer_visible)
    return out


def render_json(rows, view="agent") -> str:
    """The downloadable JSON artifact (change history for the period)."""
    import json
    payload = {
        "generated": _iso(datetime.datetime.utcnow()),
        "view": view,
        "count": len(rows),
        "events": [entry_dict(r, view=view) for r in rows],
    }
    return json.dumps(payload, indent=2, default=str)


def render_markdown(rows, view="agent") -> str:
    """The downloadable Markdown artifact — the "historical documentation"
    the customer can keep, and the agent can read to never redo work."""
    lines = [
        "# BareNOC Change History",
        "",
        f"Generated {_iso(datetime.datetime.utcnow())} · view: **{view}** · "
        f"{len(rows)} event(s)",
        "",
        "| When (UTC) | Actor | Event | Asset | Summary |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        ev = r.event_type.replace("_", " ")
        lines.append(
            f"| {_iso(r.timestamp)} | {r.actor} | {ev} | {r.asset or '—'} | "
            f"{r.summary} |")
    if rows:
        lines += ["", "## Details", ""]
        for r in rows:
            lines.append(f"### {_iso(r.timestamp)} — {r.event_type} — {r.asset or '—'}")
            lines.append(f"- **Summary:** {r.summary}")
            if view == "agent":
                if r.detail:
                    lines.append(f"- **Detail:** {r.detail}")
                links = r.links or {}
                if links:
                    rendered = "; ".join(f"{k}: {v}" for k, v in sorted(links.items()))
                    lines.append(f"- **Links:** {rendered}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
