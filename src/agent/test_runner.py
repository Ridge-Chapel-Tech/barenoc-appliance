#!/usr/bin/env python3
"""Tests for the agent runner's service-account credential handling.

Run from src/agent:
    python3 -m unittest test_runner -v
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import time
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
        cmd = runner._build_cmd("enroll_device", "192.0.2.99", {})
        self.assertEqual(cmd[0], "bash")
        self.assertTrue(cmd[1].endswith("enroll_device.sh"))
        self.assertEqual(cmd[-1], "600")
        cmd = runner._build_cmd("enroll_device", "192.0.2.99", {"ttl": 300})
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
        # detail stays in the work notes, never in the chat. Guidance now asks
        # for varied, stage-matched phrasing (08-18 chat-tone-diversity).
        sysctx = runner._build_sysctx("")
        low = sysctx.lower()
        self.assertIn("vary your wording", low)
        self.assertIn("taking a look at that now", low)
        self.assertIn("connecting to the device", low)
        self.assertIn("applying that change now", low)
        self.assertIn("verifying everything looks right", low)
        self.assertIn("no meta-narration", low)
        self.assertIn("here's my final answer to the customer", low)
        self.assertIn("never put internal reasoning", low)
        self.assertIn("keep those in the", low)

    def test_sysctx_keeps_hard_self_protection(self):
        sysctx = runner._build_sysctx("")
        self.assertIn("HARD SELF-PROTECTION RULE", sysctx)
        self.assertIn("docker compose", sysctx)
        self.assertIn("NEVER write to the BareNOC web API", sysctx)

    def test_sysctx_identity_protection_rule(self):
        # TKT-20260823-4534 + 08-26: the agent must never reference, seek, or
        # retain the developer/owner/any user's identity — work notes included
        # (scrubbed by the same rule as customer-facing text).
        sysctx = runner._build_sysctx("")
        low = sysctx.lower()
        self.assertIn("IDENTITY PROTECTION RULE", sysctx)
        self.assertIn("personal identity", low)
        self.assertIn("the barenoc team", low)
        self.assertIn("never hunt for identities", low)
        self.assertIn("tailscale status", low)
        self.assertIn("work notes", low)
        self.assertIn("scrubbed by the same rule", low)

    def test_sysctx_redacts_identity_from_context(self):
        # a mock artifact carrying the developer identity must be redacted
        # before it reaches the agent (the context-build test).
        ctx = ("Artifact: built by yery (yery.odell@gmail.com), "
               "dev path /home/yery/Projects/BareNOC")
        sysctx = runner._build_sysctx(ctx)
        self.assertNotIn("yery", sysctx)
        self.assertNotIn("odell", sysctx.lower())
        self.assertIn("[redacted]", sysctx)
        self.assertIn("Artifact: built by", sysctx)

    def test_sysctx_appends_ticket_context(self):
        sysctx = runner._build_sysctx("Ticket: TKT-20260817-9400 updates")
        self.assertIn("Ticket context:", sysctx)
        self.assertIn("TKT-20260817-9400", sysctx)

    def test_sysctx_infra_change_contract(self):
        # agent-foresight: every port/VLAN/network/switch/gateway action must be
        # plan-first + checkpointed, never an improvised write (the 08-19 incident).
        sysctx = runner._build_sysctx("")
        self.assertIn("INFRA-CHANGE CONTRACT", sysctx)
        self.assertIn("ENUMERATE CURRENT STATE FIRST", sysctx)
        self.assertIn("BLAST-RADIUS REASONING", sysctx)
        self.assertIn("CAPTURE the full 'before' state", sysctx)
        self.assertIn("ROLLBACK-ON-FAILURE", sysctx)
        self.assertIn("NEVER change the ports carrying the appliance", sysctx)
        self.assertIn("infra_checkpoint.py", sysctx)

    def test_sysctx_checkpoint_dir_injected(self):
        sysctx = runner._build_sysctx("", checkpoint_dir="/tmp/cp/TKT-1")
        self.assertIn("CHECKPOINT DIRECTORY", sysctx)
        self.assertIn("/tmp/cp/TKT-1", sysctx)

    def test_sysctx_env_digest_appended_when_provided(self):
        # L1 knowledge-layer: the environment digest is appended when the
        # caller supplies it, and never when it doesn't (default stays pure).
        sysctx = runner._build_sysctx("")
        self.assertNotIn("ENVIRONMENT DIGEST", sysctx)
        sysctx = runner._build_sysctx("", env_digest="ENVIRONMENT DIGEST:\nENVIRONMENT: 3 managed devices")
        self.assertIn("ENVIRONMENT DIGEST", sysctx)
        self.assertIn("ENVIRONMENT: 3 managed devices", sysctx)
        self.assertIn("HARD SELF-PROTECTION RULE", sysctx)  # base ctx intact


class SysCtxWebResearchTest(unittest.TestCase):
    """L3 research (web fetch/search) sysctx guidance + deployment egress gate."""

    def test_disabled_by_default(self):
        sysctx = runner._build_sysctx("")
        self.assertIn("WEB RESEARCH (L3 — DISABLED)", sysctx)
        self.assertNotIn("WEB RESEARCH (L3 — ENABLED", sysctx)
        self.assertIn("Do NOT fetch or", sysctx)

    def test_enabled_block_when_opted_in(self):
        sysctx = runner._build_sysctx("", web_research=True)
        self.assertIn("WEB RESEARCH (L3 — ENABLED", sysctx)
        self.assertIn("web_search.sh", sysctx)
        self.assertIn("web_fetch.sh", sysctx)
        self.assertIn("FETCH → SUMMARIZE → CITE", sysctx)
        self.assertIn("cite each source as a URL", sysctx)
        self.assertNotIn("WEB RESEARCH (L3 — DISABLED)", sysctx)

    def test_base_ctx_still_present_when_enabled(self):
        sysctx = runner._build_sysctx("", web_research=True)
        self.assertIn("HARD SELF-PROTECTION RULE", sysctx)
        self.assertIn("INFRA-CHANGE CONTRACT", sysctx)

    def test_web_research_enabled_reads_secret_file(self):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
        tmp.write(json.dumps({"enabled": True}))
        tmp.close()
        orig = runner.WEB_RESEARCH_SECRET_FILE
        try:
            runner.WEB_RESEARCH_SECRET_FILE = tmp.name
            self.assertTrue(runner._web_research_enabled())
            os.unlink(tmp.name)
            self.assertFalse(runner._web_research_enabled())
        finally:
            runner.WEB_RESEARCH_SECRET_FILE = orig
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


class IdentityRedactionTest(unittest.TestCase):
    """Known personal identifiers must be redacted before the agent sees them
    (context/task) and never pass the progress-note filter (TKT-4534/08-26)."""

    def test_redact_known_identifiers(self):
        cases = [
            ("The developer is yery.", "The developer is [redacted]."),
            ("built by Yery O'Dell", "built by [redacted]"),
            ("email yery.odell@gmail.com", "email [redacted]"),
            ("yery@odell.dev is the dev email", "[redacted] is the dev email"),
            ("tailnet login yery.odell@", "tailnet login [redacted]"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(runner._redact_identities(raw), expected)

    def test_redact_preserves_generic_content(self):
        # only the KNOWN identifiers are redacted — never a generic name scan
        self.assertEqual(runner._redact_identities("John Smith's laptop"),
                         "John Smith's laptop")
        self.assertEqual(runner._redact_identities("Email me at a@b.com"),
                         "Email me at a@b.com")
        self.assertEqual(runner._redact_identities(""), "")


class CheckpointRollbackTest(unittest.TestCase):
    """Checkpoint + rollback mechanics: capture the full before-state, and on a
    mid-flight timeout surface 'applied step N of M, rollback state at <path>'
    with the restore command (never a half-applied mystery)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ckpt-")
        self.orig = runner.CHECKPOINT_BASE
        runner.CHECKPOINT_BASE = self.tmp

    def tearDown(self):
        runner.CHECKPOINT_BASE = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_read_checkpoint_roundtrip(self):
        state = {"switch_mac": "aa:bb:cc:dd:ee:01",
                 "ports": [{"port_idx": 6, "native_network_id": "x",
                            "tagged_network_ids": ["y"]}]}
        path = runner._write_checkpoint("TKT-CKPT-1", state, step=2, total=5)
        self.assertTrue(os.path.exists(path))
        cp = runner._read_checkpoint("TKT-CKPT-1")
        self.assertEqual(cp["state"], state)
        self.assertEqual(cp["step"], 2)
        self.assertEqual(cp["total"], 5)

    def test_rollback_hint_reports_step_of_total(self):
        runner._write_checkpoint("TKT-CKPT-2", {"ports": []}, step=2, total=5)
        hint = runner._rollback_hint("TKT-CKPT-2")
        self.assertIn("applied step 2 of 5, rollback state at", hint["message"])
        self.assertTrue(os.path.exists(hint["checkpoint"]))
        self.assertIn("infra_checkpoint.py restore", hint["restore_command"])

    def test_rollback_hint_without_checkpoint(self):
        hint = runner._rollback_hint("TKT-CKPT-NONE")
        self.assertIsNone(hint["checkpoint"])
        self.assertIn("no checkpoint captured", hint["message"])
        self.assertIn("infra_checkpoint.py restore", hint["restore_command"])

    def test_timeout_result_surfaces_checkpoint_and_restore(self):
        runner._write_checkpoint("TKT-CKPT-3", {"ports": []}, step=3, total=9)
        result = runner._timeout_result("TKT-CKPT-3", 600)
        self.assertFalse(result["success"])
        self.assertTrue(result["timed_out"])
        out = result["output"]
        self.assertIn("applied step 3 of 9, rollback state at", out["message"])
        self.assertTrue(os.path.exists(out["checkpoint"]))
        self.assertIn("infra_checkpoint.py restore", out["restore"])


class PiTimeoutReplayTest(unittest.TestCase):
    """The 08-19 incident replay: pi times out mid-execution after capturing its
    before-state -> the watchdog reports the checkpoint + restore state."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pi-replay-")
        self.orig_ckpt = runner.CHECKPOINT_BASE
        runner.CHECKPOINT_BASE = os.path.join(self.tmp, "checkpoints")
        self.fake_pi = os.path.join(self.tmp, "fake-pi")
        with open(self.fake_pi, "w") as f:
            f.write("#!/bin/sh\nsleep 3\n")
        os.chmod(self.fake_pi, 0o755)

    def tearDown(self):
        runner.CHECKPOINT_BASE = self.orig_ckpt
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_timeout_reports_checkpoint_and_restore(self):
        # Simulate the agent: capture the before-state (step 2 of 5), then hang.
        runner._write_checkpoint("TKT-REPLAY-1",
                                 {"switch_mac": "aa:bb:cc:dd:ee:01",
                                  "ports": [{"port_idx": 6,
                                             "native_network_id": "production"}]},
                                 step=2, total=5)
        env = {"PI_AGENT_BIN": self.fake_pi,
               "PI_AGENT_WORKDIR": os.path.join(self.tmp, "pi-work")}
        with patch.dict(os.environ, env), patch("time.sleep", return_value=None):
            result = runner._run_pi_task_impl(
                "change port 6 native vlan", "", "TKT-REPLAY-1", timeout=1)
        self.assertFalse(result["success"])
        self.assertTrue(result.get("timed_out"))
        out = result.get("output") or {}
        self.assertIn("applied step 2 of 5, rollback state at", out.get("message", ""))
        self.assertTrue(os.path.exists(out.get("checkpoint", "")))
        self.assertIn("infra_checkpoint.py restore", out.get("restore", ""))


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

        def fake_impl(task, context, ticket_id, timeout=600, web_research=False):
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

        def fake_impl(task, context, ticket_id, timeout=600, web_research=False):
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

    def test_identity_note_is_scrubbed(self):
        # TKT-20260823-4534 / 08-26: a note naming the developer/owner must be
        # replaced with a friendly generic — never reach the customer.
        samples = [
            "The prior work established the developer is yery",
            "the lead developer is yery.odell@ on the tailnet",
            "built by Yery O'Dell",
        ]
        for raw in samples:
            with self.subTest(raw=raw[:40]):
                self.assertTrue(runner._is_technical_note(raw),
                                f"should be technical: {raw!r}")
                friendly, was_filtered = runner._friendly_progress_note(raw)
                self.assertTrue(was_filtered)
                self.assertIn(friendly, runner._FRIENDLY_PROGRESS)
                self.assertNotEqual(friendly, raw)


class ProgressTonePoolTest(unittest.TestCase):
    """The 08-18 chat-tone-diversity work: a much larger, categorized pool.
    Category matching maps the raw note's activity via keyword cues; selection
    is deterministic per (ticket seed, note) and never immediately repeats."""

    def test_pool_has_dozens_of_variants_per_category(self):
        self.assertEqual(set(runner._TONE_POOL), set(runner._CATEGORIES))
        for category, phrases in runner._TONE_POOL.items():
            self.assertGreaterEqual(len(phrases), 10,
                                    f"{category} should have 10+ variants")
        # every phrase is short, friendly, and technical-free
        for phrase in runner._FRIENDLY_PROGRESS:
            self.assertLessEqual(len(phrase), 80)
            self.assertFalse(runner._is_technical_note(phrase),
                             f"pool phrase should be friendly: {phrase!r}")

    def test_category_matching(self):
        cases = [
            ("checking the logs to trace the outage", "investigating"),
            ("fetching the device list", "investigating"),
            ("reading through the config", "investigating"),
            ("ssh into the switch to talk to it", "connecting"),
            ("connecting to the gateway", "connecting"),
            ("installing the package now", "applying"),
            ("applying the change now", "applying"),
            ("configuring the new settings", "applying"),
            ("verifying everything is in place", "verifying"),
            ("confirming the result is correct", "verifying"),
            ("waiting for the long download", "waiting"),
            ("still processing the build", "waiting"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(runner._categorize(text), expected)

    def test_stable_seed_same_note_same_phrase(self):
        # a re-read of the same note (same ticket seed, no recent list) is
        # deterministic — the phrase never depends on wall-clock or randomness.
        a, fa = runner._friendly_progress_note("installing the dnf package", seed=42)
        b, fb = runner._friendly_progress_note("installing the dnf package", seed=42)
        self.assertTrue(fa and fb)
        self.assertEqual(a, b)

    def test_no_immediate_repeat(self):
        tone = runner._ProgressTone("TKT-20260818-0001")
        notes = [
            "installing the dnf package now",
            "configuring ssh access on the gateway",
            "applying the apt-get update now",
            "curl the device list from unifi",
            "checking the journalctl output",
            "verifying the dnf transaction result",
            "waiting for the long dnf transaction",
        ]
        seen = []
        for n in notes:
            phrase, filtered = tone.friendly(n)
            self.assertTrue(filtered, f"should be technical: {n!r}")
            self.assertIn(phrase, runner._FRIENDLY_PROGRESS)
            if seen:
                self.assertNotEqual(phrase, seen[-1], "consecutive notes must differ")
            seen.append(phrase)

    def test_adversarial_technical_strings_scrub(self):
        adversarial = [
            "cat /etc/shadow && echo uid=1001 > /tmp/x",
            "sudo sed -i s/foo/bar/ /etc/hosts",
            "POST https://localhost/api/v1/jobs/result with bearer token",
            "user barenoc uid 1001 gid 1001 NOPASSWD: ALL",
            "192.0.2.207 ssh root@0.0.0.0",
            "chmod 777 /opt/barenoc/volumes/secrets/llm_provider.json",
        ]
        for raw in adversarial:
            with self.subTest(raw=raw[:40]):
                friendly, filtered = runner._friendly_progress_note(raw)
                self.assertTrue(filtered, f"should scrub: {raw!r}")
                self.assertIn(friendly, runner._FRIENDLY_PROGRESS)
                for leak in ("/", "sudo", "uid", "192.168", "http", "token", "\t"):
                    self.assertNotIn(leak, friendly,
                                     f"leak {leak!r} in {friendly!r}")


class ProgressHeartbeatTest(unittest.TestCase):
    """Elapsed-time heartbeat: long pi tasks (>2 min with no distinct activity)
    inject a keep-alive note; every Nth heartbeat carries the elapsed time."""

    def test_elapsed_heartbeat_text(self):
        self.assertEqual(
            runner._elapsed_heartbeat(59),
            "Still working — about 1 min in — this one's a longer task…")
        self.assertIn("3 min in", runner._elapsed_heartbeat(180))
        self.assertIn("longer task", runner._elapsed_heartbeat(180))
        self.assertIn("1h", runner._elapsed_heartbeat(3600))

    def test_heartbeat_phrase_every_nth_includes_elapsed(self):
        # every 3rd heartbeat carries the elapsed-time text
        self.assertIn("min in", runner._heartbeat_phrase(180, 3))
        # the others draw a varied waiting phrase (no elapsed text)
        for nth in (1, 2):
            p = runner._heartbeat_phrase(180, nth)
            self.assertIn(p, runner._TONE_POOL["waiting"])
            self.assertNotIn("min in", p)

    def test_progress_tone_heartbeat_gating(self):
        tone = runner._ProgressTone("TKT-1", started_at=1000.0)
        self.assertIsNone(tone.heartbeat(1119))          # before 2 min
        hb = tone.heartbeat(1120)                        # 120s elapsed, idle 120s
        self.assertIsNotNone(hb)
        self.assertEqual(tone.last_note_at, 1120)
        self.assertIsNone(tone.heartbeat(1160))          # only 40s of idle
        self.assertIsNotNone(tone.heartbeat(1165))       # 45s idle gap met

    def test_progress_tone_heartbeat_every_third_elapsed(self):
        tone = runner._ProgressTone("TKT-1", started_at=0.0)
        phrases = []
        now = 120.0
        for _ in range(3):
            tone.last_note_at = now - 45   # ensure the idle gap is met
            hb = tone.heartbeat(now)
            self.assertIsNotNone(hb)
            phrases.append(hb)
            now += 45
        self.assertIn("min in", phrases[2])
        self.assertNotIn("min in", phrases[0])
        self.assertNotIn("min in", phrases[1])


class TonePoolParityTest(unittest.TestCase):
    """The runner VENDORS its pool/patterns from src/api/tone_pool.py (it
    deploys as a single self-contained file). Assert the copies stay in sync so
    the runner and the API-side queue_status speak the same vocabulary."""

    def test_runner_matches_shared_module(self):
        api_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
        sys.path.insert(0, api_dir)
        import tone_pool
        self.assertEqual(runner._TONE_POOL, tone_pool._POOL)
        self.assertEqual(runner._CATEGORY_KEYWORDS, tone_pool._CATEGORY_KEYWORDS)
        self.assertEqual(runner._CATEGORIES, tone_pool.CATEGORIES)
        self.assertEqual(
            [p.pattern for p in runner._TECH_NOTE_PATTERNS],
            [p.pattern for p in tone_pool._TECH_NOTE_PATTERNS])
        self.assertEqual(
            [p.pattern for p in runner._IDENTITY_PATTERNS],
            [p.pattern for p in tone_pool.IDENTITY_PATTERNS])
        self.assertEqual(runner._FRIENDLY_PROGRESS, tone_pool.all_phrases())


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


class LoginCacheAndBackoffTest(unittest.TestCase):
    """08-17 runner-login robustness: a short-lived token cache (reuse ≤5 min)
    + retry/backoff on 429/5xx + a single re-login on 401 for the /jobs/result
    POST, so a rate-limit blip can never orphan a finished job again."""

    def setUp(self):
        runner._TOKEN_CACHE["token"] = None
        runner._TOKEN_CACHE["expires_at"] = 0.0

    @staticmethod
    def _fake_resp(status=200, body='{"access_token": "TOK1"}'):
        class Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            def read(self):
                return self._body.encode() if isinstance(self._body, str) else self._body
        return Resp(status, body)

    @staticmethod
    def _http_error(code):
        import io
        return __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "https://localhost/x", code, f"err {code}", None, io.BytesIO(b""))

    def test_login_is_cached_within_ttl(self):
        with patch("runner._api_credentials", return_value={"username": "agent", "password": "pw"}), \
             patch("runner.urllib.request.urlopen",
                   return_value=self._fake_resp()) as urlopen:
            t1 = runner._api_login()
            t2 = runner._api_login()
        self.assertEqual(t1, "TOK1")
        self.assertEqual(t2, "TOK1")          # reused from the cache
        self.assertEqual(urlopen.call_count, 1)  # only ONE login POST

    def test_login_renews_after_ttl_expiry(self):
        runner._TOKEN_CACHE["token"] = "OLD"
        runner._TOKEN_CACHE["expires_at"] = time.time() - 1
        with patch("runner._api_credentials", return_value={"username": "agent", "password": "pw"}), \
             patch("runner.urllib.request.urlopen",
                   return_value=self._fake_resp(body='{"access_token": "NEW"}')):
            token = runner._api_login()
        self.assertEqual(token, "NEW")

    def test_login_retries_429_then_succeeds(self):
        with patch("runner._api_credentials", return_value={"username": "agent", "password": "pw"}), \
             patch("runner.urllib.request.urlopen",
                   side_effect=[self._http_error(429), self._http_error(503),
                                self._fake_resp()]) as urlopen, \
             patch("runner._sleep") as sleep:
            token = runner._api_login()
        self.assertEqual(token, "TOK1")
        self.assertEqual(urlopen.call_count, 3)
        self.assertTrue(sleep.call_count >= 2)  # backoff between attempts

    def test_login_401_returns_empty_and_clears_cache(self):
        runner._TOKEN_CACHE["token"] = "STALE"
        runner._TOKEN_CACHE["expires_at"] = time.time() + 1000
        with patch("runner._api_credentials", return_value={"username": "agent", "password": "pw"}), \
             patch("runner.urllib.request.urlopen", side_effect=self._http_error(401)):
            token = runner._api_login(force=True)
        self.assertEqual(token, "")
        self.assertIsNone(runner._TOKEN_CACHE["token"])

    def test_login_surfaces_error_after_retries(self):
        with patch("runner._api_credentials", return_value={"username": "agent", "password": "pw"}), \
             patch("runner.urllib.request.urlopen", side_effect=self._http_error(429)), \
             patch("runner._sleep"):
            token = runner._api_login()
        self.assertEqual(token, "")

    def test_post_result_relogins_once_on_401(self):
        payload = {"ticket_id": "TKT-1", "success": True}
        with patch("runner._api_login", side_effect=["STALE", "FRESH"]) as login, \
             patch("runner.urllib.request.urlopen",
                   side_effect=[self._http_error(401), self._fake_resp(body="{}")]) as urlopen:
            ok = runner._post_jobs_result("TKT-1", payload)
        self.assertTrue(ok)
        self.assertEqual(urlopen.call_count, 2)
        # second login must be a forced, cache-bypassing re-login
        login.assert_any_call(force=True)

    def test_post_result_retries_429_with_backoff(self):
        with patch("runner._api_login", return_value="TOK"), \
             patch("runner.urllib.request.urlopen",
                   side_effect=[self._http_error(429), self._fake_resp(body="{}")]) as urlopen, \
             patch("runner._sleep") as sleep:
            ok = runner._post_jobs_result("TKT-1", {"success": True})
        self.assertTrue(ok)
        self.assertEqual(urlopen.call_count, 2)
        self.assertTrue(sleep.called)

    def test_post_result_second_401_surfaces_error(self):
        # re-login ONCE then surface the error — never loop on bad creds
        with patch("runner._api_login", side_effect=["STALE", "STALE2"]), \
             patch("runner.urllib.request.urlopen", side_effect=self._http_error(401)):
            ok = runner._post_jobs_result("TKT-1", {"success": True})
        self.assertFalse(ok)

    def test_post_result_no_token_returns_false(self):
        with patch("runner._api_login", return_value=""), \
             patch("runner.urllib.request.urlopen") as urlopen:
            ok = runner._post_jobs_result("TKT-1", {"success": True})
        self.assertFalse(ok)
        urlopen.assert_not_called()


class SweepRunnerTest(unittest.TestCase):
    """The sweep path streams PROGRESS notes and aborts cleanly at the
    deadline — a subnet sweep never hangs the runner (the 08-19 fix)."""

    def test_streams_progress_and_parses_json(self):
        cmd = ["bash", "-c",
               'echo "PROGRESS: Scanned 1 of 1 hosts (1 up)" >&2; '
               'echo \'{"network": "192.0.2.0/30", "found": [{"ip": "192.0.2.1"}], "count": 1}\'']
        notes = []
        with patch("runner._post_progress", side_effect=lambda tid, text, tone=None: notes.append(text)):
            res = runner._run_sweep(cmd, "TKT-SWEEP-1", os.environ.copy(),
                                    "network_discovery", "192.0.2.0/30")
        self.assertTrue(res["success"])
        self.assertEqual(res["output"]["count"], 1)
        self.assertEqual(notes, ["Scanned 1 of 1 hosts (1 up)"])

    def test_sweep_aborts_cleanly_on_timeout(self):
        # A long sleep must be killed at the deadline and reported as a clean
        # abort — never an indefinite hang.
        cmd = ["bash", "-c", "sleep 30"]
        with patch("runner.JOB_TIMEOUT", 1), patch("runner._post_progress"):
            res = runner._run_sweep(cmd, "TKT-SWEEP-2", os.environ.copy(),
                                    "network_discovery", "10.0.0.0/24")
        self.assertFalse(res["success"])
        self.assertIn("Timed out", res["error"])


class PiLocalProviderTest(unittest.TestCase):
    """Compliance LLM egress (local-only): pi runs the on-prem endpoint."""

    def setUp(self):
        self.orig = runner.PI_PROVIDER_SECRET_FILE

    def tearDown(self):
        runner.PI_PROVIDER_SECRET_FILE = self.orig

    def test_local_secret_file_returns_base_url(self):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
        tmp.write(json.dumps({
            "provider": "openai",
            "model": "qwen2.5:7b-instruct",
            "api_key": "ollama",
            "base_url": "http://192.168.1.50:11434/v1",
            "local": True,
        }))
        tmp.close()
        runner.PI_PROVIDER_SECRET_FILE = tmp.name
        try:
            cfg = runner._pi_provider_config()
        finally:
            os.unlink(tmp.name)
        self.assertEqual(cfg["provider"], "openai")
        self.assertEqual(cfg["model"], "qwen2.5:7b-instruct")
        self.assertEqual(cfg["base_url"], "http://192.168.1.50:11434/v1")
        self.assertEqual(cfg["api_key"], "ollama")

    def test_cloud_secret_file_has_no_base_url(self):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
        tmp.write(json.dumps({"provider": "deepseek", "model": "deepseek-v4-flash",
                              "api_key": "sk-test"}))
        tmp.close()
        runner.PI_PROVIDER_SECRET_FILE = tmp.name
        try:
            cfg = runner._pi_provider_config()
        finally:
            os.unlink(tmp.name)
        self.assertEqual(cfg["provider"], "deepseek")
        self.assertNotIn("base_url", cfg)

    def test_missing_file_falls_back_without_crash(self):
        runner.PI_PROVIDER_SECRET_FILE = "/nonexistent/llm_provider.json"
        # .env is also absent in the test env — must not raise
        cfg = runner._pi_provider_config()
        self.assertIn("provider", cfg)


class PiUsageMeteringTest(unittest.TestCase):
    """The runner sums pi's persisted per-message usage from the session JSONL
    and reports it in the job result; when pi exposes no usage it falls back to
    a clearly-labeled chars/4 estimate (never a silent 0.00)."""

    def _write_session(self, session_dir, entries):
        os.makedirs(session_dir, exist_ok=True)
        path = os.path.join(session_dir, "run.jsonl")
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def _assistant(self, mid, parent, inp, out, cache_read=0):
        return {
            "type": "message", "id": mid, "parentId": parent,
            "timestamp": "2026-08-30T00:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input": inp, "output": out,
                          "cacheRead": cache_read, "cacheWrite": 0,
                          "reasoning": 0, "totalTokens": inp + out + cache_read},
                "stopReason": "stop",
            },
        }

    def test_sums_real_usage(self):
        d = tempfile.mkdtemp(prefix="pi-usage-")
        try:
            self._write_session(d, [
                {"type": "session", "version": 3},
                self._assistant("a", None, 100, 50, 10),
                self._assistant("b", "a", 40, 20, 5),
            ])
            s = runner._sum_pi_session_usage(d)
            self.assertEqual(s, {"input": 140, "output": 70,
                                 "cache_read": 15, "cache_write": 0,
                                 "reasoning": 0})
            block = runner._pi_usage_block(d, "task text", "reply")
            self.assertFalse(block["estimated"])
            self.assertEqual(block["input_tokens"], 140)
            self.assertEqual(block["output_tokens"], 70)
            self.assertEqual(block["total_tokens"], 225)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_estimates_when_no_usage(self):
        d = tempfile.mkdtemp(prefix="pi-usage-empty-")
        try:
            block = runner._pi_usage_block(d, "x" * 400, "y" * 200)
            self.assertTrue(block["estimated"])
            self.assertEqual(block["input_tokens"], 100)   # 400 / 4
            self.assertEqual(block["output_tokens"], 50)   # 200 / 4
            self.assertIn("estimate", block["note"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_ignores_non_usage_entries(self):
        d = tempfile.mkdtemp(prefix="pi-usage-mixed-")
        try:
            self._write_session(d, [
                {"type": "session", "version": 3},
                {"type": "message", "id": "u", "parentId": None,
                 "timestamp": "2026-08-30T00:00:00.000Z",
                 "message": {"role": "user", "content": "hi"}},
                {"type": "custom", "id": "c", "parentId": "u",
                 "timestamp": "2026-08-30T00:00:01.000Z",
                 "customType": "x", "data": {}},
            ])
            self.assertEqual(runner._sum_pi_session_usage(d), {})
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_file_scope_prevents_double_count(self):
        # A re-dispatched ticket reuses its session dir — summing only THIS
        # run's files must not pick up an earlier run's usage.
        d = tempfile.mkdtemp(prefix="pi-usage-scope-")
        try:
            self._write_session(d, [self._assistant("old", None, 1000, 500)])
            with open(os.path.join(d, "new.jsonl"), "w") as f:
                f.write(json.dumps(self._assistant("new", "old", 40, 20)) + "\n")
            s = runner._sum_pi_session_usage(d, files=["new.jsonl"])
            self.assertEqual(s["input"], 40)
            self.assertEqual(s["output"], 20)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
