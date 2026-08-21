#!/usr/bin/env python3
"""Tests for the apply_patch (multi-source update check) result formatter.

The 08-20 multi-source gap: the engine only checked the OS package manager,
while the App Center aggregates rpm + flatpak + firmware. The formatter now
surfaces the per-source report and treats "any source non-zero" as
"updates available".

Run in-container:
    docker compose exec api python3 -m unittest test_jobs_format -v
"""

import base64
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="jobs-format-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from routes.jobs import _format_info_answer  # noqa: E402


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class ApplyPatchFormatTest(unittest.TestCase):
    def test_per_source_report_surfaced(self):
        out = {
            "package_manager": "dnf",
            "sources": {"dnf": 2, "flatpak": 0, "firmware": 1, "snap": 0,
                        "rpm_ostree": 0},
            "total": 3,
            "updates_available": True,
            "updates_b64": _b64("[dnf] 2 package update(s) available\n"),
        }
        text = _format_info_answer("apply_patch", out)
        self.assertIn("3 update(s) available", text)
        self.assertIn("OS packages: 2", text)
        self.assertIn("Firmware: 1", text)
        self.assertNotIn("Flatpak", text)  # zero sources aren't listed

    def test_any_source_nonzero_when_total_missing(self):
        # total may be absent in older/agent paths — recompute from sources.
        out = {
            "package_manager": "apt",
            "sources": {"apt": 0, "flatpak": 2, "firmware": 0, "snap": 0,
                        "rpm_ostree": 0},
            "updates_available": True,
        }
        text = _format_info_answer("apply_patch", out)
        self.assertIn("2 update(s) available", text)
        self.assertIn("Flatpak: 2", text)

    def test_all_zero_reports_up_to_date(self):
        out = {
            "package_manager": "dnf",
            "sources": {"dnf": 0, "flatpak": 0, "firmware": 0, "snap": 0,
                        "rpm_ostree": 0},
            "total": 0,
            "updates_available": False,
            "updates_b64": "",
        }
        text = _format_info_answer("apply_patch", out)
        self.assertIn("up to date", text)
        self.assertNotIn("update(s) available", text)


class ApplyUpdatesFormatTest(unittest.TestCase):
    """The gated apply_updates result shape: per-source applied counts + the
    reboot-needed flag. Never says 'rebooted' — it surfaces the flag only."""

    def test_applied_counts_and_reboot_flag(self):
        out = {
            "package_manager": "dnf",
            "applied": {"dnf": 2, "flatpak": 1, "firmware": 0, "snap": 0,
                        "rpm_ostree": 0},
            "total_applied": 3,
            "reboot_needed": True,
            "detail_b64": _b64("[dnf] applied (2)\n"),
        }
        text = _format_info_answer("apply_updates", out)
        self.assertIn("3 update(s) applied", text)
        self.assertIn("OS packages: 2", text)
        self.assertIn("Flatpak: 1", text)
        self.assertNotIn("Firmware", text)  # zero sources aren't listed
        self.assertIn("reboot is needed", text)

    def test_nothing_to_apply(self):
        out = {
            "package_manager": "apt",
            "applied": {"apt": 0, "flatpak": 0, "firmware": 0, "snap": 0,
                        "rpm_ostree": 0},
            "total_applied": 0,
            "reboot_needed": False,
        }
        text = _format_info_answer("apply_updates", out)
        self.assertIn("nothing to apply", text)
        self.assertNotIn("reboot", text)

    def test_failed_sources_surfaced(self):
        out = {
            "package_manager": "dnf",
            "applied": {"dnf": 2, "flatpak": 0, "firmware": 1, "snap": 0,
                        "rpm_ostree": 0},
            "total_applied": 2,
            "failed": ["firmware"],
            "reboot_needed": False,
        }
        text = _format_info_answer("apply_updates", out)
        self.assertIn("failed: firmware", text)
        self.assertIn("2 update(s) applied", text)


if __name__ == "__main__":
    unittest.main()
