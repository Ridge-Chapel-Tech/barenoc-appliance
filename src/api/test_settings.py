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


class AutonomyPiFlagTest(unittest.TestCase):
    """Saving autonomy=autonomous must write an ACTIVE PI_AGENT_ENABLED=true
    (bare value) so the worker's _pi_enabled() hot-read path routes open-ended
    tickets to Lily instead of silently falling back to the judge (08-17 bug)."""

    def _save_policy(self, config: dict, read_env: dict) -> dict:
        from database import SessionLocal, init_db
        init_db()
        db = SessionLocal()
        captured = {}
        try:
            with patch.object(s, "_read_env_file", return_value=dict(read_env)), \
                 patch.object(s, "_write_env_file", side_effect=lambda e: captured.update(e)):
                s.update_section("policy", config, db=db,
                                 user=SimpleNamespace(role="admin", username="tester"))
        finally:
            db.close()
        return captured

    def test_autonomous_save_writes_pi_flag(self):
        captured = self._save_policy({"profile": "autonomous"}, {})
        self.assertEqual(captured["LLM_POLICY_PROFILE"], "autonomous")
        self.assertEqual(captured["PI_AGENT_ENABLED"], "true")

    def test_fresh_install_autonomous_default_enables_pi(self):
        # Fresh install: .env.example ships PI_AGENT_ENABLED commented out, so
        # the read env has no key. Saving the wizard's autonomous default must
        # still append the active flag (order-independent).
        captured = self._save_policy({"profile": "autonomous"}, {})
        self.assertEqual(captured["PI_AGENT_ENABLED"], "true")

    def test_existing_false_flag_overwritten(self):
        # Order-independent the other way: an already-active false line is rewritten.
        captured = self._save_policy({"profile": "autonomous"},
                                     {"PI_AGENT_ENABLED": "false"})
        self.assertEqual(captured["PI_AGENT_ENABLED"], "true")

    def test_flag_value_is_bare(self):
        # No inline "#" comment or trailing whitespace — either becomes part of
        # the value and breaks read_env_file's parse (found 08-17).
        captured = self._save_policy({"profile": "autonomous"}, {})
        self.assertEqual(captured["PI_AGENT_ENABLED"], "true")
        self.assertNotIn("#", captured["PI_AGENT_ENABLED"])
        self.assertNotIn(" ", captured["PI_AGENT_ENABLED"])

    def test_non_autonomous_leaves_flag_untouched(self):
        # Balanced/strict don't touch the flag (semantics unchanged): a
        # manually-enabled pi stays enabled, an unset one stays unset.
        captured = self._save_policy({"profile": "balanced"},
                                     {"PI_AGENT_ENABLED": "true"})
        self.assertEqual(captured["PI_AGENT_ENABLED"], "true")
        captured2 = self._save_policy({"profile": "strict"}, {})
        self.assertNotIn("PI_AGENT_ENABLED", captured2)


class EnvFileWriteTest(unittest.TestCase):
    """_write_env_file: in-place (same inode) + bare values, no inline comments."""

    def test_write_preserves_inode_and_writes_bare_flag(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            path = f.name
            f.write("# PI_AGENT_ENABLED=false\nLLM_POLICY_PROFILE=balanced\n")
        try:
            ino_before = os.stat(path).st_ino
            s._write_env_file({"LLM_POLICY_PROFILE": "autonomous",
                               "PI_AGENT_ENABLED": "true"}, path=path)
            ino_after = os.stat(path).st_ino
            self.assertEqual(ino_before, ino_after)  # in-place, not tmpfile+rename
            content = open(path).read()
            # active flag is bare (no inline comment), commented line preserved
            self.assertIn("PI_AGENT_ENABLED=true\n", content)
            self.assertNotIn("PI_AGENT_ENABLED=true #", content)
            self.assertIn("# PI_AGENT_ENABLED=false", content)
            self.assertIn("LLM_POLICY_PROFILE=autonomous\n", content)
        finally:
            os.unlink(path)


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

    def test_identity_appliance_defaults(self):
        r = s.get_section("identity", user=SimpleNamespace(role="admin"))
        self.assertEqual(r["appliance_ip"], "192.168.4.207")
        self.assertEqual(r["appliance_host"], "app.barenoc.com")
        self.assertIs(r["passkey_viable"], True)
        self.assertIn("app.barenoc.com", r["hosts_lines"])

    def test_identity_passkey_warning_on_private_tld(self):
        with patch.object(s, "_read_env_file",
                          return_value={"APPLIANCE_DOMAIN": "company.local"}):
            r = s.get_section("identity", user=SimpleNamespace(role="admin"))
        self.assertIs(r["passkey_viable"], False)
        self.assertIn("public-suffix", r["passkey_warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
