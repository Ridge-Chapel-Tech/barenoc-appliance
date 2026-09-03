#!/usr/bin/env python3
"""Tests for the rate-window scheduler (F6 cost optimization — the WHEN lane).

    python3 -m unittest test_ratewindows -v

Covers: day parsing, peak/off-peak predicates, the cost factor, plan_start
(critical never defers / peak defers / off-peak runs), config normalization,
and the off-peak waiting queue (enqueue/dequeue/due) against a scratch dir.
"""

import datetime
import os
import tempfile
import unittest

import ratewindows as rw

UTC = datetime.timezone.utc


def _monday(hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 9, 7, hour, minute, tzinfo=UTC)


class ParseDaysTest(unittest.TestCase):
    def test_weekdays(self):
        self.assertEqual(rw.parse_days("mon-fri"), frozenset(range(0, 5)))
        self.assertEqual(rw.parse_days("weekdays"), frozenset(range(0, 5)))

    def test_weekend(self):
        self.assertEqual(rw.parse_days("weekend"), frozenset({5, 6}))

    def test_all(self):
        self.assertEqual(rw.parse_days("all"), frozenset(range(7)))

    def test_comma_list(self):
        self.assertEqual(rw.parse_days("mon,wed,fri"), frozenset({0, 2, 4}))

    def test_bitmask(self):
        # bit 0 = Monday, bit 1 = Tuesday
        self.assertEqual(rw.parse_days(0b0000011), frozenset({0, 1}))

    def test_bad(self):
        self.assertIsNone(rw.parse_days("nope"))
        self.assertIsNone(rw.parse_days(None))
        self.assertIsNone(rw.parse_days(True))


class PeakPredicateTest(unittest.TestCase):
    def test_default_config_peak_windows(self):
        # Mon 02:00 UTC is inside peak window 1 (01:00-04:00)
        self.assertTrue(rw.is_peak(_monday(2)))
        # Mon 08:00 UTC is inside peak window 2 (06:00-10:00)
        self.assertTrue(rw.is_peak(_monday(8)))
        # Mon 05:00 UTC is between the two peak windows -> off-peak
        self.assertFalse(rw.is_peak(_monday(5)))
        # Saturday -> off-peak
        self.assertFalse(rw.is_peak(datetime.datetime(2026, 9, 5, 2, 0, tzinfo=UTC)))

    def test_factor_at(self):
        self.assertEqual(rw.factor_at(_monday(2)), 1.0)          # peak
        self.assertEqual(rw.factor_at(_monday(5)), 0.5)          # off-peak (half price)

    def test_naive_datetime_treated_as_utc(self):
        self.assertTrue(rw.is_peak(datetime.datetime(2026, 9, 7, 2, 0)))


class PlanStartTest(unittest.TestCase):
    def test_critical_never_defers(self):
        plan = rw.plan_start(_monday(2), critical=True)
        self.assertFalse(plan["defer"])
        self.assertIn("critical", plan["reason"])

    def test_off_peak_runs_now(self):
        plan = rw.plan_start(_monday(5))
        self.assertFalse(plan["defer"])
        self.assertEqual(plan["state"], "OFF-PEAK")

    def test_peak_defers_to_next_off_peak(self):
        plan = rw.plan_start(_monday(2))
        self.assertTrue(plan["defer"])
        self.assertEqual(plan["state"], "PEAK")
        self.assertIsNotNone(plan["start_at"])
        # The next off-peak start is 04:00 UTC (end of the first peak window).
        start = datetime.datetime.fromisoformat(plan["start_at"])
        self.assertEqual((start.hour, start.minute), (4, 0))

    def test_degenerate_24_7_peak_returns_none_start(self):
        cfg = {"off_peak_factor": 0.5,
               "peak_windows": [{"days": "all", "start": 0, "end": 24}]}
        plan = rw.plan_start(_monday(2), cfg=cfg)
        self.assertTrue(plan["defer"])
        self.assertIsNone(plan["start_at"])


class NormalizeConfigTest(unittest.TestCase):
    def test_bad_factor_coerced(self):
        cfg = rw.normalize_config({"off_peak_factor": 5.0,
                                   "peak_windows": [{"days": "mon-fri", "start": 1, "end": 4}]})
        self.assertEqual(cfg["off_peak_factor"], 0.5)

    def test_bad_window_dropped(self):
        cfg = rw.normalize_config({"peak_windows": [
            {"days": "mon-fri", "start": 9, "end": 4},   # end <= start
            {"days": "mon-fri", "start": 1, "end": 4},   # valid
        ]})
        self.assertEqual(len(cfg["peak_windows"]), 1)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_dequeue_due(self):
        rw.enqueue("TKT-1", "ping the switch",
                   "2026-09-07T04:00:00+00:00", state_dir_=self.tmp.name)
        items = rw.list_queue(self.tmp.name)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["topic"], "TKT-1")
        # not due yet
        now = datetime.datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
        self.assertEqual(rw.due(self.tmp.name, now=now), [])
        # due once the boundary passes
        now = datetime.datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
        self.assertEqual(len(rw.due(self.tmp.name, now=now)), 1)
        rw.dequeue("TKT-1", state_dir_=self.tmp.name)
        self.assertEqual(rw.list_queue(self.tmp.name), [])


class ToggleTest(unittest.TestCase):
    def test_defaults_on(self):
        self.assertTrue(rw.cost_optimization_enabled(env={}))
        self.assertTrue(rw.offpeak_defer_enabled(env={}))

    def test_explicit_off(self):
        self.assertFalse(rw.cost_optimization_enabled(env={"LLM_COST_OPTIMIZATION": "false"}))
        self.assertFalse(rw.offpeak_defer_enabled(env={"LLM_OFFPEAK_DEFER": "0"}))

    def test_explicit_on(self):
        self.assertTrue(rw.cost_optimization_enabled(env={"LLM_COST_OPTIMIZATION": "true"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
