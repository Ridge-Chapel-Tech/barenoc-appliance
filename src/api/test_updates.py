#!/usr/bin/env python3
"""Tests for the update progress + notification feature.

Runs inside the barenoc-api container (needs FastAPI/SQLAlchemy). Email is
mocked; STATUS_DIR is patched to a scratch dir for the progress-merge test.

    docker compose exec api python3 -m unittest test_updates -v
"""

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
                       "checked_at": "2026-08-17T00:00:00Z"}, f)
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
        # …and so are its handlers (Check now / Update now / Rollback / Schedule).
        self.assertNotIn('function updCheck', html)
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
        # Check Now is GONE (auto-check on load + release banner is the only flow);
        # the POST /api/v1/updates/check endpoint stays intact for tests/integrations.
        self.assertNotIn('function updCheck', html)
        self.assertNotIn('>Check now<', html)
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


if __name__ == "__main__":
    unittest.main()
