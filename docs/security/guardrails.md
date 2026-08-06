# Security Guardrails — Anti-Abuse & Safety

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Design Philosophy

The BareNOC agent has the ability to execute actions on customer infrastructure. This makes it a high-value target. Our defense is layered:

```
Ticket ──▶ Auth ──▶ Sanitize ──▶ LLM ──▶ Validate ──▶ Execute ──▶ Audit
  │          │          │          │         │            │          │
  │     Who can     Strip      Constrain   Check        Run in     Log
  │     submit?     injection  output      bounds       sandbox    everything
```

---

## Layer 1: Ticket Authentication

### Rules

- Every ticket requires a valid JWT token (httpOnly cookie)
- Anonymous ticket submission is **blocked at the network level** (Nginx)
- Rate limits per user:

```python
RATE_LIMITS = {
    "admin":    {"tickets_per_hour": 10, "concurrent": 3},
    "operator": {"tickets_per_hour": 5,  "concurrent": 2},
    "readonly": {"tickets_per_hour": 3,  "concurrent": 1},
}
```

### Role Definitions

| Role | Can Submit Tickets | Can Approve Escalations | Can View Reports | Scope |
|------|-------------------|------------------------|-----------------|-------|
| `admin` | ✅ Yes | ✅ Yes | ✅ Yes | All devices |
| `operator` | ✅ Yes | ⚠️ P3+ only | ✅ Yes | Assigned devices |
| `readonly` | ✅ Investigate only | ❌ No | ✅ Yes | All devices |
| `system` | ✅ Automated alerts | ✅ Yes | ✅ Yes | All devices |

---

## Layer 2: Prompt Injection Sanitization

### Blocked Patterns

All ticket text is scanned before sending to the LLM:

```python
BLOCKED_PATTERNS = [
    # Instruction override attempts
    r"ignore (all )?(previous|above|below|prior) (instructions|directions|commands)",
    r"(forget|disregard|override) (all |everything |)instructions",
    r"you (are |are now |will now )?(a |an |) ?(free|unleashed|DAN|jail)",
    r"system prompt",
    r"new instructions? follow",
    r"do not (follow|obey|comply|listen)",

    # Direct command execution attempts
    r"(rm|del|format|mkfs|dd) .*[/\\]",
    r"chmod (777|755|ugo)",
    r"chown .*:",
    r"sudo .*passwd",
    r"base64.*decode|base64.*encode",
    r"wget.*|curl.*\|.*sh",
    r"python3? -c ",
    r"eval\(",
    r"exec\(",

    # Social engineering / policy bypass
    r"output in (json|yaml|markdown) without",
    r"don.t (mention|include|tell|show)",
    r"this is (a |an )?(test|simulation|joke|hypothetical)",

    # Data exfiltration
    r"(send|post|upload) (to|via) (http|https|ftp)",
    r"(cat|dump|export|copy) .*(config|password|secret|key|cert)",
]
```

### Sanitizer Action

```python
def sanitize_ticket(text: str) -> tuple[str | None, str | None]:
    """
    Returns (sanitized_text, error_message).
    If blocked, text is None and error explains why.
    """
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return None, f"Rejected: ticket contains blocked pattern"
    return text, None
```

---

## Layer 3: LLM Output Constraints

### Allowed Actions (Enum)

The LLM _never_ writes free-form commands. It selects from:

```python
class AllowedAction(str, Enum):
    PING_TEST       = "ping_test"
    SNMP_POLL       = "snmp_poll"
    DEVICE_STATUS   = "device_status"
    APPLY_PATCH     = "apply_patch"       # from allowlist only
    REBOOT_DEVICE   = "reboot_device"     # immediate SSH reboot
    COLLECT_LOGS    = "collect_logs"
    ESCALATE_HUMAN  = "escalate_human"    # catch-all for uncertainty
```

### Structured Output Format

```json
{
    "action": "apply_patch",
    "target": "switch-01",
    "device_type": "unifi_switch",
    "params": {
        "patch_id": "FW-6.6.55",
        "scheduled_at": "2025-07-30T02:00:00Z",
        "confirm": true
    },
    "reason": "CVE-2025-1234: critical vulnerability in UniFi Switch firmware",
    "confidence": 0.95
}
```

### Confidence Gates

| Confidence | Action |
|-----------|--------|
| 0.95–1.00 | Auto-execute (P1/P2 only, admin submitter) |
| 0.80–0.94 | Hold for human approval for all priorities |
| < 0.80 | Escalate to human (never auto-execute) |

---

## Layer 4: Job Validation

Before a job reaches the Pi Agent, it passes through validation:

```python
def validate_job(job: dict) -> tuple[bool, str]:
    """
    Validates a job before execution.
    Returns (is_valid, reason_if_invalid).
    """

    # 1. Schema check
    if job["action"] not in AllowedAction.__members__:
        return False, f"Unknown action: {job['action']}"

    # 2. Target must be in managed inventory
    if job["target"] not in MANAGED_DEVICES:
        return False, f"Unknown target: {job['target']}"

    # 3. Parameter bounds
    if job["action"] == "reboot_device":
        if "scheduled_at" not in job.get("params", {}):
            return False, "Reboot requires scheduled_at"
        # Don't allow reboots during business hours
        hour = parse_time(job["params"]["scheduled_at"]).hour
        if 8 <= hour <= 18:
            return False, "Reboot during business hours requires human approval"

    # 4. Patch allowlist
    if job["action"] == "apply_patch":
        if job["params"]["patch_id"] not in PATCH_ALLOWLIST:
            return False, f"Patch {job['params']['patch_id']} not in allowlist"

    return True, ""
```

---

## Layer 5: Runtime Sandbox

The Pi Agent runs with restricted privileges:

```bash
# System user
User=pi-agent
Shell=/usr/sbin/nologin
# No sudo access
# No group memberships beyond its own

# SSH keys are:
# - Encrypted at rest with AES-256
# - Decrypted per-job, held in memory only
# - Scoped to target device user (e.g., monitoring@switch-01)

# No raw command execution
# The agent receives structured jobs and runs predefined scripts only:
/opt/barenoc/scripts/
├── ping_check.sh
├── snmp_poll.sh
├── patch_debian.sh
├── patch_ubuntu.sh
├── reboot_device.sh
├── collect_logs.sh
└── verify_connectivity.sh
```

---

## Layer 6: Human-in-the-Loop

| Priority | Auto-Execute | Requires Confirmation | Notes |
|----------|-------------|----------------------|-------|
| **P1** (outage) | ✅ From admin | ❌ | Auto-escalate to support email |
| **P2** (degraded) | ✅ Confidence ≥ 0.95 | ⚠️ Below 0.95 | Logged for audit |
| **P3** (routine) | ❌ | ✅ Always | Queue for human review |
| **P4** (cosmetic) | ❌ | ✅ Always | Daily digest, batch approval |

### Escalation Behavior

If the LLM is unsure (`confidence < 0.80`) or the action is not in the allowlist:

```python
# Auto-generate a human-readable ticket
{
    "type": "escalation",
    "original_ticket_id": "TKT-20250729-042",
    "reason": "LLM confidence 0.65 — below auto-execute threshold",
    "llm_plan": "Reboot switch-01 to clear ARP cache",
    "review_url": "https://barenoc.local/admin/escalations/042",
    "submitted_by": "system",
    "assigned_to": "admin"
}
```

---

## Layer 7: Audit Trail

All actions are logged immutably:

```bash
/opt/barenoc/volumes/logs/audit/
├── 2025-07-29/
│   ├── TKT-001.json            # Original ticket content
│   ├── TKT-001-sanitized.json  # After sanitization
│   ├── TKT-001-prompt.txt      # LLM prompt (with context)
│   ├── TKT-001-response.json   # Raw LLM response
│   ├── TKT-001-job.json        # Validated job sent to agent
│   ├── TKT-001-execution.log   # Agent stdout/stderr
│   └── TKT-001-result.json     # Final outcome
```

### Log Immutability

```sql
-- SQLite table with append-only trigger
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL,
    data JSON NOT NULL,
    sha256_hash TEXT NOT NULL
);

-- Trigger prevents UPDATE or DELETE
CREATE TRIGGER prevent_audit_modification
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is immutable');
END;

CREATE TRIGGER prevent_audit_deletion
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is immutable');
END;
```

---

## Incident Response

If a security incident is suspected (e.g., a prompt injection bypass):

1. **Disable ticket intake immediately**: `touch /opt/barenoc/LOCKDOWN`
2. **Freeze audit logs**: `chmod 0400 /opt/barenoc/volumes/logs/audit/`
3. **Take Proxmox snapshot**: `qm snapshot 100 security-incident-$(date +%F)`
4. **Investigate** the full chain — ticket → prompt → response → job → execution
5. **Patch** the vulnerability
6. **Rotate** all API keys, JWT secrets, and SSH keys
7. **Notify** affected customers
8. **Document** the incident and add new BLOCKED_PATTERNS
