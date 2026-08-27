"""Compliance controls — toggleable security/governance panel + attestation.

v2 toggle model (2026-08-25): every compliance control is an individual
toggle; a one-click **Compliance baseline** preset flips the recommended set;
an **attestation snapshot** export proves the posture to an auditor. Home UX
keeps the streamlined defaults (cloud LLM, no MFA requirement, autosync on,
remote support off).

State + `enabled_since` provenance persist in .env (the same hot-reloaded
file the worker/telemetry/auth read) under COMPLIANCE_* keys. This module is
pure stdlib — no FastAPI/SQLAlchemy — so the API and tests can import it
without a web/ORM context.
"""

import datetime
import hashlib
import json
import os

ENV_FILE = "/opt/barenoc/.env"

# ── control inventory (single source of truth) ─────────────────────────────
# kind: "bool" ("on"/"off") | "choice" (options) | "fixed" (read-only)
CONTROLS = {
    "llm_egress": {
        "label": "LLM egress",
        "kind": "choice",
        "default": "cloud",
        "baseline": "local",
        "options": ("cloud", "local"),
        "description": "Where ticket/chat/network-summary LLM calls may go. "
                       "'local' = an on-prem (Ollama/LM Studio) endpoint only — "
                       "no data leaves your network.",
    },
    "mfa_enforcement": {
        "label": "MFA enforcement",
        "kind": "bool",
        "default": "off",
        "baseline": "on",
        "description": "Require a second factor (passkey or TOTP) for admin/operator "
                       "sign-in. Passkey-first via Pocket ID; TOTP is the fallback.",
    },
    "telemetry": {
        "label": "Telemetry",
        "kind": "bool",
        "default": "on",
        "baseline": "off",
        "description": "Local-only time-series metrics collection (never egresses). "
                       "Off = no collection.",
    },
    "remote_support": {
        "label": "Remote support",
        "kind": "bool",
        "default": "off",
        "baseline": "off",
        "description": "Vendor Tailscale support path. Off by default; enabling "
                       "records explicit consent in the audit log.",
    },
    "retention": {
        "label": "Retention policy",
        "kind": "choice",
        "default": "sane",
        "baseline": "strict",
        "options": ("sane", "strict"),
        "description": "Per-category max-age pruning. 'strict' prunes sooner.",
        "warning": "Retention 'strict' prunes old rows sooner; relaxing back to "
                   "'sane' will not restore already-pruned data.",
    },
    "audit_log": {
        "label": "Audit log",
        "kind": "bool",
        "default": "on",
        "baseline": "on",
        "description": "Immutable hash-chained audit trail (viewer + export + verify).",
    },
    "session_policy": {
        "label": "Session policy",
        "kind": "choice",
        "default": "relaxed",
        "baseline": "strict",
        "options": ("relaxed", "strict"),
        "description": "Idle timeout + login lockout. 'strict' = short idle window "
                       "and account lockout after repeated failures.",
    },
    "data_deletion": {
        "label": "Data deletion",
        "kind": "fixed",
        "default": "available",
        "baseline": "available",
        "description": "Per-user purge + factory reset. Always available.",
    },
}

CONTROL_KEYS = tuple(CONTROLS.keys())

# Always-on floor (never toggleable — read-only in the UI + attestation).
NON_NEGOTIABLE = [
    "Self-protection invariants (SELF_PATTERNS / SELF_DEVICES / SELF_ACTIONS — restrictions.py)",
    "mTLS device identity (step-ca issued certificates)",
    "Encryption at rest (app-level secrets + LUKS2 USB backups)",
    "Update integrity (GPG-signed releases, verified before apply)",
    "3-layer backups (VM snapshot + encrypted USB + optional NAS)",
]

# Session-policy presets (idle minutes, lockout-after-failures; 0 = disabled)
SESSION_POLICY_VALUES = {
    "relaxed": {"idle_min": 0, "lockout_after": 0},
    "strict": {"idle_min": 30, "lockout_after": 5},
}

# Retention presets (days per category; 0 = never prune).
# KEEP IN SYNC with src/scheduler/main.py RETENTION_CATEGORIES.
RETENTION_PRESETS = {
    "sane":   {"metrics": 30, "audit_log": 365, "tickets": 0, "chat_messages": 0,
               "scan_runs": 90, "findings": 90, "firmware_upgrades": 365,
               "link_episodes": 30, "starlink_episodes": 30,
               "service_check_episodes": 30},
    "strict": {"metrics": 14, "audit_log": 90, "tickets": 365, "chat_messages": 180,
               "scan_runs": 30, "findings": 30, "firmware_upgrades": 180,
               "link_episodes": 7, "starlink_episodes": 7,
               "service_check_episodes": 7},
}

PRESET_PREV_KEY = "COMPLIANCE_PRESET_PREV"


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def read_env() -> dict:
    """Read .env fresh (file only — same semantics as the rest of the API)."""
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


def write_env(env: dict) -> None:
    """In-place .env rewrite (same inode) preserving comments — mirrors
    routes/settings._write_env_file so the two writers never fight."""
    lines_out = []
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()
    except Exception:
        lines = []
    env = dict(env)
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines_out.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in env:
                lines_out.append(f"{key}={env.pop(key)}\n")
            continue
        lines_out.append(line)
    for key, value in env.items():
        lines_out.append(f"{key}={value}\n")
    with open(ENV_FILE, "w") as f:
        f.writelines(lines_out)


def _state_key(key: str) -> str:
    return f"COMPLIANCE_{key.upper()}"


def _since_key(key: str) -> str:
    return f"COMPLIANCE_{key.upper()}_SINCE"


def _norm(key: str, value) -> str:
    """Validate + normalize a control value. Raises ValueError."""
    c = CONTROLS[key]
    v = str(value).strip().lower()
    if c["kind"] == "bool":
        if v in ("on", "true", "1", "yes"):
            return "on"
        if v in ("off", "false", "0", "no"):
            return "off"
        raise ValueError(f"{key} must be on/off")
    if c["kind"] == "choice":
        if v not in c["options"]:
            raise ValueError(f"{key} must be one of {', '.join(c['options'])}")
        return v
    if c["kind"] == "fixed":
        return c["default"]
    raise ValueError(f"unknown kind for {key}")


def get_controls(env: dict = None) -> dict:
    """The 8 controls as {key: {state, enabled_since, default, baseline, ...}}."""
    env = env if env is not None else read_env()
    out = {}
    for key in CONTROL_KEYS:
        c = CONTROLS[key]
        raw = env.get(_state_key(key))
        state = _norm(key, raw) if raw else c["default"]
        out[key] = {
            "label": c["label"],
            "kind": c["kind"],
            "default": c["default"],
            "baseline": c["baseline"],
            "options": list(c.get("options", [])),
            "description": c["description"],
            "warning": c.get("warning"),
            "state": state,
            "enabled_since": env.get(_since_key(key)) or None,
        }
    return out


def settings_hash(controls: dict = None) -> str:
    """SHA-256 of the canonical compliance config (states + provenance).

    Deterministic (sorted keys). Tampering with any control state or its
    enabled_since timestamp changes the hash — the auditor-facing integrity
    anchor for the attestation export.
    """
    controls = controls if controls is not None else get_controls()
    canonical = {k: {"state": v["state"], "enabled_since": v.get("enabled_since")}
                 for k, v in controls.items()}
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def mirror_to_effective(env: dict) -> dict:
    """Translate compliance states into the effective env keys the worker/
    telemetry/auth hot-read. Mutates + returns the env dict."""
    ctl = get_controls(env)
    env["LLM_EGRESS"] = ctl["llm_egress"]["state"]
    env["MFA_ENFORCED"] = "true" if ctl["mfa_enforcement"]["state"] == "on" else "false"
    env["TELEMETRY_ENABLED"] = "true" if ctl["telemetry"]["state"] == "on" else "false"
    env["AUDIT_LOG_ENABLED"] = "true" if ctl["audit_log"]["state"] == "on" else "false"
    sp = SESSION_POLICY_VALUES.get(ctl["session_policy"]["state"],
                                   SESSION_POLICY_VALUES["relaxed"])
    env["SESSION_IDLE_TIMEOUT_MIN"] = str(sp["idle_min"])
    env["SESSION_LOCKOUT_AFTER"] = str(sp["lockout_after"])
    profile = ctl["retention"]["state"]
    env["RETENTION_PROFILE"] = profile
    preset = RETENTION_PRESETS.get(profile, RETENTION_PRESETS["sane"])
    for cat, days in preset.items():
        env.setdefault(f"RETENTION_{cat.upper()}_DAYS", str(days))
    # Telemetry's existing retention path honors the same metrics max-age.
    env.setdefault("TELEMETRY_RETENTION_DAYS", str(preset["metrics"]))
    return env


def set_control(key: str, value, env: dict = None, persist: bool = True):
    """Set one control (records enabled_since). Returns (controls, env)."""
    if key not in CONTROLS:
        raise ValueError(f"unknown control: {key}")
    if CONTROLS[key]["kind"] == "fixed":
        raise ValueError(f"{key} is read-only (always {CONTROLS[key]['default']})")
    env = env if env is not None else read_env()
    state = _norm(key, value)
    env[_state_key(key)] = state
    env[_since_key(key)] = _now_iso()
    mirror_to_effective(env)
    if persist:
        write_env(env)
    return get_controls(env), env


def apply_preset(env: dict = None, persist: bool = True):
    """Compliance baseline preset: flip all 8 controls to baseline.

    Captures the prior values (once) under COMPLIANCE_PRESET_PREV so
    revert_preset() can restore them.
    """
    env = env if env is not None else read_env()
    current = get_controls(env)
    if PRESET_PREV_KEY not in env:
        prev = {k: v["state"] for k, v in current.items()}
        env[PRESET_PREV_KEY] = json.dumps(prev, sort_keys=True, separators=(",", ":"))
    for key in CONTROL_KEYS:
        if CONTROLS[key]["kind"] == "fixed":
            continue
        env[_state_key(key)] = CONTROLS[key]["baseline"]
        env[_since_key(key)] = _now_iso()
    mirror_to_effective(env)
    if persist:
        write_env(env)
    return get_controls(env), env


def revert_preset(env: dict = None, persist: bool = True):
    """Restore the pre-preset values (if any). Returns (controls, env, restored)."""
    env = env if env is not None else read_env()
    raw = env.pop(PRESET_PREV_KEY, None)
    restored = False
    if raw:
        try:
            prev = json.loads(raw)
        except Exception:
            prev = {}
        for key in CONTROL_KEYS:
            if key in prev and CONTROLS[key]["kind"] != "fixed":
                env[_state_key(key)] = prev[key]
                env[_since_key(key)] = _now_iso()
        restored = True
    mirror_to_effective(env)
    if persist:
        write_env(env)
    return get_controls(env), env, restored


def local_endpoint_missing(env: dict = None) -> bool:
    """True when egress is local but no on-prem provider is configured."""
    env = env if env is not None else read_env()
    if get_controls(env)["llm_egress"]["state"] != "local":
        return False
    from llm_providers import load_providers
    providers = load_providers(env)
    return not any((p.get("deployment") or "hosted") == "on_prem"
                   for p in providers.values())


def attestation(env: dict = None, appliance_version: str = None) -> dict:
    """The posture snapshot an auditor asks for ("show me the config")."""
    controls = get_controls(env)
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "appliance_version": appliance_version or "unknown",
        "controls": {k: {"state": v["state"], "enabled_since": v.get("enabled_since"),
                         "baseline": v["baseline"]}
                     for k, v in controls.items()},
        "settings_hash": settings_hash(controls),
        "settings_hash_algorithm": "sha256",
        "audit_log_export": "/api/v1/audit-log/export",
        "non_negotiable": NON_NEGOTIABLE,
        "local_endpoint_missing": local_endpoint_missing(env),
    }
