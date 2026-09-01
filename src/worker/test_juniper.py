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
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="juniper-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))            # worker/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))  # api/ (queue_status, models…)

from queue_status import is_paused, derive_status  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402
from models import User, ChatMessage, Ticket, AuditLog, PendingAction, Device  # noqa: E402
import juniper  # noqa: E402
import device_resolver  # noqa: E402


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


class IntakeTitleTest(unittest.TestCase):
    """Interpreted chat-ticket titles: LLM summary preferred, first-sentence
    heuristic fallback — never blocks ticket creation."""

    def test_heuristic_first_sentence_preserves_url(self):
        # Dots INSIDE the URL are not sentence breaks; the period before
        # "Can you…" is. The raw-message title from the 08-30 ticket.
        title = juniper._heuristic_title(
            "I see my plex server is on http://192.168.4.13/. Can you update it?")
        self.assertEqual(title, "I See My Plex Server Is On http://192.168.4.13/")
        self.assertLessEqual(len(title), 80)

    def test_heuristic_trims_to_80(self):
        long_text = (
            "Please configure a recurring backup schedule for the network "
            "attached storage device in the server closet and also the other "
            "one under the stairs next to the router shelf"
        ) * 3
        title = juniper._heuristic_title(long_text)
        self.assertLessEqual(len(title), 80)

    def test_heuristic_empty_input(self):
        self.assertEqual(juniper._heuristic_title(""), "Support request")
        self.assertEqual(juniper._heuristic_title("   "), "Support request")

    def test_llm_title_preferred(self):
        with patch("llm_client.generate_title", return_value="Plex server check"):
            title = juniper._intake_title(
                "I see my plex server is on http://192.168.4.13/")
        self.assertEqual(title, "Plex server check")

    def test_llm_down_falls_back_to_heuristic(self):
        with patch("llm_client.generate_title", return_value=None):
            title = juniper._intake_title(
                "I see my plex server is on http://192.168.4.13/")
        self.assertEqual(title, "I See My Plex Server Is On http://192.168.4.13/")


class IntakeTitleDbTest(unittest.TestCase):
    """The intake path stores the interpreted (short) title, not the raw
    message (chat message -> ticket title)."""

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
        self.sender = User(username="owner", display_name="Owner",
                           hashed_password="x", role="tenant", is_active=True)
        db.add_all([self.bot, self.sender])
        db.commit()
        db.close()

    def _msg(self, body):
        db = SessionLocal()
        bot = db.query(User).filter(User.is_bot == True).first()  # noqa: E712
        sender = db.query(User).filter(User.username == "owner").first()
        m = ChatMessage(from_user_id=sender.id, to_user_id=bot.id, body=body)
        db.add(m)
        db.commit()
        db.refresh(m)
        db.close()
        return m

    def _run_intake(self, body):
        db = SessionLocal()
        bot = db.query(User).filter(User.is_bot == True).first()  # noqa: E712
        sender = db.query(User).filter(User.username == "owner").first()
        msg = self._msg(body)
        with patch("llm_client.generate_title", return_value="Firmware update for switch"):
            juniper._handle_intake(db, bot, msg, sender, msg.body)
        db.close()

    def test_intake_uses_llm_title(self):
        self._run_intake("please update the firmware on the switch")
        db = SessionLocal()
        t = db.query(Ticket).order_by(Ticket.id.desc()).first()
        db.close()
        self.assertEqual(t.title, "Firmware update for switch")
        self.assertEqual(t.source, "chat")

    def test_intake_falls_back_to_heuristic_when_llm_down(self):
        db = SessionLocal()
        bot = db.query(User).filter(User.is_bot == True).first()  # noqa: E712
        sender = db.query(User).filter(User.username == "owner").first()
        msg = self._msg("please update the firmware on the switch")
        with patch("llm_client.generate_title", return_value=None):
            juniper._handle_intake(db, bot, msg, sender, msg.body)
        db.close()

        db = SessionLocal()
        t = db.query(Ticket).order_by(Ticket.id.desc()).first()
        db.close()
        self.assertEqual(t.title, "Please Update The Firmware On The Switch")
        self.assertLessEqual(len(t.title), 80)


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


class CloseIntentTest(unittest.TestCase):
    """close_intent / ack_intent — the ticket-thread close/ack detectors."""

    def test_close_bare(self):
        self.assertTrue(juniper.close_intent("close"))

    def test_close_yes_please(self):
        self.assertTrue(juniper.close_intent("yes, please close"))

    def test_close_yes_please_no_comma(self):
        self.assertTrue(juniper.close_intent("yes please close"))

    def test_close_the_ticket(self):
        self.assertTrue(juniper.close_intent("close the ticket"))

    def test_close_this_ticket(self):
        self.assertTrue(juniper.close_intent("close this ticket"))

    def test_close_you_can_close_it(self):
        self.assertTrue(juniper.close_intent("you can close it"))

    def test_close_done_thanks_close_it(self):
        self.assertTrue(juniper.close_intent("done, thanks — close it"))

    def test_close_go_ahead_and_close(self):
        self.assertTrue(juniper.close_intent("go ahead and close"))

    def test_close_please_close_the_ticket(self):
        self.assertTrue(juniper.close_intent("please close the ticket"))

    def test_close_confirmed_close_it(self):
        self.assertTrue(juniper.close_intent("confirmed, close it"))

    def test_close_port_is_not_a_ticket_close(self):
        self.assertFalse(juniper.close_intent("close this port on the switch"))

    def test_close_port_5_is_not_a_ticket_close(self):
        self.assertFalse(juniper.close_intent("please close port 5"))

    def test_close_after_past_tense_narration(self):
        # 09-01 monitor tickets: the user resolved it themselves, then closed.
        self.assertTrue(juniper.close_intent("I moved the link to port 2. close"))
        self.assertTrue(juniper.close_intent("moved the link from 3 to 2, close"))
        self.assertTrue(juniper.close_intent("I fixed it, please close"))

    def test_close_with_trailing_work_is_not_a_close(self):
        # A close directive followed by a new-work request must dispatch.
        self.assertFalse(juniper.close_intent("close and run all updates"))
        self.assertFalse(juniper.close_intent("please close and check the devices"))
        self.assertFalse(juniper.close_intent("close the ticket and reboot the gateway"))

    def test_close_mixed_request_is_not_a_pure_close(self):
        # A new work request that also mentions closing must still dispatch.
        self.assertFalse(juniper.close_intent("please install the update and close the ticket"))

    def test_close_not_how_to(self):
        # "how do I close…" is a question, not a close request.
        self.assertFalse(juniper.close_intent("how do I close the ticket"))


    def test_ack_yes_family(self):
        """The 08-20 TKT-1827 case: 'yes. thank you' (and the yes-family) are
        acknowledgments — they must ACK, never re-dispatch a fresh session."""
        for t in ("yes. thank you", "yes", "yes please", "yep thanks",
                  "yeah, that works", "sure, sounds good", "yup"):
            self.assertTrue(juniper.ack_intent(t), t)
        # an ack with NEW work is not an ack ("yes, and also check port 5")
        self.assertFalse(juniper.ack_intent("yes, and also check port 5"))

    def test_ack_thanks(self):
        self.assertTrue(juniper.ack_intent("thanks"))

    def test_ack_thank_you(self):
        self.assertTrue(juniper.ack_intent("thank you"))

    def test_ack_ok(self):
        self.assertTrue(juniper.ack_intent("ok"))

    def test_ack_got_it(self):
        self.assertTrue(juniper.ack_intent("got it"))

    def test_ack_sounds_good(self):
        self.assertTrue(juniper.ack_intent("sounds good"))

    def test_ack_confirmed(self):
        self.assertTrue(juniper.ack_intent("confirmed"))

    def test_ack_that_works(self):
        self.assertTrue(juniper.ack_intent("that works"))

    def test_ack_its_fixed(self):
        self.assertTrue(juniper.ack_intent("it's fixed"))

    def test_ack_not_a_new_request(self):
        self.assertFalse(juniper.ack_intent("please run updates"))

    def test_ack_not_close(self):
        # ack_intent only handles thanks/ack — a close is close_intent's job.
        self.assertFalse(juniper.ack_intent("yes, please close"))


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
        self.tech = User(username="tech", display_name="Tech",
                         hashed_password="x", role="technician", is_active=True)
        db.add_all([self.bot, self.owner, self.other, self.op, self.tech])
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

    def test_non_owner_customer_confirm_routed_to_requester(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        text = self._run("close TKT-20260816-0001", "other")
        self.assertIn("waiting on", text.lower())
        self.assertIn("owner", text)
        self.assertEqual(self._ticket("TKT-20260816-0001").status, "open")

    def test_operator_can_close_others_ticket(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        text = self._run("close TKT-20260816-0001", "op")
        self.assertEqual(text, "Done — TKT-20260816-0001 is closed.")
        self.assertEqual(self._ticket("TKT-20260816-0001").status, "closed")

    def test_technician_can_close_others_ticket(self):
        self._make_ticket("owner", "TKT-20260816-0001")
        text = self._run("close TKT-20260816-0001", "tech")
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


class PendingItemsContextTest(unittest.TestCase):
    """Role-aware pending-items context + per-user front-desk isolation."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(ChatMessage).delete()
        db.query(AuditLog).delete()
        db.query(PendingAction).delete()
        db.query(Ticket).delete()
        db.query(User).delete()
        db.commit()
        self.bot = User(username="juniper", display_name="Juniper",
                        hashed_password="x", role="admin", is_active=True, is_bot=True)
        self.alice = User(username="alice", display_name="Alice",
                          hashed_password="x", role="user", is_active=True)
        self.bob = User(username="bob", display_name="Bob",
                        hashed_password="x", role="user", is_active=True)
        self.tech = User(username="tech", display_name="Tech",
                         hashed_password="x", role="technician", is_active=True)
        self.admin = User(username="admin2", display_name="Admin",
                          hashed_password="x", role="admin", is_active=True)
        db.add_all([self.bot, self.alice, self.bob, self.tech, self.admin])
        db.commit()
        db.close()

    def _ticket(self, submitter_username, status, ticket_id):
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

    def _pending(self, kind="approval", required_role="technician", status="pending"):
        db = SessionLocal()
        a = PendingAction(kind=kind, title="Firmware upgrade", detail="d",
                          device_name="UCG", device_type="gateway",
                          firmware_from="1.0", firmware_to="2.0",
                          status=status, required_role=required_role, extra={})
        db.add(a)
        db.commit()
        db.refresh(a)
        db.close()
        return a

    def _user(self, username):
        db = SessionLocal()
        u = db.query(User).filter(User.username == username).first()
        db.close()
        return u

    def _run(self, body, sender_username):
        db = SessionLocal()
        sender = db.query(User).filter(User.username == sender_username).first()
        bot = db.query(User).filter(User.is_bot == True).first()  # noqa: E712
        msg = ChatMessage(from_user_id=sender.id, to_user_id=bot.id, body=body)
        out = juniper.handle_message(db, bot, msg, sender)
        text = out.body
        db.close()
        return text

    def test_customer_pending_is_only_their_own(self):
        self._ticket("alice", "customer_action", "TKT-20260818-0001")
        ctx = juniper.pending_context(SessionLocal(), self._user("alice"))
        self.assertIn("TKT-20260818-0001", ctx)
        ctx_bob = juniper.pending_context(SessionLocal(), self._user("bob"))
        self.assertNotIn("TKT-20260818-0001", ctx_bob)

    def test_customer_never_sees_other_users_items(self):
        # alice has an awaiting ticket; bob must never see it in his context.
        self._ticket("alice", "customer_action", "TKT-20260818-0001")
        p = juniper.pending_items(SessionLocal(), self._user("bob"))
        self.assertEqual(p["tickets_awaiting_verification"], [])
        self.assertEqual(p["escalations"], [])
        self.assertEqual(p["firmware_approvals"], [])

    def test_tech_sees_escalations_and_approvals_when_visible(self):
        self._ticket("alice", "escalated", "TKT-20260818-0002")
        pa = self._pending(kind="approval", required_role="technician")
        with patch("llm_providers.read_env_file",
                   return_value={"FIRMWARE_TECH_VISIBILITY": "true"}):
            p = juniper.pending_items(SessionLocal(), self._user("tech"))
        self.assertEqual([e.ticket_id for e in p["escalations"]], ["TKT-20260818-0002"])
        self.assertEqual([a.id for a in p["firmware_approvals"]], [pa.id])

    def test_tech_visibility_off_hides_firmware_but_keeps_escalations(self):
        self._ticket("alice", "escalated", "TKT-20260818-0002")
        self._pending(kind="approval", required_role="technician")
        with patch("llm_providers.read_env_file", return_value={}):
            p = juniper.pending_items(SessionLocal(), self._user("tech"))
        self.assertEqual([e.ticket_id for e in p["escalations"]], ["TKT-20260818-0002"])
        self.assertEqual(p["firmware_approvals"], [])

    def test_gateway_approval_admin_only_regardless(self):
        pa = self._pending(kind="approval", required_role="admin")
        with patch("llm_providers.read_env_file",
                   return_value={"FIRMWARE_TECH_VISIBILITY": "true"}):
            p_tech = juniper.pending_items(SessionLocal(), self._user("tech"))
        self.assertEqual(p_tech["firmware_approvals"], [])
        p_admin = juniper.pending_items(SessionLocal(), self._user("admin2"))
        self.assertEqual([a.id for a in p_admin["firmware_approvals"]], [pa.id])

    def test_greeting_includes_summary_lines(self):
        self._ticket("alice", "customer_action", "TKT-20260818-0001")
        g = juniper.front_desk_greeting(SessionLocal(), self._user("alice"))
        self.assertIn("1 ticket(s) awaiting your verification", g)

    def test_greeting_tech_includes_escalation_and_approval_counts(self):
        self._ticket("alice", "escalated", "TKT-20260818-0002")
        self._pending(kind="approval", required_role="technician")
        with patch("llm_providers.read_env_file",
                   return_value={"FIRMWARE_TECH_VISIBILITY": "true"}):
            g = juniper.front_desk_greeting(SessionLocal(), self._user("tech"))
        self.assertIn("1 escalation(s) requiring review", g)
        self.assertIn("1 pending action approval(s) in your scope", g)

    def test_handle_message_pending_lists_items(self):
        self._ticket("alice", "customer_action", "TKT-20260818-0001")
        text = self._run("pending", "alice")
        self.assertIn("TKT-20260818-0001", text)

    def test_approve_pending_action(self):
        pa = self._pending(kind="approval", required_role="technician")
        with patch("llm_providers.read_env_file",
                   return_value={"FIRMWARE_TECH_VISIBILITY": "true"}):
            text = self._run(f"approve #{pa.id}", "tech")
        self.assertIn("Approved", text)
        db = SessionLocal()
        self.assertEqual(db.query(PendingAction).get(pa.id).status, "approved")
        db.close()

    def test_approve_denied_without_visibility(self):
        pa = self._pending(kind="approval", required_role="technician")
        with patch("llm_providers.read_env_file", return_value={}):
            text = self._run(f"approve #{pa.id}", "tech")
        self.assertIn("outside your scope", text)
        db = SessionLocal()
        self.assertEqual(db.query(PendingAction).get(pa.id).status, "pending")
        db.close()

    def test_approve_gateway_denied_for_tech(self):
        pa = self._pending(kind="approval", required_role="admin")
        with patch("llm_providers.read_env_file",
                   return_value={"FIRMWARE_TECH_VISIBILITY": "true"}):
            text = self._run(f"approve #{pa.id}", "tech")
        self.assertIn("outside your scope", text)

    def test_resolve_escalation(self):
        pa = self._pending(kind="escalation", required_role="technician")
        with patch("llm_providers.read_env_file",
                   return_value={"FIRMWARE_TECH_VISIBILITY": "true"}):
            text = self._run(f"resolve #{pa.id}", "tech")
        self.assertIn("Resolved", text)
        db = SessionLocal()
        self.assertEqual(db.query(PendingAction).get(pa.id).status, "resolved")
        db.close()


class DeviceBindingTest(unittest.TestCase):
    """Part A: chat tickets bind to their target device (name/hostname/IP).

    "update my plex" binds to the plex device; "hello" binds nothing;
    ambiguous references bind nothing (never guess wrong); the bound ticket's
    context names the device.
    """

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(ChatMessage).delete()
        db.query(AuditLog).delete()
        db.query(Ticket).delete()
        db.query(Device).delete()
        db.query(User).delete()
        db.commit()
        self.bot = User(username="juniper", display_name="Juniper",
                        hashed_password="x", role="admin", is_active=True, is_bot=True)
        self.owner = User(username="owner", display_name="Owner",
                          hashed_password="x", role="tenant", is_active=True)
        db.add_all([self.bot, self.owner])
        db.commit()
        # Capture ids BEFORE the session closes (a closed-session object's
        # attribute access raises DetachedInstanceError on the runner).
        self.bot_id = self.bot.id
        self.owner_id = self.owner.id
        db.close()

    def _device(self, name, agent=False, hostname=None, ip=None):
        db = SessionLocal()
        d = Device(name=name, hostname=hostname or name,
                   ip_address=ip or f"192.0.2.{len(name) % 250}",
                   device_type="server", claimed=True, status="online",
                   adoption_method="agent" if agent else "ssh",
                   agent_version="0.2.0" if agent else None)
        db.add(d)
        db.commit()
        db.refresh(d)
        db.close()
        return d

    def _run(self, body, sender_username):
        db = SessionLocal()
        sender = db.query(User).filter(User.username == sender_username).first()
        bot = db.query(User).filter(User.is_bot == True).first()  # noqa: E712
        msg = ChatMessage(from_user_id=sender.id, to_user_id=bot.id, body=body)
        out = juniper.handle_message(db, bot, msg, sender)
        body = out.body  # read BEFORE db.close() — mapped attrs expire on close
        db.close()
        return body

    def _latest_ticket(self):
        """Return the latest ticket's loaded fields (never a detached instance —
        attribute access on a closed-session object raises DetachedInstanceError)."""
        from types import SimpleNamespace
        db = SessionLocal()
        try:
            t = db.query(Ticket).order_by(Ticket.id.desc()).first()
            if t is None:
                return None
            return SimpleNamespace(
                id=t.id, ticket_id=t.ticket_id, source=t.source,
                target_device_id=t.target_device_id, status=t.status,
                action=t.action, title=t.title)
        finally:
            db.close()

    # ── resolver ─────────────────────────────────────────────────────────
    def test_resolve_single_plex_binds(self):
        plex = self._device("plex", agent=True)
        db = SessionLocal()
        got = device_resolver.resolve_device_from_text(db, "update my plex server")
        db.close()
        self.assertIsNotNone(got)
        self.assertEqual(got.id, plex.id)

    def test_resolve_hello_no_bind(self):
        self._device("plex", agent=True)
        db = SessionLocal()
        got = device_resolver.resolve_device_from_text(db, "hello")
        db.close()
        self.assertIsNone(got)

    def test_resolve_ambiguous_no_bind(self):
        # Two distinct records both matching the text, neither clearly the
        # target -> None (never guess wrong).
        self._device("plex", agent=True)
        self._device("plex", agent=True)
        db = SessionLocal()
        got = device_resolver.resolve_device_from_text(db, "update plex")
        db.close()
        self.assertIsNone(got)

    def test_resolve_prefers_agent_on_tie(self):
        # The Plex 2-record case: same name, one agent-managed, one SSH.
        ssh = self._device("plex", agent=False)
        agent = self._device("plex", agent=True)
        db = SessionLocal()
        got = device_resolver.resolve_device_from_text(db, "update my plex server")
        db.close()
        self.assertIsNotNone(got)
        self.assertEqual(got.id, agent.id)
        self.assertNotEqual(got.id, ssh.id)

    def test_resolve_by_ip(self):
        d = self._device("nas", agent=True, ip="192.168.4.50")
        db = SessionLocal()
        got = device_resolver.resolve_device_from_text(db, "update 192.168.4.50")
        db.close()
        self.assertIsNotNone(got)
        self.assertEqual(got.id, d.id)

    # ── intake binding ───────────────────────────────────────────────────
    def test_intake_update_plex_binds(self):
        plex = self._device("plex", agent=True)
        self._run("update my plex server", "owner")
        t = self._latest_ticket()
        self.assertIsNotNone(t)
        self.assertEqual(t.source, "chat")
        self.assertEqual(t.target_device_id, plex.id)

    def test_intake_hello_no_bind(self):
        self._device("plex", agent=True)
        before = self._latest_ticket()
        self._run("hello", "owner")
        after = self._latest_ticket()
        # A greeting is answered conversationally — no new ticket is created.
        self.assertEqual(before, after)

    def test_intake_ambiguous_no_bind(self):
        self._device("plex", agent=True)
        self._device("plex", agent=True)
        self._run("update plex", "owner")
        t = self._latest_ticket()
        self.assertIsNotNone(t)
        self.assertIsNone(t.target_device_id)  # never guess wrong

    # ── context names the device ─────────────────────────────────────────
    def test_bound_device_context_names_device(self):
        plex = self._device("plex", agent=True)
        import main as worker
        ctx = worker._bound_device_context(plex)
        self.assertIn("plex", ctx)
        self.assertIn("agent-managed", ctx)

    def test_pi_task_context_names_bound_device(self):
        plex = self._device("plex", agent=True)
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-20260830-0001", title="update my plex server",
                   description="update my plex server", priority="P3",
                   status="open", source="chat", submitter_id=self.owner_id,
                   target_device_id=plex.id, work_notes="[]")
        db.add(t)
        db.commit()
        import main as worker
        ctx = worker._pi_task_context(db, t, "update my plex server")
        db.close()
        self.assertIn("Target device for this ticket", ctx)
        self.assertIn("plex", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
