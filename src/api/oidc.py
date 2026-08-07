"""OIDC client for Pocket ID (passkey login) — manual PKCE authorization-code flow.

Config (from .env file, fresh read each call):
  OIDC_ENABLED=true|false
  OIDC_PROVIDER_URL=https://<host>/auth/pocket-id/
  OIDC_CLIENT_ID=barenoc
  OIDC_CLIENT_SECRET=...
  OIDC_GROUP_ADMIN=barenoc-admins
  OIDC_GROUP_OPERATOR=barenoc-operators
"""

import os
import base64
import hashlib
import secrets
import time
import urllib.parse

import httpx

ENV_FILE = "/opt/barenoc/.env"
DISCOVERY_CACHE: dict = {"url": None, "data": None, "fetched": 0.0}
DISCOVERY_TTL = 3600

# The appliance's own Pocket ID serves a SELF-SIGNED cert (like every other
# internal service) — the API must not reject it. The trust boundary is the
# LAN + the PKCE code exchange + the client secret; TLS is transport only.
# (Mirrors how the agent talks to the local API and unifi.py to the controller.)
_HTTX_VERIFY = False


def read_env() -> dict:
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


def oidc_config() -> dict:
    env = read_env()
    return {
        "enabled": _get(env, "OIDC_ENABLED", "false").lower() in ("1", "true", "yes"),
        "provider_url": _get(env, "OIDC_PROVIDER_URL", "").rstrip("/"),
        "client_id": _get(env, "OIDC_CLIENT_ID", ""),
        "client_secret": _get(env, "OIDC_CLIENT_SECRET", ""),
        "group_admin": _get(env, "OIDC_GROUP_ADMIN", "barenoc-admins"),
        "group_operator": _get(env, "OIDC_GROUP_OPERATOR", "barenoc-operators"),
    }


def oauth_login_config() -> dict:
    """GitHub / Google OAuth login capability flags (config only — the actual
    authorization-code flows are future work). A provider counts as available
    only when its toggle is on AND both client id + secret are configured, so
    the login page never shows a dead button mid-setup."""
    env = read_env()

    def ready(enabled_key: str, id_key: str, secret_key: str) -> bool:
        on = _get(env, enabled_key, "false").lower() in ("1", "true", "yes")
        return on and bool(_get(env, id_key, "")) and bool(_get(env, secret_key, ""))

    return {
        "github_enabled": ready("GITHUB_LOGIN_ENABLED", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"),
        "google_enabled": ready("GOOGLE_LOGIN_ENABLED", "GOOGLE_LOGIN_CLIENT_ID", "GOOGLE_LOGIN_CLIENT_SECRET"),
    }


def discovery(cfg: dict) -> dict:
    """Fetch (and cache) the provider's OIDC discovery document."""
    if not cfg.get("provider_url"):
        return {}
    url = f"{cfg['provider_url']}/.well-known/openid-configuration"
    cache = DISCOVERY_CACHE
    if cache["url"] == url and cache["data"] and (time.time() - cache["fetched"]) < DISCOVERY_TTL:
        return cache["data"]
    try:
        r = httpx.get(url, timeout=10, verify=_HTTX_VERIFY)
        r.raise_for_status()
        data = r.json()
        cache.update(url=url, data=data, fetched=time.time())
        return data
    except Exception:
        return {}


def generate_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(cfg: dict, verifier: str, state: str, redirect_uri: str) -> str:
    disc = discovery(cfg)
    endpoint = disc.get("authorization_endpoint") or f"{cfg['provider_url']}/authorize"
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "scope": "openid profile email groups",
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


def exchange_code(cfg: dict, code: str, verifier: str, redirect_uri: str) -> dict:
    disc = discovery(cfg)
    endpoint = disc.get("token_endpoint") or f"{cfg['provider_url']}/token"
    r = httpx.post(endpoint, data={

        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code_verifier": verifier,
    }, timeout=15, verify=_HTTX_VERIFY)
    r.raise_for_status()
    return r.json()


def fetch_userinfo(cfg: dict, access_token: str) -> dict:
    disc = discovery(cfg)
    endpoint = disc.get("userinfo_endpoint") or f"{cfg['provider_url']}/userinfo"
    r = httpx.get(endpoint, headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
                   verify=_HTTX_VERIFY)
    r.raise_for_status()
    return r.json()


def role_from_groups(cfg: dict, claims: dict) -> str:
    """Map Pocket ID groups to a BareNOC role (admin > operator > readonly)."""
    groups = claims.get("groups") or claims.get("group") or []
    if not isinstance(groups, list):
        groups = [groups]
    if any(g == cfg["group_admin"] for g in groups):
        return "admin"
    if any(g == cfg["group_operator"] for g in groups):
        return "operator"
    return "readonly"
