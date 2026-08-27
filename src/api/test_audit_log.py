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



class RetentionPseudonymizeTest(unittest.TestCase):
    """08-27 locked model: events kept forever, personal fields blanked after
    the window, chain re-chained so verify_chain stays green."""

    def test_pseudonymize_blanks_personal_and_rechains(self):
        import datetime
        from audit import log_event, pseudonymize_audit_log, verify_chain
        init_db()
        db = SessionLocal()
        try:
            ev1 = log_event(db, "auth.login", "alice",
                            {"ip": "192.0.2.10", "device_id": 7}, None)
            ev2 = log_event(db, "auth.login", "bob",
                            {"ip": "198.51.100.9", "device_id": 9}, None)
            old = datetime.datetime.utcnow() - datetime.timedelta(days=400)
            db.query(AuditLog).filter(AuditLog.id.in_([ev1.id, ev2.id])).update(
                {"timestamp": old}, synchronize_session=False)
            db.commit()
            out = pseudonymize_audit_log(db, keep_days=365)
            self.assertEqual(out["pseudonymized"], 2)
            self.assertGreaterEqual(out["rechained"], 2)
            self.assertIsNotNone(out["event"])
            row = db.query(AuditLog).filter(AuditLog.id == ev1.id).first()
            self.assertEqual(row.actor, "[redacted]")
            self.assertIn(row.data.get("ip"), (None, "[redacted]"))
            self.assertEqual(row.data.get("device_id"), 7)  # non-personal kept
            self.assertTrue(verify_chain(db)["ok"],
                            "chain must verify after the re-chain pass")
        finally:
            db.close()

    def test_no_window_is_noop(self):
        from audit import pseudonymize_audit_log
        init_db()
        db = SessionLocal()
        try:
            out = pseudonymize_audit_log(db, keep_days=0)
            self.assertEqual(out["pseudonymized"], 0)
        finally:
            db.close()

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


class PayloadGuardTest(unittest.TestCase):
    """Volume honesty: audit payloads stay small and never contain secrets."""

    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _db(self):
        return SessionLocal()

    def test_secret_values_redacted(self):
        import audit
        db = self._db()
        log_event(db, "settings_change", "alice", {"password": "hunter2", "note": "ok"})
        row = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        self.assertEqual(row.data.get("password"), "[redacted]")
        self.assertEqual(row.data.get("note"), "ok")
        db.close()

    def test_oversized_payload_is_capped(self):
        import audit
        db = self._db()
        big = {"fields": [str(i) * 50 for i in range(100)]}
        log_event(db, "settings_change", "alice", big)
        row = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        size = len(json.dumps(row.data))
        self.assertLessEqual(size, audit.MAX_EVENT_DATA_BYTES + 20)
        self.assertNotEqual(row.data, big)
        # the hash chain must still verify against the capped payload
        v = verify_chain(db)
        self.assertTrue(v["ok"])
        db.close()

    def test_new_events_stay_small(self):
        # the catalog's own payloads are tiny — the 2 vCPU/4 GB baseline stays
        # honest even at high event volume.
        from audit_catalog import payload_bytes
        for payload in (
            {"device_id": 1, "device_name": "sw-1", "credential_type": "snmp", "action": "decrypt"},
            {"kind": "audit_log"},
            {"type": "vm", "result": "success"},
            {"result": "failed", "reason": "agent API auth failing (sustained window)"},
        ):
            self.assertLessEqual(payload_bytes(payload), 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
