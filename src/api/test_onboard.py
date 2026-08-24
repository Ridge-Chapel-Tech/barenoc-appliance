#!/usr/bin/env python3
"""Tests for the served onboarding scripts (routes/onboard.py) — issue #105.

The root-trust fix: the browser-trust step must run BEFORE the "installation
complete" popup (never after), the Linux script must embed the canonical
verify+anchor logic (correct root only, Fedora/RHEL update-ca-trust AND
Debian/Ubuntu update-ca-certificates, self-clean, verify-after), and macOS
must trust via the System keychain + verify with curl.

    docker compose exec api python3 -m unittest test_onboard -v
"""

import os
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="onboard-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from routes import onboard


def _linux() -> str:
    with patch.object(onboard, "root_fingerprint", return_value="f" * 64), \
         patch.object(onboard, "ensure_control_key", return_value={"public_key": "ssh-ed25519 AAAAtest"}):
        return onboard._linux_script("https://192.0.2.207")


def _mac() -> str:
    with patch.object(onboard, "root_fingerprint", return_value="f" * 64), \
         patch.object(onboard, "ensure_control_key", return_value={"public_key": "ssh-ed25519 AAAAtest"}):
        return onboard._mac_script("https://192.0.2.207")


class OnboardTrustOrderingTest(unittest.TestCase):
    """issue #105: the cert-accept/trust step must precede the complete popup."""

    def test_linux_trust_before_completion_popup(self):
        s = _linux()
        trust = s.index("# --- Optional: trust the BareNOC signing root")
        handshake = s.index("Verifying the handshake")
        complete = s.index("This device is now onboarded")   # the zenity popup
        done = s.index("==> Done.")
        self.assertLess(trust, handshake)
        self.assertLess(trust, complete)
        self.assertLess(complete, done)

    def test_mac_trust_before_completion(self):
        s = _mac()
        trust = s.index("# --- Optional: trust the BareNOC root CA")
        handshake = s.index("Verifying the handshake")
        self.assertLess(trust, handshake)


class OnboardLinuxTrustBlockTest(unittest.TestCase):
    """The Linux script anchors the CORRECT root via the canonical script."""

    def test_embeds_canonical_trust_root_sh(self):
        s = _linux()
        self.assertIn("BARENOC_TRUST_ROOT_SH", s)
        # Fedora/RHEL + Debian/Ubuntu stores both supported
        self.assertIn("update-ca-trust", s)
        self.assertIn("update-ca-certificates", s)

    def test_verify_after_install(self):
        s = _linux()
        self.assertIn("openssl verify", s)
        self.assertIn("curl (no -k)", s)

    def test_self_cleans_stale_anchors(self):
        s = _linux()
        self.assertIn("removed stale anchor", s)

    def test_rejects_unrelated_root_and_leaf(self):
        # the canonical script refuses to anchor anything but the signing root
        s = _linux()
        self.assertIn("refusing to anchor", s)
        self.assertIn("does NOT sign", s)

    def test_reports_trust_result(self):
        s = _linux()
        self.assertIn("TRUST_RC", s)
        self.assertIn("Browser trust", s)


class OnboardMacTrustBlockTest(unittest.TestCase):
    def test_keychain_trust_and_verify(self):
        s = _mac()
        self.assertIn("security add-trusted-cert", s)
        self.assertIn("curl (no -k)", s)
        self.assertIn('delete-certificate -c "BareNOC Internal CA Root"', s)


if __name__ == "__main__":
    unittest.main()
