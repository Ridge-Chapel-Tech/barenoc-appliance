#!/usr/bin/env python3
"""Tests for the ticket progress-note endpoint (add_progress_note).

Run in-container:
    docker compose exec api python3 -m unittest test_tickets -v
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="tickets-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Ticket, User
from routes.tickets import (
    PROGRESS_NOTE_MAX_CHARS,
    ProgressNote,
    add_progress_note,
)


class ProgressNoteTest(unittest.TestCase):
    """The stored agent_progress note must not be sliced at 250/300 chars — a
    real pi answer round-trips whole, and a truncated note carries an
    ellipsis (08-17 pi-answer-truncation incident)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        db.query(User).delete()
        u = User(username="operator", hashed_password="x", role="operator")
        db.add(u)
        db.commit()
        db.close()

    def _make_ticket(self, ticket_id="TKT-PROG-1"):
        db = SessionLocal()
        t = Ticket(ticket_id=ticket_id, title="dnf check-update on laptop",
                   description="", priority="P3", status="in_progress",
                   source="manual")
        db.add(t)
        db.commit()
        db.close()

    def test_long_note_round_trips_uncut(self):
        # a real answer is > 250 chars; it must be stored whole, not sliced at
        # the old [:250]/[:300] caps.
        self._make_ticket()
        detail = (
            "dnf check-update results: a new kernel, Firefox, and several "
            "library updates are pending on the laptop. I would suggest "
            "applying them at the next maintenance window — the kernel update "
            "in particular needs a reboot to take effect, so it is best done "
            "when the machine is not in active use. The full list is in the "
            "logs attached to this ticket."
        )
        self.assertGreater(len(detail), 250)
        db = SessionLocal()
        r = add_progress_note("TKT-PROG-1", ProgressNote(detail=detail),
                              db=db, user=SimpleNamespace(role="operator"))
        db.commit()
        self.assertEqual(r["note"], detail)  # no ellipsis, no cut

        t = db.query(Ticket).filter(Ticket.ticket_id == "TKT-PROG-1").first()
        notes = json.loads(t.work_notes)
        self.assertEqual(notes[-1]["detail"], detail)
        self.assertEqual(notes[-1]["event"], "agent_progress")
        db.close()

    def test_over_cap_gets_ellipsis(self):
        self._make_ticket()
        detail = "x" * 2500
        db = SessionLocal()
        r = add_progress_note("TKT-PROG-1", ProgressNote(detail=detail),
                              db=db, user=SimpleNamespace(role="operator"))
        db.commit()
        self.assertTrue(r["note"].endswith("…"))
        self.assertLessEqual(len(r["note"]), PROGRESS_NOTE_MAX_CHARS + 1)
        db.close()

    def test_cap_is_2000(self):
        self.assertEqual(PROGRESS_NOTE_MAX_CHARS, 2000)

    def test_empty_note_rejected(self):
        from fastapi import HTTPException
        self._make_ticket()
        db = SessionLocal()
        with self.assertRaises(HTTPException):
            add_progress_note("TKT-PROG-1", ProgressNote(detail="   "),
                              db=db, user=SimpleNamespace(role="operator"))
        db.close()

    def test_missing_ticket_404(self):
        from fastapi import HTTPException
        db = SessionLocal()
        with self.assertRaises(HTTPException) as ctx:
            add_progress_note("TKT-NOPE", ProgressNote(detail="hi"),
                              db=db, user=SimpleNamespace(role="operator"))
        self.assertEqual(ctx.exception.status_code, 404)
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
