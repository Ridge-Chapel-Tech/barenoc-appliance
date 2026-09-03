#!/usr/bin/env python3
"""Tests for the LLM tier router (F6 cost optimization — the WHERE lane).

    python3 -m unittest test_tierrouter -v

Covers: the built-in class → tier decisions (judgment/customer-visible stay
cloud, the executor is window-inverted), the local-down cloud fallback, the
cost-stats counter + savings math, tier-map merging, and on-prem provider
resolution from BareNOC's registry.
"""

import datetime
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import tierrouter

UTC = datetime.timezone.utc

_RATE = {
    "provider": "deepseek",
    "off_peak_factor": 0.5,
    "peak_windows": [
        {"days": "mon-fri", "start": 1, "end": 4},
        {"days": "mon-fri", "start": 6, "end": 10},
    ],
}


def _monday(hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 9, 7, hour, 0, tzinfo=UTC)


class TierDecisionTest(unittest.TestCase):
    def test_judgment_classes_stay_cloud(self):
        for cls in ("ticket_judge", "ticket_technician"):
            peak = tierrouter.tier_for(cls, _monday(2), tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
            offpeak = tierrouter.tier_for(cls, _monday(5), tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
            self.assertEqual(peak["tier"], "cloud", cls)
            self.assertEqual(offpeak["tier"], "cloud", cls)
            self.assertFalse(peak["draft_flagged"])

    def test_title_is_cloud_and_customer_visible(self):
        d = tierrouter.tier_for("ticket_title", _monday(2),
                                tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
        self.assertEqual(d["tier"], "cloud")
        self.assertTrue(d["customer_visible"])

    def test_executor_is_window_inverted(self):
        peak = tierrouter.tier_for("ticket_executor", _monday(2),
                                   tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
        offpeak = tierrouter.tier_for("ticket_executor", _monday(5),
                                      tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
        self.assertEqual(peak["tier"], "local")       # relief valve at peak
        self.assertEqual(offpeak["tier"], "cloud")    # quality off-peak (half price)
        self.assertEqual(peak["local_model"], "qwen2.5:7b")

    def test_unknown_class_safe_default(self):
        d = tierrouter.tier_for("does_not_exist", _monday(2),
                                tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
        self.assertEqual(d["tier"], "cloud")

    def test_customer_visible_local_flags_draft(self):
        tm = tierrouter.load_tier_map(env={})
        tm["classes"]["ticket_title"] = {"default_tier": "local", "customer_visible": True}
        d = tierrouter.tier_for("ticket_title", _monday(2), tier_map=tm, rate_cfg=_RATE)
        self.assertEqual(d["tier"], "local")
        self.assertTrue(d["draft_flagged"])


class LocalFallbackTest(unittest.TestCase):
    def test_local_down_falls_back_to_cloud(self):
        env = {"LOCAL_LLM_URL": "http://192.168.4.67:11434",
               "LOCAL_LLM_MODEL": "qwen2.5:7b"}
        with patch("tierrouter.local_healthy", return_value=False):
            d = tierrouter.route("ticket_executor", _monday(2), env=env,
                                 tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
        self.assertEqual(d["tier"], "cloud")
        self.assertTrue(d["local_fallback"])

    def test_local_healthy_keeps_local(self):
        env = {"LOCAL_LLM_URL": "http://192.168.4.67:11434",
               "LOCAL_LLM_MODEL": "qwen2.5:7b"}
        with patch("tierrouter.local_healthy", return_value=True):
            d = tierrouter.route("ticket_executor", _monday(2), env=env,
                                 tier_map=tierrouter.DEFAULT_TIER_MAP, rate_cfg=_RATE)
        self.assertEqual(d["tier"], "local")
        self.assertFalse(d["local_fallback"])


class CostStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_call_increments_and_savings(self):
        tierrouter.record_call("local", "ticket_executor",
                               env={}, dt_utc=_monday(5), state_dir_=self.tmp.name)
        tierrouter.record_call("cloud", "ticket_judge",
                               env={}, dt_utc=_monday(5), state_dir_=self.tmp.name)
        s = tierrouter.cost_summary(env={}, state_dir_=self.tmp.name)
        self.assertEqual(s["calls"], {"local": 1, "cloud": 1})
        self.assertEqual(s["by_class"]["ticket_executor"]["local"], 1)
        self.assertEqual(s["by_class"]["ticket_judge"]["cloud"], 1)
        self.assertEqual(s["est_savings_usd"], 0.002)  # 1 local × $0.002

    def test_window_flip_resets_counter(self):
        tierrouter.record_call("local", "ticket_executor",
                               env={}, dt_utc=_monday(5), state_dir_=self.tmp.name)
        # A PEAK-time call flips the window state -> counter resets.
        tierrouter.record_call("cloud", "ticket_judge",
                               env={}, dt_utc=_monday(2), state_dir_=self.tmp.name)
        s = tierrouter.cost_summary(env={}, state_dir_=self.tmp.name)
        self.assertEqual(s["calls"], {"local": 0, "cloud": 1})
        self.assertEqual(s["window_state"], "PEAK")

    def test_local_fallback_counter(self):
        tierrouter.record_call("cloud", "ticket_executor",
                               env={}, dt_utc=_monday(5), local_fallback=True,
                               state_dir_=self.tmp.name)
        s = tierrouter.cost_summary(env={}, state_dir_=self.tmp.name)
        self.assertEqual(s["local_down_fallbacks"], 1)


class TierMapTest(unittest.TestCase):
    def test_partial_file_merges_over_default(self):
        # A file that only overrides one class still keeps the rest valid.
        with patch("tierrouter.tier_map_path", return_value=os.path.join(
                tempfile.mkdtemp(), "tier_map.json")):
            # no file -> default
            tm = tierrouter.load_tier_map(env={})
        self.assertIn("ticket_judge", tm["classes"])
        self.assertIn("ticket_executor", tm["classes"])

    def test_all_classes(self):
        self.assertIn("ticket_executor", tierrouter.all_classes(env={}))


class LocalProviderTest(unittest.TestCase):
    @staticmethod
    def _fake_llm_providers(providers: dict):
        mod = types.ModuleType("llm_providers")
        mod.load_providers = lambda env=None: providers
        mod.read_env_file = lambda: {}
        return mod

    def test_on_prem_provider_resolved(self):
        providers = {
            "deepseekv4": {"name": "deepseekv4", "deployment": "hosted",
                           "base_url": "https://api.deepseek.com",
                           "chat_model": "deepseek-v4-flash"},
            "ollama": {"name": "ollama", "deployment": "on_prem",
                       "base_url": "http://10.0.10.20:11434",
                       "chat_model": "llama3.1:8b"},
        }
        with patch.dict(sys.modules, {"llm_providers": self._fake_llm_providers(providers)}):
            p = tierrouter.local_provider(env={})
            self.assertEqual(p["name"], "ollama")
            self.assertEqual(tierrouter.local_url(env={}), "http://10.0.10.20:11434")
            self.assertEqual(tierrouter.local_model(env={}), "llama3.1:8b")

    def test_env_override_wins(self):
        env = {"LOCAL_LLM_URL": "http://m7:11434", "LOCAL_LLM_MODEL": "qwen2.5:14b"}
        self.assertEqual(tierrouter.local_url(env=env), "http://m7:11434")
        self.assertEqual(tierrouter.local_model(env=env), "qwen2.5:14b")

    def test_no_local_returns_none(self):
        with patch.dict(sys.modules, {"llm_providers": self._fake_llm_providers({})}):
            self.assertIsNone(tierrouter.local_provider(env={}))
            self.assertEqual(tierrouter.local_url(env={}), "")
            self.assertEqual(tierrouter.local_model(env={}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
