#!/usr/bin/env python3
"""Retention pruner tests (compliance retention control): per-category max-age.

Run from src/scheduler:
    python3 -m unittest test_retention -v
"""

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main as scheduler  # noqa: E402


class RetentionConfigTest(unittest.TestCase):
    def test_sane_defaults(self):
        with patch.object(scheduler, "_read_env", return_value={}):
            cfg = scheduler.retention_config()
        self.assertEqual(cfg["metrics"]["days"], 30)
        self.assertEqual(cfg["audit_log"]["days"], 0)   # never deleted (08-27 model: pseudonymized instead)
        self.assertEqual(cfg["tickets"]["days"], 0)   # never
        self.assertEqual(cfg["chat_messages"]["days"], 0)

    def test_strict_defaults(self):
        with patch.object(scheduler, "_read_env", return_value={"RETENTION_PROFILE": "strict"}):
            cfg = scheduler.retention_config()
        self.assertEqual(cfg["metrics"]["days"], 14)
        self.assertEqual(cfg["audit_log"]["days"], 0)  # never hard-deleted; 365d pseudonymize window is API-side
        self.assertEqual(cfg["tickets"]["days"], 365)

    def test_explicit_override_wins(self):
        with patch.object(scheduler, "_read_env",
                          return_value={"RETENTION_PROFILE": "strict",
                                        "RETENTION_METRICS_DAYS": "99"}):
            cfg = scheduler.retention_config()
        self.assertEqual(cfg["metrics"]["days"], 99)


class PruneRetentionTest(unittest.TestCase):
    def _make_db(self):
        tmp = tempfile.mkdtemp(prefix="retention-")
        path = os.path.join(tmp, "test.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE metrics (id INTEGER PRIMARY KEY, ts DATETIME);
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, timestamp DATETIME);
        CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, created_at DATETIME);
        CREATE TABLE findings (id INTEGER PRIMARY KEY, run_id INTEGER, created_at DATETIME);
        """)
        old = (datetime.datetime.utcnow() - datetime.timedelta(days=200)).strftime(
            "%Y-%m-%d %H:%M:%S")
        recent = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany("INSERT INTO metrics (ts) VALUES (?)", [(old,), (recent,)])
        conn.executemany("INSERT INTO audit_log (timestamp) VALUES (?)",
                         [(old,), (recent,)])
        conn.executemany("INSERT INTO scan_runs (created_at) VALUES (?)",
                         [(old,), (recent,)])
        conn.executemany("INSERT INTO findings (run_id, created_at) VALUES (?, ?)",
                         [(1, old), (1, recent)])
        conn.commit()
        conn.close()
        return path

    def test_prune_honors_max_age(self):
        path = self._make_db()
        with patch.object(scheduler, "_read_env",
                          return_value={"RETENTION_PROFILE": "strict"}):
            deleted = scheduler.prune_retention(path)
        self.assertEqual(deleted["metrics"], 1)
        self.assertEqual(deleted.get("audit_log", 0), 0)  # audit never hard-deleted (08-27 model)
        self.assertEqual(deleted["scan_runs"], 1)
        self.assertEqual(deleted["findings"], 1)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 2)  # both survive
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0], 1)
        finally:
            conn.close()

    def test_zero_days_never_prunes(self):
        path = self._make_db()
        # tickets/chat default to 0 (never) in sane profile — metrics 30d, so
        # 200-day-old metric is still pruned; but with an explicit 0 the row
        # stays.
        with patch.object(scheduler, "_read_env",
                          return_value={"RETENTION_METRICS_DAYS": "0",
                                        "RETENTION_AUDIT_LOG_DAYS": "0",
                                        "RETENTION_SCAN_RUNS_DAYS": "0",
                                        "RETENTION_FINDINGS_DAYS": "0"}):
            deleted = scheduler.prune_retention(path)
        self.assertEqual(deleted["metrics"], 0)
        self.assertEqual(deleted["audit_log"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
