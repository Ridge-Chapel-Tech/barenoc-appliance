"""Work notes helper for tickets."""

import json
import datetime


def parse_notes(raw) -> list:
    """Parse a ticket's ``work_notes`` field into a list of note dicts.

    Guards against the 08-20 ticket-readability corruption (#102): a
    double-encoded JSON string (``json.dumps(json.dumps(notes))``) stored in
    the field parses to a bare string, and iterating that string yields
    individual characters. This unwraps one level of double-encoding and
    ALWAYS returns a list — never a bare string — so neither readers nor the
    writer can propagate the corruption. Anything else malformed yields ``[]``.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, dict)]
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(value, list):
        return [n for n in value if isinstance(n, dict)]
    if isinstance(value, str):
        # One level of double-encoding: the string itself is the notes array
        # (or some other JSON). Unwrap it; anything else is junk -> [].
        try:
            inner = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(inner, list):
            return [n for n in inner if isinstance(n, dict)]
    return []


def add_note(ticket, event: str, detail: str, actor: str = "system"):
    """Append a work note to a ticket's work_notes JSON field.

    The field is always written back as a JSON array. A malformed existing
    value (e.g. the double-encoded string from #102) is recovered via
    ``parse_notes`` instead of crashing the note write (AttributeError on a
    bare string) or re-storing a bare string."""
    notes = parse_notes(ticket.work_notes)

    notes.append({
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "event": event,
        "detail": detail,
        "actor": actor,
    })

    # Keep only last 50 notes
    ticket.work_notes = json.dumps(notes[-50:])
    ticket.updated_at = datetime.datetime.utcnow()
