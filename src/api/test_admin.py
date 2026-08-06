#!/usr/bin/env python3
"""In-container tests for the LLM Monitor (/api/v1/admin/llm-usage) which now
aggregates from the audit log (llm_request events) — survives ticket wipes.

    docker compose exec api python3 -m unittest test_admin -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="admin-llm-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Ticket, User
from audit import log_event
from routes.admin import llm_usage


class LlmUsageTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        # clear audit llm_request rows
        from models import AuditLog
        db.query(AuditLog).filter(AuditLog.event_type == "llm_request").delete()
        db.commit()
        db.close()

    def _log(self, ticket_id, model, pt, rt, cost, action="ping_test"):
        db = SessionLocal()
        log_event(db, "llm_request", "system", {
            "ticket_id": ticket_id, "model": model,
            "prompt_tokens": pt, "response_tokens": rt,
            "cost_usd": cost, "cost_estimate": True,
            "confidence": 0.95, "action": action, "target": "sw-1",
        }, ticket_id)
        db.close()

    def test_aggregates_from_audit_with_surviving_ticket(self):
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-TEST-1", title="ping the gateway", description="",
                   priority="P3", status="closed", source="manual")
        db.add(t)
        db.commit()
        db.close()
        self._log("TKT-TEST-1", "deepseek/deepseek-chat", 100, 50, 0.0010)
        self._log("TKT-TEST-1", "deepseek/deepseek-chat", 200, 80, 0.0020)
        self._log("TKT-TEST-1", "deepseek/deepseek-reasoner", 500, 200, 0.0100)

        r = llm_usage(days=7, db=SessionLocal(), user=SimpleNamespace(role="admin"))
        self.assertEqual(r["total_calls"], 3)
        self.assertEqual(r["total_tokens"], 1130)   # 100+50 + 200+80 + 500+200
        self.assertAlmostEqual(r["total_cost_usd"], 0.0130, places=6)
        self.assertEqual(r["by_model"]["deepseek/deepseek-chat"]["calls"], 2)
        self.assertEqual(r["by_model"]["deepseek/deepseek-reasoner"]["calls"], 1)
        self.assertEqual(r["recent"][0]["title"], "ping the gateway")  # ticket exists

    def test_deleted_ticket_shows_placeholder(self):
        self._log("TKT-GONE-9", "deepseek/deepseek-chat", 10, 5, 0.0001)
        r = llm_usage(days=7, db=SessionLocal(), user=SimpleNamespace(role="admin"))
        self.assertEqual(r["total_calls"], 1)
        self.assertEqual(r["recent"][0]["ticket_id"], "TKT-GONE-9")
        self.assertEqual(r["recent"][0]["title"], "—")  # ticket was wiped

    def test_empty_returns_zeroes(self):
        r = llm_usage(days=7, db=SessionLocal(), user=SimpleNamespace(role="admin"))
        self.assertEqual(r["total_calls"], 0)
        self.assertEqual(r["daily"], [])
        self.assertEqual(r["recent"], [])


class TicketPrefsTest(unittest.TestCase):
    """Per-user default tickets-page filters."""
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(User).delete()
        u = User(username="homeuser", hashed_password="x", role="admin")
        db.add(u)
        db.commit()
        self.uid = u.id
        db.close()

    def test_save_and_get_prefs(self):
        from routes.tickets import set_ticket_prefs, get_ticket_prefs
        db = SessionLocal()
        u = db.query(User).get(self.uid)
        set_ticket_prefs({"status": "", "priority": ""}, db=db, user=u)
        g = get_ticket_prefs(user=u)
        self.assertEqual(g["status"], "")
        self.assertEqual(g["priority"], "")
        set_ticket_prefs({"status": "in_progress", "priority": "P2"}, db=db, user=u)
        db.expire_all()
        g = get_ticket_prefs(user=u)
        self.assertEqual(g, {"status": "in_progress", "priority": "P2"})
        db.close()

    def test_invalid_status_rejected(self):
        from routes.tickets import set_ticket_prefs
        from fastapi import HTTPException
        db = SessionLocal()
        u = db.query(User).get(self.uid)
        with self.assertRaises(HTTPException):
            set_ticket_prefs({"status": "weird"}, db=db, user=u)
        db.close()


class CustomerActionEmailTest(unittest.TestCase):
    """The agent-result path (jobs.py) must also email the submitter."""
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        db.query(User).delete()
        u = User(username="owner", email="owner@example.com", hashed_password="x",
                 role="admin")
        db.add(u)
        db.commit()
        self.uid = u.id
        db.close()

    def test_agent_completed_customer_action_emails(self):
        import time as _t
        from routes.jobs import report_job_result
        from routes.jobs import JobResult
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-EMAIL-1", title="list vlans", description="",
                   priority="P3", status="in_progress", source="manual",
                   submitter_id=self.uid)
        db.add(t)
        db.commit()
        from unittest.mock import patch
        with patch("emailer.send_email", return_value=(True, "")) as send:
            report_job_result(
                JobResult(ticket_id="TKT-EMAIL-1", action="network_info",
                          success=True, output={"networks": [{"name": "X", "vlan": 5}]}),
                db=db, user=SimpleNamespace(role="operator"))
        _t.sleep(0.4)  # notify runs in a background thread
        db.expire_all()
        t = db.query(Ticket).filter(Ticket.ticket_id == "TKT-EMAIL-1").first()
        self.assertEqual(t.status, "customer_action")
        self.assertGreaterEqual(send.call_count, 1)
        if send.call_count:
            args = send.call_args
            self.assertIn("owner@example.com", args[0][0])
            self.assertIn("needs your input", args[0][1])
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
