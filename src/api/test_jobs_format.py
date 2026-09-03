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

from routes.jobs import _format_info_answer, _fmt_bytes  # noqa: E402


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


class WindowsFormatTest(unittest.TestCase):
    """F8 — windows_diag / windows_cleanup result notes the owner reads."""

    def test_fmt_bytes(self):
        self.assertEqual(_fmt_bytes(0), "0 B")
        self.assertEqual(_fmt_bytes(512), "512 B")
        self.assertEqual(_fmt_bytes(2048), "2.0 KB")
        self.assertEqual(_fmt_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(_fmt_bytes(3 * 1024 ** 3), "3.0 GB")

    def test_windows_diag_report(self):
        out = {
            "hostname": "DADS-PC", "os": "Windows 11 Pro",
            "volumes": [{"device": "C:", "size_gb": 476.9, "free_gb": 38.2,
                          "free_pct": 8.0, "disk_full": True}],
            "top_cpu": [{"name": "chrome", "pid": 1, "cpu_s": 900}],
            "top_ram": [{"name": "chrome", "pid": 1, "ram_mb": 2048}],
            "startup_items": [{"name": "Adobe CollabSync", "command": "x", "location": "y"}],
            "defender": {"available": True, "real_time_enabled": True,
                          "signature_age_days": 3, "signature_version": "1.2.3"},
            "recent_events": [{"time": "2026-09-03T10:00:00", "level": "Error",
                                "id": 1000, "provider": "Disk", "message": "bad sector"}],
            "recent_events_count": 1,
            "boot": {"last_boot_time": "2026-09-02T08:00:00", "uptime_days": 1.2},
            "smart": {"available": True, "disks": [{
                "device": "PhysicalDisk0", "health": "Healthy",
                "temperature_c": 42, "wear": 5, "power_on_hours": 1200}]},
        }
        text = _format_info_answer("windows_diag", out)
        self.assertIn("Windows health report for DADS-PC (Windows 11 Pro)", text)
        self.assertIn("LOW DISK", text)
        self.assertIn("top CPU: chrome (900.0s)", text)
        self.assertIn("top RAM: chrome (2048 MB)", text)
        self.assertIn("Defender: real-time ON, signatures 3d old (v1.2.3)", text)
        self.assertIn("last boot: 2026-09-02T08:00:00 (up 1.2d)", text)
        self.assertIn("SMART: PhysicalDisk0 — Healthy, 42°C, wear 5%, 1200h on", text)

    def test_windows_cleanup_report(self):
        out = {
            "hostname": "DADS-PC",
            "bytes_recovered": 5 * 1024 ** 3,
            "processes_stopped": ["AdobeCollabSync"],
            "autostart_removed": ["HKCU:\\Run\\Adobe CollabSync"],
            "before_bytes": 6 * 1024 ** 3,
            "temp_after_bytes": 1024 ** 3,
            "recycle_after_bytes": 0,
        }
        text = _format_info_answer("windows_cleanup", out)
        self.assertIn("Cleaned DADS-PC: recovered 5.0 GB", text)
        self.assertIn("stopped processes: AdobeCollabSync", text)
        self.assertIn("removed autostart entries (1)", text)

    def test_windows_cleanup_nothing_to_do(self):
        out = {"hostname": "DADS-PC", "bytes_recovered": 0,
               "processes_stopped": [], "autostart_removed": []}
        text = _format_info_answer("windows_cleanup", out)
        self.assertIn("nothing to clean", text)


if __name__ == "__main__":
    unittest.main()
