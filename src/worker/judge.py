"""Judge — the lawfulness gatekeeper of the two-phase AI pipeline.

The Judge reads a (sanitized) ticket and returns a VERDICT:

    {
      "lawful":       "yes" | "no" | "ambiguous",
      "action_class": "<one allowed action, or null>",
      "risk":         "low" | "medium" | "high",
      "scope":        "managed" | "external" | "policy" | "unknown",
      "checks":       {"legal": bool, "doable": bool, "safe": bool, "in_scope": bool},
      "reason":       "<plain-language justification>"
    }

The Judge NEVER executes anything — it only decides policy. The Executor
(executor.py) turns an approved verdict into a concrete job, which then still
passes through the code-level action_validator + confidence gates. That keeps
the hard guarantees in immutable code and the judgment in the model.

Pipeline is opt-in: LLM_JUDGE_ENABLED=true (default false -> single-phase,
current behavior).

Cost controls:
  * short-circuit: known-good read-only patterns on managed inventory get a
    rule-based verdict with NO LLM call at all.
  * verdict cache: sha256(request) -> verdict, JSON file with TTL
    (LLM_VERDICT_CACHE_TTL_H, default 24h).

Env knobs:
  LLM_JUDGE_ENABLED
  LLM_PROVIDER_<NAME>_JUDGE_MODEL   (defaults to reasoner model)
  LLM_VERDICT_CACHE_FILE            (default /opt/barenoc/volumes/db/llm_verdicts_cache.json)
  LLM_VERDICT_CACHE_TTL_H           (default 24)
"""

import os
import re
import json
import time
import hashlib
from dataclasses import dataclass, field, asdict

from llm_client import get_provider
from action_validator import find_subnet

# Catalog the judge may pick from — mirror of action_validator.AllowedAction.
# (unifi_port_config lives only in the chat-client path today; see action_validator.)
ACTION_CATALOG = [
    "ping_test", "snmp_poll", "device_status", "apply_patch", "reboot_device",
    "collect_logs", "network_discovery", "network_info", "system_time",
    "ticket_status",
    "unifi_clients", "unifi_devices", "unifi_ports", "unifi_port_config",
    "unifi_client_port", "unifi_firewall_rules", "unifi_restart",
    "unifi_port_bounce", "unifi_port_rename", "unifi_ensure_wireless_uplinks", "batch",
    "unifi_set_ssid_password",
    "unifi_network_create",
    "enroll_device",
    "fingerprint_device", "install_chat_client", "complete_ticket",
    "request_customer_input", "escalate_human",
    "windows_diag", "windows_cleanup", "windows_netdiag",
]

JUDGE_SYSTEM_PROMPT = """You are the JUDGE in a two-phase network-operations pipeline.

A customer/operator request has arrived. Your ONLY job is to decide whether it is
LAWFUL to attempt — policy judgment, nothing else. You never execute anything.

Judge four questions:
  1. legal      — Is there an allowed action that could fulfill this? (catalog below)
  2. doable     — Is the target a managed device we hold, or at least a plausible
                  on-network target? (hostname/IP/subnet)
  3. safe       — Would executing this risk an outage, security breach, or damage?
                  (e.g. rebooting a gateway mid-day, touching firewalls, deleting data)
  4. in_scope   — Is this about THIS customer's network, not some external/unrelated system?

Rules:
- "no"  when the request is illegal, out of scope, unsafe, or impossible
        (e.g. security-policy changes, firewall edits, accounts, external targets,
        reboot of core network gear without a maintenance window).
- "ambiguous" when you cannot decide, info is missing, or it needs a human's call.
- "yes" only when all four checks pass cleanly.
- Never pick an action outside the catalog.
- For "yes", name exactly ONE action_class that fits best.

Respond with ONLY a JSON object, no prose:
{
  "lawful": "yes|no|ambiguous",
  "action_class": "<catalog action or null>",
  "risk": "low|medium|high",
  "scope": "managed|external|policy|unknown",
  "checks": {"legal": true, "doable": true, "safe": true, "in_scope": true},
  "reason": "<2-3 sentence justification>"
}

ACTION CATALOG (pick only from these):
""" + ", ".join(ACTION_CATALOG)


# ── short-circuit (rule-based, free) ────────────────────────────────────────

# Risk categories (policy.py) — any hit forces a real judge call.
# "all" = every category active; "none" = never force on keywords.
_RISK_PATTERNS = None  # resolved from policy; fallback below

# Fallback when no policy is in play (legacy / bare calls): all categories on.
from policy import ALL_RISK_PATTERNS as _ALL_RISK_PATTERNS


def _active_risk_patterns(risk_filters: "str | None") -> list:
    """Resolve which risk regexes are active for this call."""
    if risk_filters is None:
        return list(_ALL_RISK_PATTERNS)
    rf = str(risk_filters).strip().lower()
    if rf == "none":
        return []
    if rf in ("", "all", "*"):
        return list(_ALL_RISK_PATTERNS)
    out = []
    from policy import RISK_CATEGORIES
    for cat in (c.strip() for c in rf.split(",") if c.strip()):
        out.extend(RISK_CATEGORIES.get(cat, []))
    return out

# Known-good read-only patterns -> immediate low-risk verdict, no LLM call.
_KNOWN_GOOD_PATTERNS = [
    # a bare ticket reference in the Lily/ticket pipeline is a status/summary
    # request — answer read-only from the ticket's derived status (bug #16).
    # Placed FIRST: it is the most specific signal and must win over the
    # generic "status" -> device_status pattern below.
    (re.compile(r"\bTKT-\d{8}-\d{4}\b", re.I), "ticket_status"),
    (re.compile(r"\bwho('s| is)? online\b|\b(connected|active) clients?\b", re.I), "unifi_clients"),
    (re.compile(r"\bswitch ports?\b|\bport table\b", re.I), "unifi_ports"),
    (re.compile(r"\bping\b", re.I), "ping_test"),
    (re.compile(r"\b(online|offline|up|down|status|reachable|connectivity|healthy)\b", re.I), "device_status"),
    (re.compile(r"\b(vlan|subnet|ssid|networks?|ip ?addresses?)\b", re.I), "network_info"),
]

# "what endpoints are responding on 192.168.1.0/24" is a subnet ping-sweep
# (network_discovery), NOT a network/VLAN summary — the word "network" used to
# match the network_info pattern above and return the VLAN/SSID table instead
# of the live-host sweep the customer asked for.
_ENDPOINT_SCAN_RE = re.compile(
    r"\b(endpoints?|hosts?|devices?|machines?|clients?)\b[^.!?\n]{0,120}"
    r"\b(respond(?:ing|s)?|reachable|online|alive|up|answer(?:ing|s)?|active|live)\b",
    re.I)


@dataclass
class Verdict:
    lawful: str                 # "yes" | "no" | "ambiguous"
    action_class: str = ""
    risk: str = "unknown"
    scope: str = "unknown"
    checks: dict = field(default_factory=dict)
    reason: str = ""
    model: str = ""
    prompt_tokens: int = 0
    response_tokens: int = 0
    cost_usd: float = 0.0
    cost_estimate: bool = False
    cached: bool = False
    short_circuit: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def short_circuit_verdict(text: str, priority: str,
                          risk_filters: "str | None" = None) -> "Verdict | None":
    """Rule-based verdict for obviously-safe requests. None => must call the judge.
    risk_filters: policy selector for which risk words force a judge call."""
    if not text:
        return None
    t = text.lower()
    for pat in _active_risk_patterns(risk_filters):
        if re.search(pat, t):
            return None  # write/risky — always go to the judge
    # Endpoint scan over a named subnet: the customer asked which endpoints/
    # hosts respond on a concrete CIDR — that is a subnet ping-sweep, and it
    # must win over the generic "network" -> network_info pattern below.
    if _ENDPOINT_SCAN_RE.search(t) and find_subnet(t):
        return Verdict(
            lawful="yes",
            action_class="network_discovery",
            risk="low",
            scope="managed",
            checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
            reason="Short-circuit: endpoint scan over a subnet -> network_discovery ping sweep.",
            short_circuit=True,
        )
    for pat, action in _KNOWN_GOOD_PATTERNS:
        if pat.search(t):
            return Verdict(
                lawful="yes",
                action_class=action,
                risk="low",
                scope="managed",
                checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                reason="Short-circuit: matches a known-good read-only pattern; no judge call needed.",
                short_circuit=True,
            )
    return None


# ── verdict cache ───────────────────────────────────────────────────────────

DEFAULT_CACHE_FILE = "/opt/barenoc/volumes/db/llm_verdicts_cache.json"
DEFAULT_CACHE_TTL = 24 * 3600


def _cache_path() -> str:
    return os.getenv("LLM_VERDICT_CACHE_FILE", DEFAULT_CACHE_FILE)


def _cache_ttl() -> int:
    try:
        return int(os.getenv("LLM_VERDICT_CACHE_TTL_H", "24")) * 3600
    except ValueError:
        return DEFAULT_CACHE_TTL


def _cache_key(priority: str, ticket_text: str) -> str:
    return hashlib.sha256(f"{priority}|{ticket_text}".encode()).hexdigest()


def _cache_load() -> dict:
    try:
        with open(_cache_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_save(data: dict) -> None:
    try:
        with open(_cache_path(), "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _cache_get(key: str) -> "Verdict | None":
    cache = _cache_load()
    entry = cache.get(key)
    if not entry:
        return None
    if time.time() - entry.get("_ts", 0) > _cache_ttl():
        return None
    v = Verdict(**{k: v for k, v in entry.items() if k != "_ts"})
    v.cached = True
    return v


def _cache_set(key: str, verdict: Verdict) -> None:
    cache = _cache_load()
    entry = verdict.to_dict()
    entry["_ts"] = time.time()
    cache[key] = entry
    _cache_save(cache)


# ── parsing ─────────────────────────────────────────────────────────────────

def _parse_verdict(raw_text: str) -> "dict | None":
    """Extract the verdict JSON (handles think blocks + code fences)."""
    text = (raw_text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.replace("```", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _coerce_verdict(raw: dict) -> "Verdict | None":
    """Validate + coerce a parsed verdict dict into a Verdict."""
    if not isinstance(raw, dict):
        return None
    lawful = str(raw.get("lawful", "")).strip().lower()
    if lawful not in ("yes", "no", "ambiguous"):
        return None
    ac = (raw.get("action_class") or "").strip().lower()
    if lawful == "yes" and ac not in ACTION_CATALOG:
        return None  # judge named an action that doesn't exist — don't trust it
    risk = str(raw.get("risk", "unknown")).lower()
    if risk not in ("low", "medium", "high", "unknown"):
        risk = "unknown"
    checks = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    return Verdict(
        lawful=lawful,
        action_class=ac,
        risk=risk,
        scope=str(raw.get("scope", "unknown")),
        checks={k: bool(v) for k, v in checks.items() if k in
                ("legal", "doable", "safe", "in_scope")},
        reason=str(raw.get("reason", "")).strip(),
    )


# ── mock judge (no API key / dev) ───────────────────────────────────────────

def _mock_judge(ticket_text: str, priority: str) -> Verdict:
    """Deterministic keyword judge for development + tests."""
    t = ticket_text.lower()
    if any(w in t for w in ("firewall", "delete", "block ", "account", "password")):
        return Verdict(lawful="no", action_class="", risk="high", scope="policy",
                       checks={"legal": False, "doable": True, "safe": False, "in_scope": False},
                       reason="Mock judge: security/policy change — requires a human technician.",
                       model="mock")
    if any(w in t for w in ("reboot", "restart", "patch", "upgrade")):
        return Verdict(lawful="ambiguous", action_class="", risk="high", scope="managed",
                       checks={"legal": True, "doable": True, "safe": False, "in_scope": True},
                       reason="Mock judge: write action — needs a maintenance window + human approval.",
                       model="mock")
    if re.search(r"\bTKT-\d{8}-\d{4}\b", t):
        return Verdict(lawful="yes", action_class="ticket_status", risk="low", scope="managed",
                       checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                       reason="Mock judge: ticket status lookup.",
                       model="mock")
    if any(w in t for w in ("ping", "status", "online", "report", "vlan", "network", "snmp")):
        return Verdict(lawful="yes", action_class="device_status", risk="low", scope="managed",
                       checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                       reason="Mock judge: routine read-only request.",
                       model="mock")
    return Verdict(lawful="ambiguous", action_class="", risk="unknown", scope="unknown",
                   checks={}, reason="Mock judge: cannot classify — escalate for human review.",
                   model="mock")


# ── entry point ─────────────────────────────────────────────────────────────

def judge_request(ticket_text: str, priority: str,
                  device_context: "str | None" = None,
                  provider_name: "str | None" = None,
                  risk_filters: "str | None" = None) -> Verdict:
    """Judge a ticket: cache -> short-circuit -> LLM (or mock). Never executes.
    risk_filters: policy-driven risk-word selector ("all"/"none"/categories)."""
    key = _cache_key(priority, ticket_text)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    sc = short_circuit_verdict(ticket_text, priority, risk_filters=risk_filters)
    if sc is not None:
        _cache_set(key, sc)
        return sc

    provider = get_provider(provider_name)
    if provider is None or not provider.get("api_key"):
        v = _mock_judge(ticket_text, priority)
        _cache_set(key, v)
        return v

    from llm_client import call_llm
    resp = call_llm(
        ticket_text=ticket_text,
        priority=priority,
        device_context=device_context,
        provider_name=provider_name,
        system_prompt=JUDGE_SYSTEM_PROMPT,
        model_tier="judge",
        max_tokens=600,
        temperature=0.0,
        task_class="ticket_judge",
    )
    if resp is None:
        return Verdict(lawful="ambiguous", action_class="", risk="unknown", scope="unknown",
                       checks={}, reason="Judge model call failed; escalate for human review.")

    parsed = _parse_verdict(resp.raw_text)
    if parsed is None:
        v = Verdict(lawful="ambiguous", action_class="", risk="unknown", scope="unknown",
                    checks={},
                    reason=f"Judge returned an unparseable verdict: {resp.raw_text[:200]}",
                    model=resp.model, prompt_tokens=resp.prompt_tokens,
                    response_tokens=resp.response_tokens, cost_usd=resp.cost_usd,
                    cost_estimate=resp.cost_estimate)
        _cache_set(key, v)
        return v

    v = _coerce_verdict(parsed)
    if v is None:
        v = Verdict(lawful="ambiguous", action_class="", risk="unknown", scope="unknown",
                    checks={},
                    reason=f"Judge verdict failed schema validation: {resp.raw_text[:200]}",
                    model=resp.model, prompt_tokens=resp.prompt_tokens,
                    response_tokens=resp.response_tokens, cost_usd=resp.cost_usd,
                    cost_estimate=resp.cost_estimate)
    else:
        v.model = resp.model
        v.prompt_tokens = resp.prompt_tokens
        v.response_tokens = resp.response_tokens
        v.cost_usd = resp.cost_usd
        v.cost_estimate = resp.cost_estimate
    _cache_set(key, v)
    return v
