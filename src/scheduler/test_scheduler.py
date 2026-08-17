#!/usr/bin/env python3
"""Tests for the scheduler's update-schedule v2 (local-time + one-time).

Runs with stdlib only (the scheduler main.py imports no third-party packages),
so it works in CI's python3.12 and on the VM host.

    python3 -m unittest test_scheduler -v
"""

import datetime
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


if __name__ == "__main__":
    unittest.main()
