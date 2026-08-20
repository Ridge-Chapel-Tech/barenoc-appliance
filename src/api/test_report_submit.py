#!/usr/bin/env python3
"""Tests for the Submit Report flow (vetting, gate, submit payload).

Runs inside the barenoc-api container (report_vet/report_gate/report_submit
import llm_providers + httpx, so the api deps are needed). The LLM adapter is
patched with fixture responses — no live provider call.

    docker compose exec api python3 -m unittest test_report_submit -v
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import report_gate
import report_submit
import report_vet


def _fake_adapter(raw: str):
    def adapter(provider, model, messages, temperature, max_tokens, timeout):
        return raw, 1, 1
    return adapter


def _vet(raw: str) -> dict:
    providers = {"test": {"type": "openai", "api_key": "k",
                          "deployment": "hosted", "judge_model": "m"}}
    with patch.object(report_vet, "read_env_file", return_value={}), \
         patch.object(report_vet, "load_providers", return_value=providers), \
         patch.object(report_vet, "provider_order", return_value=["test"]), \
         patch.object(report_vet, "judge_model_name", return_value="m"), \
         patch.dict(report_vet.ADAPTERS, {"openai": _fake_adapter(raw)}):
        return report_vet.vet_comment("wifi drops at night")


class VettingTest(unittest.TestCase):
    def test_bug(self):
        r = _vet('{"verdict": "bug", "explanation": "clear defect"}')
        self.assertEqual(r["verdict"], "bug")

    def test_not_bug(self):
        r = _vet('{"verdict": "not-bug", "explanation": "that is a feature request"}')
        self.assertEqual(r["verdict"], "not-bug")

    def test_unclear(self):
        r = _vet('{"verdict": "unclear", "explanation": "needs symptoms"}')
        self.assertEqual(r["verdict"], "unclear")

    def test_prose_with_fenced_json(self):
        r = _vet('Sure.\n```json\n{"verdict":"bug","explanation":"x"}\n```')
        self.assertEqual(r["verdict"], "bug")

    def test_invalid_verdict_falls_back_to_bug(self):
        r = _vet('{"verdict": "praise", "explanation": "x"}')
        self.assertEqual(r["verdict"], "bug")

    def test_provider_failure_fails_open(self):
        providers = {"test": {"type": "openai", "api_key": "k",
                              "deployment": "hosted", "judge_model": "m"}}
        def boom(*a, **k):
            raise RuntimeError("down")
        with patch.object(report_vet, "read_env_file", return_value={}), \
             patch.object(report_vet, "load_providers", return_value=providers), \
             patch.object(report_vet, "provider_order", return_value=["test"]), \
             patch.object(report_vet, "judge_model_name", return_value="m"), \
             patch.dict(report_vet.ADAPTERS, {"openai": boom}):
            r = report_vet.vet_comment("wifi")
        self.assertEqual(r["verdict"], "bug")
        self.assertIn("unavailable", r["note"])

    def test_no_provider_fails_open(self):
        with patch.object(report_vet, "read_env_file", return_value={}), \
             patch.object(report_vet, "load_providers", return_value={}), \
             patch.object(report_vet, "provider_order", return_value=[]):
            r = report_vet.vet_comment("wifi")
        self.assertEqual(r["verdict"], "bug")
        self.assertIn("no LLM provider", r["note"])


class GateTest(unittest.TestCase):
    def test_open_default(self):
        st = report_gate.report_gate_status(env={})
        self.assertTrue(st["open"])
        self.assertEqual(st["mode"], "open")

    def test_open_explicit(self):
        self.assertTrue(report_gate.report_gate_status(env={"REPORT_GATE": "open"})["open"])

    def test_support_gated_denies_without_entitlement(self):
        st = report_gate.report_gate_status(env={"REPORT_GATE": "support"})
        self.assertFalse(st["open"])
        self.assertEqual(st["mode"], "support")
        self.assertIn("Support subscription", st["note"])
        self.assertFalse(report_gate.report_gate_allowed(env={"REPORT_GATE": "support"}))

    def test_support_gated_beta_grant_active(self):
        env = {"REPORT_GATE": "support", "SUPPORT_GRANT": "beta-grant-key",
               "SUPPORT_GRANT_EXPIRES_AT": "2999-01-01T00:00:00Z"}
        st = report_gate.report_gate_status(env=env)
        self.assertTrue(st["open"])
        self.assertTrue(st["beta"])
        self.assertTrue(report_gate.support_allowed(env=env))
        self.assertTrue(report_gate.report_gate_allowed(env=env))

    def test_support_gated_beta_grant_expired(self):
        env = {"REPORT_GATE": "support", "SUPPORT_GRANT": "beta-grant-key",
               "SUPPORT_GRANT_EXPIRES_AT": "2000-01-01T00:00:00Z"}
        st = report_gate.report_gate_status(env=env)
        self.assertFalse(st["open"])
        self.assertFalse(report_gate.support_grant_active(env=env))
        self.assertFalse(report_gate.report_gate_allowed(env=env))

    def test_support_gated_beta_grant_missing(self):
        st = report_gate.report_gate_status(env={"REPORT_GATE": "support"})
        self.assertFalse(st["open"])
        self.assertFalse(report_gate.support_grant_active(env={"REPORT_GATE": "support"}))


class PayloadTest(unittest.TestCase):
    def test_payload_shape(self):
        with patch.object(report_submit, "read_env_file",
                          return_value={"APPLIANCE_HOST": "app.barenoc.com"}):
            user = SimpleNamespace(username="admin", display_name="Administrator")
            p = report_submit.build_payload(
                "wifi drops", user, bundle="# bundle", bundle_filename="b.md",
                flagged=True)
        self.assertEqual(p["comment"], "wifi drops")
        self.assertEqual(p["reporter"], "admin")
        self.assertEqual(p["display_name"], "Administrator")
        self.assertEqual(p["bundle"], "# bundle")
        self.assertEqual(p["bundle_filename"], "b.md")
        self.assertEqual(p["appliance"], "app.barenoc.com")
        self.assertIs(p["flagged"], True)
        self.assertIn("version", p)

    def test_payload_display_name_falls_back_to_username(self):
        with patch.object(report_submit, "read_env_file", return_value={}):
            user = SimpleNamespace(username="admin", display_name="")
            p = report_submit.build_payload("x", user)
        self.assertEqual(p["display_name"], "admin")


if __name__ == "__main__":
    unittest.main()
