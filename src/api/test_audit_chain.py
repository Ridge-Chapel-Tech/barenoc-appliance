#!/usr/bin/env python3
"""Audit hash-chain concurrency + repair tests (2026-08-30).

Covers the fork bug: audit.log_event used to READ the tail hash and INSERT the
next row without a write lock, so two concurrent writers could read the same
previous_hash and both insert — forking the linear chain (prod evidence: ids
8502 + 13601). This module proves the BEGIN IMMEDIATE fix serializes writers,
and that repair_chain re-chains a forked log idempotently.

Run from src/api:
    python3 -m unittest test_audit_chain -v
"""

import os
import tempfile
import threading
import unittest

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("DATABASE_URL",
                      f"sqlite:///{tempfile.mkdtemp(prefix='audit-chain-')}/test.db")

from database import SessionLocal, init_db  # noqa: E402
from models import AuditLog  # noqa: E402
from audit import log_event, verify_chain, repair_chain, compute_hash  # noqa: E402

init_db()


class ConcurrentChainTest(unittest.TestCase):
    """The fork reproduction: concurrent log_event calls must not produce two
    rows sharing one previous_hash. Fails on the old read-then-insert; passes
    with the BEGIN IMMEDIATE write lock."""

    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def test_concurrent_log_event_no_fork(self):
        # Seed one row first so every concurrent writer reads a NON-NULL tail
        # hash — a fork over an empty log would show up as two NULL
        # previous_hash values, which the duplicate-previous_hash assert below
        # would not catch (NULL is legit for the first row only).
        db = SessionLocal()
        log_event(db, "seed", "system", {"seed": True})
        db.close()

        n = 16
        barrier = threading.Barrier(n)
        errors = []

        def worker(i):
            s = SessionLocal()
            try:
                barrier.wait()
                log_event(s, "auth.login", f"user{i}",
                          {"ip": "192.0.2.10", "method": "password"})
            except Exception as e:  # pragma: no cover - failure signal
                errors.append(repr(e))
            finally:
                s.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])

        db = SessionLocal()
        try:
            v = verify_chain(db)
            self.assertTrue(v["ok"], f"chain forked under concurrency: {v}")

            rows = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
            self.assertEqual(len(rows), n + 1)
            # Every row after the first must point at a UNIQUE previous hash.
            prevs = [r.previous_hash for r in rows[1:]]
            self.assertTrue(all(prevs), "non-first rows must have a previous_hash")
            self.assertEqual(len(prevs), len(set(prevs)),
                             "two rows share a previous_hash (fork)")
        finally:
            db.close()


class RepairChainTest(unittest.TestCase):
    """repair_chain: idempotent re-chain of a forked log."""

    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _db(self):
        return SessionLocal()

    def test_idempotent_noop_when_green(self):
        db = self._db()
        log_event(db, "login", "alice", {"a": 1})
        log_event(db, "login", "alice", {"a": 2})
        log_event(db, "settings_change", "alice", {"b": 3})
        db.close()

        db = self._db()
        out = repair_chain(db)
        self.assertEqual(out["repaired"], 0)
        self.assertEqual(out["fixed_ids"], [])
        self.assertIsNone(out["event"])
        self.assertIn("consistent", out["note"])
        self.assertTrue(verify_chain(db)["ok"])
        self.assertEqual(db.query(AuditLog).filter(
            AuditLog.event_type == "chain.repaired").count(), 0)
        db.close()

    def test_repairs_forged_fork_and_records_event(self):
        db = self._db()
        log_event(db, "login", "alice", {"a": 1})
        log_event(db, "login", "bob", {"a": 2})
        log_event(db, "login", "carol", {"a": 3})
        rows = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
        # Forge a fork: row 3 duplicates row 2's previous_hash instead of
        # pointing at row 2's sha256_hash.
        rows[2].previous_hash = rows[1].previous_hash
        rows[2].sha256_hash = compute_hash(rows[2].data, rows[2].previous_hash)
        db.commit()
        self.assertFalse(verify_chain(db)["ok"])
        db.close()

        db = self._db()
        out = repair_chain(db)
        self.assertGreaterEqual(out["repaired"], 1)
        self.assertIn(rows[2].id, out["fixed_ids"])
        self.assertTrue(verify_chain(db)["ok"])
        # The repair itself is audited: a chain.repaired event with count+ids.
        ev = (db.query(AuditLog)
                .filter(AuditLog.event_type == "chain.repaired")
                .order_by(AuditLog.id.desc()).first())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.data.get("count"), out["repaired"])
        self.assertIn(str(rows[2].id), ev.data.get("ids", ""))
        db.close()

    def test_repair_rechains_the_whole_tail_after_a_break(self):
        # A natural fork (like prod 8502): the broken row duplicates the prior
        # row's pointer, and the rows AFTER it chain to the forked sha. The
        # repair must cascade and fix every row from the break to the end.
        db = self._db()
        for i in range(4):
            log_event(db, "login", f"u{i}", {"a": i})
        rows = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
        rows[2].previous_hash = rows[1].previous_hash
        rows[2].sha256_hash = compute_hash(rows[2].data, rows[2].previous_hash)
        rows[3].previous_hash = rows[2].sha256_hash
        rows[3].sha256_hash = compute_hash(rows[3].data, rows[3].previous_hash)
        db.commit()
        self.assertFalse(verify_chain(db)["ok"])
        db.close()

        db = self._db()
        out = repair_chain(db)
        self.assertTrue(verify_chain(db)["ok"])
        self.assertIn(rows[2].id, out["fixed_ids"])
        self.assertIn(rows[3].id, out["fixed_ids"])
        db.close()


class CompactIdsTest(unittest.TestCase):
    def test_ranges_and_singles(self):
        from audit import _compact_ids
        self.assertEqual(_compact_ids([1, 2, 3, 5, 7, 8]), "1-3,5,7-8")
        self.assertEqual(_compact_ids([42]), "42")
        self.assertEqual(_compact_ids([]), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
