#!/usr/bin/env python3
"""Sanitizer regression tests — the 08-19 optimize change-plan false positive."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from sanitizer import sanitize_ticket


class SanitizerTest(unittest.TestCase):
    def test_real_rm_command_still_blocked(self):
        self.assertIsNone(sanitize_ticket("rm -rf /opt/barenoc")[0])

    def test_confirm_phrase_not_blocked(self):
        """The 08-19 false positive: 'confirm the change took effect AND the
        path/device stays up (it re-informs / answers)' (from the optimize
        change-plan) must NOT be blocked — the 'rm' inside 'confirm' + a later
        '/' is not a command."""
        t = ("confirm the change took effect AND the path/device stays up "
             "(it re-informs / answers)")
        self.assertIsNotNone(sanitize_ticket(t)[0])

    def test_model_phrase_not_blocked(self):
        # 'del' inside 'model' must not trigger
        self.assertIsNotNone(sanitize_ticket("the model of the device / here")[0])

    def test_format_phrase_not_blocked(self):
        # 'format' as a word but not a command with a path
        self.assertIsNotNone(sanitize_ticket("format the output as a table / ok")[0])


if __name__ == "__main__":
    unittest.main()
