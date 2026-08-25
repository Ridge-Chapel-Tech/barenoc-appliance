# Audit Logging

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Audit Trail Architecture

Every action in BareNOC is logged to an append-only, tamper-evident audit trail.

```
[Ticket Created] ──▶ [Ticket Processed] ──▶ [Action Taken]
        │                    │                      │
        ▼                    ▼                      ▼
  ┌──────────┐       ┌──────────────┐        ┌──────────┐
  │ audit_log │ ────▶ │ audit_log   │ ──────▶ │audit_log │
  └──────────┘       └──────────────┘        └──────────┘
        │                    │                      │
        └────────────────────┼──────────────────────┘
                             ▼
                    [Immutable SQLite DB]
                    [Append-only file logs]
```

---

## What Gets Logged

### Every Ticket

```json
{
    "event_id": "evt_20250729_001",
    "timestamp": "2025-07-29T14:30:00Z",
    "event_type": "ticket_created",
    "ticket_id": "TKT-20250729-042",
    "submitted_by": "user@customer.com",
    "role": "admin",
    "priority": "P2",
    "action": "reboot_device",
    "target": "switch-01",
    "sanitized": true,
    "original_text_hash": "sha256:a1b2c3..."
}
```

### Every LLM Interaction

```json
{
    "event_id": "evt_20250729_002",
    "timestamp": "2025-07-29T14:30:05Z",
    "event_type": "llm_request",
    "ticket_id": "TKT-20250729-042",
    "model": "deepseek-chat",
    "prompt_tokens": 2341,
    "response_tokens": 187,
    "cost_usd": 0.0084,
    "confidence": 0.97
}
```

### Every Job Execution

```json
{
    "event_id": "evt_20250729_003",
    "timestamp": "2025-07-29T14:30:10Z",
    "event_type": "job_executed",
    "ticket_id": "TKT-20250729-042",
    "job_action": "reboot_device",
    "job_target": "switch-01",
    "job_params": {"scheduled_at": "2025-07-30T02:00:00Z"},
    "exit_code": 0,
    "duration_ms": 4523,
    "execution_log_hash": "sha256:d4e5f6..."
}
```

---

## Log Storage

### SQLite Database (Structured)

```sql
-- /opt/barenoc/volumes/db/barenoc.db
-- Table: audit_log

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL,
    ticket_id TEXT,
    actor TEXT NOT NULL,
    data JSON NOT NULL,
    previous_hash TEXT,
    sha256_hash TEXT NOT NULL
);

-- Immutability triggers
CREATE TRIGGER prevent_audit_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is immutable - updates not allowed');
END;

CREATE TRIGGER prevent_audit_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log is immutable - deletes not allowed');
END;

-- Index for queries
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX idx_audit_ticket ON audit_log(ticket_id);
CREATE INDEX idx_audit_type ON audit_log(event_type);
```

### File Logs (Unstructured)

```bash
/opt/barenoc/volumes/logs/audit/
├── 2025-07-29/
│   ├── TKT-001.json
│   ├── TKT-001-llm-prompt.txt
│   ├── TKT-001-llm-response.txt
│   ├── TKT-001-job.json
│   ├── TKT-001-execution.log
│   └── TKT-001-result.json
├── 2025-07-30/
│   └── ...
└── ...
```

### Tamper Evidence

Each audit event stores the SHA-256 hash of the previous event, creating a hash chain:

```
Event 1: hash = sha256(data1 + "0")
Event 2: hash = sha256(data2 + event1.hash)
Event 3: hash = sha256(data3 + event2.hash)
...
```

Verification script:

```python
def verify_audit_chain(db_path: str) -> bool:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT event_id, data, previous_hash, sha256_hash FROM audit_log ORDER BY id")
    prev_hash = None
    for row in cursor:
        event_id, data, previous_hash, sha256_hash = row
        if previous_hash != (prev_hash or "0"):
            print(f"Chain broken at {event_id}")
            return False
        expected = hashlib.sha256((data + (prev_hash or "0")).encode()).hexdigest()
        if expected != sha256_hash:
            print(f"Hash mismatch at {event_id}")
            return False
        prev_hash = sha256_hash
    return True
```

---

## Log Retention

| Log Type | Retention | Action After Retention |
|----------|-----------|----------------------|
| Audit DB | 90 days | Archived to compressed file |
| Audit file logs | 90 days | Compressed with gzip |
| Execution logs | 30 days | Deleted |
| LLM prompts/responses | 90 days | Archived |
| System logs (syslog) | 30 days | Rotated by logrotate |

---

## Query Examples

### Recent Escalations

```sql
SELECT * FROM audit_log
WHERE event_type = 'escalation'
ORDER BY timestamp DESC
LIMIT 20;
```

### LLM Cost Report

```sql
SELECT date(timestamp) as day,
       model,
       COUNT(*) as requests,
       SUM(cost_usd) as total_cost
FROM audit_log
WHERE event_type = 'llm_request'
GROUP BY day, model
ORDER BY day DESC;
```

### User Activity

```sql
SELECT actor,
       COUNT(*) as actions,
       COUNT(DISTINCT ticket_id) as tickets
FROM audit_log
WHERE timestamp >= datetime('now', '-7 days')
GROUP BY actor
ORDER BY actions DESC;
```

---

## Audit Viewer, Export & Chain-Verify (v2026.08.25.b+)

The appliance ships a **user-facing audit viewer** (sidebar → **Audit Log**):

- **View** — every event with actor, action, target, and timestamp, newest
  first; per-ticket and per-actor filters.
- **Export** — JSON download of the log (`/api/v1/audit-log/export`) for
  offline review / auditor handoff.
- **Chain-verify** — recomputes each row's hash against its recorded
  predecessor and reports `{ok, count, broken_at, …}`; a tampered row shows
  up as a broken link at the exact point of modification.

### Compliance toggle

The `Audit log` control (Settings → Security) gates recording: when off,
`log_event` is a no-op and nothing is written (the viewer shows empty). Keep
it **on** for regulated workspaces. The toggle is part of the **Compliance
baseline** preset (audit = on) and is included in the **attestation snapshot**
export (state + enabled-since + settings hash).

See also: [Compliance Controls — Operator & Auditor Guide](../customer/compliance_controls.md)
