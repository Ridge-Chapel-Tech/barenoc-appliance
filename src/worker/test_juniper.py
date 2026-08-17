#!/usr/bin/env python3
"""Tests for the Juniper responder logic (worker side, Phase 1).

Pure-logic tests (no live DB / LLM): pause-directive semantics, deterministic
intake priority rules, queue-status stage derivation, and conduit directive
parsing.

    cd src/worker && python3 -m unittest test_juniper -v
"""

import os
import sys
import json
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta

_TMP = tempfile.mkdtemp(prefix="juniper-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))            # worker/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))  # api/ (queue_status, models…)

from queue_status import is_paused, derive_status  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402
from models import User, ChatMessage, Ticket, AuditLog  # noqa: E402
import juniper  # noqa: E402


def _ticket(work_notes, status="open", ticket_id="TKT-20260816-0001", **kw):
    return SimpleNamespace(work_notes=json.dumps(work_notes), status=status,
                           ticket_id=ticket_id, assigned_to=None, action=None,
                           llm_confidence=None, resolution=None, **kw)


def _note(event, detail, ts):
    return {"event": event, "detail": detail, "timestamp": ts, "actor": "juniper"}


class IsPausedTest(unittest.TestCase):
    """pause_until vs pause_cleared ordering; a past timestamp = not paused."""

    def test_future_pause_is_paused(self):
        future = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        t = _ticket([_note("pause_until", future, datetime.utcnow().isoformat())])
        self.assertTrue(is_paused(t))

    def test_past_pause_not_paused(self):
        past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        t = _ticket([_note("pause_until", past,
                           (datetime.utcnow() - timedelta(hours=2)).isoformat())])
        self.assertFalse(is_paused(t))

    def test_pause_then_clear_not_paused(self):
        future = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        t = _ticket([
            _note("pause_until", future,
                  (datetime.utcnow() - timedelta(hours=2)).isoformat()),
            _note("pause_cleared", "resumed",
                  (datetime.utcnow() - timedelta(hours=1)).isoformat()),
        ])
        self.assertFalse(is_paused(t))

    def test_clear_then_pause_is_paused(self):
        future = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        t = _ticket([
            _note("pause_cleared", "resumed",
                  (datetime.utcnow() - timedelta(hours=2)).isoformat()),
            _note("pause_until", future,
                  (datetime.utcnow() - timedelta(hours=1)).isoformat()),
        ])
        self.assertTrue(is_paused(t))

    def test_no_directives_not_paused(self):
        t = _ticket([_note("processing", "picked up", datetime.utcnow().isoformat())])
        self.assertFalse(is_paused(t))

    def test_unparseable_target_not_paused(self):
        t = _ticket([_note("pause_until", "not-a-time", datetime.utcnow().isoformat())])
        self.assertFalse(is_paused(t))


class IntakePriorityTest(unittest.TestCase):
    def test_install_is_p2(self):
        p, note = juniper.judge_intake_priority("I need Doom installed on my laptop")
        self.assertEqual(p, "P2")
        self.assertIn("install", note)

    def test_outage_is_p1(self):
        p, _ = juniper.judge_intake_priority("The internet is down!")
        self.assertEqual(p, "P1")

    def test_urgent_is_p1(self):
        p, _ = juniper.judge_intake_priority("URGENT: our whole network is offline")
        self.assertEqual(p, "P1")

    def test_security_is_p2(self):
        p, _ = juniper.judge_intake_priority("we have a virus on a workstation")
        self.assertEqual(p, "P2")

    def test_routine_update_is_p3(self):
        p, _ = juniper.judge_intake_priority("please update the firmware on the switch")
        self.assertEqual(p, "P3")

    def test_informational_is_p4(self):
        p, _ = juniper.judge_intake_priority("thanks for the help")
        self.assertEqual(p, "P4")


class DeriveStatusTest(unittest.TestCase):
    def test_processing_stage(self):
        now = datetime.utcnow()
        t = _ticket([_note("processing", "Picked up", now.isoformat())],
                    status="in_progress")
        st = derive_status(t, now=now)
        self.assertEqual(st["stage"], "working")
        self.assertEqual(st["label"], "Picked up — the technician is working on this")
        self.assertEqual(st["last_event"], "processing")
        self.assertIsNotNone(st["idle_seconds"])

    def test_no_activity_queued(self):
        t = _ticket([], status="open")
        st = derive_status(t)
        self.assertEqual(st["stage"], "waiting")
        self.assertEqual(st["label"], "No activity yet — queued")
        self.assertIsNone(st["idle_seconds"])

    def test_user_message_skipped_for_stage(self):
        now = datetime.utcnow()
        t = _ticket([
            _note("user_message", "hello", (now - timedelta(hours=2)).isoformat()),
            _note("agent_completed", "done", (now - timedelta(hours=1)).isoformat()),
        ], status="completed")
        st = derive_status(t, now=now)
        self.assertEqual(st["stage"], "done")
        self.assertEqual(st["label"], "Completed")
        self.assertIsNone(st["idle_seconds"])  # completed is not an active status

    def test_template_detail_fills(self):
        now = datetime.utcnow()
        t = _ticket([_note("customer_input", "please confirm the reboot",
                           now.isoformat())], status="customer_action")
        st = derive_status(t, now=now)
        self.assertEqual(st["stage"], "waiting")
        self.assertEqual(st["label"], "Waiting on you — please confirm the reboot")


class DirectiveTest(unittest.TestCase):
    def test_pause_clock_time(self):
        now = datetime(2026, 8, 16, 15, 0, 0)  # 3 PM -> 8 PM is today
        d = juniper.parse_directive("pause TKT-20260816-4509 until 8 PM", now=now)
        self.assertEqual(d["kind"], "pause")
        self.assertEqual(d["ticket_id"], "TKT-20260816-4509")
        self.assertEqual(d["label"], "20:00")
        self.assertEqual(d["target_dt"].hour, 20)
        self.assertEqual(d["target_dt"].day, 16)

    def test_pause_past_clock_rolls_to_tomorrow(self):
        now = datetime(2026, 8, 16, 21, 0, 0)  # 9 PM — "8 PM" already past
        d = juniper.parse_directive("pause TKT-20260816-4509 until 8 PM", now=now)
        self.assertEqual(d["label"], "20:00")
        self.assertEqual(d["target_dt"].day, 17)

    def test_pause_now_is_indefinite(self):
        now = datetime(2026, 8, 16, 12, 0, 0)
        d = juniper.parse_directive("pause TKT-20260816-4509 until now", now=now)
        self.assertEqual(d["kind"], "pause")
        self.assertEqual(d["label"], "now")
        self.assertGreater(d["target_dt"], now + timedelta(days=100))

    def test_resume(self):
        d = juniper.parse_directive("resume TKT-20260816-4509")
        self.assertEqual(d["kind"], "resume")
        self.assertEqual(d["ticket_id"], "TKT-20260816-4509")

    def test_note_to_technician(self):
        d = juniper.parse_directive("note to technician: please try the aux port")
        self.assertEqual(d["kind"], "note")
        self.assertEqual(d["message"], "please try the aux port")
        self.assertIsNone(d["ticket_id"])

    def test_note_to_technician_on_ticket(self):
        d = juniper.parse_directive("note to technician on TKT-20260816-4509: it's plugged in now")
        self.assertEqual(d["kind"], "note")
        self.assertEqual(d["ticket_id"], "TKT-20260816-4509")
        self.assertEqual(d["message"], "it's plugged in now")

    def test_not_a_directive(self):
        self.assertIsNone(juniper.parse_directive("what's happening?"))

    def test_close_with_id(self):
        d = juniper.parse_directive("close TKT-20260816-4509")
        self.assertEqual(d["kind"], "close")
        self.assertEqual(d["ticket_id"], "TKT-20260816-4509")

    def test_close_the_ticket_no_id(self):
        # The 08-17 live report: "verrified. close the ticket." must resolve
        # to a close directive (context fallback, no id).
        d = juniper.parse_directive("verrified. close the ticket.")
        self.assertEqual(d["kind"], "close")
        self.assertIsNone(d["ticket_id"])

    def test_close_this_ticket_no_id(self):
        d = juniper.parse_directive("close this ticket")
        self.assertEqual(d["kind"], "close")
        self.assertIsNone(d["ticket_id"])

    def test_close_the_ticket_prefers_explicit_id(self):
        d = juniper.parse_directive("close the ticket TKT-20260816-4509")
        self.assertEqual(d["kind"], "close")
        self.assertEqual(d["ticket_id"], "TKT-20260816-4509")

    def test_close_port_is_not_a_ticket_close(self):
        # "close this port" is an action request, not a ticket directive.
        self.assertIsNone(juniper.parse_directive("close this port on the switch"))


class CloseDirectiveHandlerTest(unittest.TestCase):
    """DB-backed close directive behavior (owner/operator/admin gated)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(ChatMessage).delete()
        db.query(AuditLog).delete()
        db.query(Ticket).delete()
        db.query(User).delete()
        db.commit()
        self.bot = User(username="juniper", display_name="Juniper",
                        hashed_password="x", role="admin", is_active=True, is_bot=True)
        self.owner = User(username="owner", display_name="Owner",
                          hashed_password="x", role="tenant", is_active=True)
        self.other = User(username="other", display_name="Other",
                          hashed_password="x", role="tenant", is_active=True)
        self.op = User(username="op", display_name="Operator",
                       hashed_password="x", role="operator", is_active=True)
        db.add_all([self.bot, self.owner, self.other, self.op])
        db.commit()
        db.close()

    def _make_ticket(self, submitter_username, ticket_id, status="open"):
        db = SessionLocal()
        sub = db.query(User).filter(User.username == submitter_username).first()
        t = Ticket(ticket_id=ticket_id, title="test ticket", description="d",
                   priority="P3", status=status, source="chat",
                   submitter_id=sub.id, work_notes="[]")
        db.add(t)
        db.commit()
        db.refresh(t)
        db.close()
        return t

    def _run(self, body, sender_username):
        db = SessionLocal()
        sender = db.query(User).filter(User.username == sender_username).first()
        bot = db.query(User).filter(User.is_bot == True).first()  # noqa: E712
        msg = ChatMessage(from_user_id=sender.id, to_user_id=bot.id, body=body)
        out = juniper.handle_message(db, bot, msg, sender)
        text = out.body
        db.close()
        return text

    def _ticket(self, ticket_id):
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        db.close()
        return t

    def test_owner_closes_own_ticket(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        text = self._run("close TKT-20260816-0001", "owner")
        self.assertEqual(text, "Done — TKT-20260816-0001 is closed.")
        t = self._ticket("TKT-20260816-0001")
        self.assertEqual(t.status, "closed")
        self.assertIsNotNone(t.resolved_at)
        self.assertEqual(t.assigned_to, "owner")  # who closed it

    def test_owner_closes_by_context_no_id(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        self._make_ticket("owner", "TKT-20260816-0002")
        text = self._run("verrified. close the ticket.", "owner")
        # most recent active ticket resolves
        self.assertEqual(text, "Done — TKT-20260816-0002 is closed.")
        self.assertEqual(self._ticket("TKT-20260816-0002").status, "closed")
        self.assertEqual(self._ticket("TKT-20260816-0001").status, "open")

    def test_non_owner_denied(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        text = self._run("close TKT-20260816-0001", "other")
        self.assertIn("only the ticket owner or an operator", text)
        self.assertEqual(self._ticket("TKT-20260816-0001").status, "open")

    def test_operator_can_close_others_ticket(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        text = self._run("close TKT-20260816-0001", "op")
        self.assertEqual(text, "Done — TKT-20260816-0001 is closed.")
        self.assertEqual(self._ticket("TKT-20260816-0001").status, "closed")

    def test_unknown_ticket_honest_reply(self):
        text = self._run("close TKT-20260816-9999", "owner")
        self.assertEqual(text, "I can't find TKT-20260816-9999.")

    def test_close_context_without_active_ticket(self):
        text = self._run("close the ticket", "owner")
        self.assertIn("I don't see an active ticket to close", text)

    def test_already_closed_reply(self):
        self._make_ticket("owner", "TKT-20260816-0001", status="closed")
        text = self._run("close TKT-20260816-0001", "owner")
        self.assertEqual(text, "TKT-20260816-0001 is already closed.")

    def test_close_writes_audit_event(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        self._run("close TKT-20260816-0001", "owner")
        db = SessionLocal()
        ev = db.query(AuditLog).filter(AuditLog.event_type == "ticket_closed",
                                       AuditLog.ticket_id == "TKT-20260816-0001").first()
        db.close()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.actor, "juniper")


if __name__ == "__main__":
    unittest.main(verbosity=2)
