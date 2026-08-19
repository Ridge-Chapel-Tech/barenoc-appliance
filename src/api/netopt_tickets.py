"""Ticket-spawn helper for the Network Optimization fix-ticket rollout.

The admin **Optimize** button turns selected scan findings into ADMIN tickets
that flow through the normal pipeline (Juniper queue + Lily/pi technician +
the existing approval gates). The button creates TICKETS — never direct
controller writes — so the authority model stays intact.

This module is the single place that builds the ticket content so the admin's
comment is UNMISSABLE. The worker pipeline reads ticket descriptions into its
task/system context (``ticket_text = title + description``), so embedding the
``ADMIN_CONTEXT_NOTE`` + the admin comment at the TOP of the description is the
comment-read wiring: the note and the instructions reach the worker/Juniper
sysctx verbatim.
"""

import datetime
import json

from models import Ticket
from worknotes import add_note
from network_opt_rules import fixability

ADMIN_CONTEXT_NOTE = ("This ticket has admin context/instructions — read them "
                      "fully before any action.")
ADMIN_CONTEXT_BANNER = "⚠️ ADMIN CONTEXT — READ FULLY BEFORE ANY ACTION"

SEVERITY_PRIORITY = {"critical": "P1", "warning": "P2", "info": "P3"}
_PRIORITY_ORDER = {"P1": 3, "P2": 2, "P3": 1, "P4": 0}

PER_ITEM_CAP = 10
SOURCE = "optimize"

# ``schemas.generate_ticket_id`` is millisecond-based and COLLIDES when several
# tickets are created in a tight loop (per-item batches) — salt the sequence so
# each spawned ticket gets a distinct, DB-unique TKT id.
_ticket_seq = [0]


def _unique_ticket_id(db) -> str:
    for _ in range(1000):
        now = datetime.datetime.utcnow()
        date_part = now.strftime("%Y%m%d")
        seq = (int(now.timestamp() * 1000) + _ticket_seq[0]) % 10000
        _ticket_seq[0] += 1
        tid = f"TKT-{date_part}-{seq:04d}"
        if db.query(Ticket).filter(Ticket.ticket_id == tid).first():
            continue
        return tid
    raise RuntimeError("could not allocate a unique ticket id")


def _get(finding, key, default=""):
    """Read a field from a Finding row (or a plain dict in unit tests)."""
    if isinstance(finding, dict):
        return finding.get(key, default)
    return getattr(finding, key, default)


def priority_for_severity(severity: str) -> str:
    return SEVERITY_PRIORITY.get((severity or "").strip().lower(), "P3")


def highest_priority(severities) -> str:
    best = "P3"
    for sev in (severities or []):
        p = priority_for_severity(sev)
        if _PRIORITY_ORDER.get(p, 0) > _PRIORITY_ORDER.get(best, 0):
            best = p
    return best


def _evidence_text(finding) -> str:
    ev = _get(finding, "evidence", None)
    if not ev:
        return ""
    if isinstance(ev, str):
        return ev.strip()
    try:
        return json.dumps(ev, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(ev)


def _comment_for(finding, comments) -> str:
    """Look up the admin comment for a finding (by id first, then stable key)."""
    comments = comments or {}
    c = comments.get(str(_get(finding, "id", "")), "")
    if not c:
        c = comments.get(_get(finding, "finding_key", ""), "")
    return (c or "").strip()


def _finding_block(finding, run_id, comment="", index=None) -> str:
    fx = fixability(_get(finding, "finding_key", ""))
    lines = []
    if index is not None:
        lines.append(f"--- Finding {index}: {_get(finding, 'title', '')} ---")
    else:
        lines.append(f"Finding: {_get(finding, 'title', '')}")
    lines.append(f"Severity: {_get(finding, 'severity', '')} · "
                 f"Category: {_get(finding, 'category', '')} · "
                 f"Key: {_get(finding, 'finding_key', '')}")
    detail = _get(finding, "detail", "")
    if detail:
        lines.append(str(detail))
    lines.append(f"Suggested action: {fx.get('suggested_action', '')}")
    if comment:
        lines.append(f"Admin comment: {comment}")
    ev = _evidence_text(finding)
    if ev:
        lines.append("Evidence:")
        lines.append(ev)
    if index is None:
        lines.append(f"Network Optimization run reference: #{run_id}")
    return "\n".join(lines)


def finding_description(finding, run_id, comment="") -> str:
    """Per-item ticket description: admin banner first, then the finding
    detail + evidence + suggested_action + admin comment + run reference."""
    comment = (comment or "").strip()
    parts = [ADMIN_CONTEXT_BANNER, ADMIN_CONTEXT_NOTE, ""]
    if comment:
        parts += [f"Admin comment: {comment}", ""]
    parts += [_finding_block(finding, run_id)]
    return "\n".join(parts)


def batched_description(findings, run_id, comments=None) -> str:
    """One ticket listing every selected finding as a section."""
    comments = comments or {}
    parts = [ADMIN_CONTEXT_BANNER, ADMIN_CONTEXT_NOTE, "",
             f"Network Optimization run #{run_id} — fix "
             f"{len(findings)} selected finding(s).", ""]
    for i, f in enumerate(findings, 1):
        parts.append(_finding_block(f, run_id, comment=_comment_for(f, comments),
                                    index=i))
        parts.append("")
    parts.append(f"Network Optimization run reference: #{run_id}")
    return "\n".join(parts)


def _create_ticket(db, title, description, priority, submitter_id):
    ticket = Ticket(
        ticket_id=_unique_ticket_id(db),
        title=title,
        description=description,
        priority=priority if priority in ("P1", "P2", "P3", "P4") else "P3",
        status="open",
        source=SOURCE,
        submitter_id=submitter_id,
    )
    db.add(ticket)
    return ticket


def spawn_optimize_tickets(db, run_id, findings, mode="per_item", comments=None,
                           submitter_id=None) -> dict:
    """Create the admin tickets for the selected (already-validated) findings.

    Does NOT commit — the caller commits once (single transaction). Returns a
    summary dict for the API response.
    """
    findings = list(findings or [])
    mode = (mode or "per_item").strip().lower()
    created = []

    if mode == "batched":
        sevs = [_get(f, "severity", "info") for f in findings]
        priority = highest_priority(sevs)
        title = (f"Network Optimization: fix {len(findings)} findings "
                 f"(run #{run_id})")[:256]
        ticket = _create_ticket(
            db, title, batched_description(findings, run_id, comments),
            priority, submitter_id)
        add_note(ticket, "admin_context", ADMIN_CONTEXT_NOTE, actor="admin")
        created.append({"ticket_id": ticket.ticket_id, "priority": ticket.priority,
                        "finding_ids": [_get(f, "id") for f in findings]})
    else:
        for f in findings:
            priority = priority_for_severity(_get(f, "severity", "info"))
            comment = _comment_for(f, comments)
            title = _get(f, "title", "")[:256] or f"Fix {_get(f, 'finding_key', '')}"
            ticket = _create_ticket(
                db, title, finding_description(f, run_id, comment=comment),
                priority, submitter_id)
            add_note(ticket, "admin_context", ADMIN_CONTEXT_NOTE, actor="admin")
            created.append({"ticket_id": ticket.ticket_id, "priority": ticket.priority,
                            "finding_ids": [_get(f, "id")]})

    return {"mode": mode, "created": created, "count": len(created)}
