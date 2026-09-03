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
from unittest.mock import patch, mock_open

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

    def test_disabled_schedule_never_queues(self):
        sched = {"enabled": False, "mode": "recurring", "day": "0", "hour": 3}
        post = self._run(sched, {})
        post.assert_not_called()

    def test_recurring_day_zero_fires_on_sunday(self):
        # day 0 = Sunday (the default auto-update window). 2026-08-23 is a
        # Sunday — assert no off-by-one against the default hour 3.
        with patch.object(main, "_local_now",
                          return_value=datetime.datetime(2026, 8, 23, 3, 0)):
            sched = {"enabled": True, "mode": "recurring", "day": "0", "hour": 3}
            last = {}
            post = self._run(sched, last)
        post.assert_called_once_with("/updates/now", {}, "tok")
        self.assertEqual(last["update"], "0-2026-08-23")

    def test_recurring_day_zero_skips_monday(self):
        # The following day (Monday) must NOT fire for the Sunday default.
        with patch.object(main, "_local_now",
                          return_value=datetime.datetime(2026, 8, 24, 3, 0)):
            sched = {"enabled": True, "mode": "recurring", "day": "0", "hour": 3}
            post = self._run(sched, {})
        post.assert_not_called()


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


class UpdateProgressHookTest(unittest.TestCase):
    """check_update_progress: terminal transitions email once and, on FAILED,
    file the post-update auto-report once (restart-safe via the marker files)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="upd-prog-")
        self.notify_marker = os.path.join(self.tmp, "notify.key")
        self.report_marker = os.path.join(self.tmp, "report.key")
        p = patch.multiple(main, UPDATE_NOTIFY_MARKER=self.notify_marker,
                           UPDATE_AUTO_REPORT_MARKER=self.report_marker)
        p.start()
        self.addCleanup(p.stop)

    def _status(self, stage):
        return {"current": "2026.08.20.b",
                "progress": {"stage": stage, "pct": 100,
                             "message": "m", "at": "2026-08-20T00:00:00Z"}}

    def test_failed_stage_notifies_and_auto_reports(self):
        get = patch.object(main, "_api_get", return_value=self._status("failed"))
        post = patch.object(main, "_api_post")
        postj = patch.object(main, "_api_post_json", return_value={"reported": True})
        get.start(); self.addCleanup(get.stop)
        mp = post.start(); self.addCleanup(post.stop)
        mj = postj.start(); self.addCleanup(postj.stop)
        main.check_update_progress("tok", {})
        self.assertEqual(mp.call_count, 1)
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(mj.call_args[0][0], "/updates/auto-report")
        body = mj.call_args[0][1]
        self.assertEqual(body["stage"], "failed")
        self.assertEqual(body["version"], "2026.08.20.b")

    def test_done_stage_notifies_only(self):
        get = patch.object(main, "_api_get", return_value=self._status("done"))
        post = patch.object(main, "_api_post")
        postj = patch.object(main, "_api_post_json")
        get.start(); self.addCleanup(get.stop)
        mp = post.start(); self.addCleanup(post.stop)
        mj = postj.start(); self.addCleanup(postj.stop)
        main.check_update_progress("tok", {})
        self.assertEqual(mp.call_count, 1)
        self.assertEqual(mj.call_count, 0)

    def test_non_terminal_stage_noop(self):
        get = patch.object(main, "_api_get", return_value=self._status("download"))
        post = patch.object(main, "_api_post")
        postj = patch.object(main, "_api_post_json")
        get.start(); self.addCleanup(get.stop)
        mp = post.start(); self.addCleanup(post.stop)
        mj = postj.start(); self.addCleanup(postj.stop)
        main.check_update_progress("tok", {})
        self.assertEqual(mp.call_count, 0)
        self.assertEqual(mj.call_count, 0)

    def test_transition_reported_once_across_restarts(self):
        get = patch.object(main, "_api_get", return_value=self._status("failed"))
        post = patch.object(main, "_api_post")
        postj = patch.object(main, "_api_post_json", return_value={"reported": True})
        get.start(); self.addCleanup(get.stop)
        mp = post.start(); self.addCleanup(post.stop)
        mj = postj.start(); self.addCleanup(postj.stop)
        main.check_update_progress("tok", {})
        self.assertEqual(mj.call_count, 1)
        # a scheduler restart (fresh in-memory dict) must NOT re-report — the
        # on-disk marker is the restart-safe guard.
        main.check_update_progress("tok", {})
        self.assertEqual(mj.call_count, 1)
        self.assertEqual(mp.call_count, 1)


class ServiceCheckPollTest(unittest.TestCase):
    """check_service_checks: the scheduler's poll pass for service monitors."""

    def test_config_defaults(self):
        with patch.object(main, "_read_env", return_value={}):
            self.assertEqual(main._service_check_config(), (True, 5))

    def test_config_disabled_and_interval(self):
        with patch.object(main, "_read_env", return_value={
                "SERVICE_CHECK_ENABLED": "false",
                "SERVICE_CHECK_INTERVAL_MIN": "10"}):
            self.assertEqual(main._service_check_config(), (False, 10))

    def test_poll_posts_once(self):
        with patch.object(main, "_read_env", return_value={
                "SERVICE_CHECK_ENABLED": "true"}):
            post = patch.object(main, "_api_post")
            mp = post.start(); self.addCleanup(post.stop)
            main.check_service_checks("tok")
            mp.assert_called_once_with("/service-checks/poll", {}, "tok")

    def test_poll_http_error_is_logged_not_raised(self):
        with patch.object(main, "_read_env", return_value={
                "SERVICE_CHECK_ENABLED": "true"}):
            post = patch.object(main, "_api_post", side_effect=
                                urllib.error.HTTPError("u", 500, "boom", None, None))
            mp = post.start(); self.addCleanup(post.stop)
            main.check_service_checks("tok")   # must not raise
            self.assertEqual(mp.call_count, 1)


class RevokeIntegrityPollTest(unittest.TestCase):
    """check_revoke_integrity: the scheduler's poll pass for the integrity sweep."""

    def test_poll_posts_once(self):
        postj = patch.object(main, "_api_post_json",
                             return_value={"status": "ok", "checked": 1, "flagged": 0})
        mj = postj.start(); self.addCleanup(postj.stop)
        main.check_revoke_integrity("tok")
        mj.assert_called_once_with("/revoke-integrity/sweep", {}, "tok")

    def test_poll_flags_logged(self):
        postj = patch.object(main, "_api_post_json",
                             return_value={"status": "ok", "checked": 2, "flagged": 1})
        mj = postj.start(); self.addCleanup(postj.stop)
        with patch.object(main.logger, "warning") as warn:
            main.check_revoke_integrity("tok")
        warn.assert_called_once()
        self.assertIn("flagged 1", warn.call_args[0][0])

    def test_poll_http_error_is_logged_not_raised(self):
        postj = patch.object(main, "_api_post_json", side_effect=
                             urllib.error.HTTPError("u", 500, "boom", None, None))
        mj = postj.start(); self.addCleanup(postj.stop)
        main.check_revoke_integrity("tok")   # must not raise
        self.assertEqual(mj.call_count, 1)


class WindowsScheduleTest(unittest.TestCase):
    """F8 — per-device Windows health/cleanup schedule due-logic."""

    def test_period_key_daily(self):
        now = datetime.datetime(2026, 9, 3, 14, 0)
        self.assertEqual(main._win_period_key("daily", now), "2026-09-03")
        self.assertEqual(main._win_period_key("low_usage", now), "2026-09-03")

    def test_period_key_weekly(self):
        now = datetime.datetime(2026, 9, 3, 14, 0)
        self.assertEqual(main._win_period_key("weekly", now), "2026-W36")

    def test_sched_int(self):
        self.assertEqual(main._win_sched_int({}, "health_hour", 3), 3)
        self.assertEqual(main._win_sched_int({"health_hour": "bad"}, "health_hour", 3), 3)
        self.assertEqual(main._win_sched_int({"health_hour": 7}, "health_hour", 3), 7)

    def _run_windows(self, devices, now, ticket_id="TKT-TEST-0001"):
        get = patch.object(main, "_api_get", return_value={"devices": devices})
        tz = patch.object(main, "_appliance_tz", return_value="UTC")
        ln = patch.object(main, "_local_now", return_value=now)
        pj = patch.object(main, "_api_post_json",
                          return_value={"ticket_id": ticket_id})
        pa = patch.object(main, "_api_patch")
        op = patch("builtins.open", mock_open())
        get.start(); tz.start(); ln.start()
        mock_pj = pj.start(); mock_pa = pa.start(); op.start()
        for p in (get, tz, ln, pj, pa, op):
            self.addCleanup(p.stop)
        main.check_windows_health_schedule("tok", {})
        return mock_pj, mock_pa

    def test_daily_health_due_queues_and_marks(self):
        now = datetime.datetime(2026, 9, 3, 14, 0)
        devices = [{"id": 7, "name": "dads-pc", "ip_address": "10.0.0.5",
                    "ssh_configured": True,
                    "windows_health_schedule": {"health": "daily",
                                                "health_hour": 3, "cleanup": "off"}}]
        pj, pa = self._run_windows(devices, now)
        pj.assert_called_once()
        self.assertEqual(pj.call_args[0][0], "/devices/7/windows-health-ticket")
        self.assertEqual(pj.call_args[0][1], {"kind": "health"})
        pa.assert_called_once_with("/devices/7/windows-schedule/mark-run",
                                   {"health": "2026-09-03"}, "tok")

    def test_daily_health_skips_when_already_run_today(self):
        now = datetime.datetime(2026, 9, 3, 14, 0)
        devices = [{"id": 7, "name": "dads-pc", "ip_address": "10.0.0.5",
                    "ssh_configured": True,
                    "windows_health_schedule": {"health": "daily", "health_hour": 3,
                                                "cleanup": "off",
                                                "last_health_run": "2026-09-03"}}]
        pj, pa = self._run_windows(devices, now)
        pj.assert_not_called()
        pa.assert_not_called()

    def test_weekly_health_skips_wrong_day(self):
        now = datetime.datetime(2026, 9, 3, 14, 0)
        wrong_day = (now.weekday() + 1) % 7 + 1 if (now.weekday() + 1) % 7 < 6 else 0
        devices = [{"id": 7, "name": "dads-pc", "ip_address": "10.0.0.5",
                    "ssh_configured": True,
                    "windows_health_schedule": {"health": "weekly", "health_hour": 3,
                                                "health_day": wrong_day,
                                                "cleanup": "off"}}]
        pj, pa = self._run_windows(devices, now)
        pj.assert_not_called()

    def test_low_usage_cleanup_queues_in_window(self):
        now = datetime.datetime(2026, 9, 3, 3, 0)
        devices = [{"id": 7, "name": "dads-pc", "ip_address": "10.0.0.5",
                    "ssh_configured": True,
                    "windows_health_schedule": {"health": "off", "cleanup": "low_usage",
                                                "cleanup_window_start": 2,
                                                "cleanup_window_end": 5}}]
        pj, pa = self._run_windows(devices, now)
        pj.assert_called_once()
        self.assertEqual(pj.call_args[0][1], {"kind": "cleanup"})
        pa.assert_called_once_with("/devices/7/windows-schedule/mark-run",
                                   {"cleanup": "2026-09-03"}, "tok")

    def test_on_request_cleanup_never_scheduled(self):
        now = datetime.datetime(2026, 9, 3, 3, 0)
        devices = [{"id": 7, "name": "dads-pc", "ip_address": "10.0.0.5",
                    "ssh_configured": True,
                    "windows_health_schedule": {"health": "off",
                                                "cleanup": "on_request"}}]
        pj, pa = self._run_windows(devices, now)
        pj.assert_not_called()
        pa.assert_not_called()

    def test_non_ssh_device_skipped(self):
        now = datetime.datetime(2026, 9, 3, 14, 0)
        devices = [{"id": 7, "name": "dads-pc", "ip_address": "10.0.0.5",
                    "ssh_configured": False,
                    "windows_health_schedule": {"health": "daily", "health_hour": 3}}]
        pj, pa = self._run_windows(devices, now)
        pj.assert_not_called()


if __name__ == "__main__":
    unittest.main()
