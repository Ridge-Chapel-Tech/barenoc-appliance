#!/usr/bin/env python3
"""In-memory rate limiting (M2-T9).

Fixed-window per-client counters keyed by the client's REAL IP. Behind nginx
we take the LAST hop of X-Forwarded-For: nginx sets it with
$proxy_add_x_forwarded_for, which APPENDS the true remote address last, so
earlier hops are client-spoofable and never trusted.

Rules are path-prefix based and configurable via .env (re-read with a short
TTL so a .env edit hot-reloads without a container recreate):

    RATE_LIMIT_ENABLED   default "true"
    RATE_LIMIT_LOGIN     default "20/minute"   (login/register/refresh/…)
    RATE_LIMIT_CHAT      default "120/minute"  (mobile chat front door)
    RATE_LIMIT_API       default "300/minute"  (everything else under /api)

Exceeding a limit returns HTTP 429 with a Retry-After header. Pure ASGI —
no body buffering, no dependency on the request path beyond scope.
"""
import os
import threading
import time

from llm_providers import ENV_FILE

# ── default rules (ordered — first prefix match wins) ──────────────────────
# (path prefix, env key, default spec)
RULE_DEFS = [
    ("/api/v1/auth/login",          "RATE_LIMIT_LOGIN", "20/minute"),
    ("/api/v1/auth/register",       "RATE_LIMIT_LOGIN", "20/minute"),
    ("/api/v1/auth/refresh",        "RATE_LIMIT_LOGIN", "20/minute"),
    ("/api/v1/auth/change-password", "RATE_LIMIT_LOGIN", "20/minute"),
    ("/api/v1/auth/oidc",           "RATE_LIMIT_LOGIN", "30/minute"),
    ("/api/v1/chat",                "RATE_LIMIT_CHAT", "120/minute"),
    ("/api/v1",                     "RATE_LIMIT_API",  "300/minute"),
]

_MAX_BUCKETS = 20000          # hard cap on tracked (path, ip) buckets
_CFG_TTL = 15.0               # seconds between .env re-reads

# ── config loading (cached, hot-reload friendly) ───────────────────────────
_cfg_cache = {"at": 0.0, "enabled": True, "rules": []}


def _read_env() -> dict:
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


def _cfg() -> tuple[bool, list]:
    """(enabled, rules) — rules = [(prefix, limit, window_seconds), …]."""
    now = time.monotonic()
    if now - _cfg_cache["at"] >= _CFG_TTL or not _cfg_cache["rules"]:
        env = _read_env()

        def get(key, default):
            return env.get(key) or os.getenv(key) or default

        enabled = str(get("RATE_LIMIT_ENABLED", "true")).strip().lower() \
            not in ("0", "false", "no", "off")
        rules = []
        for prefix, key, default in RULE_DEFS:
            parsed = _parse_rate(get(key, default))
            if parsed is not None:
                rules.append((prefix, parsed[0], parsed[1]))
        _cfg_cache["enabled"] = enabled
        _cfg_cache["rules"] = rules
        _cfg_cache["at"] = now
    return _cfg_cache["enabled"], _cfg_cache["rules"]


def _parse_rate(spec: str):
    """'N/period' -> (count, window_seconds) | None. period: s|second|m|minute|h|hour."""
    try:
        n, _, period = spec.strip().lower().partition("/")
        count = int(n)
        p = period[:1]
        window = {"s": 1, "m": 60, "h": 3600}.get(p)
        if window is None or count <= 0:
            return None
        return count, window
    except (ValueError, AttributeError):
        return None


def client_ip(scope: dict) -> str:
    """Real client IP: last X-Forwarded-For hop, else the peer address."""
    for k, v in scope.get("headers", []):
        if k.lower() == b"x-forwarded-for":
            parts = [p.strip() for p in v.decode("latin-1").split(",") if p.strip()]
            if parts:
                return parts[-1]
            break
    client = scope.get("client")
    return client[0] if client else ""


# ── limiter ────────────────────────────────────────────────────────────────

class _Bucket:
    __slots__ = ("window_start", "window", "count")

    def __init__(self, now: float, window: int):
        self.window_start = now
        self.window = window
        self.count = 1


class RateLimiter:
    """Fixed-window counters keyed by (path, ip). Thread-safe; prune-friendly."""

    def __init__(self):
        self._buckets = {}
        self._lock = threading.Lock()

    def check(self, path: str, ip: str):
        """Returns (allowed: bool, retry_after_seconds: int | None)."""
        enabled, rules = _cfg()
        if not enabled or not ip or not path.startswith("/api/"):
            return True, None
        limit = window = None
        for prefix, lim, win in rules:
            if path == prefix or path.startswith(prefix + "/"):
                limit, window = lim, win
                break
        if limit is None:
            return True, None

        now = time.monotonic()
        key = (path, ip)
        with self._lock:
            self._prune(now)
            b = self._buckets.get(key)
            if b is None:
                self._buckets[key] = _Bucket(now, window)
                return True, None
            if now - b.window_start >= b.window:
                b.window_start = now
                b.count = 1
                return True, None
            b.count += 1
            if b.count <= limit:
                return True, None
            retry = int(b.window - (now - b.window_start)) + 1
            return False, max(retry, 1)

    def _prune(self, now: float):
        if len(self._buckets) < _MAX_BUCKETS * 0.75:
            return
        dead = [k for k, b in self._buckets.items() if now - b.window_start >= b.window]
        for k in dead:
            del self._buckets[k]
        if len(self._buckets) > _MAX_BUCKETS:
            over = len(self._buckets) - _MAX_BUCKETS
            for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1].window_start)[:over]:
                del self._buckets[k]


# ── ASGI middleware ────────────────────────────────────────────────────────

class RateLimitMiddleware:
    """Rejects /api/* requests that exceed their path rule with HTTP 429."""

    def __init__(self, app):
        self.app = app
        self.limiter = RateLimiter()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        allowed, retry = self.limiter.check(path, client_ip(scope))
        if allowed:
            return await self.app(scope, receive, send)
        from fastapi.responses import JSONResponse
        resp = JSONResponse(
            {"detail": "Too many requests. Please try again shortly."},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
        await resp(scope, receive, send)
