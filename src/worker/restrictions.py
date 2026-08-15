"""Hard restrictions — "let it run wild, but I've fenced the dangerous corners."

Independent of the autonomy policy: even in `autonomous` mode these DENY.
Hot-reloaded from .env on every call (file first, env fallback):

  RESTRICT_ACTIONS    comma list of action names that are never allowed
  RESTRICT_DEVICES    comma list of device names / hostnames / IPs that are
                      never acted on (substring match against the target)
  RESTRICT_PATTERNS   comma list of phrases that block the request outright
                      (case-insensitive, matched against title + description)

SELF-PROTECTION (always on, NOT overridable): the appliance may never harm
itself or attempt anything that would take itself offline — no matter the
profile, restrictions config, or what the ticket asks. See SELF_PATTERNS /
SELF_DEVICES below. This is the one clause that even Autonomous obeys.
"""

import os
from llm_providers import read_env_file

# The invariant, matched against ticket text BEFORE any LLM/judge/pi work.
# Deliberately specific to the appliance — "reboot the router" stays allowed.
SELF_PATTERNS = [
    # appliance power / stack control
    "shutdown the appliance", "shut down the appliance", "power off the appliance",
    "poweroff the appliance", "reboot the appliance", "restart the appliance",
    "stop the appliance", "turn off the appliance",
    "reboot barenoc", "shutdown barenoc", "stop barenoc",
    # docker stack
    "docker compose down", "docker compose stop", "stop all containers",
    "remove all containers", "delete all containers", "docker rm",
    # data destruction
    "delete the database", "wipe the database", "erase the database",
    "delete the backups", "erase the backups", "wipe the backups",
    "delete /opt/barenoc", "erase /opt/barenoc", "rm -rf /opt/barenoc",
    "delete the credentials", "delete the .env", "erase the .env",
    # storage / network surgery
    "format the disk", "mkfs", "wipe the disk", "dd if=/dev/zero",
    "flush the firewall", "flush iptables", "flush nftables", "drop all firewall rules",
    "change the appliance ip", "change the appliance gateway",
    "change the appliance dns", "change the appliance's ip",
]

# Actions that may never run (catalog is read-only/safe today; the hook stays
# for future write-actions that could affect the appliance itself).
SELF_ACTIONS = ["appliance_reboot", "appliance_poweroff", "stack_stop"]


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


def _self_devices() -> list:
    """The appliance's own identity — never a valid action target."""
    env = {}
    try:
        env = read_env_file() or {}
    except Exception:
        pass
    ip = (env.get("APPLIANCE_IP") or os.environ.get("APPLIANCE_IP") or "").strip()
    devs = [d for d in (ip, "bareNOC", "bareNOC.local", "app.barenoc.com",
                        "localhost", "127.0.0.1") if d]
    return devs


def blocks_request(text: str):
    """Pattern-level deny — checked before any LLM/judge/pi work.
    Self-protection first (never overridable), then user restrictions."""
    t = (text or "").lower()
    for pat in SELF_PATTERNS:
        if pat in t:
            return (f'self-protection: "{pat}" is never allowed — the appliance '
                    "must not harm itself or take itself offline")
    for pat in load_restrictions()["patterns"]:
        if pat and pat in t:
            return f'the request contains a blocked phrase: "{pat}"'
    return None


def check(text: str, action, target):
    """Action/device-level deny — hard cap on execution regardless of profile."""
    r = load_restrictions()
    act = (action or "").strip().lower()
    if act in SELF_ACTIONS:
        return f"action '{act}' is self-protection-blocked (never allowed)"
    if act in r["actions"]:
        return f"action '{act}' is on the restricted list"
    t = (target or "").strip().lower()
    for d in _self_devices():
        if d and d in t:
            return f"device matching '{d}' is the appliance itself — never a target"
    for d in r["devices"]:
        if d and d in t:
            return f"device matching '{d}' is restricted"
    return None
