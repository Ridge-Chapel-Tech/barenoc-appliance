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


if __name__ == "__main__":
    unittest.main(verbosity=2)
