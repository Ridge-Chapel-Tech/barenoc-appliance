#!/usr/bin/env python3
"""Tests for honest LLM token accounting in llm_client.call_llm.

Run from src/worker:
    python3 -m unittest test_llm_client -v

Covers the repair-retry path: when the model's first reply isn't valid JSON and
a repair call is issued, BOTH calls consumed provider tokens — the cost must
sum them (retries included, each API call counted exactly once), never discard
the repair usage nor double-count a single successful call.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))           # worker/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))  # api/

import llm_client

_VALID = '{"action": "ping_test", "target": "switch-01", "params": {}, ' \
         '"reason": "r", "confidence": 0.9}'


def _provider():
    return {
        "name": "deepseek", "type": "openai", "api_key": "k",
        "chat_model": "deepseek-chat", "reasoner_model": "deepseek-reasoner",
        "judge_model": "deepseek-reasoner", "deployment": "hosted",
        "base_url": "", "input_price": 0, "output_price": 0,
        "price_mode": "auto", "thinking": "auto",
    }


class RepairRetryTokenAccountingTest(unittest.TestCase):
    def test_repair_retry_tokens_are_accumulated(self):
        calls = {"n": 0}

        def fake_adapter(p, model, messages, temperature, max_tokens, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return "this is not json", 100, 50
            return _VALID, 40, 20

        with patch.object(llm_client, "provider_chain", return_value=[_provider()]), \
             patch.dict(llm_client.ADAPTERS, {"openai": fake_adapter}), \
             patch.object(llm_client, "resolve_prices", return_value=(0.27, 1.10, False)):
            resp = llm_client.call_llm("ping the switch", "P3", provider_name="deepseek")

        self.assertIsNotNone(resp)
        self.assertEqual(resp.prompt_tokens, 140)   # 100 + 40 (both calls counted)
        self.assertEqual(resp.response_tokens, 70)  # 50 + 20
        expected = round((140 / 1_000_000 * 0.27) + (70 / 1_000_000 * 1.10), 6)
        self.assertEqual(resp.cost_usd, expected)
        self.assertFalse(resp.cost_estimate)

    def test_single_call_is_not_double_counted(self):
        calls = {"n": 0}

        def fake_adapter(p, model, messages, temperature, max_tokens, timeout):
            calls["n"] += 1
            return _VALID, 100, 50

        with patch.object(llm_client, "provider_chain", return_value=[_provider()]), \
             patch.dict(llm_client.ADAPTERS, {"openai": fake_adapter}), \
             patch.object(llm_client, "resolve_prices", return_value=(0.27, 1.10, False)):
            resp = llm_client.call_llm("ping the switch", "P3", provider_name="deepseek")

        self.assertIsNotNone(resp)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(resp.prompt_tokens, 100)
        self.assertEqual(resp.response_tokens, 50)


if __name__ == "__main__":
    unittest.main()
