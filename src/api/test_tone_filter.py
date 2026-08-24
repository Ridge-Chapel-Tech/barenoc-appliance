#!/usr/bin/env python3
"""Unit tests for the final-answer tone cleanup (tone_filter.strip_meta_narration).

The agent's meta-narration prefixes + trailing fences are stripped so the
customer reads the answer directly; the answer itself is kept whole (never
truncated). The LIVE progress-note filter is tested separately in
src/agent/test_runner.py (ProgressToneFilterTest).

Run from src/api:
    python3 -m unittest test_tone_filter -v
"""

import unittest

from tone_filter import strip_meta_narration


class FinalAnswerCleanupTest(unittest.TestCase):
    def test_strips_lily_finished_prefix(self):
        self.assertEqual(
            strip_meta_narration("Lily finished:\nIt's all set — launch it with: pac-man"),
            "It's all set — launch it with: pac-man")

    def test_strips_heres_my_final_answer_prefix(self):
        self.assertEqual(
            strip_meta_narration("Here's my final answer to the customer: Good news, "
                                 "everything is installed."),
            "Good news, everything is installed.")

    def test_strips_heres_what_i_found_prefix(self):
        self.assertEqual(
            strip_meta_narration("Here's what I found:\n  • device is online"),
            "• device is online")

    def test_strips_completion_narration_and_fences(self):
        # the exact 08-17 live example shape
        raw = ("Lily finished:\n"
               "I have completed the installation and verified everything. "
               "Here's my final answer to the customer.\n"
               "---\n"
               "Good news — Pac-Man is installed and ready to play.")
        self.assertEqual(
            strip_meta_narration(raw),
            "Good news — Pac-Man is installed and ready to play.")

    def test_strips_trailing_fence_only(self):
        self.assertEqual(
            strip_meta_narration("It's all set.\n---"),
            "It's all set.")

    def test_keeps_answer_whole_and_untouched(self):
        answer = ("Good news — Pac-Man is installed.\n\n"
                  "To play: open your applications menu, search for Pac-Man, "
                  "and click it. It will remember your high scores.")
        self.assertEqual(strip_meta_narration(answer), answer)

    def test_never_empties_the_answer(self):
        # a completion-only answer must not be stripped to nothing
        self.assertEqual(
            strip_meta_narration("I have completed the installation and verified everything."),
            "I have completed the installation and verified everything.")

    def test_empty_text(self):
        self.assertEqual(strip_meta_narration(""), "")
        self.assertEqual(strip_meta_narration("   "), "")


class TonePoolTest(unittest.TestCase):
    """The shared tone pool (imported by queue_status for API-side parity)."""

    def test_categorize_keyword_cues(self):
        from tone_pool import categorize
        cases = [
            ("checking the logs to trace the outage", "investigating"),
            ("ssh into the switch to talk to it", "connecting"),
            ("installing the package now", "applying"),
            ("verifying everything is in place", "verifying"),
            ("waiting for the long download", "waiting"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(categorize(text), expected)

    def test_friendly_note_scrubs_technical(self):
        from tone_pool import friendly_note, all_phrases
        text, filtered = friendly_note("ssh tech@192.0.2.207 && sudo apt-get update")
        self.assertTrue(filtered)
        self.assertIn(text, all_phrases())
        self.assertNotIn("/", text)
        self.assertNotIn("sudo", text)

    def test_friendly_note_passes_through(self):
        from tone_pool import friendly_note
        text, filtered = friendly_note("Connecting to the device now…")
        self.assertFalse(filtered)
        self.assertEqual(text, "Connecting to the device now…")


class QueueStatusParityTest(unittest.TestCase):
    """derive_status's 'Working on it — {detail}' label must use the shared
    pool: technical agent_progress detail scrubs to a friendly phrase; friendly
    detail passes through; empty detail keeps the existing 'Working on it'."""

    @staticmethod
    def _ticket(event, detail):
        import json
        import datetime
        notes = json.dumps([{
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "event": event,
            "detail": detail,
            "actor": "Lily",
        }])

        class T:
            ticket_id = "TKT-20260818-0001"
            status = "in_progress"
            work_notes = notes
            assigned_to = "pi-agent"
            action = "pi_task"
            llm_confidence = None
            resolution = None
        return T()

    def test_technical_progress_detail_scrubbed(self):
        from queue_status import derive_status
        st = derive_status(self._ticket(
            "agent_progress", "ssh tech@192.0.2.207 && sudo apt-get update"))
        label = st["label"]
        self.assertTrue(label.startswith("Working on it — "), label)
        self.assertNotIn("ssh", label)
        self.assertNotIn("192.168", label)
        self.assertNotIn("sudo", label)

    def test_friendly_progress_detail_passes_through(self):
        from queue_status import derive_status
        st = derive_status(self._ticket(
            "agent_progress", "Connecting to the device now…"))
        self.assertEqual(st["label"], "Working on it — Connecting to the device now…")

    def test_empty_detail_stays_working_on_it(self):
        from queue_status import derive_status
        st = derive_status(self._ticket("agent_progress", ""))
        self.assertEqual(st["label"], "Working on it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
