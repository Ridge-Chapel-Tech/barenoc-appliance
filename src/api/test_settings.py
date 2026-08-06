#!/usr/bin/env python3
"""In-container tests for the Settings policy GET (effective values).

The GET must return the profile's effective defaults (worker-consistent) so
an intentionally-empty save (e.g. no approval priorities in autonomous) stays
empty on reload — never re-injected as "P1,P2".

    docker compose exec api python3 -m unittest test_settings -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="settings-policy-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from routes import settings as s


def _get_general(env: dict) -> dict:
    with patch.object(s, "_read_env_file", return_value=env):
        return s.get_section("general", user=SimpleNamespace(role="admin"))


class ChatToggleTest(unittest.TestCase):
    """Desktop chat client enable/disable lives in Settings → General."""

    def test_defaults_enabled(self):
        r = _get_general({})
        self.assertIs(r["chat_client_enabled"], True)

    def test_explicit_enabled(self):
        r = _get_general({"CHAT_CLIENT_ENABLED": "true"})
        self.assertIs(r["chat_client_enabled"], True)

    def test_explicit_disabled(self):
        r = _get_general({"CHAT_CLIENT_ENABLED": "false"})
        self.assertIs(r["chat_client_enabled"], False)

    def test_put_persists_bool(self):
        from database import SessionLocal, init_db
        from audit import log_event
        from types import SimpleNamespace
        import tempfile as _tf
        _t = _tf.mkdtemp(prefix="chat-toggle-put-")
        os.environ["DATABASE_URL"] = f"sqlite:///{_t}/test.db"
        init_db()
        db = SessionLocal()
        env = {}
        with patch.object(s, "_read_env_file", return_value=env), \
             patch.object(s, "_write_env_file", side_effect=lambda e: env.update(e)):
            r = s.update_section("general", {"chat_client_enabled": False},
                                 db=db, user=SimpleNamespace(role="admin", username="tester"))
        self.assertEqual(r["updated"], 1)
        self.assertEqual(env["CHAT_CLIENT_ENABLED"], "false")
        db.close()


def _get(env: dict) -> dict:
    with patch.object(s, "_read_env_file", return_value=env):
        return s.get_section("policy", user=SimpleNamespace(role="admin"))


class PolicyGetEffectiveTest(unittest.TestCase):
    def test_autonomous_defaults(self):
        r = _get({"LLM_POLICY_PROFILE": "autonomous"})
        self.assertEqual(r["approval_priorities"], "")
        self.assertEqual(r["risk_filters"], "none")
        self.assertTrue(r["write_autoexec"])
        self.assertTrue(r["judge_required"])

    def test_strict_defaults(self):
        r = _get({"LLM_POLICY_PROFILE": "strict"})
        self.assertEqual(r["approval_priorities"], "P1,P2")
        self.assertEqual(r["risk_filters"], "all")
        self.assertFalse(r["write_autoexec"])

    def test_empty_save_stays_empty(self):
        # the reported bug: user unchecks all priorities, saves (env written
        # empty), reload must NOT re-check P1/P2
        r = _get({"LLM_POLICY_PROFILE": "autonomous",
                  "LLM_POLICY_APPROVAL_PRIORITIES": ""})
        self.assertEqual(r["approval_priorities"], "")

    def test_explicit_override_wins(self):
        r = _get({"LLM_POLICY_PROFILE": "autonomous",
                  "LLM_POLICY_APPROVAL_PRIORITIES": "P3"})
        self.assertEqual(r["approval_priorities"], "P3")

    def test_legacy_defaults(self):
        r = _get({})
        self.assertEqual(r["approval_priorities"], "P1,P2")
        self.assertEqual(r["risk_filters"], "all")


class LlmProviderUpdateTest(unittest.TestCase):
    """PUT /settings/llm: provider_order validation + pruning removed providers."""

    def _update(self, config: dict, env: dict) -> dict:
        captured = {}
        with patch.object(s, "_read_env_file", return_value=dict(env)), \
             patch.object(s, "_write_env_file", side_effect=lambda e: captured.update(e)), \
             patch.object(s, "log_event"), \
             patch.object(s, "_write_provider_secret"):
            s._update_llm(config, db=None, user=SimpleNamespace(username="admin"))
        return captured

    def test_removed_provider_keys_pruned(self):
        env = {
            "LLM_ACTIVE_PROVIDER": "deepseekv4",
            "LLM_PROVIDER_ORDER": "deepseekv4,gemini",
            "LLM_PROVIDER_DEEPSEEKV4_TYPE": "openai",
            "LLM_PROVIDER_GEMINI_TYPE": "gemini",
            "LLM_PROVIDER_GEMINI_BASE_URL": "https://generativelanguage.googleapis.com",
        }
        captured = self._update({"providers": [{
            "name": "deepseekv4", "type": "openai",
            "base_url": "https://api.deepseek.com", "deployment": "hosted",
            "chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-flash",
            "thinking": "disabled", "price_mode": "static"}]}, env)
        self.assertNotIn("LLM_PROVIDER_GEMINI_TYPE", captured)
        self.assertNotIn("LLM_PROVIDER_GEMINI_BASE_URL", captured)
        self.assertEqual(captured.get("LLM_PROVIDER_ORDER"), "deepseekv4")
        self.assertEqual(captured.get("LLM_ACTIVE_PROVIDER"), "deepseekv4")
        self.assertEqual(captured.get("LLM_PROVIDER_DEEPSEEKV4_TYPE"), "openai")

    def test_order_rejects_duplicates(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._update({"provider_order": ["a", "a"]},
                         {"LLM_PROVIDER_A_TYPE": "openai"})

    def test_order_rejects_more_than_three(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._update({"provider_order": ["a", "b", "c", "d"]},
                         {"LLM_PROVIDER_A_TYPE": "openai",
                          "LLM_PROVIDER_B_TYPE": "openai",
                          "LLM_PROVIDER_C_TYPE": "openai",
                          "LLM_PROVIDER_D_TYPE": "openai"})


class BackupsSectionTest(unittest.TestCase):
    """Settings → Backups: schedule conf written for the host poller."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="backups-sec-")
        self.orig_conf = s.BACKUP_CONF
        self.orig_status = s.BACKUP_STATUS_JSON
        s.BACKUP_CONF = os.path.join(self.tmp, "backup_schedule.conf")
        s.BACKUP_STATUS_JSON = os.path.join(self.tmp, "status.json")

    def tearDown(self):
        s.BACKUP_CONF = self.orig_conf
        s.BACKUP_STATUS_JSON = self.orig_status

    def _put(self, cfg):
        from database import SessionLocal, init_db
        init_db()
        db = SessionLocal()
        try:
            return s.update_section("backups", cfg, db=db,
                                    user=SimpleNamespace(role="admin", username="t"))
        finally:
            db.close()

    def test_get_defaults(self):
        r = s.get_section("backups", user=SimpleNamespace(role="admin"))
        self.assertIs(r["usb_backup_enabled"], True)
        self.assertEqual(r["usb_backup_day"], "3")   # Wednesday
        self.assertEqual(r["usb_backup_hour"], 2)

    def test_put_writes_conf(self):
        r = self._put({"usb_backup_enabled": True, "usb_backup_day": "daily",
                       "usb_backup_hour": 3})
        self.assertEqual(r["updated"], 3)
        conf = open(s.BACKUP_CONF).read()
        self.assertIn("USB_BACKUP_ENABLED=true", conf)
        self.assertIn("USB_BACKUP_DAY=daily", conf)
        self.assertIn("USB_BACKUP_HOUR=3", conf)

    def test_put_roundtrip(self):
        self._put({"usb_backup_day": "6", "usb_backup_hour": 5})
        r = s.get_section("backups", user=SimpleNamespace(role="admin"))
        self.assertEqual(r["usb_backup_day"], "6")
        self.assertEqual(r["usb_backup_hour"], 5)

    def test_bad_day_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._put({"usb_backup_day": "tuesday"})

    def test_bad_hour_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._put({"usb_backup_hour": 24})

    def test_run_now_sets_flag(self):
        r = self._put({"run_usb_backup_now": True})
        self.assertEqual(r["updated"], 1)
        self.assertIn("RUN_USB_BACKUP_NOW=true", open(s.BACKUP_CONF).read())

    def test_schedule_edit_cancels_pending_run_now(self):
        self._put({"run_usb_backup_now": True})
        self._put({"usb_backup_hour": 4})
        self.assertIn("RUN_USB_BACKUP_NOW=false", open(s.BACKUP_CONF).read())

    def test_appliance_host_fresh_status(self):
        import json as _json
        from datetime import datetime, timezone
        with open(s.BACKUP_STATUS_JSON, "w") as f:
            f.write(_json.dumps({"updated": datetime.now(timezone.utc).isoformat()}))
        r = s.get_section("backups", user=SimpleNamespace(role="admin"))
        self.assertIs(r["appliance_host"], True)

    def test_appliance_host_stale_status(self):
        import json as _json
        from datetime import datetime, timedelta, timezone
        with open(s.BACKUP_STATUS_JSON, "w") as f:
            f.write(_json.dumps({"updated": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}))
        r = s.get_section("backups", user=SimpleNamespace(role="admin"))
        self.assertIs(r["appliance_host"], False)

    def test_appliance_host_byo_no_status_file(self):
        # BYO: no host has ever pushed status.json
        r = s.get_section("backups", user=SimpleNamespace(role="admin"))
        self.assertIs(r["appliance_host"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
