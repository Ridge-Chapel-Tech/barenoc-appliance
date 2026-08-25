#!/usr/bin/env python3
"""Compliance controls tests (2026-08-25): toggle persistence + baseline
preset + LLM egress enforcement (the one code-path toggle).

Run from src/api:
    python3 -m unittest test_compliance_controls -v
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import compliance  # noqa: E402

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker")
if _WORKER not in sys.path:
    sys.path.append(_WORKER)  # append (not insert) so src/api 'main' stays first


class ControlDefaultsTest(unittest.TestCase):
    def test_home_defaults(self):
        c = compliance.get_controls({})
        self.assertEqual(c["llm_egress"]["state"], "cloud")
        self.assertEqual(c["mfa_enforcement"]["state"], "off")
        self.assertEqual(c["telemetry"]["state"], "on")
        self.assertEqual(c["remote_support"]["state"], "off")
        self.assertEqual(c["retention"]["state"], "sane")
        self.assertEqual(c["audit_log"]["state"], "on")
        self.assertEqual(c["session_policy"]["state"], "relaxed")
        self.assertEqual(c["data_deletion"]["state"], "available")

    def test_every_control_present(self):
        c = compliance.get_controls({})
        self.assertEqual(set(c.keys()), set(compliance.CONTROL_KEYS))
        for k, v in c.items():
            self.assertIn("state", v)
            self.assertIn("enabled_since", v)
            self.assertIn("baseline", v)


class TogglePersistenceTest(unittest.TestCase):
    def test_set_control_persists_and_records_since(self):
        env = {}
        ctl, env = compliance.set_control("llm_egress", "local", env=env,
                                          persist=False)
        self.assertEqual(ctl["llm_egress"]["state"], "local")
        self.assertIsNotNone(ctl["llm_egress"]["enabled_since"])
        self.assertEqual(env["COMPLIANCE_LLM_EGRESS"], "local")
        self.assertIn("COMPLIANCE_LLM_EGRESS_SINCE", env)
        self.assertEqual(env["LLM_EGRESS"], "local")  # effective mirror

    def test_controls_are_independent(self):
        env = {}
        compliance.set_control("telemetry", "off", env=env, persist=False)
        compliance.set_control("mfa_enforcement", "on", env=env, persist=False)
        c = compliance.get_controls(env)
        self.assertEqual(c["telemetry"]["state"], "off")
        self.assertEqual(c["mfa_enforcement"]["state"], "on")
        self.assertEqual(c["llm_egress"]["state"], "cloud")  # untouched

    def test_mirrors_effective_env(self):
        env = {}
        compliance.set_control("telemetry", "off", env=env, persist=False)
        compliance.set_control("mfa_enforcement", "on", env=env, persist=False)
        self.assertEqual(env["TELEMETRY_ENABLED"], "false")
        self.assertEqual(env["MFA_ENFORCED"], "true")

    def test_invalid_control_rejected(self):
        with self.assertRaises(ValueError):
            compliance.set_control("nope", "x", env={}, persist=False)
        with self.assertRaises(ValueError):
            compliance.set_control("llm_egress", "both", env={}, persist=False)
        with self.assertRaises(ValueError):
            compliance.set_control("mfa_enforcement", "maybe", env={}, persist=False)

    def test_fixed_control_is_readonly(self):
        with self.assertRaises(ValueError):
            compliance.set_control("data_deletion", "off", env={}, persist=False)


class PresetTest(unittest.TestCase):
    def test_preset_flips_exactly_the_8_rows(self):
        env = {}
        ctl, env = compliance.apply_preset(env=env, persist=False)
        self.assertEqual(ctl["llm_egress"]["state"], "local")
        self.assertEqual(ctl["mfa_enforcement"]["state"], "on")
        self.assertEqual(ctl["telemetry"]["state"], "off")
        self.assertEqual(ctl["remote_support"]["state"], "off")
        self.assertEqual(ctl["retention"]["state"], "strict")
        self.assertEqual(ctl["audit_log"]["state"], "on")
        self.assertEqual(ctl["session_policy"]["state"], "strict")
        self.assertEqual(ctl["data_deletion"]["state"], "available")
        self.assertIn(compliance.PRESET_PREV_KEY, env)
        for k in compliance.CONTROL_KEYS:
            if compliance.CONTROLS[k]["kind"] == "fixed":
                continue  # fixed controls are read-only; never stamped
            self.assertIsNotNone(ctl[k]["enabled_since"])

    def test_revert_restores_prior_values(self):
        env = {}
        compliance.set_control("llm_egress", "cloud", env=env, persist=False)
        compliance.apply_preset(env=env, persist=False)
        self.assertEqual(compliance.get_controls(env)["llm_egress"]["state"],
                         "local")
        ctl, env, restored = compliance.revert_preset(env=env, persist=False)
        self.assertTrue(restored)
        self.assertEqual(ctl["llm_egress"]["state"], "cloud")

    def test_individual_toggle_after_preset(self):
        env = {}
        compliance.apply_preset(env=env, persist=False)
        compliance.set_control("telemetry", "on", env=env, persist=False)
        c = compliance.get_controls(env)
        self.assertEqual(c["telemetry"]["state"], "on")   # adjusted back
        self.assertEqual(c["retention"]["state"], "strict")  # preset kept


class EgressFilterTest(unittest.TestCase):
    """local-only: the worker chain must never attempt a hosted provider."""

    ENV = {
        "LLM_EGRESS": "local",
        "LLM_PROVIDER_ORDER": "deepseekv4,ollama",
        "LLM_PROVIDER_DEEPSEEKV4_TYPE": "openai",
        "LLM_PROVIDER_DEEPSEEKV4_BASE_URL": "https://api.deepseek.com",
        "LLM_PROVIDER_DEEPSEEKV4_API_KEY": "sk-dead",
        "LLM_PROVIDER_DEEPSEEKV4_DEPLOYMENT": "hosted",
        "LLM_PROVIDER_DEEPSEEKV4_CHAT_MODEL": "deepseek-v4-flash",
        "LLM_PROVIDER_OLLAMA_TYPE": "openai",
        "LLM_PROVIDER_OLLAMA_BASE_URL": "http://192.168.1.50:11434",
        "LLM_PROVIDER_OLLAMA_DEPLOYMENT": "on_prem",
        "LLM_PROVIDER_OLLAMA_CHAT_MODEL": "qwen2.5:7b-instruct",
    }

    def test_provider_order_filters_to_on_prem(self):
        from llm_providers import provider_order, effective_providers
        self.assertEqual(provider_order(self.ENV), ["ollama"])
        self.assertEqual(set(effective_providers(self.ENV).keys()), {"ollama"})

    def test_cloud_mode_keeps_hosted(self):
        from llm_providers import provider_order
        env = dict(self.ENV)
        env["LLM_EGRESS"] = "cloud"
        self.assertEqual(provider_order(env), ["deepseekv4", "ollama"])

    def test_call_llm_zero_cloud_calls_ticket_resolves(self):
        import llm_client
        calls = {"cloud": 0, "local": 0}

        def fake_adapter(provider, model, messages, temperature, max_tokens,
                         timeout):
            if (provider.get("deployment") or "hosted") == "on_prem":
                calls["local"] += 1
            else:
                calls["cloud"] += 1
            return ('{"action":"ping_test","target":"switch-01","params":{},'
                    '"reason":"ok","confidence":0.9}', 10, 5)

        with patch("llm_client.read_env_file", return_value=dict(self.ENV)), \
             patch("llm_client.ADAPTERS",
                   {"openai": fake_adapter, "anthropic": fake_adapter,
                    "gemini": fake_adapter}), \
             patch("llm_client.resolve_prices", return_value=(0.0, 0.0, True)):
            llm_client.maybe_refresh()
            resp = llm_client.call_llm("ping the gateway", "P3")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.action, "ping_test")
        self.assertEqual(calls["cloud"], 0)
        self.assertEqual(calls["local"], 1)


class CloudKeyRefusalTest(unittest.TestCase):
    """local-only egress: saving a hosted provider's key → 400 with the policy
    message."""

    def test_cloud_key_rejected_when_local(self):
        from fastapi import HTTPException
        from routes import settings as s
        env = {"LLM_EGRESS": "local"}
        with patch.object(s, "_read_env_file", return_value=env), \
             patch.object(s, "_write_env_file", side_effect=lambda e: env.update(e)), \
             patch.object(s, "log_event"), patch.object(s, "_write_provider_secret"):
            with self.assertRaises(HTTPException) as cm:
                s._update_llm({"providers": [{
                    "name": "deepseekv4", "type": "openai",
                    "base_url": "https://api.deepseek.com",
                    "deployment": "hosted", "chat_model": "deepseek-v4-flash",
                    "api_key": "sk-abc"}]}, db=None,
                    user=SimpleNamespace(username="admin"))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("Local-only", cm.exception.detail)

    def test_on_prem_key_allowed_when_local(self):
        from routes import settings as s
        env = {"LLM_EGRESS": "local"}
        captured = {}
        with patch.object(s, "_read_env_file", return_value=dict(env)), \
             patch.object(s, "_write_env_file", side_effect=lambda e: captured.update(e)), \
             patch.object(s, "log_event"), patch.object(s, "_write_provider_secret"):
            r = s._update_llm({"providers": [{
                "name": "ollama", "type": "openai",
                "base_url": "http://192.168.1.50:11434",
                "deployment": "on_prem", "chat_model": "qwen2.5:7b-instruct",
                "api_key": ""}]}, db=None, user=SimpleNamespace(username="admin"))
        self.assertEqual(r["status"], "ok")


class ProviderSecretLocalTest(unittest.TestCase):
    def test_write_provider_secret_pins_local_primary(self):
        from routes import settings as s
        tmp = tempfile.mkdtemp(prefix="provsecret-")
        secret = os.path.join(tmp, "llm_provider.json")
        env = {
            "LLM_EGRESS": "local",
            "LLM_PROVIDER_ORDER": "deepseekv4,ollama",
            "LLM_PROVIDER_DEEPSEEKV4_TYPE": "openai",
            "LLM_PROVIDER_DEEPSEEKV4_BASE_URL": "https://api.deepseek.com",
            "LLM_PROVIDER_DEEPSEEKV4_DEPLOYMENT": "hosted",
            "LLM_PROVIDER_DEEPSEEKV4_CHAT_MODEL": "deepseek-v4-flash",
            "LLM_PROVIDER_DEEPSEEKV4_API_KEY": "sk",
            "LLM_PROVIDER_OLLAMA_TYPE": "openai",
            "LLM_PROVIDER_OLLAMA_BASE_URL": "http://192.168.1.50:11434",
            "LLM_PROVIDER_OLLAMA_DEPLOYMENT": "on_prem",
            "LLM_PROVIDER_OLLAMA_CHAT_MODEL": "qwen2.5:7b-instruct",
        }
        with patch.object(s, "_read_env_file", return_value=env), \
             patch.object(s, "PROVIDER_SECRET_FILE", secret):
            s._write_provider_secret()
        with open(secret) as f:
            d = json.load(f)
        self.assertTrue(d.get("local"))
        self.assertEqual(d["provider"], "openai")
        self.assertEqual(d["model"], "qwen2.5:7b-instruct")
        self.assertEqual(d["base_url"], "http://192.168.1.50:11434")
        self.assertEqual(d["api_key"], "ollama")


if __name__ == "__main__":
    unittest.main(verbosity=2)
