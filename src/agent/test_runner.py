#!/usr/bin/env python3
"""Tests for the agent runner's service-account credential handling.

Run from src/agent:
    python3 -m unittest test_runner -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runner


class AgentCredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", delete=False)
        self.tmp.close()
        self.orig_path = runner.API_CREDENTIALS_FILE

    def tearDown(self):
        runner.API_CREDENTIALS_FILE = self.orig_path
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _write(self, content: str):
        with open(self.tmp.name, "w") as f:
            f.write(content)
        runner.API_CREDENTIALS_FILE = self.tmp.name

    def test_parses_credentials(self):
        self._write("username=agent\npassword=abc123def\n")
        creds = runner._api_credentials()
        self.assertEqual(creds, {"username": "agent", "password": "abc123def"})

    def test_missing_file_returns_empty(self):
        runner.API_CREDENTIALS_FILE = "/nonexistent/credentials"
        self.assertEqual(runner._api_credentials(), {})

    def test_incomplete_file_returns_empty(self):
        self._write("username=agent\n")
        self.assertEqual(runner._api_credentials(), {})

    def test_comments_and_blank_lines_ignored(self):
        self._write("# comment\n\nusername=agent\npassword=xyz\n")
        self.assertEqual(runner._api_credentials(),
                         {"username": "agent", "password": "xyz"})

    def test_login_without_file_returns_empty(self):
        runner.API_CREDENTIALS_FILE = "/nonexistent/credentials"
        self.assertEqual(runner._api_login(), "")

    def test_approval_gate(self):
        # held jobs run only after the human approves (status -> in_progress)
        self.assertTrue(runner._approval_allowed("in_progress"))
        self.assertFalse(runner._approval_allowed("escalated"))     # still waiting
        self.assertFalse(runner._approval_allowed("open"))          # not approved
        self.assertFalse(runner._approval_allowed("customer_action"))
        self.assertFalse(runner._approval_allowed("closed"))        # rejected/closed
        self.assertFalse(runner._approval_allowed("failed"))
        self.assertFalse(runner._approval_allowed(""))              # API error / unknown

    def test_build_cmd_batch_actions(self):
        # the port actions build (switch_mac, port_idx[, name]) args
        cmd = runner._build_cmd("unifi_port_bounce", "aa:bb", {"port_idx": 7})
        self.assertEqual(cmd[-2:], ["aa:bb", "7"])
        cmd = runner._build_cmd("unifi_port_rename", "aa:bb", {"port_idx": 7, "name": "cam"})
        self.assertEqual(cmd[-3:], ["aa:bb", "7", "cam"])

    def test_build_cmd_ticket_status(self):
        # read-only: script takes the TKT-… id as its only arg (no SSH)
        cmd = runner._build_cmd("ticket_status", "",
                                {"ticket_id": "TKT-20260816-5935"})
        self.assertEqual(cmd[0:3], ["bash",
                                     os.path.join(runner.SCRIPTS_DIR, "ticket_status.sh"),
                                     "TKT-20260816-5935"])
        self.assertNotIn("ssh", " ".join(cmd).lower())

    def test_build_cmd_unifi_network_create(self):
        # name + vlan required; subnet/dhcp optional (script defaults them)
        cmd = runner._build_cmd("unifi_network_create", "",
                                {"name": "IoT", "vlan": 12})
        self.assertEqual(cmd[2:], ["IoT", "12", "", "true"])
        cmd = runner._build_cmd("unifi_network_create", "",
                                {"name": "Cameras", "vlan": 50,
                                 "subnet": "192.168.50.1/24", "dhcp": "false"})
        self.assertEqual(cmd[2:], ["Cameras", "50", "192.168.50.1/24", "false"])

    def test_build_cmd_enroll_device(self):
        # target + resolved ssh creds + optional ttl
        cmd = runner._build_cmd("enroll_device", "192.168.4.99", {})
        self.assertEqual(cmd[0], "bash")
        self.assertTrue(cmd[1].endswith("enroll_device.sh"))
        self.assertEqual(cmd[-1], "600")
        cmd = runner._build_cmd("enroll_device", "192.168.4.99", {"ttl": 300})
        self.assertEqual(cmd[-1], "300")

    # ── SSH credential resolution (stored device creds for SSH actions) ──

    def test_resolve_ssh_uses_stored_creds(self):
        # No params: device's stored creds win, written as a 0600 temp key
        runner.DEVICE_BY_IP["192.0.2.99"] = 7
        runner._TEMP_KEYS.clear()
        with patch("runner._device_ssh_creds",
                   return_value={"ssh_user": "tech", "ssh_key": "PRIVATE-KEY"}):
            user, key = runner._resolve_ssh("192.0.2.99", {})
        self.assertEqual(user, "tech")
        with open(key) as f:
            # the temp key is normalized to end with a newline (ssh-keygen
            # rejects keys without it on OpenSSL 3.0)
            self.assertEqual(f.read(), "PRIVATE-KEY\n")
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        self.assertIn(key, runner._TEMP_KEYS)
        os.unlink(key)
        runner._TEMP_KEYS.remove(key)

    def test_resolve_ssh_params_win_over_stored(self):
        runner.DEVICE_BY_IP["192.0.2.99"] = 7
        with patch("runner._device_ssh_creds",
                   return_value={"ssh_user": "stored", "ssh_key": "STORED-KEY"}):
            user, key = runner._resolve_ssh("192.0.2.99",
                                            {"ssh_user": "explicit", "ssh_key": "/tmp/explicit.key"})
        self.assertEqual((user, key), ("explicit", "/tmp/explicit.key"))

    def test_resolve_ssh_falls_back_to_defaults(self):
        runner.DEVICE_BY_IP.pop("192.0.2.99", None)
        with patch("runner._device_ssh_creds", return_value=None):
            user, key = runner._resolve_ssh("192.0.2.99", {})
        self.assertEqual(user, "barenoc")
        self.assertEqual(key, runner.DEFAULT_SSH_KEY)

    def test_ssh_cmd_uses_resolved_creds(self):
        # an SSH action's command carries the resolved user + temp key path
        runner.DEVICE_BY_IP["192.0.2.99"] = 7
        runner._TEMP_KEYS.clear()
        with patch("runner._device_ssh_creds",
                   return_value={"ssh_user": "tech", "ssh_key": "KEY"}):
            cmd = runner._build_cmd("collect_logs", "192.0.2.99", {"lines": 25})
        self.assertEqual(cmd[0:3], ["bash", os.path.join(runner.SCRIPTS_DIR, "collect_logs.sh"), "192.0.2.99"])
        self.assertEqual(cmd[3], "25")
        self.assertEqual(cmd[4], "tech")
        self.assertTrue(cmd[5].startswith("/tmp/pi-agent-"))
        os.unlink(cmd[5])
        runner._TEMP_KEYS.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
