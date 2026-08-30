#!/usr/bin/env python3
"""Tests for the dashboard reporting KPIs (08-17 gate + user feedback).

Covers:
- tz normalization: `_hours()` used to subtract an offset-naive `created_at`
  from an offset-aware `resolved_at` (or vice-versa) and raise
  ``TypeError: can't subtract offset-naive and offset-aware datetimes`` —
  which 500'd the whole Performance & Reporting widget. Every datetime is
  normalized to naive UTC before the subtraction.
- days sensitivity: the Est. manned-NOC cost KPI scales with the Last-*days
  dropdown (support tickets created in the window, not a fixed resolved count).
- honest AI spend: tracked catalog-path LLM cost summed + the metering-gap
  note (pi/Lily sessions aren't metered).
- correct averages: resolution = support tickets closed in the period (open /
  auto / negative-resolution tickets excluded); first response = first real
  customer-facing reply (customer messages / internal notes / auto-closes
  don't count).

    docker compose exec api python3 -m unittest test_dashboard -v
"""

import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP = tempfile.mkdtemp(prefix="dashboard-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Ticket
from routes import dashboard


class HoursTzNormalizationTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        db.commit()
        db.close()

    def test_hours_naive_and_aware(self):
        # naive UTC start, offset-aware resolved_at — the exact mixed mix.
        created = datetime(2026, 8, 1, 0, 0, 0)
        resolved = datetime(2026, 8, 1, 3, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(dashboard._hours(created, resolved), 3.5)

    def test_hours_naive_both(self):
        a = datetime(2026, 8, 1, 0, 0, 0)
        b = datetime(2026, 8, 1, 6, 0, 0)
        self.assertEqual(dashboard._hours(a, b), 6.0)

    def test_hours_none_returns_none(self):
        self.assertIsNone(dashboard._hours(None, datetime.utcnow()))
        self.assertIsNone(dashboard._hours(datetime.utcnow(), None))

    def test_report_stats_renders_for_ticket_with_resolved_at(self):
        """A ticket with resolved_at must not blow up the reports (regression)."""
        db = SessionLocal()
        created = datetime.utcnow() - timedelta(hours=2)
        # Make resolved_at offset-aware while created_at stays naive — the
        # exact .207 repro that raised before normalization.
        resolved = (datetime.utcnow() - timedelta(hours=1)).replace(tzinfo=timezone.utc)
        t = Ticket(ticket_id="TKT-20260801-0001", title="mixed tz", description="",
                   priority="P3", status="closed", source="manual",
                   created_at=created, resolved_at=resolved)
        db.add(t)
        db.commit()
        stats = dashboard._report_stats(db, days=30)
        db.close()
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["avg_resolution_hours"], 1.0)
        self.assertEqual(stats["avg_open_hours"], 1.0)


class ReportsKpiDaysSensitivityTest(unittest.TestCase):
    """The cost KPI must track the Last-*days dropdown like the other KPIs."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        from models import AuditLog
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _ticket(self, tid, days_ago, status="closed", source="manual",
                resolved_days_ago=None, notes=None):
        db = SessionLocal()
        now = datetime.utcnow()
        created = now - timedelta(days=days_ago)
        resolved = now - timedelta(days=resolved_days_ago) if resolved_days_ago is not None else None
        t = Ticket(ticket_id=tid, title=tid, description="", priority="P3",
                   status=status, source=source, created_at=created,
                   resolved_at=resolved, work_notes=json.dumps(notes or []))
        db.add(t)
        db.commit()
        db.close()

    def _llm_audit(self, ticket_id, cost, days_ago, **extra):
        db = SessionLocal()
        from models import AuditLog
        from audit import generate_event_id, compute_hash
        data = {"ticket_id": ticket_id, "model": "test", "cost_usd": cost}
        data.update(extra)
        prev = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        prev_hash = prev.sha256_hash if prev else None
        e = AuditLog(
            event_id=generate_event_id(), event_type="llm_request",
            ticket_id=ticket_id, actor="system", data=data,
            previous_hash=prev_hash, sha256_hash=compute_hash(data, prev_hash),
            timestamp=datetime.utcnow() - timedelta(days=days_ago),
        )
        db.add(e)
        db.commit()
        db.close()

    def test_manned_noc_cost_scales_with_days(self):
        # 1 support ticket in the last 7 days, 3 in the last 30.
        self._ticket("TKT-D1", 2, resolved_days_ago=1)
        self._ticket("TKT-D2", 20, resolved_days_ago=19)
        self._ticket("TKT-D3", 20, resolved_days_ago=18)
        db = SessionLocal()
        s7 = dashboard._report_stats(db, days=7)
        s30 = dashboard._report_stats(db, days=30)
        db.close()
        self.assertEqual(s7["support_created"], 1)
        self.assertEqual(s30["support_created"], 3)
        # Default rate $45/h × 1h/ticket.
        self.assertEqual(s7["est_manned_noc_cost_usd"], 45.0)
        self.assertEqual(s30["est_manned_noc_cost_usd"], 135.0)

    def test_auto_tickets_do_not_scale_cost(self):
        self._ticket("TKT-AUTO", 2, source="auto", resolved_days_ago=1)
        self._ticket("TKT-MAN", 2, source="manual", resolved_days_ago=1)
        db = SessionLocal()
        s = dashboard._report_stats(db, days=7)
        db.close()
        self.assertEqual(s["support_created"], 1)
        self.assertEqual(s["support_resolved"], 1)
        self.assertEqual(s["est_manned_noc_cost_usd"], 45.0)

    def test_ai_spend_sums_tracked_cost_and_labels_gap(self):
        self._llm_audit("TKT-C1", 0.000132, 1)
        self._llm_audit("TKT-C2", 0.00055, 2)
        db = SessionLocal()
        s = dashboard._report_stats(db, days=7)
        db.close()
        self.assertAlmostEqual(s["llm_cost_usd"], 0.000682, places=6)
        self.assertEqual(s["support_cost_usd"], s["llm_cost_usd"])
        # Known-price catalog calls are metered, not estimated.
        self.assertAlmostEqual(s["llm_cost_metered_usd"], 0.000682, places=6)
        self.assertEqual(s["llm_cost_estimate_usd"], 0.0)
        self.assertIn("metered", s["llm_metering_note"])
        self.assertIn("$0.00", s["llm_metering_note"])

    def test_ai_spend_splits_metered_and_estimate(self):
        self._llm_audit("TKT-E1", 0.0001, 1, cost_estimate=True, source="pi_agent")
        self._llm_audit("TKT-E2", 0.0005, 1, cost_estimate=False, source="catalog")
        db = SessionLocal()
        s = dashboard._report_stats(db, days=7)
        db.close()
        self.assertAlmostEqual(s["llm_cost_usd"], 0.0006, places=6)
        self.assertAlmostEqual(s["llm_cost_estimate_usd"], 0.0001, places=6)
        self.assertAlmostEqual(s["llm_cost_metered_usd"], 0.0005, places=6)
        self.assertEqual(s["llm_pi_calls"], 1)

    def test_unknown_cost_tickets_are_counted_not_silent(self):
        # A legacy pi-dispatched ticket with no cost record at all.
        db = SessionLocal()
        now = datetime.utcnow()
        t = Ticket(ticket_id="TKT-U1", title="legacy pi", description="",
                   priority="P3", status="closed", source="manual",
                   action="pi_task", assigned_to="customer",
                   created_at=now - timedelta(days=1),
                   resolved_at=now - timedelta(hours=23))
        db.add(t)
        db.commit()
        db.close()
        db = SessionLocal()
        s = dashboard._report_stats(db, days=7)
        db.close()
        self.assertEqual(s["llm_calls"], 0)
        self.assertEqual(s["llm_unknown_cost_tickets"], 1)
        self.assertEqual(s["support_cost_usd"], 0.0)

    def test_unknown_cost_ignores_unworked_tickets(self):
        # A ticket with no AI work and no cost is legitimately $0 — not unknown.
        self._ticket("TKT-ESC", 1, status="escalated", resolved_days_ago=1)
        db = SessionLocal()
        s = dashboard._report_stats(db, days=7)
        db.close()
        self.assertEqual(s["llm_unknown_cost_tickets"], 0)


class ReportsAveragesTest(unittest.TestCase):
    """Resolution + first-response averages exclude garbage and use a
    customer-facing-reply definition (08-17 user report)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        from models import AuditLog
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _mk(self, tid, created_hours_ago, status="open", source="manual",
            resolved_at=None, notes=None):
        db = SessionLocal()
        now = datetime.utcnow()
        t = Ticket(
            ticket_id=tid, title=tid, description="", priority="P3",
            status=status, source=source,
            created_at=now - timedelta(hours=created_hours_ago),
            resolved_at=resolved_at,
            work_notes=json.dumps(notes or []),
        )
        db.add(t)
        db.commit()
        db.close()

    def _note(self, event, ts, detail="x"):
        return {"event": event, "timestamp": ts, "detail": detail}

    def _stats(self, days=7):
        db = SessionLocal()
        s = dashboard._report_stats(db, days=days)
        db.close()
        return s

    def test_open_ticket_no_resolution_excluded(self):
        now = datetime.utcnow()
        created = now - timedelta(hours=2)
        self._mk("TKT-OPEN", 2, status="in_progress", notes=[
            self._note("ai_tech_feedback", (created + timedelta(minutes=5)).isoformat()),
        ])
        s = self._stats()
        self.assertEqual(s["support_resolved"], 0)
        self.assertIsNone(s["avg_resolution_hours"])
        self.assertEqual(s["avg_first_response_min"], 5.0)

    def test_greeting_only_counts_first_response_but_not_resolution(self):
        now = datetime.utcnow()
        created = now - timedelta(hours=1)
        self._mk("TKT-HI", 1, status="customer_action", notes=[
            self._note("ai_tech_feedback", (created + timedelta(minutes=1)).isoformat()),
        ])
        s = self._stats()
        self.assertEqual(s["support_resolved"], 0)
        self.assertEqual(s["avg_first_response_min"], 1.0)

    def test_user_message_only_is_not_a_response(self):
        now = datetime.utcnow()
        self._mk("TKT-USERMSG", 1, notes=[
            self._note("user_message", (now - timedelta(minutes=30)).isoformat()),
        ])
        s = self._stats()
        self.assertIsNone(s["avg_first_response_min"])

    def test_agent_response_internal_note_is_not_a_reply(self):
        now = datetime.utcnow()
        self._mk("TKT-INTERNAL", 1, notes=[
            self._note("processing", (now - timedelta(minutes=3)).isoformat()),
            self._note("agent_response", (now - timedelta(minutes=2)).isoformat()),
        ])
        s = self._stats()
        self.assertIsNone(s["avg_first_response_min"])

    def test_no_notes_first_response_none(self):
        self._mk("TKT-NONOTES", 1)
        s = self._stats()
        self.assertIsNone(s["avg_first_response_min"])

    def test_tz_aware_and_naive_notes_mix(self):
        now = datetime.utcnow()
        created = now - timedelta(hours=2)
        self._mk("TKT-TZ", 2, notes=[
            self._note("agent_progress", (created + timedelta(minutes=4)).isoformat() + "+00:00"),
            self._note("agent_completed", (created + timedelta(minutes=6)).isoformat()),
        ])
        s = self._stats()
        self.assertEqual(s["avg_first_response_min"], 4.0)

    def test_auto_ticket_excluded_from_averages(self):
        now = datetime.utcnow()
        self._mk("TKT-AUTORES", 2, source="auto", status="closed",
                 resolved_at=now - timedelta(minutes=30), notes=[
                     self._note("autoclosed", (now - timedelta(minutes=30)).isoformat()),
                 ])
        s = self._stats()
        self.assertEqual(s["support_resolved"], 0)
        self.assertEqual(s["support_created"], 0)
        self.assertIsNone(s["avg_resolution_hours"])
        self.assertIsNone(s["avg_first_response_min"])

    def test_negative_resolution_dropped(self):
        now = datetime.utcnow()
        self._mk("TKT-NEG", 2, status="closed",
                 resolved_at=now - timedelta(hours=3), notes=[
                     self._note("agent_completed", (now - timedelta(hours=1)).isoformat()),
                 ])
        s = self._stats()
        self.assertEqual(s["support_resolved"], 0)
        self.assertIsNone(s["avg_resolution_hours"])

    def test_resolution_average_is_support_only(self):
        now = datetime.utcnow()
        self._mk("TKT-R1", 5, status="closed",
                 resolved_at=now - timedelta(hours=2), notes=[
                     self._note("agent_completed", (now - timedelta(hours=2)).isoformat()),
                 ])
        self._mk("TKT-R2", 4, status="closed",
                 resolved_at=now - timedelta(hours=2), notes=[
                     self._note("agent_completed", (now - timedelta(hours=2)).isoformat()),
                 ])
        s = self._stats()
        # 3h and 2h resolutions -> 2.5h average.
        self.assertEqual(s["avg_resolution_hours"], 2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
