"""Rate-window scheduler — the appliance's provider-rate awareness (F6).

The Command Center's primary LLM (DeepSeek) prices by time-of-day; off-peak is
HALF price. This module is the single source of truth for that schedule on the
appliance and drives the "bias non-urgent LLM work into off-peak windows" half
of the cost-optimization feature. The "route bulk/cheap work to the local tier"
half lives in ``tierrouter.py``.

Config source (first hit wins):
  1. ``<state dir>/rate_windows.json`` — per-box config (multi-tenant): edit
     this file to change the rate structure, no code change needed. The state
     dir defaults to ``/opt/barenoc/volumes/db`` (the shared api/worker/scheduler
     volume) and can be overridden with ``LLM_COST_STATE_DIR``.
  2. Env keys (fallback): ``RATE_PEAK_WINDOWS`` (JSON list), ``RATE_OFF_PEAK_FACTOR``,
     ``RATE_PROVIDER``, ``RATE_WINDOWS_FILE`` (alternate config path).
  3. The built-in DEFAULT_CONFIG — the CC's current structure (shared schema):
     peak = Mon–Fri 01:00–04:00 UTC + 06:00–10:00 UTC; off-peak = everything
     else at ``off_peak_factor`` 0.5 (half of peak).

Feature toggles (per-box, multi-tenant):
  ``LLM_COST_OPTIMIZATION``  true|false (default true) — master switch.
  ``LLM_OFFPEAK_DEFER``      true|false (default true) — whether non-urgent
                             (P3/P4) tickets wait for the next off-peak window.

Every scheduling predicate is PURE + unit-testable: it takes an optional config
dict; only ``load_config`` / ``ensure_config`` / ``current_state`` and the queue
helpers touch the filesystem. A datetime is treated as UTC (naive datetimes are
assumed UTC) — the same convention the CC uses.
"""
from __future__ import annotations

import datetime
import json
import os

#: The seeded default — DeepSeek's current peak structure (UTC), shared with
#: the Command Center's state/rate_windows.json schema.
DEFAULT_CONFIG: dict = {
    "provider": "deepseek",
    "updated": "2026-09-01T00:00:00Z",
    "off_peak_factor": 0.5,
    "peak_windows": [
        {"days": "mon-fri", "start": 1, "end": 4},
        {"days": "mon-fri", "start": 6, "end": 10},
    ],
}

_DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_DAY_LABELS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: How far forward/back we scan for a window boundary (minute resolution).
_SCAN_MINUTES = 8 * 24 * 60  # 8 days — more than any weekly pattern's period


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _minutes(dt: datetime.datetime) -> int:
    return dt.hour * 60 + dt.minute


# ── env loading (file first, process env fallback — the appliance pattern) ──

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


def state_dir() -> str:
    """The per-box state directory (config + queue + cost stats)."""
    return os.getenv("LLM_COST_STATE_DIR", "/opt/barenoc/volumes/db")


def _rate_path(env: dict | None = None) -> str:
    e = env if env is not None else _env()
    return (e.get("RATE_WINDOWS_FILE") or "").strip() or \
        os.path.join(state_dir(), "rate_windows.json")


# ── feature toggles ─────────────────────────────────────────────────────────

def cost_optimization_enabled(env: dict | None = None) -> bool:
    """Master switch for F6. Default ON — the feature ships active; a box can
    opt out with ``LLM_COST_OPTIMIZATION=false``."""
    e = env if env is not None else _env()
    v = (e.get("LLM_COST_OPTIMIZATION") or "").strip().lower()
    if v == "":
        return True
    return v in ("1", "true", "yes", "on")


def offpeak_defer_enabled(env: dict | None = None) -> bool:
    """Whether non-urgent tickets wait for the next off-peak window (default ON)."""
    e = env if env is not None else _env()
    v = (e.get("LLM_OFFPEAK_DEFER") or "").strip().lower()
    if v == "":
        return True
    return v in ("1", "true", "yes", "on")


# ── config parsing / validation ─────────────────────────────────────────────

def parse_days(days) -> frozenset | None:
    """Normalize a `days` value → a frozenset of weekday ints (0=Mon..6=Sun).

    Accepts: "mon-fri" / "weekdays" / "weekend" / "all"; a comma list of day
    names; an int bitmask (bit 0 = Monday … bit 6 = Sunday); or a list of day
    names/ints. Returns None when the value can't be parsed.
    """
    if isinstance(days, bool):
        return None
    if isinstance(days, str):
        s = days.strip().lower()
        if s in ("mon-fri", "weekdays", "weekday", "monday-friday"):
            return frozenset(range(0, 5))
        if s in ("weekend", "weekends"):
            return frozenset({5, 6})
        if s in ("all", "everyday", "daily"):
            return frozenset(range(7))
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if parts:
            out = {_DAY_NAMES[p] for p in parts if p in _DAY_NAMES}
            return frozenset(out) if out else None
        return None
    if isinstance(days, int):
        if not (0 <= days <= 0b1111111):
            return None
        return frozenset(i for i in range(7) if days & (1 << i))
    if isinstance(days, (list, tuple, frozenset, set)):
        out: set = set()
        for d in days:
            if isinstance(d, int) and 0 <= d <= 6:
                out.add(d)
            elif isinstance(d, str) and d.strip().lower() in _DAY_NAMES:
                out.add(_DAY_NAMES[d.strip().lower()])
            else:
                return None
        return frozenset(out) if out else None
    return None


def _parse_window(w) -> dict | None:
    """Validate one peak-window entry → {days, start, end}. None if invalid."""
    if not isinstance(w, dict):
        return None
    days = parse_days(w.get("days"))
    if not days:
        return None
    try:
        start = int(w.get("start"))
        end = int(w.get("end"))
    except (TypeError, ValueError):
        return None
    if not (0 <= start < end <= 24):
        return None
    return {"days": days, "start": start, "end": end}


def normalize_config(raw) -> dict:
    """Validate/normalize a raw config dict → the internal canonical form.

    Never raises: bad fields fall back to the default. ``peak_windows`` become
    {days: frozenset, start, end}; ``off_peak_factor`` is coerced to (0, 1].
    """
    raw = raw if isinstance(raw, dict) else {}
    windows: list[dict] = []
    for w in (raw.get("peak_windows") or []):
        pw = _parse_window(w)
        if pw:
            windows.append(pw)
    try:
        factor = float(raw.get("off_peak_factor", 0.5))
    except (TypeError, ValueError):
        factor = 0.5
    if not (0 < factor <= 1):
        factor = 0.5
    return {
        "provider": str(raw.get("provider") or "deepseek").strip() or "deepseek",
        "updated": str(raw.get("updated") or "").strip(),
        "off_peak_factor": factor,
        "peak_windows": windows,
    }


def _config_from_env(env: dict) -> dict:
    """Build a raw config from env keys (the no-file fallback path)."""
    raw = json.loads(json.dumps(DEFAULT_CONFIG))
    pw = (env or {}).get("RATE_PEAK_WINDOWS")
    if pw:
        try:
            parsed = json.loads(pw)
            if isinstance(parsed, list):
                raw["peak_windows"] = parsed
        except ValueError:
            pass
    f = (env or {}).get("RATE_OFF_PEAK_FACTOR")
    if f:
        try:
            raw["off_peak_factor"] = float(f)
        except ValueError:
            pass
    if (env or {}).get("RATE_PROVIDER"):
        raw["provider"] = (env or {})["RATE_PROVIDER"]
    return raw


def load_config(env: dict | None = None) -> dict:
    """Read the config fresh (no caching — an edited file reloads next call).

    Precedence: rate_windows.json → env keys → DEFAULT_CONFIG.
    """
    e = env if env is not None else _env()
    raw = None
    try:
        with open(_rate_path(e), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    if raw is None:
        raw = _config_from_env(e)
    return normalize_config(raw)


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


def ensure_config(env: dict | None = None) -> str:
    """Seed rate_windows.json (0600) if absent. Returns its path.

    Written from the default (merged with env overrides) so the owner/agents
    have a real file to edit when the rate structure changes.
    """
    e = env if env is not None else _env()
    p = _rate_path(e)
    if not os.path.exists(p):
        raw = _config_from_env(e)
        raw["updated"] = raw.get("updated") or _now_iso()
        _write_private_json(p, raw)
    return p


# ── the pure scheduling predicates ──────────────────────────────────────────

def is_peak(dt_utc, cfg=None) -> bool:
    """True when `dt_utc` falls inside any peak window."""
    c = normalize_config(cfg if cfg is not None else DEFAULT_CONFIG)
    dt = _as_utc(dt_utc)
    wd = dt.weekday()
    m = _minutes(dt)
    for w in c["peak_windows"]:
        if wd in w["days"] and w["start"] * 60 <= m < w["end"] * 60:
            return True
    return False


def is_off_peak(dt_utc, cfg=None) -> bool:
    """True when `dt_utc` is NOT inside a peak window (off-peak = half price)."""
    return not is_peak(dt_utc, cfg)


def factor_at(dt_utc, cfg=None) -> float:
    """The cost factor at `dt_utc`: 1.0 (peak) or `off_peak_factor` (off-peak)."""
    c = normalize_config(cfg if cfg is not None else DEFAULT_CONFIG)
    return c["off_peak_factor"] if is_off_peak(dt_utc, c) else 1.0


def next_off_peak_window(dt_utc, cfg=None) -> dict | None:
    """Return the next off-peak window as {start, end} (UTC datetimes).

    - Currently PEAK → the upcoming off-peak window (start in the future).
    - Currently OFF-PEAK → the window already in effect (start = its actual
      beginning, which may be in the past; end = the next peak boundary).

    Returns None only for a degenerate 24/7-peak config (no off-peak exists).
    """
    c = normalize_config(cfg if cfg is not None else DEFAULT_CONFIG)
    dt = _as_utc(dt_utc)
    step = datetime.timedelta(minutes=1)
    if is_off_peak(dt, c):
        start = dt
        floor = dt - datetime.timedelta(days=8)
        while start > floor and is_off_peak(start - step, c):
            start -= step
        end = dt
        while is_off_peak(end, c):
            end += step
        return {"start": start, "end": end}
    t = dt
    for _ in range(_SCAN_MINUTES):
        if is_off_peak(t, c):
            end = t
            while is_off_peak(end, c):
                end += step
            return {"start": t, "end": end}
        t += step
    return None


def plan_start(dt_utc=None, critical: bool = False, cfg=None) -> dict:
    """The scheduling decision: run now or defer to the next off-peak window.

    Returns {defer, start_at (ISO|None), reason, state, factor}. Critical work
    (P0/P1 bugs, security, prod incidents, the express lane) NEVER defers.
    """
    c = normalize_config(cfg if cfg is not None else DEFAULT_CONFIG)
    dt = _as_utc(dt_utc) if dt_utc is not None else datetime.datetime.now(datetime.timezone.utc)
    state = "PEAK" if is_peak(dt, c) else "OFF-PEAK"
    if critical:
        return {"defer": False, "start_at": None, "reason": "critical — runs anytime",
                "state": state, "factor": factor_at(dt, c)}
    if state == "OFF-PEAK":
        return {"defer": False, "start_at": None, "reason": "off-peak now",
                "state": state, "factor": factor_at(dt, c)}
    w = next_off_peak_window(dt, c)
    return {"defer": True, "start_at": (w["start"].isoformat() if w else None),
            "reason": "peak window — defer to next off-peak",
            "state": state, "factor": factor_at(dt, c)}


# ── the waiting queue (non-critical lanes parked until a window) ────────────

def queue_dir(state_dir_: str | None = None) -> str:
    d = os.path.join(state_dir_ or state_dir(), "rate-windows", "queue")
    os.makedirs(d, exist_ok=True)
    return d


def _queue_path(state_dir_: str | None, topic: str) -> str:
    return os.path.join(queue_dir(state_dir_), f"{topic}.json")


def enqueue(topic: str, task: str, start_at: str,
            critical: bool = False, state_dir_: str | None = None) -> str:
    """Park a non-critical lane until the given off-peak `start_at` (ISO)."""
    p = _queue_path(state_dir_, topic)
    _write_private_json(p, {
        "topic": topic,
        "task": (task or "").strip(),
        "critical": bool(critical),
        "created": _now_iso(),
        "start_at": start_at,
        "status": "waiting-window",
    })
    return p


def dequeue(topic: str, state_dir_: str | None = None) -> None:
    try:
        os.remove(_queue_path(state_dir_, topic))
    except OSError:
        pass


def list_queue(state_dir_: str | None = None) -> list[dict]:
    out: list[dict] = []
    try:
        names = sorted(os.listdir(queue_dir(state_dir_)))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(queue_dir(state_dir_), name), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def due(state_dir_: str | None = None, now=None) -> list[dict]:
    """Queued lanes whose `start_at` is at-or-before `now` (ready to start)."""
    now_ts = _as_utc(now) if now is not None else datetime.datetime.now(datetime.timezone.utc)
    out = []
    for item in list_queue(state_dir_):
        sa = item.get("start_at") or ""
        try:
            sa_dt = datetime.datetime.fromisoformat(sa)
            if sa_dt.tzinfo is None:
                sa_dt = sa_dt.replace(tzinfo=datetime.timezone.utc)
            if sa_dt <= now_ts:
                out.append(item)
        except ValueError:
            continue
    return out


# ── the dashboard summary ───────────────────────────────────────────────────

def _fmt(dt) -> str:
    if dt is None:
        return ""
    return dt.strftime("%a %d %b %H:%M") + " UTC"


def _serialize_days(days: frozenset) -> str:
    if days == frozenset(range(0, 5)):
        return "mon-fri"
    if days == frozenset({5, 6}):
        return "weekend"
    if days == frozenset(range(7)):
        return "all"
    return ",".join(_DAY_LABELS[i] for i in sorted(days))


def _serialize_windows(cfg: dict) -> list[dict]:
    return [{"days": _serialize_days(w["days"]), "start": w["start"], "end": w["end"]}
            for w in cfg["peak_windows"]]


def current_state(env: dict | None = None, dt_utc=None) -> dict:
    """The current rate-window summary for the dashboard + agents. Never raises."""
    ensure_config(env)
    cfg = load_config(env)
    dt = _as_utc(dt_utc) if dt_utc is not None else datetime.datetime.now(datetime.timezone.utc)
    state = "PEAK" if is_peak(dt, cfg) else "OFF-PEAK"
    w = next_off_peak_window(dt, cfg)
    out: dict = {
        "now": dt.isoformat(),
        "provider": cfg["provider"],
        "updated": cfg["updated"],
        "state": state,
        "factor": factor_at(dt, cfg),
        "off_peak_factor": cfg["off_peak_factor"],
        "peak_windows": _serialize_windows(cfg),
        "optimization_enabled": cost_optimization_enabled(env),
        "offpeak_defer_enabled": offpeak_defer_enabled(env),
    }
    if state == "PEAK":
        out["next_off_peak_start"] = (w["start"].isoformat() if w else None)
        out["next_off_peak_end"] = (w["end"].isoformat() if w else None)
        out["next_off_peak_start_display"] = _fmt(w["start"] if w else None)
    else:
        out["off_peak_until"] = (w["end"].isoformat() if w else None)
        out["off_peak_until_display"] = _fmt(w["end"] if w else None)
    queue = list_queue()
    out["queue"] = queue
    out["queue_count"] = len(queue)
    return out
