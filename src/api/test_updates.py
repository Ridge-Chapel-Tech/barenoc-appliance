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


if __name__ == "__main__":
    unittest.main()
