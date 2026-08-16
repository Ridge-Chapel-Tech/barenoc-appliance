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
from routes.chat import chat_send, ChatSend  # noqa: E402


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
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "chat_send")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
