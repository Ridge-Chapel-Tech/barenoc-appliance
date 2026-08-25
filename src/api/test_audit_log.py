#!/usr/bin/env python3
"""Audit log tests (2026-08-25): viewer, export, hash-chain verify.

Run from src/api:
    python3 -m unittest test_audit_log -v
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("DATABASE_URL",
                      f"sqlite:///{tempfile.mkdtemp(prefix='audit-log-')}/test.db")

from database import SessionLocal, init_db  # noqa: E402
from models import AuditLog  # noqa: E402
from audit import log_event, verify_chain, compute_hash  # noqa: E402

init_db()


class ChainVerifyTest(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _db(self):
        return SessionLocal()

    def test_verify_ok(self):
        db = self._db()
        log_event(db, "login", "alice", {"a": 1})
        log_event(db, "settings_change", "alice", {"b": 2})
        v = verify_chain(db)
        self.assertTrue(v["ok"])
        self.assertEqual(v["count"], 2)
        db.close()

    def test_verify_empty(self):
        db = self._db()
        v = verify_chain(db)
        self.assertTrue(v["ok"])
        self.assertEqual(v["count"], 0)
        db.close()

    def test_verify_detects_data_tamper(self):
        db = self._db()
        log_event(db, "login", "alice", {"a": 1})
        log_event(db, "login", "alice", {"a": 2})
        row = db.query(AuditLog).order_by(AuditLog.id.asc()).first()
        row.data = {"tampered": True}
        db.commit()
        v = verify_chain(db)
        self.assertFalse(v["ok"])
        self.assertIsNotNone(v["broken_at"])
        self.assertIn("hash", v["error"])
        db.close()

    def test_verify_detects_hash_tamper(self):
        db = self._db()
        log_event(db, "login", "alice", {"a": 1})
        log_event(db, "login", "alice", {"a": 2})
        row = db.query(AuditLog).order_by(AuditLog.id.asc()).first()
        row.sha256_hash = "0" * 64
        db.commit()
        v = verify_chain(db)
        self.assertFalse(v["ok"])
        self.assertIsNotNone(v["broken_at"])
        db.close()


class AuditRouteTest(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _db(self):
        return SessionLocal()

    def test_list_and_verify(self):
        from routes import audit_log
        db = self._db()
        log_event(db, "login", "alice", {"x": 1})
        r = audit_log.list_audit(limit=10, db=db, user=SimpleNamespace(role="admin"))
        self.assertGreaterEqual(r["total"], 1)
        self.assertTrue(r["chain"]["ok"])
        v = audit_log.verify(db=db, user=SimpleNamespace(role="admin"))
        self.assertTrue(v["ok"])
        db.close()

    def test_export_contains_rows_and_chain(self):
        from routes import audit_log
        db = self._db()
        log_event(db, "login", "alice", {"x": 1})
        resp = audit_log.export(db=db, user=SimpleNamespace(role="admin"))
        body = json.loads(resp.body)
        self.assertIn("rows", body)
        self.assertGreaterEqual(len(body["rows"]), 1)
        self.assertTrue(body["chain"]["ok"])
        db.close()


class AuditToggleTest(unittest.TestCase):
    """The audit-log control (compliance) gates log_event on/off."""

    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def test_log_event_noop_when_disabled(self):
        import audit
        db = SessionLocal()
        with patch.object(audit, "_audit_enabled", return_value=False):
            event = log_event(db, "login", "alice", {"x": 1})
        self.assertIsNone(event)
        self.assertEqual(db.query(AuditLog).count(), 0)
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
