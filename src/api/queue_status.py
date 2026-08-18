"""Queue status — shared stage derivation + pause directives.

Single source of truth for two things both the API and the worker need:

1. The derived "stage" of a ticket, computed from its last meaningful work
   note. `GET /api/v1/tickets/{id}/status` and the Juniper queue responder
   both use `derive_status` — one implementation, byte-identical output.

2. `pause_until` / `pause_cleared` directive parsing. The worker checks
   `is_paused` before processing any ticket; the chat thread renders the
   directive as a visible system line.

Keep this module importable from BOTH src/api and src/worker (it must not
import anything outside stdlib + the ticket object's attributes).
"""

import json
import datetime

from tone_pool import friendly_note


# Stage derived from the last meaningful work_note event. The chat thread can
# query this repeatedly while the technician works — it never mutates the
# ticket and never interrupts the pipeline.
STATUS_STAGES = {
    "agent_progress":   ("working", "Working on it — {detail}"),
    "processing":       ("working", "Picked up — the technician is working on this"),
    "auto_execute":     ("working", "Running the action (autonomous)"),
    "agent_response":   ("working", "Interim response — {detail}"),
    "agent_retry":      ("working", "Retrying — {detail}"),
    "awaiting_approval":("review",  "Awaiting your approval"),
    "escalated":        ("review",  "Escalated — needs your review"),
    "customer_input":   ("waiting", "Waiting on you — {detail}"),
    "ai_tech_feedback": ("answered","Answered — awaiting your confirmation"),
    "agent_completed":  ("done",    "Completed"),
    "completed":        ("done",    "Completed ✓"),
}
ACTIVE_STATUSES = ("open", "in_progress", "awaiting_approval", "escalated")


def list_notes(ticket) -> list:
    """Parse a ticket's work_notes JSON into a list of dicts ([] on junk)."""
    try:
        return json.loads(ticket.work_notes) if ticket.work_notes else []
    except (json.JSONDecodeError, TypeError):
        return []


def last_meaningful_note(ticket):
    """Most recent work_note that isn't user/checkin chatter, or None."""
    for n in reversed(list_notes(ticket)):
        if not isinstance(n, dict):
            continue
        ev = n.get("event") or ""
        if ev in ("user_message", "checkin_request"):
            continue
        return n
    return None


def parse_note_time(n) -> "datetime.datetime | None":
    ts = (n or {}).get("timestamp") or ""
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", ""))
    except Exception:
        return None


def derive_status(ticket, now=None) -> dict:
    """Derived stage + idle age for a ticket (the /status endpoint body).

    Mirrors the historical routes/tickets.py `ticket_status` logic exactly —
    do not change the field names/values or the endpoint output shifts.
    """
    now = now or datetime.datetime.utcnow()
    n = last_meaningful_note(ticket)
    ev = (n or {}).get("event") or ""
    kind, label_t = STATUS_STAGES.get(ev, ("waiting", "No activity yet — queued"))
    detail = str((n or {}).get("detail") or "")[:200]
    # Parity with the runner's progress filter: for agent_progress (live AI work
    # notes), a technical detail is scrubbed to a category-matched friendly
    # phrase from the shared tone pool, so the /status label never leaks paths,
    # sudo/uids, IPs, or API detail. Friendly details pass through unchanged.
    if ev == "agent_progress" and detail:
        detail, _ = friendly_note(detail)
    label = label_t.format(detail=detail) if detail else label_t.split("{")[0].rstrip(" — ")
    at = parse_note_time(n)

    active = ticket.status in ACTIVE_STATUSES
    idle = None
    if active and at:
        try:
            idle = max(0, int((now - at).total_seconds()))
        except Exception:
            idle = None

    return {
        "ticket_id": ticket.ticket_id,
        "status": ticket.status,
        "stage": kind,
        "label": label,
        "last_event": ev,
        "last_event_at": at.isoformat() + "Z" if at else None,
        "idle_seconds": idle,
        "assigned_to": ticket.assigned_to,
        "action": ticket.action,
        "confidence": ticket.llm_confidence,
        "resolution": ticket.resolution,
    }


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", ""))
    except Exception:
        return None


def is_paused(ticket, now=None) -> bool:
    """True when a ticket has a pending `pause_until` directive.

    Semantics (the last pause directive in the note list wins):
      * `pause_until` with a target time in the FUTURE  -> paused
      * `pause_until` followed by a later `pause_cleared` -> NOT paused
      * `pause_until` whose target time has already passed -> NOT paused
    Notes are appended in chronological order, so iterating in list order and
    remembering the last directive is equivalent to "latest pause_until > now
    and no later pause_cleared".
    """
    now = now or datetime.datetime.utcnow()
    directive = None  # ("pause_until", target_dt) | ("pause_cleared", None) | None
    for n in list_notes(ticket):
        if not isinstance(n, dict):
            continue
        ev = n.get("event") or ""
        if ev == "pause_until":
            directive = ("pause_until", _iso_ts(n.get("detail")))
        elif ev == "pause_cleared":
            directive = ("pause_cleared", None)
    if not directive or directive[0] != "pause_until":
        return False
    target = directive[1]
    return target is not None and target > now
