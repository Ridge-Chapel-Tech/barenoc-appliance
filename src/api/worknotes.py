"""Work notes helper for tickets."""

import json
import datetime
from typing import Optional


def add_note(ticket, event: str, detail: str, actor: str = "system"):
    """Append a work note to a ticket's work_notes JSON field."""
    notes = []
    if ticket.work_notes:
        try:
            notes = json.loads(ticket.work_notes)
        except (json.JSONDecodeError, TypeError):
            notes = []

    notes.append({
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "event": event,
        "detail": detail,
        "actor": actor,
    })

    # Keep only last 50 notes
    ticket.work_notes = json.dumps(notes[-50:])
    ticket.updated_at = datetime.datetime.utcnow()
