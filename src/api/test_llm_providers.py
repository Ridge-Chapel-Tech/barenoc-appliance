import unittest
from unittest.mock import patch, MagicMock


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
