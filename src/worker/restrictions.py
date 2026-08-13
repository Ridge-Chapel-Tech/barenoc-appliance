"""Hard restrictions — "let it run wild, but I've fenced the dangerous corners."

Independent of the autonomy policy: even in `autonomous` mode these DENY.
Hot-reloaded from .env on every call (file first, env fallback):

  RESTRICT_ACTIONS    comma list of action names that are never allowed
  RESTRICT_DEVICES    comma list of device names / hostnames / IPs that are
                      never acted on (substring match against the target)
  RESTRICT_PATTERNS   comma list of phrases that block the request outright
                      (case-insensitive, matched against title + description)

A blocked request is escalated to a human with the reason — it is never
executed, never even sent to the LLM when a pattern matches.
"""

from llm_providers import read_env_file


def _csv(value) -> list:
    return [x.strip().lower() for x in (value or "").split(",") if x.strip()]


def load_restrictions() -> dict:
    try:
        env = read_env_file() or {}
    except Exception:
        env = {}
    return {
        "actions": _csv(env.get("RESTRICT_ACTIONS")),
        "devices": _csv(env.get("RESTRICT_DEVICES")),
        "patterns": _csv(env.get("RESTRICT_PATTERNS")),
    }


def blocks_request(text: str):
    """Pattern-level deny — checked before any LLM/judge/pi work."""
    for pat in load_restrictions()["patterns"]:
        if pat and pat in (text or "").lower():
            return f'the request contains a blocked phrase: "{pat}"'
    return None


def check(text: str, action, target):
    """Action/device-level deny — hard cap on execution regardless of profile."""
    r = load_restrictions()
    if action and action.strip().lower() in r["actions"]:
        return f"action '{action}' is on the restricted list"
    t = (target or "").strip().lower()
    for d in r["devices"]:
        if d and d in t:
            return f"device matching '{d}' is restricted"
    return None
