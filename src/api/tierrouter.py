"""LLM tier router — which work runs WHERE (local M7 Ollama vs cloud DeepSeek).

This lane owns the TIER policy (task class → tier) on the appliance; the
rate-window lane (``ratewindows.py``) owns the TIME policy. The two compose into
one decision:

    WHEN (rate window)  ×  WHERE (tier map)

The model is window-INVERTED, not a static split:

  - **cloud** classes (judgment): the judge's lawfulness ruling, the
    single-phase technician path (judgment when the judge is off), and
    customer-visible copy (ticket titles) → DeepSeek. Never downgraded.
  - **auto** (borderline) classes: the executor (structured job-fill, the
    routine/bulk step) runs CLOUD off-peak (half price + quality) and LOCAL at
    peak (the M7 box is the peak-hour relief valve). A `peak_override` per class
    pins the peak-time choice.
  - **local** (bulk/cheap) classes are available for operators to add in the
    per-box `tier_map.json`; the built-in appliance map keeps judgment +
    customer-visible copy on cloud by default (the safe F6 defaults).

Guardrails built in:

  - **Customer-visible:** a class flagged `customer_visible: true` that routes
    local is returned `draft_flagged: true` — the caller MUST label the output
    "draft — needs review".
  - **Local-down fallback:** `route()` probes the local box and, if it is
    unreachable, downgrades the decision to cloud and flags `local_fallback`.
  - **Local tier = BareNOC's on_prem provider.** The appliance's provider
    registry already models an on-LAN Ollama/LM Studio box
    (``LLM_PROVIDER_<NAME>_DEPLOYMENT=on_prem``); the CC-compatible
    ``LOCAL_LLM_URL`` / ``LOCAL_LLM_MODEL`` env keys still override for
    cross-box parity (shared schema).

Config source (first hit wins):
  1. ``<state dir>/tier_map.json`` (0600, auto-seeded on first use) — edit the
     file to change the policy; no code change needed.
  2. Env keys (fallback): ``TIER_MAP_FILE`` (alternate path).
  3. The built-in DEFAULT_TIER_MAP below.

The cost-stats counter (``<state dir>/llm_cost_stats.json``) records local vs
cloud calls per rate window + an estimated savings figure — the "rides
cost-metering for the proof" lane, surfaced in the reports cost KPI.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.request

try:  # pragma: no cover - depends on importability
    from . import ratewindows as _rw
except ImportError:  # pragma: no cover
    import ratewindows as _rw

#: The seeded tier map. local = M7 Ollama (bulk/cheap), cloud = DeepSeek
#: (judgment), auto = borderline (cloud off-peak / local at peak). Each class
#: carries the LOCAL MODEL + context budget for right-sizing.
DEFAULT_TIER_MAP: dict = {
    "version": 1,
    "updated": "2026-09-03T00:00:00Z",
    "note": (
        "LLM tier map — which work runs where. local = M7 Ollama (bulk/cheap), "
        "cloud = DeepSeek (judgment), auto = borderline (cloud off-peak / local "
        "at peak). peak_override pins a class during peak ('cloud' protects "
        "judgment classes). customer_visible classes route cloud or are flagged "
        "'draft — needs review'."
    ),
    "unknown_class_tier": "cloud",
    "local": {
        "url_env": "LOCAL_LLM_URL",
        "model_env": "LOCAL_LLM_MODEL",
        "default_url": "",
        "default_model": "",
        "timeout_s": 4,
        "probe_path": "/api/tags",
    },
    "cloud": {
        "url_env": "CLOUD_LLM_URL",
        "model_env": "CLOUD_LLM_MODEL",
        "key_env": "LLM_PROVIDER_DEEPSEEKV4_API_KEY",
        "default_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-flash",
    },
    "est_cloud_cost_per_call_usd": 0.002,
    "classes": {
        # ── cloud (judgment) — never downgraded to local ────────────────────
        "ticket_judge": {
            "default_tier": "cloud", "peak_override": None,
            "local_model": None, "context_budget": 8192,
            "customer_visible": False,
            "note": "judgment — lawfulness ruling (never downgraded)",
        },
        "ticket_technician": {
            "default_tier": "cloud", "peak_override": None,
            "local_model": None, "context_budget": 8192,
            "customer_visible": False,
            "note": "single-phase judgment path (judge off) — cloud",
        },
        "ticket_title": {
            "default_tier": "cloud", "peak_override": None,
            "local_model": None, "context_budget": 2048,
            "customer_visible": True,
            "note": "customer-visible ticket title — cloud",
        },
        # ── auto (borderline) — the window-inverted classes ─────────────────
        "ticket_executor": {
            "default_tier": "auto", "peak_override": None,
            "local_model": "qwen2.5:7b", "context_budget": 8192,
            "customer_visible": False,
            "note": "structured job-fill (bulk/cheap) — cloud off-peak, local at peak",
        },
    },
}

#: Local-probe cache: url → (monotonic_ts, healthy). A down box must not force a
#: probe (and its timeout) per call in a tight loop.
_PROBE_CACHE: dict[str, tuple[float, bool]] = {}
_PROBE_TTL_S = 30.0


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _env() -> dict:
    """The appliance .env (file wins) layered under process env (fallback)."""
    env: dict = {}
    try:
        from llm_providers import read_env_file
        env = read_env_file()
    except Exception:
        env = {}
    for k, v in os.environ.items():
        if k not in env:
            env[k] = v
    return env


def _state_dir() -> str:
    return _rw.state_dir()


# ── tier map (configurable — tier_map.json, 0600) ──────────────────────────

def tier_map_path(env: dict | None = None) -> str:
    e = env if env is not None else _env()
    p = (e.get("TIER_MAP_FILE") or "").strip()
    if not p:
        p = os.path.join(_state_dir(), "tier_map.json")
    return p


def _write_private_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def ensure_tier_map(env: dict | None = None) -> str:
    """Seed tier_map.json (0600) if absent. Returns its path."""
    p = tier_map_path(env)
    if not os.path.exists(p):
        seed = json.loads(json.dumps(DEFAULT_TIER_MAP))
        seed["updated"] = _now_iso()
        _write_private_json(p, seed)
    return p


def load_tier_map(env: dict | None = None) -> dict:
    """Read the tier map fresh (an edited file reloads on the next call).

    Falls back to DEFAULT_TIER_MAP when the file is absent or malformed, and
    merges an edited file over the default so a partial edit stays valid.
    """
    raw = None
    try:
        with open(tier_map_path(env), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict) or not isinstance(raw.get("classes"), dict):
        return json.loads(json.dumps(DEFAULT_TIER_MAP))
    out = json.loads(json.dumps(DEFAULT_TIER_MAP))
    out.update({k: v for k, v in raw.items() if k != "classes"})
    out["classes"] = dict(out.get("classes") or {})
    out["classes"].update(raw.get("classes") or {})
    return out


# ── provider endpoints (env-driven; BareNOC registry first) ─────────────────

def local_provider(env: dict | None = None) -> dict | None:
    """The configured on-prem (local) provider from BareNOC's registry.

    BareNOC already models an on-LAN Ollama/LM Studio box as
    ``LLM_PROVIDER_<NAME>_DEPLOYMENT=on_prem``. The first such provider is the
    local tier. Returns None when none is configured.
    """
    e = env if env is not None else _env()
    providers: dict = {}
    try:
        from llm_providers import load_providers as _load_providers
        providers = _load_providers(e)
    except Exception:
        providers = {}
    for p in providers.values():
        if (p.get("deployment") or "hosted").lower() == "on_prem":
            return p
    return None


def local_url(env: dict | None = None) -> str:
    e = env if env is not None else _env()
    v = (e.get("LOCAL_LLM_URL") or "").strip()
    if v:
        return v
    p = local_provider(e)
    return (p or {}).get("base_url", "") or ""


def local_model(env: dict | None = None) -> str:
    e = env if env is not None else _env()
    v = (e.get("LOCAL_LLM_MODEL") or "").strip()
    if v:
        return v
    p = local_provider(e)
    if not p:
        return ""
    return p.get("chat_model") or p.get("reasoner_model") or ""


def cloud_url(env: dict | None = None) -> str:
    e = env if env is not None else _env()
    tm = load_tier_map(e)
    return (e.get("CLOUD_LLM_URL") or "").strip() or tm.get("cloud", {}).get("default_url", "")


def cloud_model(env: dict | None = None) -> str:
    e = env if env is not None else _env()
    tm = load_tier_map(e)
    return (e.get("CLOUD_LLM_MODEL") or "").strip() or tm.get("cloud", {}).get("default_model", "")


def local_healthy(env: dict | None = None, timeout: float | None = None,
                  ttl: float = _PROBE_TTL_S) -> bool:
    """Probe the local Ollama box (LOCAL_LLM_URL / on_prem provider). Cached (TTL).

    Tries the configured probe_path first (Ollama's /api/tags), then the
    OpenAI-compatible /v1/models for LM Studio-style boxes. Any 200 = healthy.
    """
    url = local_url(env).rstrip("/")
    if not url:
        return False
    tm = load_tier_map(env)
    timeout = timeout if timeout is not None else float(tm.get("local", {}).get("timeout_s", 4))
    probe_path = (tm.get("local", {}).get("probe_path") or "/api/tags").strip()
    now = time.monotonic()
    cached = _PROBE_CACHE.get(url)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    ok = False
    for path in (probe_path, "/v1/models"):
        if path != probe_path and ok:
            break
        try:
            req = urllib.request.Request(url + path, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    ok = True
                    break
        except Exception:  # noqa: BLE001 - any probe failure means "local down"
            continue
    _PROBE_CACHE[url] = (now, ok)
    return ok


# ── the routing decision ────────────────────────────────────────────────────

def _result(task_class: str, tier: str, reason: str, peak: bool, rate_state: str,
            rate_factor: float, local_model: str | None, context_budget: int | None,
            customer_visible: bool, draft_flagged: bool, local_fallback: bool = False) -> dict:
    return {
        "class": task_class,
        "tier": tier,
        "reason": reason,
        "peak": peak,
        "rate_state": rate_state,
        "rate_factor": rate_factor,
        "local_model": local_model,
        "context_budget": context_budget,
        "customer_visible": customer_visible,
        "draft_flagged": draft_flagged,
        "local_fallback": local_fallback,
    }


def local_model_for(tm: dict, env: dict | None = None) -> str | None:
    """The default local model from the map, unless env overrides (LOCAL_LLM_MODEL)."""
    e = env if env is not None else {}
    return (e.get("LOCAL_LLM_MODEL") or "").strip() or tm.get("local", {}).get("default_model")


def tier_for(task_class: str, dt_utc: datetime.datetime | None = None,
             env: dict | None = None,
             tier_map: dict | None = None, rate_cfg: dict | None = None) -> dict:
    """The pure routing decision: → "local" | "cloud" + the reason.

    `tier_map` / `rate_cfg` are injectable for tests; otherwise they are loaded
    fresh from disk (no caching — an edited file takes effect next call).
    """
    tm = tier_map if tier_map is not None else load_tier_map(env)
    rc = rate_cfg if rate_cfg is not None else _rw.load_config(env)
    dt = _as_utc(dt_utc) if dt_utc is not None else _now_dt()
    peak = _rw.is_peak(dt, rc)
    state = "PEAK" if peak else "OFF-PEAK"
    factor = _rw.factor_at(dt, rc)

    cls = (tm.get("classes") or {}).get(task_class)
    if cls is None:
        tier = tm.get("unknown_class_tier", "cloud")
        if tier not in ("local", "cloud"):
            tier = "cloud"
        return _result(task_class, tier,
                       f"unknown class '{task_class}' → {tier} (safe default)",
                       peak, state, factor, None, None, False, False)

    default = cls.get("default_tier", "cloud")
    customer_visible = bool(cls.get("customer_visible"))
    local_model = cls.get("local_model") or local_model_for(tm, env)
    context_budget = cls.get("context_budget")

    if default == "local":
        tier, reason = "local", "bulk/cheap class → local (M7 iGPU)"
    elif default == "cloud":
        tier, reason = "cloud", "judgment class → cloud (DeepSeek)"
    elif default == "auto":
        # Borderline: the policy INVERTS by window.
        if peak:
            override = cls.get("peak_override")
            if override in ("local", "cloud"):
                tier, reason = override, f"auto class at peak, peak_override={override} (protected)"
            else:
                tier, reason = "local", "auto class at peak → local (M7 relief valve)"
        else:
            tier, reason = "cloud", "auto class off-peak (half price) → cloud for quality"
    else:
        tier, reason = "cloud", f"bad default_tier '{default}' → cloud (safe)"

    draft_flagged = bool(customer_visible and tier == "local")
    if draft_flagged:
        reason += " — customer-visible → flag 'draft — needs review'"

    return _result(task_class, tier, reason, peak, state, factor,
                   local_model, context_budget, customer_visible, draft_flagged)


def route(task_class: str, dt_utc: datetime.datetime | None = None,
          env: dict | None = None, probe: bool = True,
          timeout: float | None = None,
          tier_map: dict | None = None, rate_cfg: dict | None = None) -> dict:
    """The full routing decision with the graceful local-down fallback.

    `tier_for` is pure; `route()` additionally probes the local box when the
    decision is "local" and falls back to cloud (flagged) if it is down.
    """
    d = tier_for(task_class, dt_utc, env, tier_map, rate_cfg)
    if probe and d["tier"] == "local" and not local_healthy(env, timeout):
        d["tier"] = "cloud"
        d["local_fallback"] = True
        d["reason"] = d["reason"] + " — LOCAL UNREACHABLE → cloud fallback"
    return d


# ── cost-stats counter (llm_cost_stats.json — minimal, prove-savings) ───────

def cost_stats_path(state_dir_: str | None = None) -> str:
    return os.path.join(state_dir_ or _state_dir(), "llm_cost_stats.json")


def _new_stats(state: str) -> dict:
    return {
        "started_at": _now_iso(),
        "window_state": state,
        "calls": {"local": 0, "cloud": 0},
        "by_class": {},
        "local_down_fallbacks": 0,
        "est_savings_usd": 0.0,
        "est_cloud_cost_per_call_usd": DEFAULT_TIER_MAP["est_cloud_cost_per_call_usd"],
        "note": "local-vs-cloud routing counter; est. savings = local calls × est_cloud_cost_per_call_usd",
    }


def read_cost_stats(state_dir_: str | None = None) -> dict:
    try:
        with open(cost_stats_path(state_dir_), encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("calls"), dict):
            raw.setdefault("by_class", {})
            raw.setdefault("local_down_fallbacks", 0)
            raw.setdefault("est_savings_usd", 0.0)
            return raw
    except (OSError, ValueError):
        pass
    return _new_stats("OFF-PEAK")


def _write_cost_stats(state_dir_: str | None, stats: dict) -> None:
    _write_private_json(cost_stats_path(state_dir_), stats)


def record_call(tier: str, task_class: str | None = None,
                env: dict | None = None,
                dt_utc: datetime.datetime | None = None,
                local_fallback: bool = False,
                state_dir_: str | None = None) -> dict:
    """Increment the per-window counter. Resets when the rate window flips.

    Called by a consumer AFTER it actually makes the LLM call, with the tier it
    used. `local_fallback` flags a local→cloud downgrade for the local-down case.
    """
    tier = tier if tier in ("local", "cloud") else "cloud"
    e = env if env is not None else _env()
    tm = load_tier_map(e)
    rc = _rw.load_config(e)
    dt = _as_utc(dt_utc) if dt_utc is not None else _now_dt()
    state = "PEAK" if _rw.is_peak(dt, rc) else "OFF-PEAK"

    stats = read_cost_stats(state_dir_)
    if stats.get("window_state") != state:
        stats = _new_stats(state)
    stats["calls"][tier] = int(stats["calls"].get(tier, 0)) + 1
    if task_class:
        stats["by_class"].setdefault(task_class, {"local": 0, "cloud": 0})
        stats["by_class"][task_class][tier] = int(stats["by_class"][task_class].get(tier, 0)) + 1
    if local_fallback:
        stats["local_down_fallbacks"] = int(stats.get("local_down_fallbacks", 0)) + 1
    per = tm.get("est_cloud_cost_per_call_usd", DEFAULT_TIER_MAP["est_cloud_cost_per_call_usd"])
    stats["est_cloud_cost_per_call_usd"] = per
    stats["est_savings_usd"] = round(int(stats["calls"].get("local", 0)) * per, 6)
    _write_cost_stats(state_dir_, stats)
    return stats


def reset_cost_stats(env: dict | None = None, state_dir_: str | None = None) -> dict:
    rc = _rw.load_config(env)
    state = "PEAK" if _rw.is_peak(_now_dt(), rc) else "OFF-PEAK"
    stats = _new_stats(state)
    _write_cost_stats(state_dir_, stats)
    return stats


def cost_summary(env: dict | None = None, state_dir_: str | None = None) -> dict:
    """Dashboard-safe summary: {calls: {local, cloud}, est_savings_usd, …}."""
    stats = read_cost_stats(state_dir_)
    return {
        "started_at": stats.get("started_at"),
        "window_state": stats.get("window_state"),
        "calls": stats.get("calls", {"local": 0, "cloud": 0}),
        "by_class": stats.get("by_class", {}),
        "local_down_fallbacks": stats.get("local_down_fallbacks", 0),
        "est_savings_usd": stats.get("est_savings_usd", 0.0),
        "local_configured": bool(local_url(env) and local_model(env)),
        "cloud_configured": bool((env or _env()).get("CLOUD_LLM_URL")
                                 or cloud_model(env)),
        "note": stats.get("note", ""),
    }


def all_classes(env: dict | None = None) -> list[str]:
    return sorted((load_tier_map(env).get("classes") or {}).keys())
