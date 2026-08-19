"""Submit-Report gate — REPORT_GATE env (open/support).

During beta the gate is OPEN (REPORT_GATE=open, the default). Out of beta it
flips to `support` and the Submit Report button is gated to the Support
subscription. The entitlement check plugs in at GA — until then the `support`
mode denies with a clear note (the one-line stub that flips later).

Hot-reads the env file so a Settings/flag change applies without a restart
(same pattern as routes/chat.chat_client_enabled).
"""

from typing import Optional

from llm_providers import read_env_file

OPEN = "open"
SUPPORT = "support"


def report_gate_mode(env: Optional[dict] = None) -> str:
    """The effective gate mode: 'open' or 'support' (default open)."""
    env = env if env is not None else read_env_file()
    raw = (env.get("REPORT_GATE") or "").strip().lower()
    return SUPPORT if raw == SUPPORT else OPEN


def support_entitled(user) -> bool:
    """Support-subscription entitlement check.

    GA plugs the real entitlement check here (the subscription that gates the
    forum report loop out of beta). Until then this returns False so `support`
    mode is a clean deny — never a silent pass.
    """
    # Stub: no entitlement source wired yet.
    _ = user
    return False


def report_gate_allowed(user=None, env: Optional[dict] = None) -> bool:
    """True when the logged-in user may submit a report under the current gate."""
    if report_gate_mode(env) != SUPPORT:
        return True
    return support_entitled(user)


def report_gate_status(user=None, env: Optional[dict] = None) -> dict:
    """Serialisable gate state for the UI / tests."""
    mode = report_gate_mode(env)
    if mode == OPEN:
        return {"open": True, "mode": OPEN, "note": ""}
    return {
        "open": False,
        "mode": SUPPORT,
        "note": "Report submission is gated to the Support subscription.",
    }
