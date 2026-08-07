"""Executor — turns a judge-approved verdict into ONE concrete job.

Second phase of the two-phase pipeline. The Judge (judge.py) has already ruled
on lawfulness; the Executor's only job is to fill in the concrete
{action, target, params, reason, confidence} for the approved action_class.

The Executor's output is NOT trusted by itself — it still passes through
action_validator (enum + managed target + param schema) and the confidence
gates in worker/main.py. Capability = fast chat model; authority stays in code.
"""

import json

from judge import ACTION_CATALOG, Verdict
from llm_client import get_provider


def build_executor_prompt(verdict: Verdict) -> str:
    """System prompt for the executor, scoped to the judge-approved action."""
    checks = verdict.checks or {}
    checks_line = ", ".join(f"{k}={str(bool(v)).lower()}" for k, v in checks.items()) or "n/a"
    return f"""You are the EXECUTOR in a two-phase network-operations pipeline.

A JUDGE has already ruled on this request. You do NOT judge; you only produce
the concrete job for the ONE action the judge approved.

JUDGE VERDICT:
  lawful:        {verdict.lawful}
  approved action: {verdict.action_class or "escalate_human"}
  risk:          {verdict.risk}
  scope:         {verdict.scope}
  checks:        {checks_line}
  judge reason:  {verdict.reason}

RULES:
- You may ONLY output the approved action '{verdict.action_class or "escalate_human"}'.
- If that action cannot fulfill the request, output "escalate_human" and say why.
- target must be a specific managed device (hostname or IP), EXCEPT
  network_discovery (subnet CIDR) and logical actions (no target).
- **UniFi port actions (unifi_port_config / unifi_port_bounce / unifi_port_rename) accept an AP NAME as target** — the system resolves it to the AP's uplink switch + port automatically. Never refuse for want of a MAC.
- **Multi-device requests use batch** (action "batch", params {{"jobs": [...]}}, one sub-job per device/port) — never escalate something batch can handle.
- Fill params minimally and validly (e.g. apply_patch
  needs patch_id from the approved list, unifi_port_config needs port_idx+tagged).
- Set confidence honestly: high (>=0.95) only when you are certain the concrete
  job is exactly right; >=0.80 when reasonable; lower otherwise.

ALLOWED ACTIONS (catalog the judge chose from):
{json.dumps(ACTION_CATALOG)}

Respond with ONLY a JSON object:
{{
  "action": "<approved action>",
  "target": "<hostname_or_ip>",
  "params": {{}},
  "reason": "<brief explanation>",
  "confidence": <0.0 to 1.0>
}}
"""


def _mock_executor(ticket_text: str, priority: str, verdict: Verdict):
    """Deterministic executor for dev/tests — constrained to the verdict."""
    from llm_client import LLMResponse

    action = verdict.action_class or "escalate_human"
    t = ticket_text.lower()
    confidence = 0.95 if priority in ("P1", "P2") else 0.90
    target, params, reason = "", {}, f"Mock executor for approved action {action}"

    if action == "escalate_human":
        target, params, reason = "", {}, "No action approved; escalating to human review."
        confidence = 0.99
    elif action == "ping_test":
        target = "192.0.2.1" if "gateway" in t else "switch-01"
        params, reason = {"count": 4}, "Pinging target to verify connectivity"
    elif action == "device_status":
        target = "switch-01"
        params, reason = {}, "Checking device status"
    elif action == "network_info":
        params, reason = {}, "Fetching network/VLAN/SSID configuration"
    elif action == "unifi_clients":
        params, reason = {}, "Listing known + active clients from UniFi"
    elif action == "unifi_devices":
        params = {"device_type": "ap"} if "ap" in t or "wireless" in t else {}
        reason = "Listing UniFi device health/uptime" + (" (APs only)" if params else "")
    elif action == "unifi_ports":
        target = "switch-01"
        params, reason = {}, "Reading switch port table from UniFi"
    elif action == "unifi_port_config":
        target = "switch-01"
        params, reason = {"port_idx": 7, "tagged": ["Storage"], "native": "Production"}, \
                         "Assigning tagged/native VLANs to the switch port"
    elif action == "unifi_client_port":
        target = "192.0.2.50"
        params, reason = {}, "Looking up the switch port for the client"
    elif action == "unifi_firewall_rules":
        params, reason = {}, "Listing custom firewall rules from UniFi"
    elif action == "unifi_restart":
        target = "switch-01"
        params, reason = {}, "Restarting the UniFi device via the controller"
    elif action == "unifi_port_bounce":
        target = "switch-01"
        params, reason = {"port_idx": 5}, "Cycling the switch port"
    elif action == "unifi_port_rename":
        target = "switch-01"
        params, reason = {"port_idx": 5, "name": "uplink-camera"}, "Renaming the switch port"
    elif action == "unifi_ensure_wireless_uplinks":
        params, reason = {}, "Ensuring all wireless SSID VLANs are available on every AP uplink"
    elif action == "unifi_set_ssid_password":
        params, reason = {"ssid": "Kids", "password": "newpass1234"}, "Changing the SSID passphrase"
    elif action == "unifi_network_create":
        params, reason = {"name": "IoT", "vlan": 12}, "Creating the new VLAN network"
    elif action == "enroll_device":
        params, reason = {}, "Adopting the device with a certificate (step-ca)"
    elif action == "batch":
        target = ""
        params, reason = {"jobs": [
            {"action": "unifi_port_bounce", "target": "switch-01", "params": {"port_idx": 1}},
            {"action": "unifi_port_bounce", "target": "switch-01", "params": {"port_idx": 2}},
        ]}, "Batching multiple sub-actions"
    elif action == "snmp_poll":
        target = "switch-01"
        params, reason = {}, "Polling SNMP health metrics"
    elif action == "reboot_device":
        target = "switch-01"
        params, reason = {}, "Rebooting device via SSH"
    elif action == "apply_patch":
        target = "switch-01"
        params, reason = {"patch_id": "FW-6.6.55"}, "Applying approved patch"

    return LLMResponse(
        action=action, target=target, params=params, reason=reason,
        confidence=confidence,
        raw_text=json.dumps({"action": action, "target": target, "params": params,
                             "reason": reason, "confidence": confidence}),
        model="mock-executor", prompt_tokens=len(ticket_text) // 4,
        response_tokens=100, cost_usd=0.0,
    )


def call_executor(ticket_text: str, priority: str,
                  device_context: "str | None" = None,
                  verdict: "Verdict | None" = None,
                  provider_name: "str | None" = None):
    """Run the executor phase. Returns an LLMResponse (or mock when no key)."""
    if verdict is None or verdict.lawful != "yes":
        from llm_client import LLMResponse
        return LLMResponse(
            action="escalate_human", target="", params={},
            reason="No lawful verdict to execute; escalating.",
            confidence=0.99,
            raw_text='{"action": "escalate_human", "target": "", "params": {}, "reason": "No lawful verdict", "confidence": 0.99}',
            model="executor", prompt_tokens=0, response_tokens=0, cost_usd=0.0,
        )

    provider = get_provider(provider_name)
    if provider is None or not provider.get("api_key"):
        return _mock_executor(ticket_text, priority, verdict)

    from llm_client import call_llm
    return call_llm(
        ticket_text=ticket_text,
        priority=priority,
        device_context=device_context,
        provider_name=provider_name,
        system_prompt=build_executor_prompt(verdict),
        model_tier="chat",   # executor = fast model; judgment already done
        max_tokens=800,
        temperature=0.1,
    )
