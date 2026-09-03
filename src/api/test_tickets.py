#!/usr/bin/env python3
"""Tests for the ticket progress-note endpoint (add_progress_note).

Run in-container:
    docker compose exec api python3 -m unittest test_tickets -v
"""

import json
import os
import tempfile
import unittest
import datetime
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="tickets-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Ticket, User
from worknotes import add_note, parse_notes
from schemas import TicketResponse
from routes.tickets import (
    PROGRESS_NOTE_MAX_CHARS,
    ProgressNote,
    add_progress_note,
    EngageRequest,
    engage_ticket,
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


class WorkNotesCorruptionGuardTest(unittest.TestCase):
    """Regression for #102: a 6.4k malformed work_notes string (double-encoded
    JSON) must never leave readers iterating characters or the writer storing a
    bare string. parse_notes unwraps one level of double-encoding; add_note
    recovers the field and always writes back a JSON array."""

    @staticmethod
    def _malformed_sample() -> "tuple[list, str]":
        notes = [
            {
                "timestamp": "2026-08-20T12:00:00",
                "event": "agent_progress",
                "detail": ("Lily is checking the laptop now — gathering the "
                           "update state, comparing package versions, and "
                           "preparing a safe upgrade plan for the next window. "),
                "actor": "Lily",
            }
            for _ in range(30)
        ]
        malformed = json.dumps(json.dumps(notes))  # the #102 double-encode
        return notes, malformed

    def test_malformed_sample_is_a_bare_string_when_parsed_naively(self):
        notes, malformed = self._malformed_sample()
        self.assertGreater(len(malformed), 6000)          # the 6.4k scale
        self.assertIsInstance(json.loads(malformed), str)  # the corruption shape

    def test_parse_notes_unwraps_double_encoded_string(self):
        notes, malformed = self._malformed_sample()
        self.assertEqual(parse_notes(malformed), notes)

    def test_parse_notes_never_returns_a_bare_string(self):
        _, malformed = self._malformed_sample()
        for junk in (malformed, "not json", "", None, "[]", "{}", '["a"]'):
            result = parse_notes(junk)
            self.assertIsInstance(result, list, f"junk={junk!r} -> {result!r}")

    def test_add_note_recovers_malformed_field_and_writes_a_list(self):
        notes, malformed = self._malformed_sample()
        ticket = SimpleNamespace(work_notes=malformed, updated_at=None)
        add_note(ticket, "user_message", "hi, is it fixed?", actor="owner")

        stored = json.loads(ticket.work_notes)
        self.assertIsInstance(stored, list)
        self.assertEqual(stored[:-1], notes)          # history recovered intact
        self.assertEqual(stored[-1]["event"], "user_message")
        self.assertEqual(stored[-1]["detail"], "hi, is it fixed?")

    def test_add_note_resets_junk_field_to_a_list(self):
        ticket = SimpleNamespace(work_notes="not json", updated_at=None)
        add_note(ticket, "processing", "worker picked it up", actor="system")
        stored = json.loads(ticket.work_notes)
        self.assertIsInstance(stored, list)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["event"], "processing")
        self.assertEqual(stored[0]["detail"], "worker picked it up")
        self.assertEqual(stored[0]["actor"], "system")
        self.assertIn("timestamp", stored[0])

    def test_ticket_response_normalizes_malformed_work_notes(self):
        _, malformed = self._malformed_sample()
        now = datetime.datetime.utcnow()
        resp = TicketResponse(
            id=1, ticket_id="TKT-20260820-1294", title="update laptop",
            priority="P3", status="open", source="chat", work_notes=malformed,
            created_at=now, updated_at=now,
        )
        out = resp.model_dump()["work_notes"]
        self.assertEqual(json.loads(out), json.loads(json.loads(malformed)))
        self.assertIsInstance(json.loads(out), list)


class EngageTicketTest(unittest.TestCase):
    """The Engage/Act-on-this affordance: posting an instruction adds a
    customer reply (user_message note) and re-queues the ticket — the worker's
    existing chat-intake re-dispatch path consumes it (no parallel dispatch)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        db.query(User).delete()
        self.owner = User(username="owner", hashed_password="x", role="tenant")
        self.other = User(username="other", hashed_password="x", role="tenant")
        self.tech = User(username="tech", hashed_password="x", role="technician")
        db.add_all([self.owner, self.other, self.tech])
        db.commit()
        db.close()

    def _mk(self, status):
        db = SessionLocal()
        owner = db.query(User).filter(User.username == "owner").first()
        t = Ticket(ticket_id="TKT-ENG-1", title="plex server", description="",
                   priority="P3", status=status, source="chat",
                   submitter_id=owner.id, work_notes="[]",
                   resolution="waiting on you" if status == "customer_action" else None)
        db.add(t)
        db.commit()
        db.close()

    def _engage(self, ticket_id, instruction, username="owner"):
        db = SessionLocal()
        u = db.query(User).filter(User.username == username).first()
        engage_ticket(ticket_id, EngageRequest(instruction=instruction),
                      db=db, user=u)
        db.close()
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        notes = json.loads(t.work_notes) if t else []
        db.close()
        return t, notes

    def test_customer_action_moves_to_open_and_adds_reply(self):
        self._mk("customer_action")
        t, notes = self._engage("TKT-ENG-1", "please update my plex server")
        self.assertEqual(t.status, "open")
        self.assertIsNone(t.resolution)
        self.assertEqual(notes[-1]["event"], "user_message")
        self.assertEqual(notes[-1]["detail"], "please update my plex server")
        self.assertEqual(notes[-1]["actor"], "owner")

    def test_open_keeps_status_and_adds_reply(self):
        self._mk("open")
        t, notes = self._engage("TKT-ENG-1", "do it now")
        self.assertEqual(t.status, "open")
        self.assertEqual(notes[-1]["event"], "user_message")
        self.assertEqual(notes[-1]["detail"], "do it now")

    def test_in_progress_keeps_status_and_adds_reply(self):
        self._mk("in_progress")
        t, notes = self._engage("TKT-ENG-1", "also check port 5")
        self.assertEqual(t.status, "in_progress")
        self.assertEqual(notes[-1]["event"], "user_message")

    def test_escalated_rejected(self):
        from fastapi import HTTPException
        self._mk("escalated")
        with self.assertRaises(HTTPException) as ctx:
            self._engage("TKT-ENG-1", "try again")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_ticket_404(self):
        from fastapi import HTTPException
        db = SessionLocal()
        u = db.query(User).filter(User.username == "owner").first()
        with self.assertRaises(HTTPException) as ctx:
            engage_ticket("TKT-NOPE", EngageRequest(instruction="hi"),
                          db=db, user=u)
        self.assertEqual(ctx.exception.status_code, 404)
        db.close()

    def test_empty_instruction_rejected(self):
        from fastapi import HTTPException
        self._mk("open")
        with self.assertRaises(HTTPException) as ctx:
            self._engage("TKT-ENG-1", "   ")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_customer_cannot_engage_others_ticket(self):
        from fastapi import HTTPException
        self._mk("open")
        with self.assertRaises(HTTPException) as ctx:
            self._engage("TKT-ENG-1", "do it", username="other")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
