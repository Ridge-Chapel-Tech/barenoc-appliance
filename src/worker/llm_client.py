import os
import json
from typing import Optional

from llm_providers import (
    read_env_file,
    load_providers,
    active_provider_name,
    provider_order,
    ADAPTERS,
    resolve_prices,
    judge_model_name,
)


SYSTEM_PROMPT = """You are BareNOC, a network operations assistant for SMB infrastructure.
Your ONLY allowed actions are:
  1. ping_test - Ping a device to verify connectivity
  2. snmp_poll - Poll SNMP for health metrics (CPU, memory, temperature)
  3. device_status - Check device status via API
  4. apply_patch - Apply an approved firmware/software patch
  5. reboot_device - Reboot a device via SSH (immediate)
  6. collect_logs - Collect diagnostic logs from a device
  7. escalate_human - Escalate to a human operator (use when unsure or blocked)
  8. network_discovery - Ping-sweep a subnet to find live hosts (target is a CIDR like 192.0.2.0/24)
  9. install_chat_client - Install the BareNOC chat client on an onboarded Linux device via SSH (requires approval)
 10. complete_ticket - Close the ticket when the customer confirms the issue is resolved or no action is needed
 11. unifi_port_config - Assign native/tagged VLAN networks to a UniFi switch port (target = the switch MAC, params: {"port_idx": N, "tagged": ["Storage"], "native": "Production"}). This is a WRITE action: whether it runs automatically or waits for human approval is governed by the deployment's Autonomy Policy.
 12. network_info - Read-only: fetch and report the network/VLAN/SSID configuration from the UniFi controller (no target needed). Use for questions like "what vlans are on my network" or "list the subnets".
 13. request_customer_input - Ask the CUSTOMER for missing information/clarification; sets the ticket to Customer Action. Use when the ticket can't proceed without details only the customer can provide.
 14. unifi_clients - Read-only: list known + active clients from UniFi (who is online, what IPs are in use). No target needed. Optional params: {"online": true} (online now only), {"wired": true} (wired only) / {"wired": false} (wireless only) — combinable.
 15. unifi_devices - Read-only: list managed UniFi devices with health/uptime/version (how is the network gear doing). No target needed. Optional params (combinable): {"device_type": "ap" | "switch" | "gateway"}, {"status": "online" | "offline"} — e.g. "list my wireless APs" -> {"device_type": "ap"}; "which APs are offline" -> {"device_type": "ap", "status": "offline"}.
 16. unifi_ports - Read-only: port table (state/PoE/native+tagged VLANs) for a UniFi switch. Target = the switch MAC.
 17. unifi_client_port - Read-only: which switch port a wired client is plugged into. Target = the client IP.
 18. unifi_firewall_rules - Read-only: list the custom firewall rules from the UniFi controller (what is blocking X). No target needed.
 19. unifi_restart - WRITE: reboot a UniFi-managed device (AP/switch/gateway) via the controller. Target = the device MAC. Approval/autonomy governed by the Autonomy Policy.
 20. unifi_port_bounce - WRITE: cycle a UniFi switch port (drop the link / power-cycle PoE). Target = the switch MAC, params: {"port_idx": N}.
 21. unifi_port_rename - WRITE: rename a UniFi switch port. Target = the switch MAC, params: {"port_idx": N, "name": "..."}.
 22. batch - WRITE: run a list of sub-actions in ONE ticket. params: {"jobs": [{"action": "<catalog action>", "target": "...", "params": {...}}, ...]} (max 50). Use for multi-device / multi-port requests like "tag all APs with the Storage VLAN", "bounce every port on the switch", or "rename these ports".
 23. unifi_ensure_wireless_uplinks - WRITE: ensure every ENABLED wireless SSID VLAN is available on every AP's uplink port (native or tagged), preserving other port settings and exclusions. Use for "make all wireless vlans available to all APs" / "all wireless networks should reach the APs". No target needed.
 24. unifi_set_ssid_password - WRITE: change a Wi-Fi SSID's passphrase. params: {"ssid": "Kids", "password": "..."} (8-63 chars). Use for "change the wifi password" / "change the Kids SSID passphrase".
 25. unifi_network_create - WRITE: create a new corporate VLAN/subnet on the UniFi controller. params: {"name": "IoT", "vlan": 12, "subnet": "192.168.12.1/24" (optional), "dhcp": true (optional, default true)}. Use for "create a new VLAN" / "add a network for the cameras" / "spin up a 192.168.50.x subnet".
 26. enroll_device - WRITE: adopt a Linux device with a certificate from the internal CA (SSH transport; installs step-cli + a short-lived cert + auto-renewal, then the device links itself over mTLS). Target = the device (IP/name). Use for "adopt the camera" / "enroll this server" / "give the NAS a certificate".
 27. system_time - Read-only: report the appliance's current local time and timezone (no target needed). Use for "what time is it" / "what timezone is this appliance set to".

You are Lily, the BareNOC network operations assistant. Read the ticket, judge whether the request is legal and doable
with the allowed actions. If it is NOT legal/doable or needs a human decision, use
escalate_human. If it IS doable, pick the best action and proceed. When the customer has
confirmed a resolution (e.g. "that fixed it", "works now"), use complete_ticket.

You must respond with ONLY a valid JSON object in this exact format:
{
  "action": "<action_name>",
  "target": "<device_hostname_or_ip>",
  "params": { },
  "reason": "<brief explanation>",
  "confidence": <0.0 to 1.0>
}

RULES:
- Set confidence low (< 0.80) if you're unsure about the action
- For P1/P2 tickets, you may auto-execute if confidence >= 0.95
- Never suggest actions outside the allowed list above
- If the request is unclear or suspicious, use escalate_human
- Never attempt to create or modify user accounts, firewall rules, or security policies
- Never execute raw commands or scripts
- target must be a specific managed device, EXCEPT for network_discovery where the target is a subnet CIDR (e.g. 192.0.2.0/24)
- **Filter reads to what the user asked for**: only Access Points/APs (or wireless gear) -> unifi_devices {"device_type": "ap"}; only switches -> {"device_type": "switch"}; only the gateway -> {"device_type": "gateway"}; online/offline -> add {"status": "online"|"offline"} ("which APs are offline" = both). "who is online" -> unifi_clients {"online": true}; wired-only -> {"wired": true}; wireless-only -> {"wired": false}. Do NOT return the whole fleet when a subset was requested.
- **Port actions accept AP names as targets**: for unifi_port_config / unifi_port_bounce / unifi_port_rename you may target an AP by NAME (e.g. "U7 Outdoor") — the system automatically resolves it to the AP's uplink switch + port. Do NOT refuse because "I need a switch MAC" — the inventory list is all you need.
- **Always target the device NAME, never the IP** (the system resolves names; IPs are less reliable).
- **Multi-device requests use batch**: if the request spans several devices/ports ("tag all APs", "both APs", "every port"), output action "batch" with params {"jobs": [...]}, one sub-job per device — never escalate a multi-device request that batch can handle.
- Respond with ONLY the JSON object: no prose, no preamble, no code fences
"""


class LLMResponse:
    def __init__(self, action: str, target: str, params: dict, reason: str,
                 confidence: float, raw_text: str, model: str,
                 prompt_tokens: int, response_tokens: int, cost_usd: float,
                 cost_estimate: bool = False):
        self.action = action
        self.target = target
        self.params = params
        self.reason = reason
        self.confidence = confidence
        self.raw_text = raw_text
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.response_tokens = response_tokens
        self.cost_usd = cost_usd
        self.cost_estimate = cost_estimate

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "target": self.target,
            "params": self.params,
            "reason": self.reason,
            "confidence": self.confidence,
        }


# ── provider registry (hot-reloadable) ──────────────────────────────────────

PROVIDERS: dict = {}
ACTIVE_PROVIDER: str = ""
_PROVIDERS_MTIME = None


def refresh_providers() -> None:
    """Reload the provider registry from the .env file (and env fallback)."""
    global PROVIDERS, ACTIVE_PROVIDER
    env = read_env_file()
    PROVIDERS = load_providers(env)
    ACTIVE_PROVIDER = active_provider_name(env)


def maybe_refresh() -> None:
    """Reload providers only if the .env file changed (cheap mtime check)."""
    global _PROVIDERS_MTIME
    try:
        mtime = os.path.getmtime("/opt/barenoc/.env")
    except Exception:
        mtime = 0
    if mtime != _PROVIDERS_MTIME:
        refresh_providers()
        _PROVIDERS_MTIME = mtime


def get_provider(name: Optional[str] = None) -> Optional[dict]:
    maybe_refresh()
    key = (name or ACTIVE_PROVIDER).lower()
    if key in PROVIDERS:
        return PROVIDERS[key]
    if key and key in load_providers():
        return load_providers()[key]
    # fall back to the first configured provider (legacy built-in deepseek retired)
    return next(iter(PROVIDERS.values()), None)


def _parse_llm_response(raw_text: str) -> Optional[dict]:
    """Extract JSON from LLM response (handles thinking blocks and markdown code fences)."""
    text = raw_text.strip()

    # Strip any  .../think blocks (DeepSeek reasoner / thinking models)
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```", "", text)

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find any JSON object in the text
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        candidate = text[brace_start:brace_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None


# ── provider chain (failover) ──────────────────────────────────────────────
# Consecutive failures per provider; a provider is skipped after N consecutive
# failures (marked down) so the chain settles on the next healthy one, and is
# retried only when it is the last option left (wrap-around recovery).
_PROVIDER_FAILURES: dict = {}
_PROVIDER_DOWN: set = set()


def _provider_down_after() -> int:
    try:
        return max(2, int(os.getenv("LLM_PROVIDER_DOWN_AFTER", "3") or 3))
    except ValueError:
        return 3


def _note_provider_ok(name: str) -> None:
    _PROVIDER_FAILURES.pop(name, None)
    _PROVIDER_DOWN.discard(name)


def _note_provider_fail(name: str) -> None:
    n = _PROVIDER_FAILURES.get(name, 0) + 1
    _PROVIDER_FAILURES[name] = n
    if n >= _provider_down_after():
        _PROVIDER_DOWN.add(name)


def provider_chain(preferred: Optional[str] = None) -> list:
    """Ordered provider dicts for failover (preferred first when given)."""
    maybe_refresh()
    order = provider_order(read_env_file())
    if preferred:
        p = str(preferred).strip().lower()
        if p in order:
            order = [p] + [n for n in order if n != p]
        elif p in PROVIDERS:
            order = [p] + [n for n in order if n != p]
    return [PROVIDERS[n] for n in order if n in PROVIDERS]


def _tier_model(provider: dict, model_tier: str, use_reasoner: bool) -> str:
    """Resolve the model for this call tier (judge > reasoner > chat)."""
    if model_tier == "judge":
        return judge_model_name(provider)
    if model_tier == "reasoner" or use_reasoner:
        return provider.get("reasoner_model") or provider.get("chat_model") or ""
    return provider.get("chat_model") or ""


def call_llm(
    ticket_text: str,
    priority: str,
    device_context: Optional[str] = None,
    use_reasoner: bool = False,
    timeout: Optional[int] = None,
    provider_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    model_tier: str = "chat",
    # The catalog path only ever needs the short JSON envelope (action/target/
    # params/reason/confidence) — cap it so slow on-LAN fallbacks (CPU Ollama)
    # can complete within the call timeout. Judge/executor pass explicit values.
    max_tokens: int = 150,
    temperature: float = 0.1,
    extra_context: Optional[str] = None,
) -> Optional[LLMResponse]:
    """
    Call the LLM provider chain (primary → secondary → tertiary) with ticket
    content, failing over automatically.

    Failover triggers: any exception (timeout, HTTP error, connection error)
    or a provider marked down after LLM_PROVIDER_DOWN_AFTER consecutive
    failures. The first provider that answers wins. Returns None only when the
    WHOLE chain is unusable (the worker retries on schedule and opens an
    outage ticket).

    model_tier selects the model for this call:
      "chat"     -> chat_model        (fast executor / routine work)
      "reasoner" -> reasoner_model    (deep thinking; also use_reasoner=True)
      "judge"    -> judge_model       (falls back to reasoner, then chat)

    system_prompt overrides the default AI-TECHNICIAN prompt (used by the
    judge/executor split). Returns parsed LLMResponse or None on failure.
    """
    if timeout is None:
        try:
            timeout = int(os.getenv("LLM_TIMEOUT_S", "30") or 30)
        except ValueError:
            timeout = 30

    chain = provider_chain(provider_name)
    if not chain:
        print("[LLM] No provider configured")
        return None

    # Dev mode: every configured provider has no API key and isn't a keyless
    # on-prem endpoint -> mock
    if all(not p.get("api_key") and p.get("deployment") != "on_prem" for p in chain):
        return _mock_llm_call(ticket_text, priority)

    prompt = system_prompt if system_prompt is not None else SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Priority: {priority}\n\nTicket: {ticket_text}"},
    ]
    if device_context:
        messages.append({"role": "user", "content": f"Device context: {device_context}"})
    if extra_context:
        messages.append({"role": "user", "content": extra_context})

    tried = []
    last_err = "no provider attempted"
    for provider in chain:
        name = provider["name"]
        adapter = ADAPTERS.get((provider.get("type") or "").lower())
        if not adapter or (not provider.get("api_key")
                           and provider.get("deployment") != "on_prem"):
            continue
        model = _tier_model(provider, model_tier, use_reasoner)
        if not model:
            print(f"[LLM] {name}: no model configured — skipping")
            continue
        if name in _PROVIDER_DOWN:
            print(f"[LLM] {name} marked down ({_PROVIDER_FAILURES.get(name, 0)} consecutive failures) — skipping")
            tried.append(f"{name}(down)")
            continue
        try:
            raw_text, prompt_tokens, response_tokens = adapter(
                provider, model, messages, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout)

            print(f"[LLM] Raw response ({name}/{model}): {raw_text[:300]}...")

            parsed = _parse_llm_response(raw_text)
            if not parsed:
                # One repair attempt: thinking models sometimes reply with a
                # chain of thought and no JSON. Nudge once for the envelope.
                print("[LLM] No JSON envelope — one repair attempt")
                try:
                    retry_raw, _, _ = adapter(
                        provider, model,
                        messages + [{"role": "user", "content":
                            "Your previous reply was not valid JSON. Reply with ONLY the JSON object "
                            "in the exact format specified — no prose, no markdown, no reasoning."}],
                        temperature=0.0, max_tokens=max_tokens, timeout=timeout)
                    print(f"[LLM] Repair response: {retry_raw[:300]}...")
                    repaired = _parse_llm_response(retry_raw)
                    if repaired:
                        raw_text = retry_raw
                        parsed = repaired
                except Exception:
                    pass

            if not parsed:
                # Model returned prose instead of the JSON envelope. Don't kill
                # the ticket — escalate to the human approval queue, preserving
                # the response. (The provider DID respond — not a failover case.)
                print("[LLM] No JSON envelope after repair; escalating with raw text")
                parsed = {
                    "action": "escalate_human",
                    "target": "",
                    "params": {},
                    "reason": f"The model returned a non-JSON response: {raw_text[:300]}",
                    "confidence": 0.80,
                }

            print(f"[LLM] Parsed: action={parsed.get('action')}, target={parsed.get('target')}, confidence={parsed.get('confidence')}")

            inp, out, is_estimate = resolve_prices(provider, model)
            cost = (prompt_tokens / 1_000_000 * inp) + (response_tokens / 1_000_000 * out)

            _note_provider_ok(name)
            return LLMResponse(
                action=parsed.get("action", "escalate_human"),
                target=parsed.get("target", ""),
                params=parsed.get("params", {}),
                reason=parsed.get("reason", ""),
                confidence=parsed.get("confidence", 0.5),
                raw_text=raw_text,
                model=f"{name}/{model}",
                prompt_tokens=prompt_tokens,
                response_tokens=response_tokens,
                cost_usd=round(cost, 6),
                cost_estimate=is_estimate,
            )
        except Exception as e:
            _note_provider_fail(name)
            last_err = f"{name}: {e}"
            tried.append(name)
            print(f"[LLM] provider {name} failed ({e}) — trying next in chain")

    print(f"[LLM] ALL providers failed ({', '.join(tried)}) — {last_err}")
    return None


def _mock_llm_call(ticket_text: str, priority: str) -> LLMResponse:
    """Mock LLM for development/testing without API key."""
    print(f"[MOCK LLM] Processing: {ticket_text[:60]}...")

    ticket_lower = ticket_text.lower()
    confidence = 0.95 if priority in ("P1", "P2") else 0.85

    # Simple intent matching for mock
    if "ping" in ticket_lower or "connectivity" in ticket_lower:
        action = "ping_test"
        target = "192.0.2.1"
        params = {"count": 4}
        reason = "Testing connectivity as requested"
    elif "snmp" in ticket_lower or "health" in ticket_lower or "status" in ticket_lower:
        action = "snmp_poll"
        target = "switch-01"
        params = {}
        reason = "Checking device health metrics"
    elif "patch" in ticket_lower or "update" in ticket_lower or "firmware" in ticket_lower:
        action = "apply_patch"
        target = "switch-01"
        params = {"patch_id": "FW-6.6.55"}
        reason = "Applying recommended firmware update"
    elif "reboot" in ticket_lower or "restart" in ticket_lower:
        action = "reboot_device"
        target = "switch-01"
        params = {}
        reason = "Rebooting device via SSH"
    elif "log" in ticket_lower:
        action = "collect_logs"
        target = "switch-01"
        params = {"lines": 100}
        reason = "Collecting diagnostic logs"
    else:
        action = "escalate_human"
        target = ""
        params = {}
        confidence = 0.60
        reason = "Request unclear, escalating to human operator"

    return LLMResponse(
        action=action,
        target=target,
        params=params,
        reason=reason,
        confidence=confidence,
        raw_text=json.dumps({"action": action, "target": target, "params": params,
                            "reason": reason, "confidence": confidence}),
        model="mock",
        prompt_tokens=len(ticket_text) // 4,
        response_tokens=100,
        cost_usd=0.0,
    )
