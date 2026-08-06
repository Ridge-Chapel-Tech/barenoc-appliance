"""Per-deployment autonomy policy for the worker pipeline.

Controls how much autonomy the AI gets — and which risk filters are active —
per customer/deployment. Hot-reloaded from .env (file first, env fallback).

Profiles:
  autonomous  — home / owner: risk filters OFF (no forced judge on keywords),
                write actions auto-execute, NO approval queue. A request is
                either lawful -> executed, or unlawful -> denied with reason.
  balanced    — SMB owner-operator: judge always, writes auto-exec at high
                confidence (>= 0.90), P1 goes to human approval.
  strict      — Tier II support: judge always, all risk filters ON, writes and
                P1/P2 always land in the approval queue.

Granular overrides (win over the profile):
  LLM_POLICY_RISK_FILTERS        all | none | maintenance,security,network,identity,install,change
  LLM_POLICY_JUDGE_REQUIRED      true|false
  LLM_POLICY_WRITE_AUTOEXEC      true|false
  LLM_POLICY_AUTOEXEC_THRESHOLD  0.0-1.0  (write/priority actions)
  LLM_POLICY_APPROVAL_PRIORITIES P1,P2   (empty = none)

No LLM_POLICY_PROFILE set -> legacy behavior: judge off unless LLM_JUDGE_ENABLED,
gates exactly as before (read-only >=0.80 auto; writes >=0.80 approval; P1/P2 >=0.95 auto).
"""

import os
import time
from dataclasses import dataclass

from llm_providers import read_env_file

# Risk-word categories -> which force a judge call when NOT short-circuiting.
# "none" disables them (autonomous home profile): writes still go through the
# judge for lawfulness, but are never blocked just for mentioning a keyword.
RISK_CATEGORIES = {
    "maintenance": [r"\breboot(s|ed|ing)?\b", r"\brestart(s|ed|ing)?\b", r"\bpatch(es|ed|ing)?\b",
                     r"\bupgrade(s|d|ing)?\b", r"\bfirmware(s)?\b"],
    "security":    [r"\bblock(s|ed|ing)?\b", r"\bdelete(s|d)?\b", r"\bdisable(s|d)?\b",
                     r"\bfirewall(s)?\b", r"\bacl(s)?\b"],
    "network":     [r"\bport(s)?\b", r"\bvlan(s)?\b", r"\bconfig(s)?\b"],
    "identity":    [r"\bpassword(s)?\b", r"\baccount(s)?\b"],
    "install":     [r"\binstall(s|ed|ing)?\b"],
    "change":      [r"\bchange(s|d)?\b", r"\breset(s)?\b"],
}
ALL_RISK_PATTERNS = [p for pats in RISK_CATEGORIES.values() for p in pats]

PROFILES = {
    "autonomous": dict(risk_filters="none", judge_required=True, write_autoexec=True,
                       autoexec_threshold=0.80, approval_priorities=(),
                       read_only_threshold=0.60),  # harmless reads run even at modest confidence
    "balanced":   dict(risk_filters="all", judge_required=True, write_autoexec=True,
                       autoexec_threshold=0.90, approval_priorities=("P1",)),
    "strict":     dict(risk_filters="all", judge_required=True, write_autoexec=False,
                       autoexec_threshold=0.80, approval_priorities=("P1", "P2")),
}


@dataclass
class Policy:
    profile: str = ""
    risk_filters: str = "all"
    judge_required: bool = False
    write_autoexec: bool = False
    autoexec_threshold: float = 0.80
    read_only_threshold: float = 0.80
    approval_priorities: tuple = ("P1", "P2")

    @property
    def legacy(self) -> bool:
        """No profile configured -> exact pre-policy behavior."""
        return not self.profile

    def active_risk_patterns(self) -> list:
        """Compile the risk regexes active under this policy."""
        rf = (self.risk_filters or "all").strip().lower()
        if rf == "none":
            return []
        if rf in ("", "all", "*"):
            return list(ALL_RISK_PATTERNS)
        out = []
        for cat in (c.strip() for c in rf.split(",") if c.strip()):
            out.extend(RISK_CATEGORIES.get(cat, []))
        return out

    def autoexec_decision(self, action: str, priority: str,
                          confidence: float, read_only_actions: set) -> bool:
        """Should this job auto-execute (no human approval)?"""
        if action in read_only_actions:
            return confidence >= self.read_only_threshold
        if not self.write_autoexec:
            return False
        if priority in self.approval_priorities:
            return False
        return confidence >= self.autoexec_threshold

    def approval_enabled(self, action: str, priority: str) -> bool:
        """When a job doesn't auto-execute: is it reviewable (approval) or denied?
        autonomous -> never an approval queue; lawful-but-low-confidence is
        escalated/denied so the owner never has to approve their own request."""
        if self.profile == "autonomous":
            return False
        return True


# ── hot-reload (mtime, like llm_providers) ──────────────────────────────────

_CACHE = {"mtime": None, "policy": None}


def _env_value(env: dict, key: str) -> str:
    return env.get(key) or os.getenv(key, "")


def _bool_or_none(env: dict, key: str):
    v = _env_value(env, key).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


def _float_or_none(env: dict, key: str):
    v = _env_value(env, key).strip()
    try:
        return float(v)
    except ValueError:
        return None


def load_policy() -> Policy:
    env = read_env_file()
    profile = _env_value(env, "LLM_POLICY_PROFILE").strip().lower()
    base = dict(PROFILES.get(profile, {}))

    risk_filters = _env_value(env, "LLM_POLICY_RISK_FILTERS").strip().lower() \
        or base.get("risk_filters", "all")

    judge = _bool_or_none(env, "LLM_POLICY_JUDGE_REQUIRED")
    if judge is None:
        judge = base.get("judge_required", False)

    write_auto = _bool_or_none(env, "LLM_POLICY_WRITE_AUTOEXEC")
    if write_auto is None:
        write_auto = base.get("write_autoexec", False)

    threshold = _float_or_none(env, "LLM_POLICY_AUTOEXEC_THRESHOLD")
    if threshold is None:
        threshold = base.get("autoexec_threshold", 0.80)

    prios_raw = _env_value(env, "LLM_POLICY_APPROVAL_PRIORITIES").strip()
    if prios_raw:
        approval_priorities = tuple(
            p.strip().upper() for p in prios_raw.split(",") if p.strip())
    else:
        approval_priorities = base.get("approval_priorities", ("P1", "P2"))

    read_only = _float_or_none(env, "LLM_POLICY_READ_THRESHOLD")
    if read_only is None:
        read_only = base.get("read_only_threshold", 0.80)

    return Policy(profile=profile, risk_filters=risk_filters,
                  judge_required=judge, write_autoexec=write_auto,
                  autoexec_threshold=threshold,
                  read_only_threshold=read_only,
                  approval_priorities=approval_priorities)


def get_policy() -> Policy:
    """Cached policy, refreshed when the .env file changes."""
    try:
        mtime = os.path.getmtime("/opt/barenoc/.env")
    except Exception:
        mtime = 0
    if mtime != _CACHE["mtime"]:
        _CACHE["policy"] = load_policy()
        _CACHE["mtime"] = mtime
    return _CACHE["policy"]


def reset_policy_cache() -> None:
    _CACHE["mtime"] = None
    _CACHE["policy"] = None
