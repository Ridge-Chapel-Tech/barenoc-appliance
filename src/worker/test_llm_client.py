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


class GenerateTitleTest(unittest.TestCase):
    """generate_title: a cheap one-shot title call that never raises and
    never blocks the caller — None signals the heuristic fallback."""

    def test_returns_cleaned_title(self):
        def fake_adapter(p, model, messages, temperature, max_tokens, timeout):
            return '"  Title: Update the plex server  "', 10, 5

        with patch.object(llm_client, "provider_chain", return_value=[_provider()]), \
             patch.object(llm_client, "maybe_refresh"), \
             patch.dict(llm_client.ADAPTERS, {"openai": fake_adapter}):
            title = llm_client.generate_title(
                "I see my plex server is on http://192.168.4.13/")
        self.assertEqual(title, "Update the plex server")

    def test_strips_preamble_meta_text(self):
        # End-to-end B3 regression: the adapter returns the polite preamble the
        # model often adds, and generate_title must hand back only the title.
        def fake_adapter(p, model, messages, temperature, max_tokens, timeout):
            return "Sure! Here's a concise title: Update the plex server", 10, 5

        with patch.object(llm_client, "provider_chain", return_value=[_provider()]), \
             patch.object(llm_client, "maybe_refresh"), \
             patch.dict(llm_client.ADAPTERS, {"openai": fake_adapter}):
            title = llm_client.generate_title(
                "I see my plex server is on http://192.168.4.13/")
        self.assertEqual(title, "Update the plex server")

    def test_provider_failure_returns_none(self):
        def boom(p, model, messages, temperature, max_tokens, timeout):
            raise RuntimeError("down")

        with patch.object(llm_client, "provider_chain", return_value=[_provider()]), \
             patch.object(llm_client, "maybe_refresh"), \
             patch.dict(llm_client.ADAPTERS, {"openai": boom}):
            self.assertIsNone(llm_client.generate_title("anything"))

    def test_prompt_echo_returns_none_for_heuristic_fallback(self):
        # 09-02 leaked-title report: a model that echoes the title-gen
        # instruction (instead of producing a title) must yield None — never
        # the echoed prompt — so juniper falls back to the heuristic title.
        def fake_adapter(p, model, messages, temperature, max_tokens, timeout):
            return ("We need to generate a title for a support ticket. "
                    "The request: can you open a ticket for my wifi being slow"), 10, 5

        with patch.object(llm_client, "provider_chain", return_value=[_provider()]), \
             patch.object(llm_client, "maybe_refresh"), \
             patch.dict(llm_client.ADAPTERS, {"openai": fake_adapter}):
            title = llm_client.generate_title(
                "can you open a ticket for my wifi being slow")
        self.assertIsNone(title)

    def test_no_provider_returns_none(self):
        with patch.object(llm_client, "provider_chain", return_value=[]), \
             patch.object(llm_client, "maybe_refresh"):
            self.assertIsNone(llm_client.generate_title("anything"))


class CleanTitleTest(unittest.TestCase):
    """_clean_title must strip LLM meta-text (B3 regression): preambles,
    reasoning, thinking blocks, markdown emphasis and trailing punctuation —
    only the actual title may land in a chat-spawned ticket."""

    def test_plain_title_unchanged(self):
        self.assertEqual(
            llm_client._clean_title("Update the plex server"),
            "Update the plex server")

    def test_leading_title_label(self):
        self.assertEqual(
            llm_client._clean_title("Title: Update the plex server"),
            "Update the plex server")

    def test_preamble_before_title_label(self):
        # The B3 leak: a polite preamble before the labelled title.
        self.assertEqual(
            llm_client._clean_title(
                "Sure! Here's a concise title: Update the plex server"),
            "Update the plex server")

    def test_reasoning_then_title_label(self):
        self.assertEqual(
            llm_client._clean_title(
                "The customer is asking about their Plex server. "
                "Title: Update Plex server"),
            "Update Plex server")

    def test_thinking_block_is_dropped(self):
        self.assertEqual(
            llm_client._clean_title(
                "<thinking>The user wants a firmware update.</thinking> "
                "Update the switch firmware"),
            "Update the switch firmware")

    def test_markdown_emphasis_is_peeled(self):
        self.assertEqual(
            llm_client._clean_title("**Update the switch firmware**"),
            "Update the switch firmware")

    def test_quoted_title_is_peeled(self):
        self.assertEqual(
            llm_client._clean_title('"Update the plex server"'),
            "Update the plex server")

    def test_suggestion_label(self):
        self.assertEqual(
            llm_client._clean_title(
                "Here's a suggestion: Update the plex server"),
            "Update the plex server")

    def test_title_for_the_ticket_filler(self):
        self.assertEqual(
            llm_client._clean_title(
                "Here is a title for the ticket: Update the switch firmware"),
            "Update the switch firmware")

    def test_trailing_period_is_trimmed(self):
        self.assertEqual(
            llm_client._clean_title("Update the plex server."),
            "Update the plex server")

    def test_label_without_value_returns_none(self):
        self.assertIsNone(llm_client._clean_title("Title:"))

    def test_empty_input_returns_none(self):
        self.assertIsNone(llm_client._clean_title(""))
        self.assertIsNone(llm_client._clean_title("   "))

    def test_instruction_echo_generate_title_is_rejected(self):
        # 09-02 leaked-title report: the model echoed the title-gen
        # instruction instead of producing a title — the prompt text must
        # never become the ticket title.
        self.assertIsNone(llm_client._clean_title(
            "We need to generate a title for a support ticket. "
            "The request: can you open a ticket for my wifi being slow"))

    def test_instruction_echo_output_title_is_rejected(self):
        self.assertIsNone(llm_client._clean_title(
            "We need to output a title of at most 8 words for the "
            "customer request: give me a wifi report"))

    def test_multi_sentence_restatement_is_rejected(self):
        self.assertIsNone(llm_client._clean_title(
            "The customer wants their wifi fixed. They said it is slow."))


if __name__ == "__main__":
    unittest.main()
