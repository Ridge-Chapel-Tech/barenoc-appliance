"""Compliance controls API — Settings → Security/Advanced panel.

Grouped toggles + the one-click Compliance baseline preset + the attestation
snapshot export + the read-only "non-negotiable" floor. Admin-gated.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from auth import require_role
from audit import log_event
from database import get_db
from models import User
from version import APP_VERSION
import compliance

logger = logging.getLogger("barenoc.compliance")

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])


def _effective(env: dict) -> dict:
    ctl = compliance.get_controls(env)
    return {
        "llm_egress": ctl["llm_egress"]["state"],
        "mfa_enforced": ctl["mfa_enforcement"]["state"] == "on",
        "telemetry_enabled": ctl["telemetry"]["state"] == "on",
        "audit_log_enabled": ctl["audit_log"]["state"] == "on",
        "retention_profile": ctl["retention"]["state"],
        "session_idle_min": int(env.get("SESSION_IDLE_TIMEOUT_MIN", "0") or 0),
        "session_lockout_after": int(env.get("SESSION_LOCKOUT_AFTER", "0") or 0),
    }


@router.get("")
def get_compliance(user: User = Depends(require_role("admin"))):
    env = compliance.read_env()
    return {
        "controls": compliance.get_controls(env),
        "non_negotiable": compliance.NON_NEGOTIABLE,
        "appliance_version": APP_VERSION,
        "preset_applied": compliance.PRESET_PREV_KEY in env,
        "local_endpoint_missing": compliance.local_endpoint_missing(env),
        "effective": _effective(env),
    }


@router.put("")
def update_compliance(config: dict, db: Session = Depends(get_db),
                      user: User = Depends(require_role("admin"))):
    """Set one or more controls: {controls: {key: value, ...}} (or flat keys)."""
    payload = config.get("controls", config) if isinstance(config, dict) else {}
    if not payload:
        raise HTTPException(status_code=400, detail="No controls provided")
    env = compliance.read_env()
    changed = []
    for key, value in payload.items():
        if key not in compliance.CONTROLS:
            raise HTTPException(status_code=400, detail=f"Unknown control: {key}")
        if compliance.CONTROLS[key]["kind"] == "fixed":
            raise HTTPException(status_code=400,
                                detail=f"{key} is read-only (always available)")
        before = compliance.get_controls(env)[key]["state"]
        try:
            compliance.set_control(key, value, env=env, persist=False)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        after = compliance.get_controls(env)[key]["state"]
        changed.append({"control": key, "before": before, "after": after})
        if key == "remote_support" and after == "on":
            log_event(db, "remote_support_consent", user.username, {
                "control": "remote_support", "state": "on",
                "note": "Customer consented to vendor remote support.",
            })
    compliance.write_env(env)
    log_event(db, "compliance_change", user.username, {"changes": changed})
    return {"status": "ok", "controls": compliance.get_controls(env),
            "changed": changed,
            "warnings": [compliance.CONTROLS[k]["warning"] for k in payload
                         if compliance.CONTROLS[k].get("warning")]}


@router.post("/preset")
def apply_preset(db: Session = Depends(get_db),
                 user: User = Depends(require_role("admin"))):
    """Compliance baseline preset — flips the recommended set (one click).

    Captures the prior values so revert can restore them. The response lists
    what changed + any warnings (the LLM switch to local changes chat quality).
    """
    env = compliance.read_env()
    before = compliance.get_controls(env)
    compliance.apply_preset(env=env, persist=True)
    after = compliance.get_controls(env)
    changed = {k: {"before": before[k]["state"], "after": after[k]["state"]}
               for k in compliance.CONTROL_KEYS
               if before[k]["state"] != after[k]["state"]}
    warnings = ["LLM egress is now local-only — chat quality depends on your "
                "on-prem endpoint (≥7B Q4 recommended; 3B = degraded chat only)."]
    log_event(db, "compliance_preset", user.username, {"changes": changed})
    return {"status": "ok", "controls": after, "changed": changed,
            "warnings": warnings}


@router.post("/revert")
def revert_preset(db: Session = Depends(get_db),
                  user: User = Depends(require_role("admin"))):
    """Restore the values captured before the preset (if any)."""
    env = compliance.read_env()
    controls, env, restored = compliance.revert_preset(env=env, persist=True)
    log_event(db, "compliance_revert", user.username, {"restored": restored})
    return {"status": "ok", "restored": restored, "controls": controls}


@router.get("/attestation")
def get_attestation(user: User = Depends(require_role("admin"))):
    """Posture snapshot (JSON): every control {state, enabled_since}, settings
    hash, appliance version, + audit-log export link."""
    return compliance.attestation(compliance.read_env(), APP_VERSION)


@router.get("/attestation/export")
def export_attestation(db: Session = Depends(get_db),
                      user: User = Depends(require_role("admin"))):
    """Download the attestation snapshot as a file (signed-envelope shape)."""
    snap = compliance.attestation(compliance.read_env(), APP_VERSION)
    # Export event: who pulled the attestation export, when (compliance).
    actor = getattr(user, "username", None) or str(getattr(user, "role", "admin"))
    log_event(db, "export_download", actor, {"kind": "attestation"})
    import datetime as _dt
    fname = f"barenoc-attestation-{_dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=json.dumps(snap, indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
