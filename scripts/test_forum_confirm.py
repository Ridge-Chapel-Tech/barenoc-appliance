#!/usr/bin/env python3
"""Unit tests for scripts/forum_confirm.py (parsing only — no network).

Run:  ( cd scripts && python3 -m unittest test_forum_confirm )
Also wired into scripts/run_tests.sh so CI covers it on every PR.
"""

import unittest

import forum_confirm as fc

ISSUE_BODY = """**Category:** bugs
**Status:** open
**Author:** forum user
**Forum link:** https://forum.barenoc.com/thread/123e4567-e89b-12d3-a456-426614174000

## Description
The dashboard shows a stale version number.
"""


class TestExtractThreadId(unittest.TestCase):
    def test_extracts_uuid(self):
        self.assertEqual(
            fc.extract_thread_id(ISSUE_BODY),
            "123e4567-e89b-12d3-a456-426614174000",
        )

    def test_none_when_missing(self):
        self.assertIsNone(fc.extract_thread_id("no forum link here"))
        self.assertIsNone(fc.extract_thread_id(None))

    def test_ignores_other_links(self):
        body = "see https://forum.barenoc.com/other/1234 and https://example.com/thread/x"
        self.assertIsNone(fc.extract_thread_id(body))


class TestExtractVersion(unittest.TestCase):
    def test_most_recent_comment_wins(self):
        comments = [
            {"body": "Fixed in v2026.08.16"},
            {"body": "looks good"},
            {"body": "Fixed in v2026.08.17.a"},
        ]
        self.assertEqual(fc.extract_version(comments), "2026.08.17.a")

    def test_no_v_prefix(self):
        self.assertEqual(fc.extract_version([{"body": "Fixed in 2026.08.17.a"}]), "2026.08.17.a")

    def test_trailing_punctuation_stripped(self):
        self.assertEqual(
            fc.extract_version([{"body": "Fixed in v2026.08.17.a. — please verify."}]),
            "2026.08.17.a",
        )

    def test_case_insensitive(self):
        self.assertEqual(fc.extract_version([{"body": "fixed in v2026.08.17.a"}]),
                         "2026.08.17.a")

    def test_none_when_missing(self):
        self.assertIsNone(fc.extract_version([{"body": "closing as duplicate"}]))
        self.assertIsNone(fc.extract_version([]))
        self.assertIsNone(fc.extract_version(None))


class TestBuildMessage(unittest.TestCase):
    def test_exact_wording_with_v(self):
        self.assertEqual(
            fc.build_message("v2026.08.17.a"),
            "✅ Bug confirmed. Patched in v2026.08.17.a — please verify.",
        )

    def test_exact_wording_without_v(self):
        self.assertEqual(
            fc.build_message("2026.08.17.a"),
            "✅ Bug confirmed. Patched in v2026.08.17.a — please verify.",
        )

    def test_no_double_v(self):
        self.assertEqual(
            fc.build_message("v2026.08.17.a"),
            fc.build_message("2026.08.17.a"),
        )


if __name__ == "__main__":
    unittest.main()
