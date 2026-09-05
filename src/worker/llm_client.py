import os
import re
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

# F6 cost optimization — rate-window + tier-routing lanes. Imported defensively
# so the module still works in a context without the two new files.
try:
    from ratewindows import cost_optimization_enabled as _cost_opt_enabled
    import tierrouter as _tierrouter
except Exception:  # pragma: no cover - only when the modules are absent
    _cost_opt_enabled = None
    _tierrouter = None


SYSTEM_PROMPT = """You are BareNOC, a network operations assistant for SMB infrastructure.
Your ONLY allowed actions are:
  1. ping_test - Ping a device to verify connectivity
  2. snmp_poll - Poll SNMP for health metrics (CPU, memory, temperature)
  3. device_status - Check device status via API
  4. apply_patch - Apply an approved firmware/software patch
  5. reboot_device - Reboot a device via SSH (immediate)
  6. collect_logs - Collect diagnostic logs from a device
  7. escalate_human - Escalate to a human operator (use when unsure or blocked)
  8. network_discovery - Ping-sweep a subnet to find live hosts (target is a CIDR like 192.0.2.0/24). Use for questions like "what endpoints are responding on 192.168.1.0/24", "which hosts are online on the subnet", or "scan 192.168.1.0/24".
  9. install_chat_client - Install the BareNOC chat client on an onboarded Linux device via SSH (requires approval)
 10. complete_ticket - Close the ticket when the customer confirms the issue is resolved or no action is needed
 11. unifi_port_config - Assign native/tagged VLAN networks to a UniFi switch port (target = the switch MAC, params: {"port_idx": N, "tagged": ["Storage"], "native": "Production"}). This is a WRITE action: whether it runs automatically or waits for human approval is governed by the deployment's Autonomy Policy.
 12. network_info - Read-only: fetch and report the CONFIGURED network/VLAN/SSID layout from the UniFi controller (no target needed). Use for questions like "what vlans are on my network" or "list the subnets" — NOT for "what is responding/reachable on a subnet" (that is network_discovery).
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
 24. unifi_set_ssid_password - WRITE: change a Wi-Fi SSID's passphrase. params: {"ssid": "IoT", "password": "..."} (8-63 chars). Use for "change the wifi password" / "change the IoT SSID passphrase".
 25. unifi_network_create - WRITE: create a new corporate VLAN/subnet on the UniFi controller. params: {"name": "IoT", "vlan": 12, "subnet": "192.168.12.1/24" (optional), "dhcp": true (optional, default true)}. Use for "create a new VLAN" / "add a network for the cameras" / "spin up a 192.168.50.x subnet".
 26. enroll_device - WRITE: adopt a Linux device with a certificate from the internal CA (SSH transport; installs step-cli + a short-lived cert + auto-renewal, then the device links itself over mTLS). Target = the device (IP/name). Use for "adopt the camera" / "enroll this server" / "give the NAS a certificate".
 27. system_time - Read-only: report the appliance's current local time and timezone (no target needed). Use for "what time is it" / "what timezone is this appliance set to".
 28. ticket_status - Read-only: look up a ticket's live status by its TKT-… id (params: {"ticket_id": "TKT-YYYYMMDD-NNNN"}). Use when the user asks about a specific ticket ("status on TKT-…", "where's TKT-… at", "is TKT-… done?"). No target needed.
 29. windows_diag - Read-only: run a Windows PC health report over SSH (disk/volumes + disk-full, top CPU + RAM processes, startup items, Defender real-time status + signature age, recent 7-day critical/error events, boot times, SMART counters where available). Target = the Windows PC (name/IP). Use for "check my PC's health", "why is dad's PC slow", "is the disk full", "run a health check on dads-pc".
 30. windows_cleanup - Safe cleanup on a Windows PC over SSH: stop + remove autostart for known offenders (configurable list — default Adobe CollabSync + Copilot), clear TEMP + empty the recycle bin, and report bytes recovered. NEVER uninstalls software or touches partitions. Target = the Windows PC. Optional params: {"offenders": ["name", ...]}. Use for "clean up my PC", "free up disk space on dads-pc", "stop Copilot/CollabSync autostart".
 31. windows_netdiag - Network/DNS health + hardening on a Windows PC over SSH: report NIC link rate, run latency probes (gateway + public resolvers), and detect the DNS-through-router weak spot (the PC using the router as its DNS server). Read-only by default; with params {"apply_dns_fix": true} it overrides the router-as-resolver with a non-router resolver (default 1.1.1.1 / 1.0.0.1) — but ONLY on an elevated (admin) session; a standard session reports + recommends instead. Optional params: {"apply_dns_fix": bool, "resolvers": ["1.1.1.1", ...]}. Target = the Windows PC. Use for "check my PC's network", "why is the internet slow on dads-pc", "is my PC using the router for DNS", "harden dad's DNS to 1.1.1.1".

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
- **Port actions accept AP names as targets**: for unifi_port_config / unifi_port_bounce / unifi_port_rename you may target an AP by NAME (e.g. "Outdoor AP") — the system automatically resolves it to the AP's uplink switch + port. Do NOT refuse because "I need a switch MAC" — the inventory list is all you need.
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
    # F6: the tier-map class this call belongs to (ticket_judge /
    # ticket_technician / ticket_executor / ticket_title). When provided and
    # cost optimization is enabled, bulk/cheap classes route to the local tier
    # and every real call is metered local-vs-cloud.
    task_class: Optional[str] = None,
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

    # F6 cost optimization: route bulk/cheap classes to the local tier (the
    # M7 Ollama box) and meter every real call local-vs-cloud. Judgment +
    # customer-visible classes stay cloud (the tier_map defaults). When the
    # local box is unreachable, route() already downgraded to cloud, so a
    # local preference here only happens on a healthy box.
    route = None
    prefer_local = False
    if task_class and _cost_opt_enabled is not None and _tierrouter is not None \
            and _cost_opt_enabled():
        try:
            route = _tierrouter.route(task_class)
            prefer_local = bool(route and route.get("tier") == "local")
        except Exception:
            route = None
            prefer_local = False
    if prefer_local:
        local_prov = _tierrouter.local_provider()
        if local_prov:
            local_model = (route.get("local_model") or "").strip() \
                or local_prov.get("chat_model") or ""
            local_entry = dict(local_prov)
            if local_model:
                local_entry["chat_model"] = local_model
                local_entry["reasoner_model"] = local_model
                local_entry["judge_model"] = local_model
            # Local first; the rest of the cloud chain stays as the graceful
            # fallback if the local box fails mid-call.
            chain = [local_entry] + [p for p in chain if p["name"] != local_entry["name"]]
        else:
            prefer_local = False

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
                # The failed parse call DID consume tokens — accumulate the
                # repair call's usage so the metered cost is honest (retries
                # included, each API call counted exactly once).
                print("[LLM] No JSON envelope — one repair attempt")
                try:
                    retry_raw, retry_pt, retry_rt = adapter(
                        provider, model,
                        messages + [{"role": "user", "content":
                            "Your previous reply was not valid JSON. Reply with ONLY the JSON object "
                            "in the exact format specified — no prose, no markdown, no reasoning."}],
                        temperature=0.0, max_tokens=max_tokens, timeout=timeout)
                    prompt_tokens += retry_pt
                    response_tokens += retry_rt
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
                snippet = (raw_text or "").strip()[:300]
                if not snippet:
                    snippet = "(empty response — the model returned no content)"
                parsed = {
                    "action": "escalate_human",
                    "target": "",
                    "params": {},
                    "reason": f"The model returned a non-JSON response: {snippet}",
                    "confidence": 0.80,
                }

            print(f"[LLM] Parsed: action={parsed.get('action')}, target={parsed.get('target')}, confidence={parsed.get('confidence')}")

            inp, out, is_estimate = resolve_prices(provider, model)
            cost = (prompt_tokens / 1_000_000 * inp) + (response_tokens / 1_000_000 * out)

            _note_provider_ok(name)
            # F6: meter the actual tier used (local if the answering provider
            # is on-prem, else cloud). The local-down fallback (routed local,
            # answered cloud) is flagged for the savings KPI.
            if task_class and _cost_opt_enabled is not None and _tierrouter is not None \
                    and _cost_opt_enabled():
                try:
                    used_tier = "local" if (provider.get("deployment") or "hosted").lower() == "on_prem" \
                        else "cloud"
                    _tierrouter.record_call(
                        used_tier, task_class,
                        local_fallback=bool(prefer_local and used_tier == "cloud"))
                except Exception:
                    pass
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


TITLE_SYSTEM_PROMPT = (
    "You title support tickets for a network operations help desk. "
    "Read the customer's request and reply with a concise title of at most "
    "8 words that captures what they need. Reply with ONLY the title — no "
    "quotes, no trailing period, no preamble."
)


# Reasoning models wrap the requested title in thinking blocks; drop those
# whole before trying to find the title text (same idea as _parse_llm_response).
_TITLE_THINK_RE = re.compile(
    r"<(?:think|thinking|analysis|reasoning|response)>"
    r".*?</(?:think|thinking|analysis|reasoning|response)>",
    re.DOTALL | re.IGNORECASE,
)

# A labelled title ("Title:", "Summary for the ticket:", "Here's a
# suggestion:") is meta-text: keep only what follows the LAST label. The value
# may still be quoted/emphasised, so the caller peels it again after
# extraction. Anchored to start-of-text or a separator so a word like
# "subtitle" never matches.
_TITLE_LABEL_RE = re.compile(
    r"(?:^|[\s>\"'`])"
    r"(?:title|subject|summary|suggestion|idea)"
    r"(?:\s+(?:suggestion|idea|option|for\s+(?:the\s+)?(?:ticket|request|issue|report)))?"
    r"\s*[:：\-—]\s*",
    re.IGNORECASE,
)

# Surrounding markdown/quotes/bullets that wrap the title ("**Title**",
# `"Title"`, "- Title", "# Title"). Kept separate from the final punctuation
# trim so a bare "Title:" label keeps its colon long enough to be detected
# (and then discarded as a label with no value).
_TITLE_LEAD_WRAP_RE = re.compile(r"^[\s`*#_>\"'“”‘’\-–—]+")
_TITLE_TAIL_WRAP_RE = re.compile(r"[\s`*#_\"'“”‘’]+$")
_TITLE_TAIL_PUNCT_RE = re.compile(r"[\s`*#_\"'“”‘’.,;:]+$")

# A reply that merely RESTATES the title-gen instruction instead of producing
# the title is the model echoing the system prompt (the 09-02 leaked-title
# report: "We need to generate a title for a support ticket. The request: …",
# "We need to output a title of at most 8 words for the customer request: …").
# Detect and reject it so the caller's first-sentence heuristic fires rather
# than storing the prompt text as the ticket title.
_TITLE_ECHO_ACTION_RE = re.compile(
    r"\b(generate|output|write|create|make|produce)\s+(?:a|an|the)?\s*title\b",
    re.IGNORECASE,
)
_TITLE_ECHO_MARKERS = (
    "title for a support ticket",
    "title for the support ticket",
    "title of at most",
    "concise title",
    "at most 8 words",
    "at most eight words",
    "customer request",
    "customer's request",
    "the request",
    "no preamble",
    "no quotes",
    "trailing period",
    "reply with only",
    "network operations help desk",
)


def _looks_like_title_echo(t: str) -> bool:
    """True when a cleaned title reply is just a restatement of the title-gen
    instruction (an echo of the system prompt), not an actual title."""
    low = (t or "").lower()
    if _TITLE_ECHO_ACTION_RE.search(low):
        return True
    return any(m in low for m in _TITLE_ECHO_MARKERS)


def _clean_title(raw: str, max_chars: int = 80) -> "Optional[str]":
    """Reduce a raw LLM title reply to a short single-line ticket title.

    Models often wrap the requested title in meta-text — a thinking block, a
    preamble ("Sure! Here's a concise title:"), reasoning before a "Title:"
    label, markdown emphasis, or a trailing period. Strip all of that so only
    the actual title lands in the ticket (B3 regression).
    """
    if not raw:
        return None

    # 1. Drop reasoning/thinking blocks whole (their content is prose, not a
    #    title) and collapse to a single line for whitespace-safe parsing.
    t = _TITLE_THINK_RE.sub("", str(raw))
    t = " ".join(t.strip().split())
    if not t:
        return None

    # 2. Peel surrounding markdown/quotes/bullets ("**Title**", "Title",
    #    `Title`, "- Title", "# Title").
    t = _TITLE_LEAD_WRAP_RE.sub("", t)
    t = _TITLE_TAIL_WRAP_RE.sub("", t)
    if not t:
        return None

    # 3. If the model wrote a labelled title anywhere, keep the text after the
    #    LAST label ("The user wants X. Title: Update X" → "Update X"). The
    #    value may itself be quoted/emphasised, so peel it again.
    matches = list(_TITLE_LABEL_RE.finditer(t))
    if matches:
        t = t[matches[-1].end():]
        t = _TITLE_LEAD_WRAP_RE.sub("", t)
        t = _TITLE_TAIL_WRAP_RE.sub("", t)
        t = " ".join(t.split())
    if not t:
        return None

    # 4. Trim trailing punctuation/emphasis left on the final value.
    t = _TITLE_TAIL_PUNCT_RE.sub("", t)
    if not t:
        return None

    # 4b. Reject instruction echoes and multi-sentence restatements: a reply
    #     that repeats the title-gen task (or is still a full sentence) is the
    #     model narrating, not a title — fall through to the heuristic.
    if _looks_like_title_echo(t) or re.search(r"[.!?]\s+\S", t):
        return None

    # 5. Enforce the max length on a word boundary.
    if len(t) > max_chars:
        cut = t[:max_chars]
        boundary = cut.rfind(" ")
        if boundary > max_chars // 2:
            cut = cut[:boundary]
        t = cut.rstrip(" ,.;:")
    return t or None


def generate_title(text: str, timeout: "Optional[int]" = None,
                   max_chars: int = 80) -> "Optional[str]":
    """Best-effort short ticket title via a cheap one-shot LLM call.

    Walks the provider chain once with a tiny prompt + short timeout and
    returns a cleaned title, or None when the whole chain fails. Deliberately
    side-effect-free: it never mutates provider health state (_PROVIDER_DOWN /
    failure counters) and never raises — a title must never block ticket
    creation (the caller falls back to a heuristic).
    """
    if timeout is None:
        try:
            timeout = int(os.getenv("LLM_TITLE_TIMEOUT_S", "8") or 8)
        except ValueError:
            timeout = 8
    try:
        maybe_refresh()
        chain = provider_chain()
    except Exception:
        return None
    if not chain:
        return None

    body = " ".join((text or "").strip().split())[:2000]
    if not body:
        return None
    messages = [
        {"role": "system", "content": TITLE_SYSTEM_PROMPT},
        {"role": "user", "content": body},
    ]
    for provider in chain:
        adapter = ADAPTERS.get((provider.get("type") or "").lower())
        if not adapter or (not provider.get("api_key")
                           and provider.get("deployment") != "on_prem"):
            continue
        model = provider.get("chat_model") or ""
        if not model:
            continue
        try:
            raw, _pt, _rt = adapter(provider, model, messages,
                                    temperature=0.2, max_tokens=24,
                                    timeout=timeout)
            title = _clean_title(raw, max_chars)
            if title:
                # F6: ticket titles are customer-visible -> cloud by default.
                # Meter the call for the local-vs-cloud savings KPI, keyed on
                # the provider that actually answered.
                if _cost_opt_enabled is not None and _tierrouter is not None \
                        and _cost_opt_enabled():
                    try:
                        used_tier = "local" if (provider.get("deployment") or "hosted").lower() == "on_prem" \
                            else "cloud"
                        _tierrouter.record_call(used_tier, "ticket_title")
                    except Exception:
                        pass
                return title
        except Exception:
            # Next provider in the chain (or None → heuristic fallback).
            continue
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
