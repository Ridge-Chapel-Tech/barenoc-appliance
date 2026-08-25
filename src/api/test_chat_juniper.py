#!/usr/bin/env python3
"""Tests for the Juniper bot user + bot messaging (Phase 1, design §2).

Covers:
  1. chat_send to the bot user returns sent (a real ChatMessage row).
  2. chat_send to a non-user still 404s (bots are the ONLY exception).
  3. ensure_juniper_bot seeds idempotently (username = configured name
     lowercased, is_bot=True, is_active=True).
  4. Route binding (the 2026-08-16 lesson): the chat send + ticket status
     decorators bind to the intended endpoints.

    cd src/api && python3 -m unittest test_chat_juniper -v
"""

import os
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="chat-juniper-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from fastapi import HTTPException  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402
from models import User, ChatMessage  # noqa: E402
from auth import hash_password  # noqa: E402
import main as api_main  # noqa: E402
from routes.chat import chat_send, ChatSend, chat_messages, chat_mark_read, ChatMarkRead  # noqa: E402


def _user(username, role="tenant", bot=False):
    db = SessionLocal()
    u = User(username=username, display_name=username.title(),
             hashed_password=hash_password("pw"), role=role,
             is_active=True, is_bot=bot)
    db.add(u)
    db.commit()
    db.refresh(u)
    db.close()
    return u


class BotMessagingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(ChatMessage).delete()
        db.query(User).delete()
        db.commit()
        db.close()

    def test_chat_send_to_bot_user_returns_sent(self):
        bot = _user("juniper", bot=True)
        me = _user("alice")
        db = SessionLocal()
        r = chat_send(data=ChatSend(to_username="juniper", body="hi juniper"),
                      db=db, user=me)
        self.assertEqual(r["status"], "sent")
        row = db.query(ChatMessage).filter(ChatMessage.to_user_id == bot.id).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.body, "hi juniper")
        db.close()

    def test_chat_send_case_insensitive_bot_name(self):
        _user("juniper", bot=True)
        me = _user("alice")
        db = SessionLocal()
        r = chat_send(data=ChatSend(to_username="Juniper", body="hi"), db=db, user=me)
        self.assertEqual(r["status"], "sent")
        db.close()

    def test_chat_send_unknown_user_still_404(self):
        me = _user("alice")
        db = SessionLocal()
        with self.assertRaises(HTTPException) as ctx:
            chat_send(data=ChatSend(to_username="ghost", body="hi"), db=db, user=me)
        self.assertEqual(ctx.exception.status_code, 404)
        db.close()


class ChatReadMarkingTest(unittest.TestCase):
    """Read-marking was a state change on GET /messages (CSRF-visible via the
    session cookie). It is now split: GET is read-only, POST /messages/read
    marks the thread read."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(ChatMessage).delete()
        db.query(User).delete()
        db.commit()
        db.close()
        self.bot = _user("juniper", bot=True)
        self.me = _user("alice")

    def tearDown(self):
        # Leave the shared DB clean — test_roles (same process, run_tests.sh
        # convention) deletes Ticket/Device/User but NOT ChatMessage, so a
        # leftover thread row would trip its FK constraint + a SQLite lock.
        db = SessionLocal()
        db.query(ChatMessage).delete()
        db.query(User).delete()
        db.commit()
        db.close()

    def _thread(self, from_user, to_user, body="hi"):
        db = SessionLocal()
        m = ChatMessage(from_user_id=from_user.id, to_user_id=to_user.id, body=body)
        db.add(m)
        db.commit()
        db.refresh(m)
        db.close()
        return m

    def test_get_messages_is_read_only(self):
        m = self._thread(self.bot, self.me)
        db = SessionLocal()
        chat_messages(with_username="juniper", db=db, user=self.me)
        db.expire_all()
        row = db.query(ChatMessage).get(m.id)
        self.assertIsNone(row.read_at, "GET /messages must not mark messages read")
        db.close()

    def test_post_mark_read_writes_read_at(self):
        m = self._thread(self.bot, self.me)
        db = SessionLocal()
        r = chat_mark_read(data=ChatMarkRead(with_username="juniper"), db=db, user=self.me)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["marked"], 1)
        db.expire_all()
        row = db.query(ChatMessage).get(m.id)
        self.assertIsNotNone(row.read_at, "POST /messages/read must mark the message read")
        db.close()

    def test_post_mark_read_is_idempotent(self):
        self._thread(self.bot, self.me)
        db = SessionLocal()
        chat_mark_read(data=ChatMarkRead(with_username="juniper"), db=db, user=self.me)
        r2 = chat_mark_read(data=ChatMarkRead(with_username="juniper"), db=db, user=self.me)
        self.assertEqual(r2["marked"], 0)
        db.close()


class BotSeedTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(User).delete()
        db.commit()
        db.close()

    def test_bot_seeded_idempotently(self):
        with patch("llm_providers.read_env_file", return_value={}):
            os.environ.pop("BOT_QUEUE_MANAGER_NAME", None)
            api_main.ensure_juniper_bot()
            api_main.ensure_juniper_bot()
        db = SessionLocal()
        bots = db.query(User).filter(User.is_bot == True).all()  # noqa: E712
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0].username, "juniper")
        self.assertEqual(bots[0].display_name, "Juniper")
        self.assertTrue(bots[0].is_active)
        db.close()

    def test_bot_username_lowercases_configured_name(self):
        with patch("llm_providers.read_env_file",
                   return_value={"BOT_QUEUE_MANAGER_NAME": "FrontDesk"}):
            api_main.ensure_juniper_bot()
        db = SessionLocal()
        bot = db.query(User).filter(User.is_bot == True).first()  # noqa: E712
        self.assertEqual(bot.username, "frontdesk")
        self.assertEqual(bot.display_name, "FrontDesk")
        db.close()


class RouteBindingTest(unittest.TestCase):
    """Route-level guard (2026-08-16 lesson): decorators must bind to the
    intended endpoints — a misbound decorator 422s for a whole release day."""

    def test_chat_send_and_ticket_status_routes_bind(self):
        from fastapi.testclient import TestClient
        from main import app

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/chat/messages"
                     and "GET" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "chat_messages")

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/chat/messages"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "chat_send")

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/chat/messages/read"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "chat_mark_read")

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/tickets/{ticket_id}/status"
                     and "GET" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "ticket_status")

        c = TestClient(app)
        r = c.post("/api/v1/chat/messages",
                   json={"to_username": "juniper", "body": "hi"})
        self.assertNotEqual(r.status_code, 422, f"route misbound: {r.text}")
        self.assertEqual(r.status_code, 401)

        r = c.get("/api/v1/tickets/TKT-20260816-0001/status")
        self.assertNotEqual(r.status_code, 422, f"route misbound: {r.text}")
        self.assertEqual(r.status_code, 401)


class TicketStatusActionTest(unittest.TestCase):
    """ticket_status — the read-only action that answers 'status on TKT-…'."""

    def test_validate_action_accepts(self):
        from action_validator import validate_action
        self.assertTrue(validate_action("ticket_status")[0])

    def test_validate_params_accepts_good_tkt(self):
        from action_validator import validate_params
        ok, _ = validate_params("ticket_status",
                                {"ticket_id": "TKT-20260816-5935"})
        self.assertTrue(ok)

    def test_validate_params_rejects_bad_tkt(self):
        from action_validator import validate_params
        for bad in ("", "TKT-123", "TKT-20260816-593", "TKT-20260816-59355",
                    "ABC-20260816-5935", "TKT-20260816-5935x"):
            ok, msg = validate_params("ticket_status", {"ticket_id": bad})
            self.assertFalse(ok, f"should reject {bad!r}: {msg}")
        self.assertFalse(validate_params("ticket_status", {})[0])

    def test_read_only_set_membership(self):
        # ticket_status is read-only: it never needs a managed-device target and
        # the worker's READ_ONLY_ACTIONS treats it like system_time.
        from action_validator import ACTION_SCRIPTS, AllowedAction
        self.assertEqual(ACTION_SCRIPTS[AllowedAction.TICKET_STATUS],
                         "scripts/ticket_status.sh")

    def test_status_route_binds(self):
        from fastapi.testclient import TestClient
        from main import app
        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/tickets/{ticket_id}/status"
                     and "GET" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "ticket_status")
        c = TestClient(app)
        r = c.get("/api/v1/tickets/TKT-20260816-0001/status")
        self.assertNotEqual(r.status_code, 422, f"route misbound: {r.text}")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
