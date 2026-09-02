#!/usr/bin/env python3
"""Tests for the no-hang subnet sweep (src/scripts/discover.sh).

Pins the two 08-19 fixes:
  - a sweep finishes quickly (parallel + capped) and emits PROGRESS notes;
  - 100.64.0.0/10 (CGNAT + Tailscale overlay) is never swept — the buddy's
    Starlink CGNAT link case.

Runs discover.sh directly (it is a host-side script, but its embedded Python
uses only the stdlib and `ping`, both available in CI and in the api image).

    python3 -m unittest test_discover_sweep -v
"""

import json
import os
import shutil
import subprocess
import unittest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")
DISCOVER = os.path.join(_SCRIPTS, "discover.sh")


def _run(target, timeout=25):
    r = subprocess.run(["bash", DISCOVER, target],
                       capture_output=True, text=True, timeout=timeout)
    return r, json.loads(r.stdout.strip())


class DiscoverSweepTest(unittest.TestCase):
    def test_cgnat_subnet_never_swept(self):
        r, out = _run("100.64.0.0/30")
        self.assertEqual(out["skipped_cgnat"], 2)
        self.assertEqual(out["found"], [])
        self.assertEqual(out["count"], 0)

    def test_starlink_address_pinned(self):
        # a CGNAT 100.64.0.0/10 address — it must be skipped even as
        # part of a wider sweep.
        r, out = _run("100.64.0.0/30")
        self.assertEqual(out["skipped_cgnat"], 2)
        self.assertEqual(out["count"], 0)

    def test_single_host_reachable(self):
        if not shutil.which("ping"):
            self.skipTest("ping not installed")
        # ping may be installed but non-functional in unprivileged/containerized
        # environments (no CAP_NET_RAW): verify it actually works first.
        try:
            probe = subprocess.run(["ping", "-c", "1", "-W", "2", "127.0.0.1"],
                                   capture_output=True, timeout=6)
        except Exception:
            probe = None
        if probe is None or probe.returncode != 0:
            self.skipTest("ping not permitted (no raw socket)")
        r, out = _run("127.0.0.1")
        self.assertEqual([d["ip"] for d in out["found"]], ["127.0.0.1"])

    def test_small_sweep_completes_with_progress(self):
        # A /29 (6 hosts) must finish well under the runner/pi timeout and
        # emit PROGRESS notes on stderr for the runner to relay.
        r, out = _run("192.0.2.0/29", timeout=20)
        self.assertIn("count", out)
        self.assertIn("PROGRESS:", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
