#!/usr/bin/env python3
"""Tests for pi/Lily usage metering on the API side (routes/jobs.py).

Run in-container (needs sqlalchemy):
    docker compose exec api python3 -m unittest test_jobs_metering -v

Covers _meter_usage: a pi_task job result carrying `usage` prices the reported
tokens via the provider registry, accumulates cost onto the ticket, flags
estimates, and writes the same `llm_request` audit event the catalog path
writes (so the reports KPI aggregates everything in one place).
"""

import os
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="jobs-metering-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db  # noqa: E402
from models import Ticket, AuditLog          # noqa: E402
from routes.jobs import _meter_usage         # noqa: E402


class JobsMeteringTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _ticket(self, tid):
        db = SessionLocal()
        t = Ticket(ticket_id=tid, title=tid, description="", priority="P3",
                   status="in_progress", source="manual")
        db.add(t)
        db.commit()
        return db, t

    def test_meters_pi_usage_and_writes_llm_audit(self):
        db, t = self._ticket("TKT-PI1")
        out = {"usage": {"input_tokens": 1_000_000, "output_tokens": 0,
                         "cache_read_tokens": 0, "cache_write_tokens": 0,
                         "estimated": False},
               "model": "deepseek-chat", "provider": "deepseek"}
        with patch("llm_providers.load_providers", return_value={}), \
             patch("audit._audit_enabled", return_value=True):
            cost = _meter_usage(t, out, db)
        # deepseek-chat input = $0.27 / 1M tokens (DEFAULT_PRICE_TABLE).
        self.assertAlmostEqual(cost, 0.27, places=6)
        self.assertAlmostEqual(t.llm_cost_usd, 0.27, places=6)
        self.assertEqual(t.llm_prompt_tokens, 1_000_000)
        self.assertEqual(t.llm_model, "pi/deepseek-chat")
        self.assertFalse(t.llm_cost_estimate)
        a = db.query(AuditLog).filter_by(event_type="llm_request").first()
        self.assertIsNotNone(a)
        self.assertEqual(a.data["source"], "pi_agent")
        self.assertFalse(a.data["cost_estimate"])
        db.close()

    def test_estimated_usage_is_labeled(self):
        db, t = self._ticket("TKT-PI2")
        out = {"usage": {"input_tokens": 1000, "output_tokens": 500,
                         "estimated": True, "note": "chars/4 fallback"},
               "model": "some-unknown-model", "provider": "deepseek"}
        with patch("llm_providers.load_providers", return_value={}), \
             patch("audit._audit_enabled", return_value=True):
            cost = _meter_usage(t, out, db)
        self.assertTrue(cost > 0)  # blended fallback, not a silent 0.00
        self.assertTrue(t.llm_cost_estimate)
        a = db.query(AuditLog).filter_by(event_type="llm_request").first()
        self.assertTrue(a.data["cost_estimate"])
        db.close()

    def test_no_usage_is_a_no_op(self):
        db, t = self._ticket("TKT-PI3")
        self.assertIsNone(_meter_usage(t, {"response": "hi"}, db))
        self.assertIsNone(t.llm_cost_usd)
        self.assertIsNone(t.llm_prompt_tokens)
        db.close()


if __name__ == "__main__":
    unittest.main()
