#!/usr/bin/env python3
"""Tests for the scheduler's update-schedule v2 (local-time + one-time).

Runs with stdlib only (the scheduler main.py imports no third-party packages),
so it works in CI's python3.12 and on the VM host.

    python3 -m unittest test_scheduler -v
"""

import datetime
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import main


class TzTest(unittest.TestCase):
    def test_local_to_utc_dst_safe(self):
        winter = main._local_to_utc(datetime.datetime(2026, 1, 15, 12, 0),
                                    "America/New_York")
        self.assertEqual(winter.hour, 17)  # EST = UTC-5
        summer = main._local_to_utc(datetime.datetime(2026, 7, 15, 12, 0),
                                    "America/New_York")
        self.assertEqual(summer.hour, 16)  # EDT = UTC-4

    def test_local_to_utc_invalid_tz_falls_back_utc(self):
        self.assertEqual(
            main._local_to_utc(datetime.datetime(2026, 7, 15, 12, 0), "Bad/Zone").hour, 12)

    def test_parse_local_dt(self):
        self.assertEqual(main._parse_local_dt("2026-08-17T02:00"),
                         datetime.datetime(2026, 8, 17, 2, 0))
        self.assertEqual(main._parse_local_dt("2026-08-17 02:30:00"),
                         datetime.datetime(2026, 8, 17, 2, 30))


class ScheduleLogicTest(unittest.TestCase):
    def _run(self, sched, last, api_post_effect=None):
        """Run check_update_schedule with the API calls mocked; returns the
        mocked _api_post."""
        post = patch.object(main, "_api_post")
        get = patch.object(main, "_api_get", return_value=sched)
        tz = patch.object(main, "_appliance_tz", return_value="UTC")
        get.start()
        tz.start()
        mock = post.start()
        self.addCleanup(get.stop)
        self.addCleanup(tz.stop)
        self.addCleanup(post.stop)
        mock.side_effect = api_post_effect
        main.check_update_schedule("tok", last)
        return mock

    def test_onetime_not_due_does_nothing(self):
        future = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        sched = {"enabled": True, "mode": "onetime", "when": future, "fired": ""}
        post = self._run(sched, {})
        post.assert_not_called()

    def test_onetime_fires_once_and_completes(self):
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
        sched = {"enabled": True, "mode": "onetime", "when": past, "fired": ""}
        last = {}
        post = self._run(sched, last)
        self.assertEqual(post.call_count, 2)  # /updates/now + /schedule/complete
        post.assert_any_call("/updates/now", {}, "tok")
        post.assert_any_call("/updates/schedule/complete", {}, "tok")
        self.assertEqual(last["update"], f"onetime-{past}")
        # Once-per-schedule guard: a second tick does nothing.
        post.reset_mock()
        with patch.object(main, "_api_get", return_value=sched):
            main.check_update_schedule("tok", last)
        post.assert_not_called()

    def test_onetime_already_fired_skips(self):
        sched = {"enabled": True, "mode": "onetime", "when": "2020-01-01T00:00",
                 "fired": "2020-01-01T00:01:00Z"}
        post = self._run(sched, {})
        post.assert_not_called()

    def test_onetime_no_release_stays_armed(self):
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
        sched = {"enabled": True, "mode": "onetime", "when": past, "fired": ""}

        def effect(url, data, token):
            raise urllib.error.HTTPError(url, 400, "already up to date", None, None)

        last = {}
        post = self._run(sched, last, api_post_effect=effect)
        self.assertEqual(post.call_count, 1)  # /updates/now attempted, complete NOT called
        self.assertNotIn("update", last)      # guard NOT set → stays armed

    def test_recurring_local_hour_match(self):
        with patch.object(main, "_local_now",
                          return_value=datetime.datetime(2026, 8, 17, 2, 15)):
            sched = {"enabled": True, "mode": "recurring", "day": "1", "hour": 2}
            last = {}
            post = self._run(sched, last)
        post.assert_called_once_with("/updates/now", {}, "tok")
        self.assertEqual(last["update"], "1-2026-08-17")

    def test_recurring_wrong_hour_skips(self):
        with patch.object(main, "_local_now",
                          return_value=datetime.datetime(2026, 8, 17, 3, 0)):
            sched = {"enabled": True, "mode": "recurring", "day": "1", "hour": 2}
            post = self._run(sched, {})
        post.assert_not_called()

    def test_backward_compat_no_mode_is_recurring(self):
        with patch.object(main, "_local_now",
                          return_value=datetime.datetime(2026, 8, 17, 2, 0)):
            sched = {"enabled": True, "day": "daily", "hour": 2}  # old conf shape
            post = self._run(sched, {})
        post.assert_called_once_with("/updates/now", {}, "tok")


class StartupGuardTest(unittest.TestCase):
    def test_ready_on_first_attempt(self):
        with patch.object(main, "_api_health_ok", return_value=True), \
             patch.object(main, "_creds_file_ready", return_value=True), \
             patch.object(main, "_get_token", return_value="tok"), \
             patch.object(main.time, "sleep") as slp:
            self.assertEqual(main._wait_for_ready(max_wait_seconds=10), "tok")
        slp.assert_not_called()

    def test_waits_for_creds_then_returns_token(self):
        health = iter([True, True, True])
        creds = iter([False, True])
        tokens = iter(["tok"])

        def health_fn():
            return next(health, True)

        def creds_fn():
            return next(creds, True)

        def token_fn():
            return next(tokens, "")

        with patch.object(main, "_api_health_ok", side_effect=health_fn), \
             patch.object(main, "_creds_file_ready", side_effect=creds_fn), \
             patch.object(main, "_get_token", side_effect=token_fn), \
             patch.object(main.time, "sleep") as slp:
            self.assertEqual(main._wait_for_ready(max_wait_seconds=10), "tok")
        self.assertEqual(slp.call_count, 1)

    def test_waits_for_api_health(self):
        health = iter([False, True])

        def health_fn():
            return next(health, True)

        with patch.object(main, "_api_health_ok", side_effect=health_fn), \
             patch.object(main, "_creds_file_ready", return_value=True), \
             patch.object(main, "_get_token", return_value="tok"), \
             patch.object(main.time, "sleep") as slp:
            self.assertEqual(main._wait_for_ready(max_wait_seconds=10), "tok")
        self.assertEqual(slp.call_count, 1)

    def test_times_out_returns_empty_without_sleep(self):
        with patch.object(main, "_api_health_ok", return_value=True), \
             patch.object(main, "_creds_file_ready", return_value=False), \
             patch.object(main.time, "sleep") as slp:
            self.assertEqual(main._wait_for_ready(max_wait_seconds=0), "")
        slp.assert_not_called()


class TelemetryPruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-prune-")
        self.path = os.path.join(self.tmp, "barenoc.db")
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE metrics ("
            " id INTEGER PRIMARY KEY,"
            " device_id INTEGER NOT NULL,"
            " metric VARCHAR(128) NOT NULL,"
            " ts DATETIME NOT NULL,"
            " value FLOAT NOT NULL"
            ")")
        conn.execute(
            "CREATE INDEX ix_metrics_device_metric_ts "
            "ON metrics(device_id, metric, ts)")
        conn.commit()
        conn.close()

    def _insert(self, ts_iso):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO metrics (device_id, metric, ts, value) VALUES (?, ?, ?, ?)",
                     (1, "ping.latency_ms", ts_iso, 1.0))
        conn.commit()
        conn.close()

    def test_retention_math_disk_aware(self):
        # plenty of free space -> full retention window
        self.assertEqual(main._effective_retention_days(30, 10, 50.0), 30)
        # disk pressure -> clamp to 7 days
        self.assertEqual(main._effective_retention_days(30, 10, 5.0), 7)
        # a configured window shorter than the floor stays short
        self.assertEqual(main._effective_retention_days(3, 10, 5.0), 3)

    def test_prune_deletes_old_rows_only(self):
        old = (datetime.datetime.utcnow() - datetime.timedelta(days=40)).strftime(
            "%Y-%m-%d %H:%M:%S")
        fresh = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S")
        self._insert(old)
        self._insert(fresh)
        result = main.prune_telemetry(self.path, days=30, min_free_pct=10)
        self.assertEqual(result["deleted"], 1)
        conn = sqlite3.connect(self.path)
        n = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_prune_missing_table_is_safe(self):
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE metrics")
        conn.commit()
        conn.close()
        result = main.prune_telemetry(self.path, days=30, min_free_pct=10)
        self.assertEqual(result["deleted"], 0)


if __name__ == "__main__":
    unittest.main()
