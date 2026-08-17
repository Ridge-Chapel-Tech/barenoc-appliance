#!/usr/bin/env python3
"""Tests for the agent runner's service-account credential handling.

Run from src/agent:
    python3 -m unittest test_runner -v
"""

import os
import sys
import tempfile
import threading
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


class SysCtxTest(unittest.TestCase):
    """The pi system context must point pi at the sanctioned scripts, state
    plainly that the agent API service account is NOT pi's, and keep the hard
    self-protection rules intact (the 08-17 auth-loop incident)."""

    def test_sysctx_points_at_sanctioned_scripts(self):
        sysctx = runner._build_sysctx("")
        self.assertIn("USE THE SANCTIONED SCRIPTS", sysctx)
        self.assertIn("device_ssh.sh", sysctx)
        self.assertIn("ping_check.sh", sysctx)
        self.assertIn("collect_logs.sh", sysctx)
        self.assertIn("Do NOT", sysctx)

    def test_sysctx_agent_api_creds_not_yours(self):
        sysctx = runner._build_sysctx("")
        low = sysctx.lower()
        self.assertIn("not yours", low)
        self.assertIn("/opt/barenoc/agent/credentials", sysctx)
        self.assertIn("/api/v1/auth", sysctx)
        self.assertIn("do not try to log in", low)

    def test_sysctx_ticket_guidance(self):
        sysctx = runner._build_sysctx("")
        self.assertIn("TKT-", sysctx)
        self.assertIn("work notes", sysctx)
        self.assertIn("do not go hunting devices", sysctx.lower())

    def test_sysctx_friendly_chat_tone(self):
        # progress + final must be short, plain, customer-facing — technical
        # detail stays in the work notes, never in the chat.
        sysctx = runner._build_sysctx("")
        low = sysctx.lower()
        self.assertIn("let me find your laptop", low)
        self.assertIn("connecting now", low)
        self.assertIn("installing now", low)
        self.assertIn("no meta-narration", low)
        self.assertIn("here's my final answer to the customer", low)
        self.assertIn("never put internal reasoning", low)
        self.assertIn("keep those in the", low)

    def test_sysctx_keeps_hard_self_protection(self):
        sysctx = runner._build_sysctx("")
        self.assertIn("HARD SELF-PROTECTION RULE", sysctx)
        self.assertIn("docker compose", sysctx)
        self.assertIn("NEVER write to the BareNOC web API", sysctx)

    def test_sysctx_appends_ticket_context(self):
        sysctx = runner._build_sysctx("Ticket: TKT-20260817-9400 updates")
        self.assertIn("Ticket context:", sysctx)
        self.assertIn("TKT-20260817-9400", sysctx)


class PiTaskDedupTest(unittest.TestCase):
    """Per-ticket pi-task dedup: a second launch for the same ticket while one
    is running must be merged (skipped), not spawned. Different tickets still
    run concurrently (MAX_CONCURRENT semantics are untouched)."""

    def tearDown(self):
        runner._ACTIVE_PI_TICKETS.clear()

    def test_second_launch_merged_while_active(self):
        entered = threading.Event()
        release = threading.Event()
        real_impl = runner._run_pi_task_impl
        calls = []

        def fake_impl(task, context, ticket_id, timeout=600):
            calls.append(ticket_id)
            entered.set()
            release.wait(timeout=5)
            return {"success": True, "output": {"response": "done"}}

        runner._run_pi_task_impl = fake_impl
        try:
            results = {}

            def run_first():
                results["first"] = runner._run_pi_task("t", "c", "TKT-DEDUP-1")

            t = threading.Thread(target=run_first)
            t.start()
            self.assertTrue(entered.wait(timeout=5), "first pi task never started")

            # while the first is in flight, a duplicate launch is merged
            dup = runner._run_pi_task("t", "c", "TKT-DEDUP-1")
            self.assertTrue(dup.get("merged"), str(dup))
            self.assertEqual(calls, ["TKT-DEDUP-1"],  # impl not called a second time
                             f"impl called {len(calls)} times")

            release.set()
            t.join(timeout=5)
            self.assertTrue(results["first"]["success"])
            self.assertFalse(results["first"].get("merged"))

            # once the first finishes, the ticket is free again
            final = runner._run_pi_task("t", "c", "TKT-DEDUP-1")
            self.assertFalse(final.get("merged"))
            self.assertTrue(final["success"])
            self.assertEqual(calls, ["TKT-DEDUP-1", "TKT-DEDUP-1"])
        finally:
            runner._run_pi_task_impl = real_impl

    def test_different_tickets_not_merged(self):
        entered_a = threading.Event()
        release = threading.Event()
        real_impl = runner._run_pi_task_impl

        def fake_impl(task, context, ticket_id, timeout=600):
            if ticket_id == "TKT-A":
                entered_a.set()
                release.wait(timeout=5)
            return {"success": True, "output": {"response": "ok"}}

        runner._run_pi_task_impl = fake_impl
        try:
            results = {}

            def run_a():
                results["a"] = runner._run_pi_task("t", "c", "TKT-A")

            threading.Thread(target=run_a).start()
            self.assertTrue(entered_a.wait(timeout=5))
            # a different ticket is NOT merged while TKT-A runs
            b = runner._run_pi_task("t", "c", "TKT-B")
            self.assertFalse(b.get("merged"))
            self.assertTrue(b["success"])
            release.set()
        finally:
            runner._run_pi_task_impl = real_impl


class ProgressToneFilterTest(unittest.TestCase):
    """The runner's progress-note safety net: technical-looking notes are
    replaced with a friendly generic before they reach the chat; genuinely
    user-facing notes pass through untouched (08-17 chat-tone incident)."""

    def test_technical_notes_replaced_with_friendly_generic(self):
        samples = [
            # the 08-17 live example (Pac-Man install on the laptop)
            "I'm connecting to the laptop as user `barenoc` (uid 1001), which "
            "does not have passwordless sudo… NOPASSWD sudo access specifically "
            "to /usr/bin/dnf… dnf5 is usable without password",
            "Running dnf check-update on /opt/barenoc/scripts/apply_patch.sh",
            "ssh tech@192.168.10.141 && sudo apt-get update && curl http://localhost/api/v1/devices",
            "wrote /etc/sudoers.d/barenoc and reloaded systemd",
            "calling endpoint /api/v1/jobs/result with access_token from ticket TKT-20260817-5846",
            "results saved to ~/report.json",
        ]
        for raw in samples:
            with self.subTest(raw=raw[:40]):
                self.assertTrue(runner._is_technical_note(raw), f"should be technical: {raw!r}")
                friendly, was_filtered = runner._friendly_progress_note(raw)
                self.assertTrue(was_filtered)
                self.assertIn(friendly, runner._FRIENDLY_PROGRESS)
                self.assertNotEqual(friendly, raw)

    def test_user_facing_notes_pass_through(self):
        samples = [
            "Let me find your laptop…",
            "Connecting now…",
            "Working on it…",
            "Installing now…",
            "Almost done — just verifying…",
            "Done — here's how to launch it: open Pac-Man from your applications menu",
            "It's all set, this is how to run it.",
        ]
        for raw in samples:
            with self.subTest(raw=raw):
                friendly, was_filtered = runner._friendly_progress_note(raw)
                self.assertFalse(was_filtered, f"should pass through: {raw!r}")
                self.assertEqual(friendly, raw)

    def test_long_jargon_heavy_note_is_technical(self):
        raw = ("installed percona_xtradb_cluster v8_0_36 then initialized "
               "galera wsrep provider on the bootstrap node")
        self.assertTrue(runner._is_technical_note(raw))
        friendly, was_filtered = runner._friendly_progress_note(raw)
        self.assertTrue(was_filtered)
        self.assertIn(friendly, runner._FRIENDLY_PROGRESS)

    def test_empty_note_not_technical(self):
        self.assertFalse(runner._is_technical_note(""))
        friendly, was_filtered = runner._friendly_progress_note("")
        self.assertEqual(friendly, "")
        self.assertFalse(was_filtered)

class ProgressSnippetTest(unittest.TestCase):
    """Progress notes must fit a real pi answer (2000 chars, not the old 250)
    and must never cut a word silently — the ellipsis marks any truncation
    (08-17 pi-answer-truncation incident)."""

    def test_under_cap_untouched(self):
        text = ("dnf check-update: kernel + firefox pending. " * 8).strip()  # ~390 chars
        self.assertEqual(runner._ellipsize(text), text)

    def test_over_cap_gets_ellipsis(self):
        text = "word " * 1200  # 6000 chars
        out = runner._ellipsize(text)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), runner.PROGRESS_NOTE_MAX_CHARS + 1)
        self.assertTrue(out.startswith("word "))

    def test_multiline_message_kept_flat_not_three_lines(self):
        # a real answer spans more than 3 lines; the old first-3-lines slice
        # dropped the rest silently. Flattening must keep every line.
        lines = [f"line {i} with detail about the check" for i in range(1, 6)]
        text = "\n".join(lines)
        out = runner._ellipsize(text)
        self.assertEqual(out, text)          # well under the cap: no cut, no ellipsis
        self.assertIn("line 5", out)

    def test_cap_is_2000(self):
        self.assertEqual(runner.PROGRESS_NOTE_MAX_CHARS, 2000)

    def test_incomplete_streaming_message_not_posted(self):
        msg = {"role": "assistant", "stopReason": "pending",
               "content": [{"type": "text", "text": "Let me r"}]}
        self.assertIsNone(runner._assistant_complete_text(msg))

    def test_missing_stop_reason_treated_incomplete(self):
        msg = {"role": "assistant",
               "content": [{"type": "text", "text": "partial"}]}
        self.assertIsNone(runner._assistant_complete_text(msg))

    def test_complete_message_text_returned(self):
        msg = {"role": "assistant", "stopReason": "stop",
               "content": [{"type": "text", "text": "  Full answer here.  "}]}
        self.assertEqual(runner._assistant_complete_text(msg), "Full answer here.")

    def test_tool_use_message_with_text_ok(self):
        msg = {"role": "assistant", "stopReason": "toolUse",
               "content": [{"type": "text", "text": "Checking the gateway…"},
                            {"type": "toolCall", "name": "bash", "arguments": {}}]}
        self.assertEqual(runner._assistant_complete_text(msg), "Checking the gateway…")

    def test_non_dict_returns_none(self):
        self.assertIsNone(runner._assistant_complete_text(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
