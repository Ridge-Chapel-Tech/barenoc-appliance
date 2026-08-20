"""Submit-Report gate — REPORT_GATE env (open/support) + the beta support grant.

During beta the gate is OPEN (REPORT_GATE=open, the default). Out of beta it
flips to `support` and both the Submit Report button and the remote-support
toggle are gated to the Support subscription.

The beta bridge (user-approved 08-19): a `support_grant` key ships in the
release (expiring, rotated — the same semi-public pattern as the forum-submit
token). While the grant is active the `support` gate sits in **beta-open**:
the entitlement check is built as a GA stub that plugs the real subscription
in later. When the grant expires (or is absent), the gate falls through to the
entitlement check — which today denies cleanly (never a silent pass).

Hot-reads the env file so a Settings/flag change applies without a restart
(same pattern as routes/chat.chat_client_enabled).
"""

import json
from datetime import datetime, timezone
from typing import Optional

from llm_providers import read_env_file

OPEN = "open"
SUPPORT = "support"

# The beta grant is semi-public (like the forum-submit token) and lives in a
# 0600 secret file written by the shared provision step. Tests and hot-reads
# may override via SUPPORT_GRANT / SUPPORT_GRANT_EXPIRES_AT env keys.
SUPPORT_GRANT_SECRET_FILE = "/opt/barenoc/volumes/secrets/support_grant.json"


def report_gate_mode(env: Optional[dict] = None) -> str:
    """The effective gate mode: 'open' or 'support' (default open)."""
    env = env if env is not None else read_env_file()
    raw = (env.get("REPORT_GATE") or "").strip().lower()
    return SUPPORT if raw == SUPPORT else OPEN


def _grant_from_env(env: dict) -> Optional[dict]:
    """The beta grant from env overrides (used by tests + hot-reads).

    Supports SUPPORT_GRANT as a bare token or a JSON object, and
    SUPPORT_GRANT_EXPIRES_AT (ISO-8601) as the expiry.
    """
    raw = (env.get("SUPPORT_GRANT") or "").strip()
    if not raw:
        return None
    expires = (env.get("SUPPORT_GRANT_EXPIRES_AT") or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("grant"):
                return {"grant": str(parsed["grant"]).strip(),
                        "expires_at": str(parsed.get("expires_at") or "").strip()}
        except (ValueError, TypeError):
            pass
    return {"grant": raw, "expires_at": expires}


def _grant_from_file() -> Optional[dict]:
    """The beta grant from the 0600 secret file (production path)."""
    try:
        with open(SUPPORT_GRANT_SECRET_FILE) as f:
            data = json.load(f)
        grant = (data or {}).get("grant")
        if not grant:
            return None
        return {"grant": str(grant).strip(),
                "expires_at": str((data or {}).get("expires_at") or "").strip()}
    except (OSError, ValueError, TypeError):
        return None


def _read_grant(env: Optional[dict] = None) -> Optional[dict]:
    """Grant from explicit env overrides first, then the secret file.

    An explicit `env` dict (tests) is fully hermetic — it never touches the
    host secret file. The default hot-read (env=None) reads the live .env and
    the 0600 secret file written by the shared provision step.
    """
    if env is not None:
        return _grant_from_env(env)
    env = read_env_file()
    from_env = _grant_from_env(env)
    if from_env is not None:
        return from_env
    return _grant_from_file()


def _expired(expires_at: str) -> bool:
    """True when an ISO-8601 expiry has passed. Unparseable/empty = not expired
    (a missing expiry is treated as active — the operator sets it on rotation)."""
    expires_at = (expires_at or "").strip()
    if not expires_at:
        return False
    try:
        when = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= when
    except ValueError:
        return False


def support_grant_active(env: Optional[dict] = None) -> bool:
    """True while the beta `support_grant` key is present + unexpired."""
    grant = _read_grant(env)
    if not grant or not grant.get("grant"):
        return False
    return not _expired(grant.get("expires_at") or "")


def support_entitled(user) -> bool:
    """Support-subscription entitlement check.

    GA plugs the real entitlement check here (the subscription that gates the
    forum report loop + remote support out of beta). Until then this returns
    False so `support` mode is a clean deny once the beta grant expires —
    never a silent pass.
    """
    # Stub: no entitlement source wired yet.
    _ = user
    return False


def support_allowed(user=None, env: Optional[dict] = None) -> bool:
    """True when a support-gated capability may be used under `support` mode:
    the beta grant is active OR the GA entitlement check passes."""
    return support_grant_active(env) or support_entitled(user)


def report_gate_allowed(user=None, env: Optional[dict] = None) -> bool:
    """True when the logged-in user may submit a report under the current gate."""
    if report_gate_mode(env) != SUPPORT:
        return True
    return support_allowed(user, env)


def report_gate_status(user=None, env: Optional[dict] = None) -> dict:
    """Serialisable gate state for the UI / tests."""
    mode = report_gate_mode(env)
    if mode == OPEN:
        return {"open": True, "mode": OPEN, "note": ""}
    if support_grant_active(env):
        return {
            "open": True,
            "mode": SUPPORT,
            "beta": True,
            "note": ("Beta support grant active — support-gated features are "
                     "available during the beta."),
        }
    return {
        "open": False,
        "mode": SUPPORT,
        "beta": False,
        "note": "Report submission and remote support are gated to the Support subscription.",
    }
