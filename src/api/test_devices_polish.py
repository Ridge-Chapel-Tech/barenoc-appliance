#!/usr/bin/env python3
"""Cheap template/JS assertions for the devices-polish pass (08-17 feedback).

  1. Check Now is gone from the System → Updates section (auto-check on load +
     release banner is the only flow).
  3. The topology marks offline adopted UniFi gear with a distinct style.
  4. Enable control / Connect channel are gated on the device's effective
     channels (hidden when a real control channel exists).
  5. Fingerprint is not offered on Onboarded devices (discovery action only).

No browser needed — these read the shipped templates and assert the conditions
the JS uses. Wire into scripts/run_tests.sh + CI.

    docker compose exec api python3 -m unittest test_devices_polish -v
"""

import os
import unittest


class DevicesPolishTemplateTest(unittest.TestCase):
    @staticmethod
    def _read(name):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "templates", name), encoding="utf-8") as f:
            return f.read()

    def test_check_now_button_removed(self):
        html = self._read("system.html")
        self.assertNotIn('>Check now<', html)          # the manual button is gone
        self.assertNotIn('function updCheck', html)    # its handler is gone too
        # …but the auto-check on load still drives the card (the only flow).
        self.assertIn("updFetch('/check'", html)
        self.assertIn('updLoad();', html)

    def test_topology_has_offline_styling(self):
        html = self._read("devices.html")
        # Offline gear gets a distinct mermaid style (red dashed outline)…
        self.assertIn('classDef off fill:#fef2f2,stroke:#dc2626', html)
        # …and the renderer applies it when the device is offline/omitted.
        self.assertIn("d.status === 'offline' || d.offline", html)
        # …via a mermaid class STATEMENT (one class per ':::' — ':::ap off' is a
        # syntax error that broke the whole topology render, fixed 08-17).
        self.assertIn("offlineNodes.push(id)", html)
        self.assertIn("class ' + offlineNodes.join(',') + ' off'", html)
        self.assertNotIn("cls += ' off'", html)

    def test_control_channel_gating_present(self):
        html = self._read("devices.html")
        # 'monitor' is always present, so a control channel = anything beyond it.
        self.assertIn('function hasControlChannel', html)
        self.assertIn("c !== 'monitor'", html)
        # Onboarded grid: Connect channel is hidden when controlReady.
        self.assertIn("controlReady ? '' : '<button onclick=\"openCreds", html)
        # Unclaimed list: ONE Adopt action (method chooser) — no separate
        # Enable-control button there (removed 08-17, user feedback).
        self.assertIn("showAdoptModalForDevice", html)
        self.assertNotIn('onclick="openAdopt(', html)

    def test_fingerprint_not_offered_onboarded(self):
        html = self._read("devices.html")
        # The Onboarded card no longer carries a Fingerprint action…
        self.assertNotIn('Re-run nmap fingerprint', html)
        # …while the Unclaimed list keeps Fingerprint + Identify (discovery).
        self.assertIn('Run the nmap fingerprint to identify this host', html)
        self.assertIn('>Identify</button>', html)

    def test_existing_labels_still_present(self):
        """The relabels from #24/#31 are untouched by this pass."""
        html = self._read("devices.html")
        for label in ("Onboarded devices", "Unclaimed devices",
                      "Adopt", "Enable control", "Connect channel"):
            self.assertIn(label, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
