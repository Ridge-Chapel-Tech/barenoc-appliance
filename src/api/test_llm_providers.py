import unittest
from unittest.mock import patch, MagicMock


class PricingTest(unittest.TestCase):
    """Cost metering: known prices are metered; unknown hosted models use a
    documented, labeled fallback; on-prem (local) inference is free."""

    @staticmethod
    def _hosted():
        return {"deployment": "hosted", "input_price": 0, "output_price": 0,
                "price_mode": "auto", "base_url": ""}

    def test_known_static_table_price_is_not_estimate(self):
        from llm_providers import resolve_prices
        inp, out, est = resolve_prices(self._hosted(), "deepseek-chat")
        self.assertEqual((inp, out), (0.27, 1.10))
        self.assertFalse(est)

    def test_unknown_hosted_model_uses_fallback_estimate(self):
        from llm_providers import resolve_prices, FALLBACK_ESTIMATE_PRICE
        inp, out, est = resolve_prices(self._hosted(), "some-unknown-model")
        self.assertEqual(inp, FALLBACK_ESTIMATE_PRICE["input"])
        self.assertEqual(out, FALLBACK_ESTIMATE_PRICE["output"])
        self.assertTrue(est)

    def test_on_prem_unknown_model_is_free(self):
        from llm_providers import resolve_prices
        provider = {"deployment": "on_prem", "input_price": 0, "output_price": 0,
                    "price_mode": "auto", "base_url": ""}
        inp, out, est = resolve_prices(provider, "llama3")
        self.assertEqual((inp, out), (0.0, 0.0))
        self.assertFalse(est)

    def test_cost_for_tokens_math(self):
        from llm_providers import cost_for_tokens
        # deepseek-chat: input 0.27, output 1.10 per 1M tokens
        cost, est = cost_for_tokens(self._hosted(), "deepseek-chat",
                                    1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 0.27 + 1.10, places=6)
        self.assertFalse(est)

    def test_resolve_model_cost_falls_back_to_table(self):
        from llm_providers import resolve_model_cost
        # Not in the registry — but a known model name still prices from the table.
        cost, est = resolve_model_cost("deepseek-reasoner", 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.55, places=6)
        self.assertFalse(est)


class ReasoningFallbackTest(unittest.TestCase):
    """Regression (08-17 forum): DeepSeek returns content='' with a long system
    prompt — the answer lands in reasoning_content. The adapter must fall back
    to it or every request escalates as a non-JSON empty response."""

    def test_content_empty_falls_back_to_reasoning(self):
        from llm_providers import _adapter_openai
        resp = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "We need respond JSON only. ready"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        with patch("llm_providers.httpx.post", return_value=resp) as post:
            text, pt, rt = _adapter_openai({"base_url": "https://x", "api_key": "k"},
                                           "m", [{"role": "user", "content": "hi"}],
                                           temperature=0.1, max_tokens=10, timeout=5)
        self.assertEqual(text, "We need respond JSON only. ready")
        self.assertEqual(pt, 10)

    def test_normal_content_still_wins(self):
        from llm_providers import _adapter_openai
        resp = MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": "ready"}}], "usage": {}}
        with patch("llm_providers.httpx.post", return_value=resp):
            text, _, _ = _adapter_openai({"base_url": "https://x", "api_key": "k"},
                                         "m", [], temperature=0.1, max_tokens=5, timeout=5)
        self.assertEqual(text, "ready")


if __name__ == "__main__":
    unittest.main()
