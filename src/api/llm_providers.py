"""LLM provider abstraction — registry, adapters, live/static pricing.

Shared by:
- worker/llm_client.py (ticket processing)
- api/routes/settings.py (provider config + test-connection endpoint)

Provider config sources (fresh file first, env fallback):
  LLM_PROVIDER_ORDER=<name>,<name>,<name>   (failover chain: primary, secondary, tertiary)
  LLM_ACTIVE_PROVIDER=<name>          (alias of the primary — first in order; legacy)
  LLM_PROVIDER_<NAME>_TYPE=openai|anthropic|gemini
  LLM_PROVIDER_<NAME>_BASE_URL=
  LLM_PROVIDER_<NAME>_API_KEY=
  LLM_PROVIDER_<NAME>_DEPLOYMENT=hosted|on_prem   (Ollama/LM Studio LAN endpoints = on_prem)
  LLM_PROVIDER_<NAME>_CHAT_MODEL=
  LLM_PROVIDER_<NAME>_REASONER_MODEL=
  LLM_PROVIDER_<NAME>_JUDGE_MODEL=    (defaults to REASONER_MODEL; judge/executor split)
  LLM_PROVIDER_<NAME>_THINKING=      auto|disabled|enabled (reasoning models: disable for
                                      fast JSON executor calls; enabled = big token budget)
  LLM_PROVIDER_<NAME>_INPUT_PRICE=   (USD per 1M input tokens, static mode)
"""

import os
import time
import json
from typing import Optional

import httpx

ENV_FILE = "/opt/barenoc/.env"
PRICE_CACHE_FILE = "/opt/barenoc/volumes/db/llm_prices_cache.json"
PRICE_CACHE_TTL = int(os.getenv("LLM_PRICE_CACHE_TTL_H", "24")) * 3600

# Home default: cloud LLM (best quality). The express wizard's "Cloud
# (recommended)" card selects the first HOSTED provider in the chain; a fresh
# install ships DeepSeek as that provider (see .env.example). "Local only"
# maps to the SAME LLM_EGRESS control the compliance panel toggles.
HOME_CLOUD_PROVIDER = "deepseekv4"

# Maintained static price table (USD per 1M tokens) — updated on releases.
# Used for providers without a public pricing API (DeepSeek, Anthropic, Gemini, ...).
DEFAULT_PRICE_TABLE = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-opus-4-20250514": {"input": 15.00, "output": 75.00},
    "claude-haiku-4-20250514": {"input": 1.00, "output": 5.00},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}


# ── config loading ──────────────────────────────────────────────────────────

def read_env_file() -> dict:
    """Read the .env file into a dict (fresh source of truth for config)."""
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def _get(env: dict, key: str, default: str = "") -> str:
    return env.get(key, os.getenv(key, default))


def _f(env: dict, key: str) -> float:
    try:
        return float(_get(env, key, "0") or 0)
    except ValueError:
        return 0.0


def load_providers(env: Optional[dict] = None) -> dict:
    """Build the provider registry from env (file first, process env fallback).
    Providers come from LLM_PROVIDER_<NAME>_TYPE blocks ONLY in the env FILE —
    never from the container's process env. (env_file: injects .env at container
    start, so scanning os.environ would keep deleted providers alive until the
    container is recreated.) The process env is only consulted when the file
    itself has no providers (dev/hermetic runs)."""
    env = env if env is not None else read_env_file()
    providers: dict = {}

    candidates = {k for k in env.keys()
                  if k.startswith("LLM_PROVIDER_") and k.endswith("_TYPE")}
    if not candidates:
        candidates = {k for k in os.environ.keys()
                      if k.startswith("LLM_PROVIDER_") and k.endswith("_TYPE")}
    seen = set()
    for k in sorted(candidates):
        name = k[len("LLM_PROVIDER_"):-len("_TYPE")].lower()
        if name in seen:
            continue
        prefix = f"LLM_PROVIDER_{name.upper()}"
        chat = _get(env, f"{prefix}_CHAT_MODEL", "")
        reasoner = _get(env, f"{prefix}_REASONER_MODEL", "") or chat
        providers[name] = {
            "name": name,
            "type": _get(env, f"{prefix}_TYPE", "openai").lower(),
            "base_url": _get(env, f"{prefix}_BASE_URL", ""),
            "api_key": _get(env, f"{prefix}_API_KEY", ""),
            "deployment": (_get(env, f"{prefix}_DEPLOYMENT", "hosted") or "hosted").lower(),
            "chat_model": chat,
            "reasoner_model": reasoner,
            "judge_model": _get(env, f"{prefix}_JUDGE_MODEL", "") or reasoner,
            "thinking": _get(env, f"{prefix}_THINKING", "auto"),
            "input_price": _f(env, f"{prefix}_INPUT_PRICE"),
            "output_price": _f(env, f"{prefix}_OUTPUT_PRICE"),
            "price_mode": _get(env, f"{prefix}_PRICE_MODE", "auto").lower(),
        }
        seen.add(name)
    return providers


def egress_mode(env: Optional[dict] = None) -> str:
    """Compliance LLM-egress control: 'cloud' (default) or 'local'.

    'local' = local-only: the worker chain, pi provider secret and wizard
    must only ever use on-prem (Ollama/LM Studio) endpoints. The one real
    data-flow blocker in the honest-egress map (tickets/chat/network
    summaries transit a hosted LLM by default).
    """
    env = env if env is not None else read_env_file()
    v = _get(env, "LLM_EGRESS", "cloud").strip().lower()
    return "local" if v in ("local", "local-only", "on_prem") else "cloud"


def home_cloud_provider(env: Optional[dict] = None) -> str:
    """The express wizard's cloud default: the first HOSTED provider in the
    failover chain (deepseekv4 on a fresh install). Never returns an on-prem
    endpoint — 'Local only' is the compliance LLM_EGRESS path, not this."""
    env = env if env is not None else read_env_file()
    providers = load_providers(env)
    for name in provider_order(env):
        p = providers.get(name)
        if p and (p.get("deployment") or "hosted") != "on_prem":
            return name
    return HOME_CLOUD_PROVIDER


def _egress_allowed(provider: dict, mode: str) -> bool:
    if mode == "local":
        return (provider.get("deployment") or "hosted") == "on_prem"
    return True


def effective_providers(env: Optional[dict] = None) -> dict:
    """The provider registry after the compliance egress filter.

    In local-only mode this drops every hosted provider, so any consumer that
    walks this dict (or provider_order below) can never attempt a hosted call.
    """
    env = env if env is not None else read_env_file()
    providers = load_providers(env)
    mode = egress_mode(env)
    if mode == "local":
        return {k: v for k, v in providers.items() if _egress_allowed(v, mode)}
    return providers


def active_provider_name(env: Optional[dict] = None) -> str:
    """The configured active provider, or the first configured provider."""
    env = env if env is not None else read_env_file()
    providers = effective_providers(env)
    name = _get(env, "LLM_ACTIVE_PROVIDER", "").strip().lower()
    if name and name in providers:
        return name
    return next(iter(providers), "")


def _placeholder_base_url(base_url: str) -> bool:
    """True when a base URL is an unfilled placeholder — e.g. the OLLAMA
    default ``http://192.168.x.x:11434`` or ``http://your-host:11434``. Such a
    provider must never enter the failover chain (the call would only fail /
    time out on an unreachable host)."""
    b = (base_url or "").strip().lower()
    if not b:
        return False
    return any(m in b for m in (
        "x.x", "example", "placeholder", "your-", "changeme", "change-me",
        "change_me", "<", ">",
    ))


def _usable_providers(providers: dict) -> dict:
    """Drop providers whose base URL is an unfilled placeholder so they are
    never attempted by the failover chain."""
    return {n: p for n, p in providers.items()
            if not _placeholder_base_url(p.get("base_url", ""))}


def provider_order(env: Optional[dict] = None) -> list:
    """The failover chain as an ordered list of provider NAMES.

    Source: LLM_PROVIDER_ORDER (comma-separated; primary first). Unknown or
    unconfigured names are dropped; duplicates collapsed; providers with an
    unfilled placeholder base URL are dropped (never attempted). In local-only
    egress mode, hosted providers are filtered out entirely (see
    effective_providers) — the chain never contains a cloud provider.
    """
    env = env if env is not None else read_env_file()
    providers = _usable_providers(effective_providers(env))
    raw = _get(env, "LLM_PROVIDER_ORDER", "").strip()
    names = [n.strip().lower() for n in raw.split(",") if n.strip()] if raw else []
    seen = []
    for n in names:
        if n in providers and n not in seen:
            seen.append(n)
    if seen:
        return seen
    active = active_provider_name(env)
    if active in providers:
        return [active]
    return list(providers.keys())[:3]


# ── adapters ────────────────────────────────────────────────────────────────

def _adapter_openai(provider: dict, model: str, messages: list,
                    temperature: float, max_tokens: int, timeout: int) -> tuple:
    url = f"{provider['base_url'].rstrip('/')}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if str(provider.get("thinking", "auto")).lower() == "disabled":
        # DeepSeek reasoning models: skip the chain-of-thought for fast
        # executor calls — content comes back immediately as the JSON envelope.
        body["thinking"] = {"type": "disabled"}
    elif str(provider.get("thinking", "auto")).lower() == "enabled":
        body["thinking"] = {"type": "enabled"}
    resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]
    # DeepSeek reasoning models put the answer in `reasoning_content` when
    # `content` comes back empty under a long system prompt (found 08-17 —
    # every chat/ticket request escalated as a "non-JSON (empty response)").
    text = msg.get("content") or msg.get("reasoning_content") or ""
    usage = data.get("usage", {})
    return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def _adapter_anthropic(provider: dict, model: str, messages: list,
                       temperature: float, max_tokens: int, timeout: int) -> tuple:
    url = f"{provider['base_url'].rstrip('/')}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": provider.get("api_key", ""),
        "anthropic-version": "2023-06-01",
    }
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("role") != "system"]
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature}
    if system_parts:
        body["system"] = "\n".join(system_parts)
    resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage", {})
    return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


def _adapter_gemini(provider: dict, model: str, messages: list,
                    temperature: float, max_tokens: int, timeout: int) -> tuple:
    url = f"{provider['base_url'].rstrip('/')}/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": provider.get("api_key", "")}
    contents = []
    for m in messages:
        if m.get("role") == "system":
            # Fold system prompt into the first user turn
            if contents:
                contents[0]["parts"].insert(0, {"text": m["content"]})
            else:
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    body = {
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):
        text = ""
    um = data.get("usageMetadata", {})
    return text, int(um.get("promptTokenCount", 0)), int(um.get("candidatesTokenCount", 0))


ADAPTERS = {
    "openai": _adapter_openai,
    "anthropic": _adapter_anthropic,
    "gemini": _adapter_gemini,
}


# ── pricing ─────────────────────────────────────────────────────────────────

def _live_price_source(base_url: str) -> Optional[str]:
    b = (base_url or "").lower()
    if "openrouter" in b:
        return "openrouter"
    if "together" in b:
        return "together"
    return None


def _fetch_live_prices(source: str) -> dict:
    """Fetch {model: {input, output}} per-1M-token pricing. {} on failure."""
    try:
        if source == "openrouter":
            r = httpx.get("https://openrouter.ai/api/v1/models", timeout=15)
            r.raise_for_status()
            out = {}
            for m in r.json().get("data", []):
                p = m.get("pricing", {})
                out[m.get("id")] = {
                    "input": round(float(p.get("prompt", 0) or 0) * 1e6, 4),
                    "output": round(float(p.get("completion", 0) or 0) * 1e6, 4),
                }
            return out
        if source == "together":
            r = httpx.get("https://api.together.xyz/v1/models", timeout=15)
            r.raise_for_status()
            out = {}
            for m in r.json().get("data", []):
                p = m.get("pricing", {})
                out[m.get("id")] = {
                    "input": round(float(p.get("prompt", 0) or 0), 4),
                    "output": round(float(p.get("completion", 0) or 0), 4),
                }
            return out
    except Exception:
        return {}
    return {}


def _load_price_cache() -> dict:
    try:
        with open(PRICE_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_price_cache(data: dict) -> None:
    try:
        with open(PRICE_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def judge_model_name(provider: dict) -> str:
    """Resolve the judge-tier model for a provider (judge -> reasoner -> chat)."""
    for k in ("judge_model", "reasoner_model", "chat_model"):
        v = (provider or {}).get(k)
        if v:
            return v
    return ""


# Documented blended fallback for HOSTED providers whose model is absent from
# DEFAULT_PRICE_TABLE and has no explicit LLM_PROVIDER_*_INPUT/OUTPUT_PRICE.
# Marked as an estimate in every consumer (audit + KPI + tickets) — never a
# silent $0.00. (on-prem endpoints price at $0 — local inference has no
# per-token API spend — and are NOT flagged as estimates.)
FALLBACK_ESTIMATE_PRICE = {"input": 3.0, "output": 15.0}


def resolve_prices(provider: dict, model: str) -> tuple:
    """Return (input_per_1m, output_per_1m, is_estimate).

    `is_estimate` is True ONLY when the price is unknown and a documented
    blended fallback is used (unknown hosted model) — a static-table or env
    price is a KNOWN number, so metered cost stays metered.
    """
    mode = (provider.get("price_mode") or "auto").lower()
    if mode == "zero":
        return 0.0, 0.0, False

    source = _live_price_source(provider.get("base_url"))
    if source and mode in ("auto", "live"):
        cache = _load_price_cache()
        live = cache.get(source, {})
        fresh = bool(cache.get("_fetched")) and (time.time() - cache["_fetched"]) < PRICE_CACHE_TTL
        if not fresh:
            fetched = _fetch_live_prices(source)
            if fetched:
                cache[source] = fetched
                cache["_fetched"] = time.time()
                _save_price_cache(cache)
                live = fetched
        if model in live:
            return live[model]["input"], live[model]["output"], False
        # mode == "live" and unavailable: fall through to the static table
        # (still priced + marked estimate) instead of returning a silent 0.

    table = DEFAULT_PRICE_TABLE.get(model, {})
    inp = float(provider.get("input_price") or 0) or table.get("input", 0.0)
    out = float(provider.get("output_price") or 0) or table.get("output", 0.0)
    if inp or out:
        # Known price (explicit env override or the maintained static table) —
        # the cost is metered, not estimated.
        return inp, out, False
    # Unknown model, no explicit price: local inference is free; a hosted
    # endpoint gets the documented blended estimate so the KPI never shows a
    # silent $0.00 for an unknown hosted model.
    if (provider.get("deployment") or "hosted").lower() == "on_prem":
        return 0.0, 0.0, False
    return FALLBACK_ESTIMATE_PRICE["input"], FALLBACK_ESTIMATE_PRICE["output"], True


def cost_for_tokens(provider: dict, model: str, input_tokens: int,
                    output_tokens: int) -> tuple:
    """Return (cost_usd, is_estimate) for a known token count.

    cost = (input_tokens / 1M × input_price) + (output_tokens / 1M × output_price),
    rounded to 6 decimals (sub-cent LLM spend stays visible).
    """
    inp, out, is_estimate = resolve_prices(provider, model)
    cost = (int(input_tokens or 0) / 1_000_000 * inp) + \
           (int(output_tokens or 0) / 1_000_000 * out)
    return round(cost, 6), is_estimate


def provider_for_model(model: str) -> Optional[dict]:
    """Find a configured provider whose chat/reasoner/judge model == model.

    Used to price the pi-agent path (the runner reports the model it ran; the
    provider block owns the price). Returns a minimal hosted provider when the
    model isn't in the registry so DEFAULT_PRICE_TABLE / the blended fallback
    still apply.
    """
    if not model:
        return None
    for p in load_providers().values():
        for k in ("chat_model", "reasoner_model", "judge_model"):
            if (p.get(k) or "").lower() == str(model).lower():
                return p
    return {"name": "unknown", "type": "openai", "base_url": "",
            "deployment": "hosted", "input_price": 0.0, "output_price": 0.0,
            "price_mode": "auto"}


def resolve_model_cost(model: str, input_tokens: int, output_tokens: int) -> tuple:
    """(cost_usd, is_estimate) for a model name, priced via the registry."""
    provider = provider_for_model(model) or {
        "name": "unknown", "type": "openai", "base_url": "",
        "deployment": "hosted", "input_price": 0.0, "output_price": 0.0,
        "price_mode": "auto"}
    return cost_for_tokens(provider, model, input_tokens, output_tokens)


# ── probe (settings test-connection) ────────────────────────────────────────

def probe_provider(provider: dict, timeout: int = 20) -> dict:
    """Fire a tiny probe at a provider. Returns {ok, latency_ms, model, text, error}."""
    adapter = ADAPTERS.get((provider.get("type") or "").lower())
    if not adapter:
        return {"ok": False, "error": f"Unknown adapter type: {provider.get('type')}"}
    if not provider.get("api_key") and provider.get("deployment") != "on_prem":
        return {"ok": False, "error": "No API key configured"}
    model = provider.get("chat_model") or provider.get("reasoner_model") or ""
    if not model:
        return {"ok": False, "error": "No chat model configured"}
    messages = [
        {"role": "system", "content": "You are a connectivity probe. Reply with exactly: ok"},
        {"role": "user", "content": "ping"},
    ]
    # on-prem boxes (Ollama/LM Studio) cold-start slowly — give them room.
    if (provider.get("deployment") or "").lower() == "on_prem":
        timeout = max(timeout, 120)
    start = time.time()
    try:
        text, pt, rt = adapter(provider, model, messages, temperature=0.0, max_tokens=10, timeout=timeout)
        ms = round((time.time() - start) * 1000)
        return {"ok": True, "latency_ms": ms, "model": model,
                "text": (text or "").strip()[:50], "prompt_tokens": pt, "response_tokens": rt}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
