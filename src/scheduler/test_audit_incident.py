#!/usr/bin/env python3
"""Scheduler-health incident audit tests (2026-08-26).

The scheduler has no sqlalchemy — it writes audit rows directly via sqlite3
(mirroring api/audit.log_event's hash chain). These tests prove:

  1. a sustained API-auth failure window writes ONE scheduler_health event;
  2. recovery writes a recovery event (once per episode);
  3. the direct sqlite3 write produces a hash chain the API's verify_chain
     accepts (same compute_hash formula).

Run with stdlib only:
    python3 -m unittest test_audit_incident -v
"""

import datetime
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import main


def _hash(data, previous_hash=None):
    raw = json.dumps(data, sort_keys=True) + (previous_hash or "0")
    return hashlib.sha256(raw.encode()).hexdigest()


class ObserveAuthResultTest(unittest.TestCase):
    def setUp(self):
        main._AUTH_STATE["since"] = None
        main._AUTH_STATE["logged"] = False

    def test_incident_fires_once_after_sustained_window(self):
        events = []
        with patch.object(main, "_audit_event",
                          side_effect=lambda *a, **k: events.append(a)), \
             patch.object(main.time, "time", side_effect=[1000.0, 1000.0, 2000.0]):
            main._observe_auth_result(False, reason="rejected")
            main._observe_auth_result(False, reason="rejected")
            main._observe_auth_result(False, reason="rejected")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "scheduler_health")
        self.assertEqual(events[0][2]["result"], "failed")

    def test_no_incident_before_window(self):
        events = []
        with patch.object(main, "_audit_event",
                          side_effect=lambda *a, **k: events.append(a)), \
             patch.object(main.time, "time", side_effect=[1000.0, 1500.0]):
            main._observe_auth_result(False, reason="rejected")
            main._observe_auth_result(False, reason="rejected")
        self.assertEqual(events, [])

    def test_recovery_writes_event_once(self):
        main._AUTH_STATE["since"] = 1000.0
        main._AUTH_STATE["logged"] = True
        events = []
        with patch.object(main, "_audit_event",
                          side_effect=lambda *a, **k: events.append(a)):
            main._observe_auth_result(True)
            main._observe_auth_result(True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "scheduler_health")
        self.assertEqual(events[0][2]["result"], "recovered")


class DirectAuditWriteTest(unittest.TestCase):
    def _make_db(self):
        tmp = tempfile.mkdtemp(prefix="sched-audit-")
        path = os.path.join(tmp, "audit.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE audit_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " event_id TEXT UNIQUE NOT NULL,"
            " timestamp TEXT,"
            " event_type TEXT NOT NULL,"
            " ticket_id TEXT,"
            " actor TEXT NOT NULL,"
            " data TEXT NOT NULL,"
            " previous_hash TEXT,"
            " sha256_hash TEXT NOT NULL"
            ")")
        conn.commit()
        conn.close()
        return path

    def test_audit_event_writes_hash_chained_rows(self):
        path = self._make_db()
        with patch.object(main, "_db_path", return_value=path):
            self.assertTrue(main._audit_event(
                "scheduler_health", "scheduler",
                {"result": "failed", "reason": "auth rejected"}))
            self.assertTrue(main._audit_event(
                "scheduler_health", "scheduler",
                {"result": "recovered", "reason": "restored"}))
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT event_type, data, previous_hash, sha256_hash "
            "FROM audit_log ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0][2])  # first row has no previous hash
        d0 = json.loads(rows[0][1])
        d1 = json.loads(rows[1][1])
        self.assertEqual(rows[0][3], _hash(d0))
        self.assertEqual(rows[1][2], rows[0][3])        # chains to prior
        self.assertEqual(rows[1][3], _hash(d1, rows[0][3]))
        # never the credential itself
        self.assertNotIn("password", json.dumps(d0).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
