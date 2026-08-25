#!/usr/bin/env python3
"""Tests for the update progress + notification feature.

Runs inside the barenoc-api container (needs FastAPI/SQLAlchemy). Email is
mocked; STATUS_DIR is patched to a scratch dir for the progress-merge test.

    docker compose exec api python3 -m unittest test_updates -v
"""

import datetime
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="updates-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from routes import updates  # noqa: E402


class NotifyTest(unittest.TestCase):
    def _patch_email(self, sent):
        def fake_send(to, subject, body_html=None, body_text=None, overrides=None):
            sent["to"] = to
            sent["subject"] = subject
            return True, None
        return [
            patch.object(updates, "_current_version", return_value="2026.08.16.a"),
            patch("llm_providers.read_env_file",
                  return_value={"ALERT_RECIPIENTS": "ops@example.com"}),
            patch("emailer.send_email", side_effect=fake_send),
            patch("emailer.alert_html", return_value="<table/>"),
        ]

    def test_notify_done_sends_alert(self):
        sent = {}
        ps = self._patch_email(sent)
        for p in ps:
            p.start()
        try:
            r = updates.update_notify({"stage": "done", "message": "complete"},
                                      SimpleNamespace(username="admin"))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r["notified"], True)
        self.assertEqual(sent["to"], "ops@example.com")
        self.assertIn("updated to 2026.08.16.a", sent["subject"])

    def test_notify_failed_sends_alert(self):
        sent = {}
        ps = self._patch_email(sent)
        for p in ps:
            p.start()
        try:
            updates.update_notify({"stage": "failed", "message": "checksum mismatch"},
                                  SimpleNamespace(username="admin"))
        finally:
            for p in ps:
                p.stop()
        self.assertIn("FAILED", sent["subject"])

    def test_notify_no_recipients_is_noop(self):
        with patch.object(updates, "_current_version", return_value="2026.08.16.a"), \
             patch("llm_providers.read_env_file", return_value={}):
            r = updates.update_notify({"stage": "done", "message": "x"},
                                      SimpleNamespace(username="admin"))
        self.assertEqual(r["notified"], False)

    def test_notify_bad_stage_rejected(self):
        with self.assertRaises(Exception):
            updates.update_notify({"stage": "download"}, SimpleNamespace(username="admin"))


class ProgressTest(unittest.TestCase):
    def test_progress_merged_into_status(self):
        with patch.object(updates, "STATUS_DIR", _TMP):
            with open(os.path.join(_TMP, "progress.json"), "w") as f:
                json.dump({"stage": "download", "pct": 20,
                           "message": "fetching release", "at": "now"}, f)
            try:
                st = updates.update_status(SimpleNamespace(username="admin"))
            finally:
                os.remove(os.path.join(_TMP, "progress.json"))
        self.assertEqual(st["progress"]["stage"], "download")
        self.assertEqual(st["progress"]["pct"], 20)

    def test_no_progress_file_empty(self):
        with patch.object(updates, "STATUS_DIR", _TMP):
            st = updates.update_status(SimpleNamespace(username="admin"))
        self.assertEqual(st["progress"], {})


class StatusLiveVersionTest(unittest.TestCase):
    """/status must report the LIVE installed version and stop a stale
    terminal 'Complete 100%' banner (the 2026-08-17 prod repro)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="updates-status-")
        # A check result persisted when 2026.08.16.f was installed.
        with open(os.path.join(self.tmp, "status.json"), "w") as f:
            json.dump({
                "checked_at": "2026-08-16T19:46:42.168500Z",
                "current": "2026.08.16.f",
                "latest": "2026.08.16.f",
                "kind": "patch",
                "available": False,
                "changelog": "https://github.com/Ridge-Chapel-Tech/barenoc-appliance/releases/tag/v2026.08.16.f",
                "tarball": "https://barenoc.com/downloads/bareNOC-2026.08.16.f.tar.gz",
                "checksum": "https://barenoc.com/downloads/bareNOC-2026.08.16.f.sha256",
                "update_access": {"valid": True, "open": True, "key_set": True,
                                   "revoked": False, "reason": "",
                                   "note": "free & open (beta)"},
                "manifest_error": "",
            }, f)
        # The last in-app update (persisted forever) — history only.
        with open(os.path.join(self.tmp, "update_result.json"), "w") as f:
            json.dump({"ok": True, "action": "update", "version": "2026.08.16.d",
                       "at": "2026-08-16T18:27:50+00:00",
                       "services_restarted": True, "reboot_required": False}, f)
        # The terminal progress from that .d self-update.
        with open(os.path.join(self.tmp, "progress.json"), "w") as f:
            json.dump({"stage": "done", "pct": 100,
                       "message": "update complete — services restarted",
                       "at": "2026-08-16T18:27:50+00:00"}, f)

    def _status(self):
        with patch.object(updates, "STATUS_DIR", self.tmp), \
             patch.object(updates, "_current_version", return_value="2026.08.17.a"):
            return updates.update_status(SimpleNamespace(username="admin"))

    def test_live_version_wins_over_stale_status(self):
        st = self._status()
        self.assertEqual(st["current"], "2026.08.17.a")

    def test_stale_check_is_flagged_for_refresh(self):
        self.assertTrue(self._status()["check_stale"])

    def test_terminal_done_from_older_version_confirmed(self):
        st = self._status()
        # The raw stage stays visible to the notify watcher…
        self.assertEqual(st["progress"]["stage"], "done")
        # …but the card renders the steady "up to date" state, not a banner.
        self.assertTrue(st["progress"]["confirmed"])

    def test_last_update_kept_as_history(self):
        st = self._status()
        self.assertEqual(st["last_update"]["version"], "2026.08.16.d")


class ProgressConfirmationTest(unittest.TestCase):
    def _status(self, current, progress, result=None):
        tmp = tempfile.mkdtemp(prefix="updates-confirm-")
        if result is not None:
            with open(os.path.join(tmp, "update_result.json"), "w") as f:
                json.dump(result, f)
        with open(os.path.join(tmp, "progress.json"), "w") as f:
            json.dump(progress, f)
        with patch.object(updates, "STATUS_DIR", tmp), \
             patch.object(updates, "_current_version", return_value=current):
            return updates.update_status(SimpleNamespace(username="admin"))

    def test_done_matching_live_version_confirmed(self):
        st = self._status(
            "2026.08.17.a",
            {"stage": "done", "pct": 100, "message": "complete", "at": "now"},
            result={"ok": True, "action": "update", "version": "2026.08.17.a", "at": "now"},
        )
        self.assertTrue(st["progress"]["confirmed"])

    def test_inflight_progress_not_confirmed(self):
        st = self._status(
            "2026.08.17.a",
            {"stage": "download", "pct": 20, "message": "downloading", "at": "now"},
        )
        self.assertEqual(st["progress"]["stage"], "download")
        self.assertNotIn("confirmed", st["progress"])

    def test_failed_progress_not_confirmed(self):
        st = self._status(
            "2026.08.17.a",
            {"stage": "failed", "pct": 100, "message": "checksum mismatch", "at": "now"},
            result={"ok": False, "action": "update", "version": "2026.08.17.b",
                    "error": "checksum mismatch"},
        )
        self.assertEqual(st["progress"]["stage"], "failed")
        self.assertNotIn("confirmed", st["progress"])

    def test_check_not_stale_when_persisted_current_matches(self):
        tmp = tempfile.mkdtemp(prefix="updates-fresh-")
        with open(os.path.join(tmp, "status.json"), "w") as f:
            json.dump({"current": "2026.08.17.a",
                       "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, f)
        with patch.object(updates, "STATUS_DIR", tmp), \
             patch.object(updates, "_current_version", return_value="2026.08.17.a"):
            st = updates.update_status(SimpleNamespace(username="admin"))
        self.assertEqual(st["current"], "2026.08.17.a")
        self.assertFalse(st["check_stale"])


class UpdatesUxTemplateTest(unittest.TestCase):
    """UI move: the Dashboard loses the Updates card (gains a slim release
    banner), and the System page gains the full Updates section reachable via
    /system#updates. Static template checks — cheap, no browser needed."""

    @staticmethod
    def _read(name):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "templates", name), encoding="utf-8") as f:
            return f.read()

    def test_dashboard_has_release_banner_not_card(self):
        html = self._read("dashboard.html")
        self.assertIn('id="release-banner"', html)
        self.assertIn('/system#updates', html)
        # The full management card is gone from the dashboard…
        self.assertNotIn('id="updates-card"', html)
        # …and so are its handlers (Update now / Rollback / Schedule). The
        # Check-now button is System-only (dashboard shows the banner).
        self.assertNotIn('function updCheckNow', html)
        self.assertNotIn('function updNow', html)
        self.assertNotIn('function updRollback', html)
        self.assertNotIn('function updSaveSchedule', html)
        self.assertNotIn('function updToggleSchedule', html)

    def test_dashboard_banner_gated_on_available(self):
        html = self._read("dashboard.html")
        # The banner un-hides only on `available` (auto-check on load drives it).
        self.assertIn('if (s.available)', html)
        self.assertIn('loadReleaseBanner()', html)

    def test_system_has_updates_section(self):
        html = self._read("system.html")
        self.assertIn('id="updates"', html)
        # Check Now button (08-18 fix: a stable build must be able to force a
        # re-check — the auto-check only fired when the build changed).
        self.assertIn('function updCheckNow', html)
        self.assertIn('>Check now<', html)
        self.assertIn('function updNow', html)
        self.assertIn('function updRollback', html)
        self.assertIn('id="upd-progress"', html)
        self.assertIn('id="upd-schedule"', html)

    def test_system_auto_checks_on_load(self):
        html = self._read("system.html")
        # The auto-check-on-load still drives the Updates card.
        self.assertIn("updFetch('/check'", html)
        self.assertIn('updLoad();', html)

    def test_system_updates_loads_on_page_load(self):
        html = self._read("system.html")
        self.assertIn('updLoad();', html)

    def test_status_fields_used_by_ui_unchanged(self):
        with patch.object(updates, "STATUS_DIR", tempfile.mkdtemp(prefix="updates-ux-")), \
             patch.object(updates, "_current_version", return_value="2026.08.17.a"):
            st = updates.update_status(SimpleNamespace(username="admin"))
        for key in ("current", "latest", "available", "checked_at", "check_stale",
                    "schedule", "progress", "last_update", "update_access"):
            self.assertIn(key, st)


class TzConversionTest(unittest.TestCase):
    """Local-time semantics (08-17): the configured hour/day + one-time when
    are wall-clock in the appliance TZ, converted DST-safe via zoneinfo."""

    def test_local_to_utc_dst_safe(self):
        winter = updates._local_to_utc(datetime.datetime(2026, 1, 15, 12, 0),
                                       "America/New_York")
        self.assertEqual(winter.hour, 17)  # EST = UTC-5
        self.assertEqual(winter.tzinfo, datetime.timezone.utc)
        summer = updates._local_to_utc(datetime.datetime(2026, 7, 15, 12, 0),
                                       "America/New_York")
        self.assertEqual(summer.hour, 16)  # EDT = UTC-4

    def test_local_to_utc_invalid_tz_falls_back_utc(self):
        dt = updates._local_to_utc(datetime.datetime(2026, 7, 15, 12, 0), "Not/AZone")
        self.assertEqual(dt.hour, 12)

    def test_parse_local_dt_accepts_datetime_local(self):
        self.assertEqual(updates._parse_local_dt("2026-08-17T02:00"),
                         datetime.datetime(2026, 8, 17, 2, 0))
        self.assertEqual(updates._parse_local_dt("2026-08-17 02:30:00"),
                         datetime.datetime(2026, 8, 17, 2, 30))

    def test_appliance_tz_reads_env(self):
        with patch("routes.updates._read_env_file", return_value={"TZ": "America/New_York"}):
            self.assertEqual(updates._appliance_tz(), "America/New_York")
        with patch("routes.updates._read_env_file", return_value={}), \
             patch.dict(os.environ, {"TZ": "Europe/London"}):
            self.assertEqual(updates._appliance_tz(), "Europe/London")


class ScheduleConfV2Test(unittest.TestCase):
    """Schedule conf v2: mode/day/hour/when/fired, backward-compatible with
    the old enabled/day/hour file (no mode = recurring, local time)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="updates-sched-")
        self._dir = patch.object(updates, "STATUS_DIR", self.tmp)
        self._dir.start()

    def tearDown(self):
        self._dir.stop()

    def test_read_schedule_backward_compat_defaults_recurring(self):
        with open(os.path.join(self.tmp, "update_schedule.conf"), "w") as f:
            f.write("enabled=true\nday=1\nhour=3\n")
        sc = updates._read_schedule()
        self.assertEqual(sc["mode"], "recurring")
        self.assertEqual(sc["day"], "1")
        self.assertEqual(sc["hour"], 3)
        self.assertTrue(sc["enabled"])
        self.assertEqual(sc["when"], "")
        self.assertEqual(sc["fired"], "")

    def test_read_schedule_onetime(self):
        with open(os.path.join(self.tmp, "update_schedule.conf"), "w") as f:
            f.write("mode=onetime\nenabled=true\nwhen=2026-08-17T02:00\nfired=2026-08-17T02:01:00Z\n")
        sc = updates._read_schedule()
        self.assertEqual(sc["mode"], "onetime")
        self.assertEqual(sc["when"], "2026-08-17T02:00")
        self.assertEqual(sc["fired"], "2026-08-17T02:01:00Z")

    def test_set_schedule_recurring_writes_canonical(self):
        body = updates.ScheduleBody(enabled=True, mode="recurring", day="1", hour=3)
        r = updates.set_schedule(body, SimpleNamespace(username="admin"))
        self.assertEqual(r["schedule"]["mode"], "recurring")
        with open(os.path.join(self.tmp, "update_schedule.conf")) as f:
            content = f.read()
        self.assertIn("mode=recurring\n", content)
        self.assertIn("day=1\n", content)
        self.assertIn("hour=3\n", content)
        self.assertIn("when=\n", content)

    def test_set_schedule_onetime_requires_future(self):
        past = (updates._local_now() - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
        with self.assertRaises(Exception):
            updates.set_schedule(
                updates.ScheduleBody(enabled=True, mode="onetime", when=past),
                SimpleNamespace(username="admin"))
        future = (updates._local_now() + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        r = updates.set_schedule(
            updates.ScheduleBody(enabled=True, mode="onetime", when=future),
            SimpleNamespace(username="admin"))
        self.assertEqual(r["schedule"]["mode"], "onetime")
        self.assertEqual(r["schedule"]["when"], future)

    def test_complete_marks_fired_and_disables(self):
        updates._write_schedule({"mode": "onetime", "enabled": True,
                                 "day": "daily", "hour": 2,
                                 "when": "2026-08-17T02:00", "fired": ""})
        r = updates.complete_schedule(SimpleNamespace(username="admin"))
        self.assertFalse(r["schedule"]["enabled"])
        self.assertTrue(r["schedule"]["fired"])
        with open(os.path.join(self.tmp, "update_schedule.conf")) as f:
            self.assertIn("enabled=false\n", f.read())

    def test_cancel_disables_and_clears_onetime(self):
        updates._write_schedule({"mode": "onetime", "enabled": True,
                                 "day": "daily", "hour": 2,
                                 "when": "2026-08-17T02:00", "fired": ""})
        r = updates.cancel_schedule(SimpleNamespace(username="admin"))
        self.assertFalse(r["schedule"]["enabled"])
        self.assertEqual(r["schedule"]["when"], "")
        self.assertEqual(r["schedule"]["fired"], "")


class DefaultScheduleTest(unittest.TestCase):
    """Auto-update ON by default (2026-08-25): the default schedule constant +
    ensure_default_update_schedule is idempotent — writes once when the conf is
    absent, never overwrites an existing (enabled OR disabled) conf."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="updates-default-")
        self._dir = patch.object(updates, "STATUS_DIR", self.tmp)
        self._dir.start()

    def tearDown(self):
        self._dir.stop()

    def test_default_constant_is_weekly_sunday_3am(self):
        d = updates.DEFAULT_UPDATE_SCHEDULE
        self.assertEqual(d["mode"], "recurring")
        self.assertTrue(d["enabled"])
        self.assertEqual(d["day"], "0")   # 0 = Sunday
        self.assertEqual(d["hour"], 3)    # 03:00 local
        self.assertEqual(d["when"], "")
        self.assertEqual(d["fired"], "")

    def test_default_writes_when_absent(self):
        r = updates.ensure_default_update_schedule()
        self.assertTrue(r["written"])
        sc = updates._read_schedule()
        self.assertTrue(sc["enabled"])
        self.assertEqual(sc["mode"], "recurring")
        self.assertEqual(sc["day"], "0")
        self.assertEqual(sc["hour"], 3)

    def test_existing_disabled_conf_untouched_byte_for_byte(self):
        path = os.path.join(self.tmp, "update_schedule.conf")
        content = "mode=recurring\nenabled=false\nday=1\nhour=2\nwhen=\nfired=\n"
        with open(path, "w") as f:
            f.write(content)
        r = updates.ensure_default_update_schedule()
        self.assertFalse(r["written"])
        with open(path) as f:
            self.assertEqual(f.read(), content)

    def test_existing_custom_enabled_conf_untouched_byte_for_byte(self):
        path = os.path.join(self.tmp, "update_schedule.conf")
        content = "mode=recurring\nenabled=true\nday=daily\nhour=5\nwhen=\nfired=\n"
        with open(path, "w") as f:
            f.write(content)
        r = updates.ensure_default_update_schedule()
        self.assertFalse(r["written"])
        with open(path) as f:
            self.assertEqual(f.read(), content)

    def test_day_hour_semantics_no_off_by_one(self):
        # The default day 0 (Sunday) must line up with the scheduler's
        # conversion: Sunday.weekday()==6 -> (6+1)%7 == 0.
        sunday = datetime.datetime(2026, 8, 23)  # a known Sunday
        self.assertEqual(sunday.weekday(), 6)
        self.assertEqual((sunday.weekday() + 1) % 7,
                         int(updates.DEFAULT_UPDATE_SCHEDULE["day"]))


class ScheduleUiV2TemplateTest(unittest.TestCase):
    """System → Updates schedule section gains the mode toggle + one-time
    picker + current-schedule display + Cancel (static template checks)."""

    @staticmethod
    def _read(name):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "templates", name), encoding="utf-8") as f:
            return f.read()

    def test_schedule_section_has_mode_toggle_and_onetime(self):
        html = self._read("system.html")
        self.assertIn('id="upd-sched-mode"', html)
        self.assertIn('id="upd-sched-when"', html)
        self.assertIn('type="datetime-local"', html)
        self.assertIn('id="upd-sched-cancel"', html)
        self.assertIn('local time', html)

    def test_schedule_has_cancel_handler(self):
        html = self._read("system.html")
        self.assertIn('function updCancelSchedule', html)
        self.assertIn('/schedule/cancel', html)

    def test_summary_renders_local_and_recurring(self):
        html = self._read("system.html")
        self.assertIn('function updSchedSummary', html)
        self.assertIn('updModeChange', html)

    def test_autoupdate_optout_label_present(self):
        # The schedule toggle IS the opt-out — clearly labeled one-click off.
        html = self._read("system.html")
        self.assertIn('id="upd-sched-enable"', html)
        self.assertIn('Auto-update', html)
        self.assertIn('Uncheck to opt out', html)
        self.assertIn('Auto-update is off.', html)


class SignaturePlumbingTest(unittest.TestCase):
    """Release signing: versions.json assets.signature flows /check → status →
    /now → update_request.json. Backward compatible — a manifest without a
    signature yields an empty string (never a crash)."""

    def test_run_check_reads_signature(self):
        with patch.object(updates, "STATUS_DIR",
                          tempfile.mkdtemp(prefix="updates-sig-")), \
             patch.object(updates, "_fetch_json", return_value={
                 "version": "2026.08.25.a",
                 "kind": "patch",
                 "changelog": "",
                 "assets": {
                     "tarball": "https://barenoc.com/downloads/bareNOC-2026.08.25.a.tar.gz",
                     "checksums": "https://barenoc.com/downloads/bareNOC-2026.08.25.a.sha256",
                     "signature": "https://barenoc.com/downloads/bareNOC-2026.08.25.a.tar.gz.sig",
                 },
             }), \
             patch.object(updates, "_read_env_file", return_value={}), \
             patch.object(updates, "_current_version", return_value="2026.08.24.b"):
            st = updates._run_check()
        self.assertEqual(
            st["signature"],
            "https://barenoc.com/downloads/bareNOC-2026.08.25.a.tar.gz.sig")

    def test_run_check_without_signature_empty(self):
        with patch.object(updates, "STATUS_DIR",
                          tempfile.mkdtemp(prefix="updates-sig-")), \
             patch.object(updates, "_fetch_json", return_value={
                 "version": "2026.08.24.b",
                 "kind": "patch",
                 "changelog": "",
                 "assets": {"tarball": "https://x/t", "checksums": "https://x/s"},
             }), \
             patch.object(updates, "_read_env_file", return_value={}), \
             patch.object(updates, "_current_version", return_value="2026.08.24.b"):
            st = updates._run_check()
        self.assertEqual(st["signature"], "")


if __name__ == "__main__":
    unittest.main()


class VersionOrderingTest(unittest.TestCase):
    """Regression (08-17): 'available' must require a genuinely NEWER version —
    a stale manifest showing .a while running .b must NOT flag an update."""

    def test_version_gt(self):
        from routes.updates import _version_gt as g
        self.assertTrue(g("2026.08.17.b", "2026.08.17.a"))
        self.assertTrue(g("2026.08.17.a", "2026.08.16.i"))
        self.assertFalse(g("2026.08.17.a", "2026.08.17.b"))   # downgrade
        self.assertFalse(g("2026.08.17.b", "2026.08.17.b"))   # equal
        self.assertFalse(g("junk", "2026.08.17.b"))           # unparseable
        self.assertFalse(g("", ""))

class UpdatesCheckStaleTest(unittest.TestCase):
    """_check_is_stale: stable builds must still discover new releases."""

    def test_fresh_check_not_stale(self):
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.assertFalse(updates._check_is_stale(ts, hours=6))

    def test_old_check_stale(self):
        ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
        self.assertTrue(updates._check_is_stale(ts, hours=6))

    def test_bad_timestamp_stale(self):
        self.assertTrue(updates._check_is_stale("not-a-date", hours=6))

    def test_missing_timestamp_stale(self):
        self.assertTrue(updates._check_is_stale(None, hours=6))

    def test_disabled_window_never_stale(self):
        ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)).isoformat()
        self.assertFalse(updates._check_is_stale(ts, hours=0))


class AutoReportTest(unittest.TestCase):
    """The post-update auto-report hook: only real failures, gated by
    AUTO_REPORT_POST_UPDATE (default ON), ships stage + evidence through the
    in-app Submit-Report path."""

    def _call(self, body):
        return updates.update_auto_report(body or {},
                                          db=SimpleNamespace(),
                                          user=SimpleNamespace(username="agent"))

    def test_auto_report_enabled_default(self):
        with patch("routes.updates._read_env_file", return_value={}):
            self.assertTrue(updates._auto_report_enabled())

    def test_auto_report_disabled(self):
        for v in ("false", "0", "no", "off"):
            with patch("routes.updates._read_env_file",
                       return_value={"AUTO_REPORT_POST_UPDATE": v}):
                self.assertFalse(updates._auto_report_enabled(), v)

    def test_disabled_returns_not_reported(self):
        with patch.object(updates, "_auto_report_enabled", return_value=False):
            r = self._call({"stage": "failed"})
        self.assertFalse(r["reported"])
        self.assertIn("disabled", r["note"])

    def test_non_failure_stage_not_reported(self):
        with patch.object(updates, "_auto_report_enabled", return_value=True), \
             patch.object(updates, "_read_progress", return_value={"stage": "done"}):
            r = self._call({"stage": "done"})
        self.assertFalse(r["reported"])
        self.assertIn("no reportable failure", r["note"])

    def test_failure_files_report_with_evidence(self):
        with patch.object(updates, "_auto_report_enabled", return_value=True), \
             patch.object(updates, "_current_version", return_value="2026.08.20.b"), \
             patch.object(updates, "_read_progress", return_value={
                 "stage": "failed", "pct": 100,
                 "message": "post-update verification failed", "at": "now"}), \
             patch.object(updates, "_read_update_result", return_value={
                 "ok": False, "version": "2026.08.20.b", "error": "x"}), \
             patch.object(updates, "_read_verify_result", return_value={
                 "entitled": True, "ok": False}), \
             patch("report_submit.submit_report",
                   return_value={"thread_id": "abc"}) as submit, \
             patch("routes.support.build_bundle", return_value="# bundle") as bundle:
            r = self._call({"stage": "failed"})
        self.assertTrue(r["reported"])
        self.assertEqual(r["thread_id"], "abc")
        submit.assert_called_once()
        bundle.assert_called_once()
        comment = submit.call_args[0][0]
        self.assertIn("Post-update verification failed", comment)
        self.assertIn("post-update verification failed", comment)
        self.assertIn("evidence", comment)

    def test_submit_runtime_error_surfaces_without_raising(self):
        with patch.object(updates, "_auto_report_enabled", return_value=True), \
             patch.object(updates, "_read_progress", return_value={
                 "stage": "failed", "message": "boom", "at": "now"}), \
             patch.object(updates, "_read_update_result", return_value={}), \
             patch.object(updates, "_read_verify_result", return_value={}), \
             patch("routes.support.build_bundle", return_value="# bundle"), \
             patch("report_submit.submit_report",
                   side_effect=RuntimeError("forum-submit rejected the report: HTTP 502")):
            r = self._call({"stage": "failed"})
        self.assertFalse(r["reported"])
        self.assertIn("rejected", r["error"])

