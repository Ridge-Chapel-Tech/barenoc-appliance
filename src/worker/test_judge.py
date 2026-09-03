#!/usr/bin/env python3
"""Tests for the two-phase judge/executor prototype (worker side).

Run from src/worker:
    python3 -m unittest test_judge -v

Covers: short-circuit rules, verdict cache + TTL, verdict parsing/schema,
mock judge, executor scoping, model-tier selection in llm_client, and
judge_model fallback in llm_providers. (process_ticket integration needs
SQLAlchemy and runs in-container — see deploy verification.)
"""

import os
import sys
import json
import time
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))           # worker/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))  # api/ (llm_providers)

import judge
import executor
import llm_client
import llm_providers
import policy

from judge import (Verdict, short_circuit_verdict, judge_request,
                   _parse_verdict, _coerce_verdict)
from executor import call_executor, build_executor_prompt
from llm_client import LLMResponse
from policy import Policy, load_policy
from action_validator import patch_allowlist
from llm_providers import read_env_file


def _fake_llm_response(raw_text: str) -> LLMResponse:
    return LLMResponse(action="ping_test", target="switch-01", params={},
                       reason="t", confidence=0.95, raw_text=raw_text,
                       model="deepseek/deepseek-reasoner",
                       prompt_tokens=10, response_tokens=20, cost_usd=0.0001)


class JudgeShortCircuitTest(unittest.TestCase):
    def test_ping_matches_short_circuit(self):
        v = short_circuit_verdict("Can you ping the gateway?", "P3")
        self.assertIsNotNone(v)
        self.assertEqual(v.lawful, "yes")
        self.assertEqual(v.action_class, "ping_test")
        self.assertTrue(v.short_circuit)

    def test_status_matches_short_circuit(self):
        v = short_circuit_verdict("is switch-01 online?", "P3")
        self.assertIsNotNone(v)
        self.assertEqual(v.action_class, "device_status")

    def test_network_report_matches_short_circuit(self):
        # "report" alone no longer maps (generate_report was dead code);
        # the network word short-circuits to network_info instead.
        v = short_circuit_verdict("give me a network health report", "P4")
        self.assertIsNotNone(v)
        self.assertEqual(v.action_class, "network_info")

    def test_endpoint_scan_short_circuits_to_network_discovery(self):
        # "endpoints responding on a subnet" is a subnet ping-sweep, NOT a
        # network/VLAN summary — the word "network" must not route it to
        # network_info (forum: "odd results when looking for endpoints").
        v = short_circuit_verdict(
            "Can you please tell me what endpoints are responding on the "
            "192.168.1.0/24 network?", "P3")
        self.assertIsNotNone(v)
        self.assertEqual(v.action_class, "network_discovery")

    def test_endpoint_scan_without_subnet_not_short_circuited(self):
        # No concrete CIDR -> no endpoint-scan short-circuit (returns None so
        # the real judge decides; no generic pattern matches this phrase).
        v = short_circuit_verdict("which endpoints are responding", "P3")
        self.assertIsNone(v)

    def test_write_action_never_short_circuits(self):
        for text in ("reboot switch-01 tonight",
                     "apply patch FW-6.6.55",
                     "block 192.0.2.66",
                     "change the management VLAN"):
            self.assertIsNone(short_circuit_verdict(text, "P2"),
                              f"'{text}' should force a judge call")

    def test_empty_text_no_verdict(self):
        self.assertIsNone(short_circuit_verdict("", "P1"))


class VerdictParsingTest(unittest.TestCase):
    def test_parse_think_block_and_fences(self):
        raw = "<think>is this legal? yes</think>\n```json\n{\"lawful\": \"yes\", \"action_class\": \"ping_test\"}\n```"
        p = _parse_verdict(raw)
        self.assertEqual(p["lawful"], "yes")
        self.assertEqual(p["action_class"], "ping_test")

    def test_parse_embedded_json(self):
        p = _parse_verdict('prose here {"lawful": "no", "reason": "nope"} trailing')
        self.assertEqual(p["lawful"], "no")

    def test_coerce_rejects_unknown_action(self):
        self.assertIsNone(_coerce_verdict(
            {"lawful": "yes", "action_class": "rm_rf"}))

    def test_coerce_accepts_valid(self):
        v = _coerce_verdict({"lawful": "yes", "action_class": "network_info",
                             "risk": "low", "checks": {"safe": True}})
        self.assertIsNotNone(v)
        self.assertEqual(v.risk, "low")
        self.assertEqual(v.checks, {"safe": True})

    def test_coerce_rejects_bad_lawful(self):
        self.assertIsNone(_coerce_verdict({"lawful": "maybe", "action_class": "ping_test"}))


class JudgePipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["LLM_VERDICT_CACHE_FILE"] = os.path.join(self.tmpdir.name, "verdicts.json")
        os.environ["LLM_VERDICT_CACHE_TTL_H"] = "24"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_mock_judge_unlawful(self):
        # No API key -> mock judge path
        with patch("judge.get_provider", return_value={"api_key": ""}):
            v = judge_request("delete the firewall rules", "P1")
        self.assertEqual(v.lawful, "no")

    def test_mock_judge_lawful(self):
        with patch("judge.get_provider", return_value={"api_key": ""}):
            v = judge_request("is the network up?", "P3")
        self.assertEqual(v.lawful, "yes")

    def test_short_circuit_no_llm_call(self):
        provider = {"api_key": "real-key", "type": "openai", "name": "deepseek"}
        with patch("judge.get_provider", return_value=provider), \
             patch("llm_client.call_llm") as mock_call:
            v = judge_request("ping the gateway please", "P3")
        mock_call.assert_not_called()
        self.assertTrue(v.short_circuit)
        self.assertEqual(v.lawful, "yes")

    def test_llm_judge_and_cache(self):
        provider = {"api_key": "real-key", "type": "openai", "name": "deepseek"}
        raw = json.dumps({"lawful": "yes", "action_class": "ping_test", "risk": "low",
                          "scope": "managed", "checks": {"legal": True, "doable": True,
                          "safe": True, "in_scope": True}, "reason": "routine ping"})
        with patch("judge.get_provider", return_value=provider), \
             patch("llm_client.call_llm", return_value=_fake_llm_response(raw)) as mock_call:
            v1 = judge_request("snmp walk the switch for interface counters", "P3")
            v2 = judge_request("snmp walk the switch for interface counters", "P3")
        self.assertEqual(v1.lawful, "yes")
        self.assertEqual(v1.action_class, "ping_test")
        self.assertEqual(v1.model, "deepseek/deepseek-reasoner")
        self.assertEqual(mock_call.call_count, 1)   # second hit the cache
        self.assertFalse(v1.cached)
        self.assertTrue(v2.cached)

    def test_cache_ttl_expiry(self):
        os.environ["LLM_VERDICT_CACHE_TTL_H"] = "0"
        provider = {"api_key": "real-key", "type": "openai", "name": "deepseek"}
        raw = json.dumps({"lawful": "no", "action_class": None, "risk": "high",
                          "checks": {}, "reason": "denied"})
        with patch("judge.get_provider", return_value=provider), \
             patch("llm_client.call_llm", return_value=_fake_llm_response(raw)) as mock_call:
            judge_request("erase the logs", "P1")
            judge_request("erase the logs", "P1")
        self.assertEqual(mock_call.call_count, 2)   # TTL=0 -> never cached

    def test_schema_failure_becomes_ambiguous(self):
        provider = {"api_key": "real-key", "type": "openai", "name": "deepseek"}
        raw = json.dumps({"lawful": "yes", "action_class": "rm_rf"})
        with patch("judge.get_provider", return_value=provider), \
             patch("llm_client.call_llm", return_value=_fake_llm_response(raw)):
            v = judge_request("delete everything", "P1")
        self.assertEqual(v.lawful, "ambiguous")


class ExecutorTest(unittest.TestCase):
    def test_mock_executor_scoped_to_verdict(self):
        v = Verdict(lawful="yes", action_class="ping_test", risk="low",
                    checks={"legal": True}, reason="ok")
        with patch("executor.get_provider", return_value={"api_key": ""}):
            resp = call_executor("ping the gateway", "P3", verdict=v)
        self.assertEqual(resp.action, "ping_test")
        self.assertNotIn("escalate_human", resp.action)

    def test_executor_escalates_without_verdict(self):
        with patch("executor.get_provider", return_value={"api_key": ""}):
            resp = call_executor("anything", "P3", verdict=None)
        self.assertEqual(resp.action, "escalate_human")

    def test_mock_executor_network_discovery_extracts_subnet(self):
        v = Verdict(lawful="yes", action_class="network_discovery", risk="low",
                    checks={"legal": True}, reason="ok")
        with patch("executor.get_provider", return_value={"api_key": ""}):
            resp = call_executor(
                "what endpoints are responding on 192.168.1.0/24", "P3", verdict=v)
        self.assertEqual(resp.action, "network_discovery")
        self.assertEqual(resp.target, "192.168.1.0/24")

    def test_prompt_contains_only_approved_action(self):
        v = Verdict(lawful="yes", action_class="reboot_device", risk="high",
                    checks={"safe": False}, reason="needs window")
        prompt = build_executor_prompt(v)
        self.assertIn("reboot_device", prompt)
        # the catalog is present, but the rule pins the executor to one action
        self.assertIn("You may ONLY output the approved action 'reboot_device'", prompt)


class PatchAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.env_backup = dict(os.environ)
        os.environ.pop("PATCH_ALLOWLIST", None)
        # Hermetic: ignore the mounted .env (it may hold explicit values)
        self._env_patcher = patch("llm_providers.read_env_file", return_value={})
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        os.environ.clear()
        os.environ.update(self.env_backup)

    def test_defaults_when_unset(self):
        self.assertIn("FW-6.6.55", patch_allowlist())
        self.assertEqual(len(patch_allowlist()), 4)

    def test_custom_list(self):
        os.environ["PATCH_ALLOWLIST"] = "FW-1.0.0,FW-2.0.0"
        self.assertEqual(patch_allowlist(), ["FW-1.0.0", "FW-2.0.0"])

    def test_wildcard(self):
        os.environ["PATCH_ALLOWLIST"] = "*"
        self.assertEqual(patch_allowlist(), ["*"])

    def test_empty_string_falls_back(self):
        os.environ["PATCH_ALLOWLIST"] = ""
        self.assertIn("FW-6.6.55", patch_allowlist())


class NewReadActionsTest(unittest.TestCase):
    def test_who_is_online_short_circuits_to_unifi_clients(self):
        v = short_circuit_verdict("who is online right now?", "P3")
        self.assertIsNotNone(v)
        self.assertEqual(v.action_class, "unifi_clients")

    def test_connected_clients_short_circuits(self):
        v = short_circuit_verdict("list the connected clients", "P4")
        self.assertEqual(v.action_class, "unifi_clients")

    def test_switch_ports_short_circuits_when_filters_off(self):
        v = short_circuit_verdict("show me the switch ports", "P3", "none")
        self.assertIsNotNone(v)
        self.assertEqual(v.action_class, "unifi_ports")
        # with all filters on, "port" is a risk word -> judge must decide
        self.assertIsNone(short_circuit_verdict("show me the switch ports", "P3"))

    def test_new_actions_in_catalog(self):
        for a in ("unifi_clients", "unifi_devices", "unifi_ports"):
            self.assertIn(a, judge.ACTION_CATALOG)

    def test_new_actions_validator_accepts(self):
        from action_validator import validate_action
        ok, _ = validate_action("unifi_clients")
        self.assertTrue(ok)
        ok, _ = validate_action("unifi_ports")
        self.assertTrue(ok)


class TicketStatusActionTest(unittest.TestCase):
    """ticket_status — read-only catalog action that answers TKT-… references."""

    def test_in_catalog(self):
        self.assertIn("ticket_status", judge.ACTION_CATALOG)

    def test_validate_action(self):
        from action_validator import validate_action, validate_params
        self.assertTrue(validate_action("ticket_status")[0])
        self.assertTrue(validate_params("ticket_status",
                                        {"ticket_id": "TKT-20260816-5935"})[0])

    def test_validate_params_rejects_bad_tkt(self):
        from action_validator import validate_params
        for bad in ("", "TKT-123", "TKT-20260816-593", "TKT-20260816-59355",
                    "ABC-20260816-5935", "TKT-20260816-5935x"):
            ok, msg = validate_params("ticket_status", {"ticket_id": bad})
            self.assertFalse(ok, f"should reject {bad!r}: {msg}")
        self.assertFalse(validate_params("ticket_status", {})[0])

    def test_validate_params_normalizes_case(self):
        from action_validator import validate_params
        self.assertTrue(validate_params("ticket_status",
                                        {"ticket_id": "tkt-20260816-5935"})[0])

    def test_short_circuit_tkt_status(self):
        v = short_circuit_verdict("where's TKT-20260816-5935 at?", "P3")
        self.assertIsNotNone(v)
        self.assertEqual(v.action_class, "ticket_status")
        self.assertTrue(v.short_circuit)
        # the TKT pattern must win over the generic "status" -> device_status
        v2 = short_circuit_verdict("status on TKT-20260816-5935", "P3")
        self.assertEqual(v2.action_class, "ticket_status")

    def test_mock_judge_tkt_status(self):
        with patch("judge.get_provider", return_value={"api_key": ""}):
            v = judge_request("is TKT-20260816-5935 done?", "P3")
        self.assertEqual(v.lawful, "yes")
        self.assertEqual(v.action_class, "ticket_status")

    def test_mock_executor_synthetic(self):
        v = Verdict(lawful="yes", action_class="ticket_status", risk="low",
                    checks={"legal": True}, reason="ok")
        with patch("executor.get_provider", return_value={"api_key": ""}):
            resp = call_executor("status on TKT-20260816-5935", "P3", verdict=v)
        self.assertEqual(resp.action, "ticket_status")
        self.assertEqual(resp.target, "")
        self.assertEqual(resp.params.get("ticket_id"), "TKT-20260816-5935")


class UnifiPortConfigTest(unittest.TestCase):
    """unifi_port_config is now a first-class worker action (was enum-gapped)."""
    def test_validate_action(self):
        from action_validator import validate_action
        ok, _ = validate_action("unifi_port_config")
        self.assertTrue(ok)

    def test_validate_params_requires_port_idx(self):
        from action_validator import validate_params
        ok, msg = validate_params("unifi_port_config", {"tagged": ["Storage"]})
        self.assertFalse(ok)
        self.assertIn("port_idx", msg)

    def test_validate_params_requires_tagged(self):
        from action_validator import validate_params
        ok, msg = validate_params("unifi_port_config", {"port_idx": 7})
        self.assertFalse(ok)
        self.assertIn("tagged", msg)

    def test_validate_params_valid(self):
        from action_validator import validate_params
        ok, _ = validate_params("unifi_port_config",
                                {"port_idx": 7, "tagged": ["Storage"], "native": "Production"})
        self.assertTrue(ok)


class UnifiNetworkCreateTest(unittest.TestCase):
    """unifi_network_create — create a new VLAN/subnet via tickets."""
    def test_in_catalog(self):
        self.assertIn("unifi_network_create", judge.ACTION_CATALOG)

    def test_validate_action(self):
        from action_validator import validate_action
        ok, _ = validate_action("unifi_network_create")
        self.assertTrue(ok)

    def test_requires_name(self):
        from action_validator import validate_params
        ok, msg = validate_params("unifi_network_create", {"vlan": 12})
        self.assertFalse(ok)
        self.assertIn("name", msg)

    def test_requires_vlan(self):
        from action_validator import validate_params
        ok, msg = validate_params("unifi_network_create", {"name": "IoT"})
        self.assertFalse(ok)
        self.assertIn("vlan", msg)

    def test_vlan_range(self):
        from action_validator import validate_params
        ok, msg = validate_params("unifi_network_create", {"name": "IoT", "vlan": 0})
        self.assertFalse(ok)
        ok, msg = validate_params("unifi_network_create", {"name": "IoT", "vlan": 4095})
        self.assertFalse(ok)

    def test_bad_subnet(self):
        from action_validator import validate_params
        ok, msg = validate_params("unifi_network_create",
                                  {"name": "IoT", "vlan": 12, "subnet": "not-a-cidr"})
        self.assertFalse(ok)
        self.assertIn("CIDR", msg)

    def test_valid(self):
        from action_validator import validate_params
        ok, _ = validate_params("unifi_network_create",
                                {"name": "IoT", "vlan": 12, "subnet": "192.168.12.1/24"})
        self.assertTrue(ok)

    def test_enroll_device_params(self):
        from action_validator import validate_params, validate_action
        ok, _ = validate_action("enroll_device")
        self.assertTrue(ok)
        ok, _ = validate_params("enroll_device", {})
        self.assertTrue(ok)
        ok, _ = validate_params("enroll_device", {"ttl": 300})
        self.assertTrue(ok)
        ok, msg = validate_params("enroll_device", {"ttl": 99999})
        self.assertFalse(ok)

    def test_catalog_includes(self):
        self.assertIn("unifi_port_config", judge.ACTION_CATALOG)

    def test_mac_target_passes_validation(self):
        from action_validator import MANAGED_DEVICES, validate_target
        MANAGED_DEVICES["aa:bb:cc:dd:ee:01"] = {"id": 1, "ip": "10.0.0.9",
                                                 "type": "switch", "hostname": None}
        ok, _ = validate_target("aa:bb:cc:dd:ee:01")
        self.assertTrue(ok)

    def test_new_unifi_actions_validator(self):
        from action_validator import validate_action, validate_params
        for a in ("unifi_client_port", "unifi_firewall_rules", "unifi_restart"):
            ok, _ = validate_action(a)
            self.assertTrue(ok, a)
        ok, _ = validate_params("unifi_restart", {})
        self.assertTrue(ok)

    def test_new_unifi_actions_in_catalog(self):
        for a in ("unifi_client_port", "unifi_firewall_rules", "unifi_restart"):
            self.assertIn(a, judge.ACTION_CATALOG)

    def test_batch_validator(self):
        from action_validator import validate_action, validate_params, validate_target
        ok, _ = validate_action("batch")
        self.assertTrue(ok)
        # valid batch
        ok, msg = validate_params("batch", {"jobs": [
            {"action": "unifi_port_bounce", "target": "aa:bb:cc:dd:ee:01",
             "params": {"port_idx": 1}},
            {"action": "unifi_port_rename", "target": "aa:bb:cc:dd:ee:01",
             "params": {"port_idx": 2, "name": "camera"}},
        ]})
        self.assertTrue(ok, msg)
        # invalid sub-action rejected
        ok, msg = validate_params("batch", {"jobs": [{"action": "rm_rf"}]})
        self.assertFalse(ok)
        self.assertIn("batch job 0", msg)
        # nested batch rejected
        ok, msg = validate_params("batch", {"jobs": [{"action": "batch",
                                                        "params": {"jobs": []}}]})
        self.assertFalse(ok)
        # empty batch rejected
        ok, msg = validate_params("batch", {"jobs": []})
        self.assertFalse(ok)
        # cap
        ok, msg = validate_params("batch", {"jobs": [{"action": "ping_test"}] * 51})
        self.assertFalse(ok)
        self.assertIn("max 50", msg)

    def test_port_bounce_rename_validate(self):
        from action_validator import validate_action, validate_params
        for a in ("unifi_port_bounce", "unifi_port_rename"):
            self.assertTrue(validate_action(a)[0])
        self.assertFalse(validate_params("unifi_port_bounce", {})[0])
        self.assertTrue(validate_params("unifi_port_bounce", {"port_idx": 3})[0])
        self.assertFalse(validate_params("unifi_port_rename", {"port_idx": 3})[0])
        self.assertTrue(validate_params("unifi_port_rename",
                                        {"port_idx": 3, "name": "camera"})[0])
        # unifi_devices type filter
        from action_validator import validate_params as vp
        self.assertTrue(vp("unifi_devices", {"device_type": "ap"})[0])
        self.assertFalse(vp("unifi_devices", {"device_type": "nope"})[0])
        self.assertTrue(vp("unifi_devices", {"device_type": "ap", "status": "offline"})[0])
        self.assertFalse(vp("unifi_devices", {"status": "nope"})[0])
        # unifi_clients filters
        self.assertTrue(vp("unifi_clients", {"online": True})[0])
        self.assertTrue(vp("unifi_clients", {"wired": False})[0])
        self.assertFalse(vp("unifi_clients", {"online": "maybe"})[0])
        # unifi_set_ssid_password
        self.assertTrue(vp("unifi_set_ssid_password", {"ssid": "IoT", "password": "newpass1234"})[0])
        self.assertFalse(vp("unifi_set_ssid_password", {"ssid": "IoT", "password": "short"})[0])
        self.assertFalse(vp("unifi_set_ssid_password", {"password": "newpass1234"})[0])


class TargetValidationWordingTest(unittest.TestCase):
    """Friendlier unknown-target wording + whole-subnet scan helpers (bug #2)."""

    def setUp(self):
        from action_validator import MANAGED_DEVICES
        self._saved = dict(MANAGED_DEVICES)
        MANAGED_DEVICES.clear()
        MANAGED_DEVICES["gateway"] = {"id": 1, "ip": "192.0.2.1",
                                      "type": "router", "hostname": None}

    def tearDown(self):
        from action_validator import MANAGED_DEVICES
        MANAGED_DEVICES.clear()
        MANAGED_DEVICES.update(self._saved)

    def test_unknown_name_returns_friendly_product_message(self):
        from action_validator import validate_target
        ok, msg = validate_target("switch-01")
        self.assertFalse(ok)
        self.assertIn("I couldn't find a device named 'switch-01'", msg)
        self.assertIn("adopt", msg)
        self.assertNotIn("managed inventory", msg)

    def test_technical_detail_kept_for_ticket_log(self):
        from action_validator import unknown_target_detail
        detail = unknown_target_detail("switch-01")
        self.assertIn("not in managed inventory", detail)
        self.assertIn("gateway", detail)

    def test_ip_and_subnet_still_pass(self):
        from action_validator import validate_target
        self.assertTrue(validate_target("192.0.2.50")[0])
        self.assertTrue(validate_target("192.168.1.0/24")[0])

    def test_find_subnet_extracts_cidr(self):
        from action_validator import find_subnet
        self.assertEqual(find_subnet("ping sweep 192.168.1.0/24 please"),
                         "192.168.1.0/24")
        self.assertIsNone(find_subnet("ping the switch please"))


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.env_backup = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("LLM_POLICY"):
                del os.environ[k]
        # Hermetic: pretend the .env file doesn't exist (on the VM it holds
        # explicit defaults that would correctly override profile presets)
        self._env_patcher = patch("policy.read_env_file", return_value={})
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        os.environ.clear()
        os.environ.update(self.env_backup)
        policy.reset_policy_cache()

    def test_legacy_default(self):
        p = load_policy()
        self.assertTrue(p.legacy)
        self.assertFalse(p.judge_required)

    def test_autonomous_profile(self):
        os.environ["LLM_POLICY_PROFILE"] = "autonomous"
        p = load_policy()
        self.assertEqual(p.risk_filters, "none")
        self.assertTrue(p.write_autoexec)
        self.assertEqual(p.approval_priorities, ())
        self.assertTrue(p.judge_required)

    def test_strict_profile(self):
        os.environ["LLM_POLICY_PROFILE"] = "strict"
        p = load_policy()
        self.assertFalse(p.write_autoexec)
        self.assertEqual(p.approval_priorities, ("P1", "P2"))
        self.assertEqual(p.risk_filters, "all")

    def test_granular_override_wins(self):
        os.environ["LLM_POLICY_PROFILE"] = "balanced"
        os.environ["LLM_POLICY_WRITE_AUTOEXEC"] = "false"
        p = load_policy()
        self.assertFalse(p.write_autoexec)  # override beats profile default True

    def test_threshold_override(self):
        os.environ["LLM_POLICY_PROFILE"] = "autonomous"
        os.environ["LLM_POLICY_AUTOEXEC_THRESHOLD"] = "0.95"
        p = load_policy()
        self.assertEqual(p.autoexec_threshold, 0.95)

    def test_active_risk_patterns_none(self):
        os.environ["LLM_POLICY_PROFILE"] = "autonomous"
        self.assertEqual(load_policy().active_risk_patterns(), [])

    def test_active_risk_patterns_subset(self):
        os.environ["LLM_POLICY_RISK_FILTERS"] = "maintenance,network"
        pats = load_policy().active_risk_patterns()
        self.assertTrue(any("reboot" in p for p in pats))
        self.assertTrue(any("vlan" in p for p in pats))
        self.assertFalse(any("firewall" in p for p in pats))  # security category off

    def test_autoexec_matrix(self):
        RO = {"ping_test"}
        os.environ["LLM_POLICY_PROFILE"] = "autonomous"
        self.assertTrue(load_policy().autoexec_decision("reboot_device", "P3", 0.85, RO))
        os.environ["LLM_POLICY_PROFILE"] = "strict"
        self.assertFalse(load_policy().autoexec_decision("reboot_device", "P3", 0.95, RO))
        os.environ["LLM_POLICY_PROFILE"] = "balanced"
        p = load_policy()
        self.assertTrue(p.autoexec_decision("reboot_device", "P3", 0.95, RO))
        self.assertFalse(p.autoexec_decision("reboot_device", "P1", 0.95, RO))
        self.assertFalse(p.autoexec_decision("reboot_device", "P3", 0.85, RO))
        self.assertTrue(p.autoexec_decision("ping_test", "P3", 0.80, RO))
        self.assertFalse(p.autoexec_decision("ping_test", "P3", 0.79, RO))

    def test_approval_enabled(self):
        os.environ["LLM_POLICY_PROFILE"] = "autonomous"
        self.assertFalse(load_policy().approval_enabled("reboot_device", "P3"))
        os.environ["LLM_POLICY_PROFILE"] = "strict"
        self.assertTrue(load_policy().approval_enabled("reboot_device", "P3"))

    def test_autonomous_read_threshold(self):
        # harmless reads run at modest confidence in autonomous mode
        os.environ["LLM_POLICY_PROFILE"] = "autonomous"
        p = load_policy()
        self.assertEqual(p.read_only_threshold, 0.60)
        self.assertTrue(p.autoexec_decision("unifi_ports", "P3", 0.65, {"unifi_ports"}))
        self.assertFalse(p.autoexec_decision("unifi_ports", "P3", 0.55, {"unifi_ports"}))

    def test_short_circuit_risk_filters_toggle(self):
        text = "vlan config report"  # "report" is known-good; vlan/config are risk words
        self.assertIsNone(short_circuit_verdict(text, "P3"))                 # all filters -> judge
        self.assertIsNone(short_circuit_verdict(text, "P3", "all"))
        v = short_circuit_verdict(text, "P3", "none")                        # no filters -> short-circuit
        self.assertIsNotNone(v)
        self.assertEqual(v.action_class, "network_info")
        v2 = short_circuit_verdict(text, "P3", "maintenance")                 # subset that misses the words
        self.assertIsNotNone(v2)

    def test_env_file_takes_precedence_over_profile(self):
        # The .env file is the source of truth on the VM: an explicit
        # LLM_POLICY_RISK_FILTERS=all must beat the autonomous preset's "none".
        with patch("policy.read_env_file", return_value={"LLM_POLICY_RISK_FILTERS": "all"}):
            os.environ["LLM_POLICY_PROFILE"] = "autonomous"
            p = load_policy()
            self.assertEqual(p.profile, "autonomous")
            self.assertEqual(p.risk_filters, "all")  # explicit override wins

    def test_approval_priorities_env(self):
        os.environ["LLM_POLICY_PROFILE"] = "autonomous"
        os.environ["LLM_POLICY_APPROVAL_PRIORITIES"] = "P3"
        p = load_policy()
        self.assertEqual(p.approval_priorities, ("P3",))


class ModelTierTest(unittest.TestCase):
    def setUp(self):
        self.env_backup = dict(os.environ)
        self._env_patcher = patch("llm_client.read_env_file", return_value={})
        self._env_patcher.start()
        # configure a generic provider (built-in deepseek retired)
        os.environ["LLM_PROVIDER_DEEPSEEKV4_TYPE"] = "openai"
        os.environ["LLM_PROVIDER_DEEPSEEKV4_BASE_URL"] = "https://api.deepseek.com"
        os.environ["LLM_PROVIDER_DEEPSEEKV4_API_KEY"] = "test-key"
        os.environ["LLM_PROVIDER_DEEPSEEKV4_CHAT_MODEL"] = "deepseek-v4-flash"
        os.environ["LLM_PROVIDER_DEEPSEEKV4_REASONER_MODEL"] = "deepseek-reasoner"
        os.environ["LLM_ACTIVE_PROVIDER"] = "deepseekv4"
        os.environ.pop("LLM_PROVIDER_DEEPSEEKV4_JUDGE_MODEL", None)
        llm_client._PROVIDERS_MTIME = None  # force refresh on next get_provider

    def tearDown(self):
        self._env_patcher.stop()
        os.environ.clear()
        os.environ.update(self.env_backup)

    def _adapter_capture(self, provider, model, messages, temperature, max_tokens, timeout):
        self.captured_model = model
        return ('{"action": "ping_test", "target": "switch-01", "params": {}, '
                '"reason": "t", "confidence": 0.95}', 5, 10)

    def test_tier_chat_and_judge_select_different_models(self):
        with patch("llm_client.ADAPTERS", {"openai": self._adapter_capture}):
            llm_client.call_llm("ping x", "P3", model_tier="chat")
            self.assertEqual(self.captured_model, "deepseek-v4-flash")
            llm_client.call_llm("ping x", "P3", model_tier="judge")
            self.assertEqual(self.captured_model, "deepseek-reasoner")  # judge falls back to reasoner

    def test_custom_judge_model_env(self):
        os.environ["LLM_PROVIDER_DEEPSEEKV4_JUDGE_MODEL"] = "deepseek-reasoner-judge"
        with patch("llm_client.ADAPTERS", {"openai": self._adapter_capture}):
            llm_client.call_llm("ping x", "P3", model_tier="judge")
            self.assertEqual(self.captured_model, "deepseek-reasoner-judge")

    def test_judge_model_name_fallback(self):
        self.assertEqual(llm_providers.judge_model_name(
            {"judge_model": "", "reasoner_model": "r", "chat_model": "c"}), "r")
        self.assertEqual(llm_providers.judge_model_name(
            {"judge_model": "", "reasoner_model": "", "chat_model": "c"}), "c")


if __name__ == "__main__":
    unittest.main(verbosity=2)
