"""Audit event CATALOG — the single source of truth for the compliance
audit trail (2026-08-26).

Maps every audit event type -> required fields -> retention class ->
framework coverage (SOC 2 / PCI DSS / HIPAA style). The attestation export
reads ``catalog_summary()`` so an auditor can see, in one place, that the box
captures a *complete, framework-mapped* inventory rather than "the events we
happened to add".

This module is pure stdlib (no FastAPI/SQLAlchemy) so the API, the scheduler,
and tests can import it freely. It never imports ``audit`` (which depends on
the ORM) — ``audit`` imports *this* module instead.

Rules enforced here (and mirrored by audit.enforce_payload_limits):
  - never log secrets — only type/hashes/outcomes;
  - each event's data payload stays small (<= ~300 B; hard ceiling enforced
    in audit.py).
"""

# ── retention classes ─────────────────────────────────────────────────────
# Global retention is governed by the compliance "retention" control
# (audit_log category: sane=365d, strict=90d — see compliance.RETENTION_PRESETS
# and scheduler.RETENTION_CATEGORIES). The class here is a *categorisation*
# for the attestation summary, not a second pruning knob.
RETENTION = {
    "critical": {
        "label": "critical",
        "days": "365 sane / 90 strict",
        "note": "auth, credential access, user lifecycle",
    },
    "security": {
        "label": "security",
        "days": "365 sane / 90 strict",
        "note": "config, compliance toggles, exports, backups, restrictions",
    },
    "operational": {
        "label": "operational",
        "days": "365 sane / 90 strict",
        "note": "tickets, jobs, LLM, firmware, service/link/starlink episodes",
    },
}

# ── framework tags ────────────────────────────────────────────────────────
SOC2 = "soc2"
PCI = "pci"
HIPAA = "hipaa"

ALL_FRAMEWORKS = [SOC2, PCI, HIPAA]

# ── the catalog ───────────────────────────────────────────────────────────
# Each entry: {"required": [...], "retention": <class>, "frameworks": [...],
#              "description": "..."}.
# required = the data fields every call site must set (validated by
# validate_event for the events this module added; documented for the rest).
EVENT_CATALOG = {
    # ── auth & identity (critical) ──
    "auth.login": {
        "required": ["ip", "method"],
        "retention": "critical",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Interactive password/passkey/TOTP sign-in (never agent/scheduler service-token logins).",
    },
    "auth.login_failed": {
        "required": ["ip"],
        "retention": "critical",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Failed interactive sign-in (locked accounts also 423-audited upstream).",
    },
    "user.created": {
        "required": ["created_username", "role"],
        "retention": "critical",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Admin-created account (records the actor + target).",
    },
    "user.deleted": {
        "required": ["deleted_username"],
        "retention": "critical",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Account deletion (actor + target).",
    },
    "user_data_purged": {
        "required": [],
        "retention": "critical",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Per-user operational data purge (data-deletion control).",
    },

    # ── credential access (critical — the highest-value event on a box that
    #    holds network credentials) ──
    "credential_access": {
        "required": ["device_id", "credential_type", "action"],
        "retention": "critical",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "A stored device SSH/SNMP secret (or the control key) was "
                       "decrypted/fetched for use. Records actor, device, type, "
                       "action — NEVER the secret.",
    },

    # ── compliance controls (security) ──
    "compliance_change": {
        "required": ["changes"],
        "retention": "security",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "One or more compliance toggles changed (before/after states).",
    },
    "compliance_preset": {
        "required": ["changes"],
        "retention": "security",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Compliance baseline preset applied (one-click recommended set).",
    },
    "compliance_revert": {
        "required": ["restored"],
        "retention": "security",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Compliance baseline preset reverted to the captured prior values.",
    },
    "remote_support_consent": {
        "required": ["control", "state"],
        "retention": "security",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Explicit consent recorded for vendor remote support.",
    },

    # ── exports (security) ──
    "export_download": {
        "required": ["kind"],
        "retention": "security",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Support bundle / audit-log / attestation export — who pulled what, when.",
    },

    # ── backups (security — integrity/availability evidence) ──
    "backup_start": {
        "required": ["type", "result"],
        "retention": "security",
        "frameworks": [SOC2, HIPAA],
        "description": "Backup began (type: app|usb|vm|offsite).",
    },
    "backup_success": {
        "required": ["type", "result"],
        "retention": "security",
        "frameworks": [SOC2, HIPAA],
        "description": "Backup completed successfully.",
    },
    "backup_failure": {
        "required": ["type", "result"],
        "retention": "security",
        "frameworks": [SOC2, HIPAA],
        "description": "Backup failed.",
    },
    "backup_restore": {
        "required": ["type", "result"],
        "retention": "security",
        "frameworks": [SOC2, HIPAA],
        "description": "A backup was restored (or restore-drilled).",
    },

    # ── scheduler health (security) ──
    "scheduler_health": {
        "required": ["result", "reason"],
        "retention": "security",
        "frameworks": [SOC2, PCI],
        "description": "Scheduler API-auth health incident (sustained auth failure "
                       "or recovery) — the missing-agent-creds class, now visible in-app.",
    },

    # ── update schedule (security) ──
    "update_schedule_change": {
        "required": ["mode", "enabled"],
        "retention": "security",
        "frameworks": [SOC2],
        "description": "Update schedule created/changed/cancelled/completed.",
    },

    # ── settings & restrictions (security) ──
    "settings_change": {
        "required": ["section"],
        "retention": "security",
        "frameworks": [SOC2, PCI],
        "description": "Settings section changed (values of secret fields never recorded).",
    },
    "restriction_blocked": {
        "required": [],
        "retention": "security",
        "frameworks": [SOC2],
        "description": "An action was blocked by restrictions/policy.",
    },
    "factory_reset": {
        "required": ["scope"],
        "retention": "security",
        "frameworks": [SOC2, PCI, HIPAA],
        "description": "Operational data wiped (data-deletion control).",
    },
    "unifi_network_create": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "A UniFi network was created via the controller API.",
    },
    "device_adopt_start": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Device adoption/control channel started.",
    },
    "device_adopt_revoke": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Device control channel revoked.",
    },
    "device_revoke_integrity": {
        "required": ["device_id", "device"],
        "retention": "security",
        "frameworks": [SOC2, PCI],
        "description": "A revoked device had no matching device_adopt_revoke audit "
                       "event (un-audited state change) — flagged by the integrity sweep.",
    },

    # ── ticket lifecycle (operational) ──
    "ticket_created": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Ticket created.",
    },
    "ticket_rejected": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Ticket rejected by the judge.",
    },
    "ticket_completed": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Ticket completed.",
    },
    "ticket_closed": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Ticket closed.",
    },
    "ticket_checkin": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Ticket check-in note.",
    },
    "ticket_autoclosed": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Ticket auto-closed (no response).",
    },
    "customer_action": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Customer-facing action/note posted.",
    },
    "escalation": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Ticket escalated.",
    },
    "judge_verdict": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Judge (policy) verdict recorded.",
    },
    "job_created": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "A device job was created.",
    },

    # ── LLM operations (operational) ──
    "llm_request": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "LLM request made (model/tokens, never the content).",
    },
    "llm_failed": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "LLM request failed.",
    },
    "llm_outage": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "LLM provider outage declared.",
    },
    "llm_recovered": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "LLM provider recovered.",
    },

    # ── firmware lifecycle (operational) ──
    "firmware_window_created": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware maintenance window created.",
    },
    "firmware_window_deleted": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware maintenance window deleted.",
    },
    "firmware_approval_pending": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware upgrade awaiting approval.",
    },
    "firmware_approval_approved": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware upgrade approved.",
    },
    "firmware_approval_deferred": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware upgrade approval deferred.",
    },
    "firmware_approval_escalated": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware upgrade escalated.",
    },
    "firmware_pending_resolved": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware pending action resolved.",
    },
    "firmware_upgrade_started": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware upgrade started.",
    },
    "firmware_upgrade_success": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware upgrade succeeded.",
    },
    "firmware_upgrade_rolled_back": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware upgrade rolled back.",
    },
    "firmware_escalation": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Firmware escalation raised.",
    },

    # ── service checks (operational) ──
    "service_check_created": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Service check monitor created.",
    },
    "service_check_deleted": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Service check monitor deleted.",
    },
    "service_check_updated": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Service check monitor updated.",
    },
    "service_check_failed": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Service check failed.",
    },
    "service_check_escalate": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Service check escalated to a ticket.",
    },
    "service_check_recovered": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Service check recovered.",
    },

    # ── link / internet / starlink episodes (operational) ──
    "link_flap": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Link flap detected.",
    },
    "link_flap_escalate": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Link flap escalated.",
    },
    "link_outage": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Link outage declared.",
    },
    "link_stable": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Link stabilised.",
    },
    "link_monitor_off": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Link monitor disabled.",
    },
    "internet_outage": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Internet outage declared.",
    },
    "internet_recovered": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Internet recovered.",
    },
    "starlink_degraded": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Starlink link degraded.",
    },
    "starlink_outage": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Starlink link outage.",
    },
    "starlink_recovered": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Starlink link recovered.",
    },
    "starlink_device_created": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Starlink device created.",
    },
    "starlink_phantom_removed": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "Starlink phantom evidence removed.",
    },

    # ── device agent (operational) ──
    "device_agent_job_result": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "NOC_Agent job result recorded.",
    },
    "device_agent_result_rejected": {
        "required": [],
        "retention": "operational",
        "frameworks": [SOC2],
        "description": "NOC_Agent job result rejected (unknown/mismatched job).",
    },
}


def catalog_keys() -> tuple:
    """The active event types, in catalog order."""
    return tuple(EVENT_CATALOG.keys())


def required_fields(event_type: str) -> list:
    """Required data fields for an event type ([] when unknown)."""
    return list(EVENT_CATALOG.get(event_type, {}).get("required", []))


def retention_class(event_type: str) -> str:
    return EVENT_CATALOG.get(event_type, {}).get("retention", "operational")


def frameworks(event_type: str) -> list:
    return list(EVENT_CATALOG.get(event_type, {}).get("frameworks", [SOC2]))


def is_known(event_type: str) -> bool:
    return event_type in EVENT_CATALOG


# ── secret-field guard (never log secrets) ────────────────────────────────
# Key-name markers that mean a value is a secret (or a ciphertext blob) and
# must not enter an audit payload. "token" is handled separately: *_token
# (refresh_token, access_token, api_token) are secrets, but *_tokens
# (prompt_tokens, response_tokens — token COUNTS) are not.
_SECRET_WORDS = (
    "password", "passwd", "passphrase", "pin", "secret", "private",
    "credential", "cipher", "ciphertext", "blob", "apikey", "api_key",
    "authorization", "cookie", "encrypted", "community", "passphrase",
)

_BENIGN_SUFFIXES = ("_type", "_types", "_version", "_fingerprint", "_hash",
                    "_count", "_id", "_ids")


def _looks_secret(key: str) -> bool:
    k = str(key).strip().lower()
    if k.endswith(_BENIGN_SUFFIXES) or k in ("hash", "id"):
        return False
    if k in ("ssh_key", "ssh_private_key", "snmp_community", "community"):
        return True
    # "token" (and *_token) = secret; "tokens" (a count) = not a secret.
    if "token" in k and "tokens" not in k:
        return True
    return any(w in k for w in _SECRET_WORDS)


def has_secret_fields(data, _path: str = "") -> list:
    """Return the dotted paths of any secret-looking keys in a payload."""
    found = []
    if isinstance(data, dict):
        for k, v in data.items():
            key = k
            here = f"{_path}.{key}" if _path else str(key)
            if _looks_secret(key):
                found.append(here)
            found.extend(has_secret_fields(v, here))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            found.extend(has_secret_fields(v, f"{_path}[{i}]" if _path else f"[{i}]"))
    return found


def payload_bytes(data) -> int:
    """Serialized payload size (bytes) — the volume honesty guard."""
    import json
    try:
        return len(json.dumps(data, sort_keys=True, default=str).encode("utf-8"))
    except Exception:
        return 0


def validate_event(event_type: str, data: dict) -> list:
    """Return a list of problems for an event payload ([] = clean).

    Checks (a) required fields present, (b) no secret-looking keys.
    Size is checked separately by audit.enforce_payload_limits.
    """
    problems = []
    data = data or {}
    if not is_known(event_type):
        problems.append(f"unknown event_type {event_type!r}")
    for field in required_fields(event_type):
        if field not in data:
            problems.append(f"missing required field {field!r}")
    for path in has_secret_fields(data):
        problems.append(f"secret field {path!r} must not be logged")
    return problems


def catalog_summary() -> dict:
    """The one-liner + full mapping the attestation export surfaces:
    "N audit event types active, hash-chained, retention X"."""
    by_framework = {f: [] for f in ALL_FRAMEWORKS}
    for key, meta in EVENT_CATALOG.items():
        for f in meta.get("frameworks", []):
            if f in by_framework:
                by_framework[f].append(key)
    return {
        "event_types_active": len(EVENT_CATALOG),
        "hash_chained": True,
        "retention": "audit_log category: 365d (sane) / 90d (strict)",
        "retention_classes": RETENTION,
        "frameworks": {
            "soc2": len(by_framework.get(SOC2, [])),
            "pci": len(by_framework.get(PCI, [])),
            "hipaa": len(by_framework.get(HIPAA, [])),
        },
        "summary": (f"{len(EVENT_CATALOG)} audit event types active, hash-chained, "
                    f"retention {RETENTION['critical']['days']}"),
        "catalog": EVENT_CATALOG,
        "by_framework": by_framework,
    }
