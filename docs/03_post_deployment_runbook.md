# BareNOC — Post-Deployment Operational Runbook

**Version:** 1.0  
**Audience:** NOC Administrators & Technicians  
**Last Updated:** 2025-07-11

---

## Table of Contents

1. [First Login & Initial Configuration](#1-first-login--initial-configuration)
2. [User Management & RBAC](#2-user-management--rbac)
3. [Device Onboarding Workflow](#3-device-onboarding-workflow)
4. [Ticket Lifecycle & Queue Management](#4-ticket-lifecycle--queue-management)
5. [Dashboard Reference](#5-dashboard-reference)
6. [Touch Maintenance Protocol](#6-touch-maintenance-protocol)
7. [Daily Email Automation](#7-daily-email-automation)
8. [LLM Provider Management](#8-llm-provider-management)
9. [Backup & Disaster Recovery](#9-backup--disaster-recovery)
10. [Troubleshooting & FAQ](#10-troubleshooting--faq)
11. [Maintenance Procedures](#11-maintenance-procedures)

---

## 1. First Login & Initial Configuration

### 1.1 Access the Web Portal

1. From any device on the internal network, navigate to:
   ```
   https://192.0.2.207
   ```
   (Replace `X` with your site ID; `0` for the first deployment.)

2. Accept the self-signed TLS certificate warning. (For production, replace with a Let's Encrypt certificate — see Section 11.2.)

3. Log in with the default credentials:
   | Field | Value |
   |-------|-------|
   | **Username** | `admin` |
   | **Password** | `barenoc` |

4. **You will be forced to change the password on first login.** Choose a strong password (minimum 12 characters, mixed case, numbers, and symbols).

### 1.2 Initial System Configuration

Navigate to **Settings** (gear icon, top-right nav bar) and complete:

**Email transport:** the default is **vendor-managed (out-of-the-box)** —
alert/digest email goes via BareNOC's notify service (Resend) from
`noreply@notify.barenoc.com` (shown as your site name; reply-to is your
address). The table below is the **your own SMTP (self-hosted)** override for
customers who want email to stay on their own Gmail/company server.

| Setting | Description | Example |
|---------|-------------|---------|
| **Site Name** | Display name for this BareNOC instance | `"Acme Corp NOC"` or `"Smith Residence"` |
| **Site ID** | Must match `.env` `SITE_ID` | `0` |
| **Timezone** | IANA timezone string | `America/Chicago` |
| **SMTP Host** | Gmail SMTP relay | `smtp.gmail.com` |
| **SMTP Port** | Submission port | `587` |
| **SMTP Username** | Gmail address | `alerts@example.com` |
| **SMTP Password** | Gmail App Password (16 chars) | `xxxx xxxx xxxx xxxx` |
| **From Address** | Envelope-from for alerts | `barenoc@example.com` |
| **Morning Digest Time** | When to send the morning report | `07:00` |
| **EOD Summary Time** | When to send the evening report | `18:00` |

**Verify Email:** Click **"Send Test Email"** to confirm SMTP delivery. Check the configured Gmail inbox.

### 1.3 Verify UniFi Connectivity

1. Navigate to **Dashboard → Infrastructure Health**.
2. You should see the UniFi Gateway listed as a device within 60 seconds of the first topology poll.
3. If the gateway shows `unknown` or `offline`, check:
   - UniFi host IP in `.env` (`UNIFI_HOST`)
   - UniFi credentials (the `barenoc` user must exist in UniFi OS with Administrator role)
   - Network reachability from the BareNOC Docker network (`docker exec barenoc-worker ping 10.X.10.1`)

---

## 2. User Management & RBAC

### 2.1 Creating Users

**Path:** Settings → Users → **Add User**

| Field | Description |
|-------|-------------|
| Username | Login name (lowercase, no spaces) |
| Display Name | Full name shown in UI |
| Email | For notifications (can be overridden) |
| Role | `admin`, `technician`, or `observer` |
| Password | Set initial password (user will be prompted to change on first login) |

### 2.2 Role Permissions Matrix

| Capability | Admin | Technician | Observer |
|-----------|:-----:|:----------:|:--------:|
| View Health Dashboard | ✅ | ✅ | ✅ |
| View Token Dashboard | ✅ | ✅ | ✅ |
| View Ticket Queue | ✅ | ✅ | ✅ |
| Create Manual Tickets | ✅ | ✅ | ❌ |
| Approve Pending Tickets | ✅ | ✅ | ❌ |
| Cancel Tickets | ✅ | ✅ | ❌ |
| Onboard Devices | ✅ | ✅ | ❌ |
| Edit Device Credentials | ✅ | ❌ | ❌ |
| Modify System Settings | ✅ | ❌ | ❌ |
| Manage Users | ✅ | ❌ | ❌ |
| Configure LLM Providers | ✅ | ❌ | ❌ |
| View Agent Execution Logs | ✅ | ✅ | ✅ |
| Export Reports | ✅ | ✅ | ❌ |

### 2.3 Email Notification Assignment

For each user, configure which notification categories they receive:

| Notification | Recommended Recipients |
|-------------|----------------------|
| **Morning Critical Systems Digest** | All Admins, Lead Technicians |
| **End-of-Day Operational Summary** | All Admins, All Technicians |
| **Touch Maintenance Alerts** | On-site contact person; may differ from NOC staff |

A user's notification email can be overridden (e.g., send Touch Maintenance to `facilities@client.com` even if the user's email is `admin@msp.com`).

---

## 3. Device Onboarding Workflow

### 3.1 UniFi Devices (Automatic)

UniFi-managed devices (gateways, switches, APs, cameras) are **auto-discovered** every 60 seconds via the UniFi Controller API. No manual onboarding is needed.

Auto-onboarded devices appear in the Devices table with:
- `device_type`: `gateway`, `switch`, `ap`, or `camera`
- `unifi_device_id`: UniFi's internal MAC-based identifier
- `status`: live-polled (`online`, `offline`, `degraded`)

### 3.2 Endpoint Devices (Manual)

For non-UniFi endpoints (Linux/macOS workstations, Windows PCs, printers, NAS):

**Path:** Dashboard → Devices → **Onboard Device**

1. **Fill in device details:**

   | Field | Required? | Notes |
   |-------|-----------|-------|
   | Name | ✅ | Friendly name, e.g., `"Alice-MacBook-Pro"` |
   | Device Type | ✅ | `endpoint_linux`, `endpoint_macos`, `endpoint_windows`, `nas`, `other` |
   | IP Address | ✅ | Static IP or DHCP reservation recommended |
   | MAC Address | — | For identification |
   | VLAN ID | — | Which VLAN the device resides on |

2. **Provide credentials (if endpoint patching is desired):**

   **Linux/macOS (SSH key-based):**
   - Generate a dedicated SSH key pair for BareNOC:
     ```bash
     ssh-keygen -t ed25519 -f /opt/barenoc/volumes/secrets/ssh/barenoc_endpoint -C "barenoc-endpoint"
     ```
   - Copy the public key to the endpoint:
     ```bash
     ssh-copy-id -i /opt/barenoc/volumes/secrets/ssh/barenoc_endpoint.pub user@endpoint-ip
     ```
   - In the onboarding form, set:
     - SSH Username: `user`
     - SSH Key Path: `/opt/barenoc/volumes/secrets/ssh/barenoc_endpoint`

   **Windows (WinRM):**
   - Enable WinRM on the Windows endpoint (PowerShell as Admin):
     ```powershell
     Enable-PSRemoting -Force
     Set-Item WSMan:\localhost\Client\TrustedHosts -Value "192.0.2.207"
     ```
   - In the onboarding form, set:
     - WinRM Username: `DOMAIN\username` or `.\localuser`
     - WinRM Password: (encrypted at rest)

3. **Click "Onboard."** The device appears in the device list. Its initial status will be `unknown` until the next health check cycle.

### 3.3 Device Health Checks

| Device Type | Check Method | Healthy If | Interval |
|-------------|-------------|-----------|----------|
| UniFi Gateway/AP/Switch | UniFi Controller API | `state = ONLINE` | 60s |
| Linux/macOS endpoint | SSH `echo OK` + system load | SSH exit 0, load < 80% | 300s |
| Windows endpoint | WinRM `Test-Connection` | Responds within 10s | 300s |
| NAS | Ping + port check (445/2049) | Ping OK, port open | 300s |
| Printer | SNMP GET `sysUpTime` | SNMP responds | 600s |

A device that fails **3 consecutive** health checks auto-generates a P2 ticket titled `"Device Offline: <device_name>"`.

---

## 4. Ticket Lifecycle & Queue Management

### 4.1 Ticket Priority Definitions

| Priority | Description | Auto-Approved? | Model Used | Examples |
|----------|-------------|:---:|------------|----------|
| **P1 — Critical Outage** | Network down, gateway unreachable, core switch failure | ✅ Yes (auto-execute) | `deepseek-reasoner` | Internet outage, UCG failure, core switch down |
| **P2 — Major Degradation** | Partial outage, AP offline affecting >5 clients, camera NVR down | ✅ Yes (auto-execute) | `deepseek-reasoner` | Single AP down, high latency, VLAN routing issue |
| **P3 — Routine Maintenance** | Software updates, package installs, non-urgent config changes | ⬜ Configurable | `deepseek-chat` | `apt upgrade`, macOS update, install new package |
| **P4 — Low Priority** | Cosmetic, informational, optional improvements | ⬜ Configurable | `deepseek-chat` | Firmware version check, disk usage report, log review |

### 4.2 Creating a Manual Ticket

**Path:** Dashboard → Ticket Queue → **New Ticket**

1. Select priority (P1–P4).
2. Enter a title and description. Be specific — the Pi Agent receives this as its prompt.
3. Choose a category (e.g., `patching`, `software_install`, `general`).
4. Optionally link to a specific device.
5. If the ticket should wait for human approval before execution, check **"Requires Approval"**.
6. Click **Submit**.

### 4.3 Approving a Ticket

For tickets marked `requires_approval = true`:

1. Open the ticket from the queue.
2. Review the description and any linked device.
3. Click **Approve** or **Cancel**.
4. Approved tickets transition to `queued` and are picked up by the worker on the next dispatch cycle.

**Shortcut:** The setting `auto_approve_p3_p4` (default: `true`) automatically approves all P3 and P4 tickets without human intervention. Disable this in Settings if all non-outage tickets should require approval.

### 4.4 Monitoring Execution

1. Click any ticket in the queue to open its detail view.
2. The **"Agent Logs"** panel streams live output from the Pi Coding Agent via WebSocket.
3. Log levels: `info` (white), `warning` (yellow), `error` (red), `critical` (red + bold).
4. Upon completion, the ticket status updates to `completed` or `failed`, and the final output is preserved.

### 4.5 Handling Failures

When a ticket status reaches `failed` after exhausting retries:

1. **Review the agent logs** — the final output includes the Pi Agent's reasoning.
2. **Determine root cause:**
   - Was the prompt unclear? → Refine and resubmit.
   - Was a credential wrong? → Update device credentials and resubmit.
   - Did the endpoint not respond? → Check if powered on, network reachable.
   - Did Pi Agent time out? → Increase `timeout_seconds` in job settings.
3. **Take manual action** if needed, then either:
   - Click **"Retry"** to re-queue (uses reasoning model automatically on retry).
   - Click **"Close as Manual Resolution"** documenting what was done.

---

## 5. Dashboard Reference

### 5.1 Dashboard 1: Infrastructure & Service Health

**Topology View:**
- Interactive tree: Gateway → Switches → APs → Clients
- Color coding: 🟢 Online, 🟡 Degraded, 🔴 Offline, ⚪ Unknown
- Click any device for detail panel (MAC, IP, VLAN, uptime, port status)

**Summary Cards:**
| Card | Shows |
|------|-------|
| Total Devices | Count by status (online/offline/total) |
| UniFi Gateway | WAN IP, uptime, throughput (up/down) |
| Switches | PoE budget used/available, port count |
| APs | Client count per radio, channel utilization |
| Endpoints | Linux/macOS/Windows counts, patch status |

**Auto-Refresh:** Every 60 seconds (configurable).

### 5.2 Dashboard 2: Token Budgeting & Rate Limiting

**Real-Time Gauges:**

| Gauge | Range | Color Zones |
|-------|-------|-------------|
| **Daily Cost ($)** | $0–$2.00 | 🟢 <$0.25, 🟡 $0.25–$0.50, 🔴 >$0.50 |
| **Token Usage** | 0–2,000,000 | 🟢 <50%, 🟡 50–90%, 🔴 >90% |
| **Flash/Pro Split** | 0–100% | Bar chart: Flash vs Pro tokens |

**Usage Table:**
- Per-day breakdown for the last 30 days
- Columns: Date, Flash Tokens, Pro Tokens, Total Tokens, Cost
- **Export CSV** button for billing

**Model Assignment Rules:**
- Editable table showing which conditions trigger which model
- Default rules (see Section 6.2 of the Architecture Plan) can be customized per deployment

### 5.3 Dashboard 3: Ticket Queue & Agent Operations

**Queue Kanban Board:**

| Queued | In Progress | Completed | Failed |
|--------|-------------|-----------|--------|
| Sorted by priority then age | Currently executing | Last 24h | Requires attention |

**Filters:**
- By priority (P1–P4)
- By status
- By category
- By device
- Date range

**Quick Actions per Ticket:**
- ▶️ Force Dispatch (skip queue)
- ⏸️ Hold / Unhold
- ❌ Cancel
- 🔄 Retry (failed tickets)
- 📋 Copy Ticket (duplicate for similar issue)

**Live Agent Log Terminal:**
- Embedded terminal-style log viewer
- Auto-scroll to latest lines
- Search/filter logs
- Download full log as `.txt`

---

## 6. Touch Maintenance Protocol

"Touch Maintenance" refers to tasks that the Pi Coding Agent cannot complete autonomously and require a human to physically interact with hardware.

### 6.1 When Touch Maintenance is Triggered

The Pi Agent includes `[TOUCH_MAINTENANCE]` in its output when it determines any of:

| Scenario | Example |
|----------|---------|
| Power cycling required | "Switch port appears dead — manually power cycle the PoE injector" |
| Physical cabling | "Cable likely disconnected — check patch panel port 12" |
| Hardware replacement | "Switch reports failing PSU — replace unit" |
| On-site button press | "Factory reset required — hold reset button for 10 seconds" |
| Visual inspection needed | "Check LED indicators on AP — should be solid blue" |

### 6.2 The Touch Maintenance Workflow

```
1. Pi Agent outputs [TOUCH_MAINTENANCE]
       │
2. Worker detects the marker in job results
       │
3. Ticket status → "completed" (with touch_maint flag = true)
       │
4. Email sent to Touch Maintenance recipients:
       │   Subject: "[BareNOC] Physical Intervention Required — Ticket #XX"
       │   Body: Ticket title, device name/location, exact instructions
       │
5. Ticket remains visible in the queue with ⚠️ "Awaiting Physical Action" badge
       │
6. On-site person performs the physical action
       │
7. On-site person clicks "Mark Action Complete" link in the email,
   OR technician manually closes the ticket in the Web UI
       │
8. Ticket status → "completed" (fully resolved)
```

### 6.3 Touch Maintenance Email Template

```
Subject: [BareNOC] PHYSICAL INTERVENTION REQUIRED — Ticket #42

Body:
╔════════════════════════════════════════════════╗
║          TOUCH MAINTENANCE REQUIRED            ║
╚════════════════════════════════════════════════╝

Ticket:     #42 — "PoE Injector Failure — AP Office 3"
Device:     UniFi AP Office 3 (10.0.10.47)
Location:   2nd Floor, Office 3, Ceiling Mount

ACTION REQUIRED:
1. Locate the PoE injector labeled "AP-OFFICE3" in the server closet
2. Unplug power for 10 seconds
3. Reconnect power
4. Wait 60 seconds for the AP to reboot
5. Confirm AP LED is solid white

Complete this action? Click here:
    https://192.0.2.207/tickets/42/touch-resolve

---
BareNOC — Automated NOC Operations
```

---

## 7. Daily Email Automation

### 7.1 Morning Critical Systems Digest

**Sent at:** 07:00 (configurable)  
**Recipients:** Users with `receives_morning_digest = true`  
**Template location:** `/opt/barenoc/src/email/templates/morning_digest.html`

**Content:**

```
Subject: [BareNOC] Morning Critical Systems Digest — Mon, Jul 14 2025

┌─────────────────────────────────────────┐
│         OVERNIGHT SUMMARY               │
├─────────────────────────────────────────┤
│ Total checks run:           1,440       │
│ Devices passed:             47/48       │
│ New tickets auto-created:   1           │
│ Tickets resolved overnight: 3           │
│ Open P1/P2 tickets:         0           │
│ Today's token budget:       $0.03/$0.50 │
└─────────────────────────────────────────┘

CRITICAL FLAGS:
  ✅ No P1 or P2 outages detected

DEVICES REQUIRING ATTENTION:
  ⚠️ Office Printer — Offline since 02:14 (Ticket #58, P2)
  🔧 NAS Backup Target — Disk at 92% capacity (Ticket #59, P3)

RECENTLY RESOLVED:
  ✅ Ticket #55 — macOS Security Update applied to Alice-MacBook
  ✅ Ticket #56 — UniFi AP firmware check completed (all current)
  ✅ Ticket #57 — apt upgrade on Dev-Server-01

View full report: https://192.0.2.207/dashboard/health
```

### 7.2 End-of-Day Operational Summary

**Sent at:** 18:00 (configurable)  
**Recipients:** Users with `receives_eod_summary = true`  
**Template location:** `/opt/barenoc/src/email/templates/eod_summary.html`

**Content:**

```
Subject: [BareNOC] End-of-Day Operational Summary — Mon, Jul 14 2025

┌─────────────────────────────────────────┐
│         DAILY OPERATIONAL SUMMARY        │
├─────────────────────────────────────────┤
│ Tickets created:             5          │
│ Tickets resolved:            6          │
│ Tickets failed:              0          │
│ Touch maintenance events:    1          │
│                                              │
│ PATCHES APPLIED:                           │
│   Linux updates:            2 devices      │
│   macOS updates:            1 device       │
│   Windows updates:          0 devices      │
│   UniFi firmware checks:    1 (OK)         │
│                                              │
│ TOKEN USAGE:                               │
│   Flash tokens:            850,000         │
│   Pro tokens:              45,000          │
│   Total cost today:        $0.28           │
│   Month-to-date:           $4.15           │
│                                              │
│ OPEN TICKETS:                              │
│   P1: 0   P2: 1   P3: 2   P4: 1           │
│                                              │
│ TOP DEVICE BY ALERTS:                      │
│   Office Printer — 3 checks failed         │
└─────────────────────────────────────────┘

View full report: https://192.0.2.207/dashboard/tickets
```

### 7.3 Token Budget Warning Alert

**Sent when:** Today's cost exceeds 80% of the $0.50 soft cap  
**Recipients:** All Admin users

```
Subject: [BareNOC] ⚠️ TOKEN BUDGET WARNING — 82% of daily cap reached

Today's cost: $0.41 / $0.50 soft cap
Remaining:   $0.09
Current time: 14:23

Action: P3/P4 tickets will be deferred if the hard cap is reached.
        Consider reviewing active tickets at:
        https://192.0.2.207/dashboard/tokens
```

---

## 8. LLM Provider Management

### 8.1 Viewing Configured Providers

**Path:** Settings → LLM Providers

Shows a table of all configured backends:

| Name | Type | Model | Endpoint | Status | Actions |
|------|------|-------|----------|--------|---------|
| DeepSeek Fast | Fast | `deepseek-chat` | `api.deepseek.com` | 🟢 Active | Edit / Test / Disable |
| DeepSeek Reasoning | Reasoning | `deepseek-reasoner` | `api.deepseek.com` | 🟢 Active | Edit / Test / Disable |

### 8.2 Adding a Fallback Provider

1. Click **"Add Provider"**
2. Fill in:

   | Field | Example (Qwen via OpenRouter) |
   |-------|------------------------------|
   | Name | `Qwen Fallback (OpenRouter)` |
   | Endpoint URL | `https://openrouter.ai/api/v1` |
   | API Key | `sk-or-v1-...` |
   | Model Name | `qwen/qwen-2.5-72b-instruct` |
   | Model Type | `fast` |
   | Is Primary | Unchecked (primary is DeepSeek) |
   | Is Active | Checked |
   | Daily Token Cap | `1000000` |

3. Click **"Test Connection"** to verify the endpoint responds.
4. Click **"Save."**

### 8.3 Fallback Chain Logic

The worker selects models in this order:

```
1. Primary Fast   (DeepSeek Fast)      — for P3/P4
2. Primary Reason (DeepSeek Reasoning)  — for P1/P2/retries
3. Fallback Fast  (Qwen/OpenRouter)    — if Primary Fast errors
4. Fallback Reason (Qwen/OpenRouter)   — if Primary Reason errors
5. FAIL (no available model)           — ticket goes to `failed`
```

The fallback chain is configurable via drag-and-drop reordering in the LLM Providers settings page.

---

## 9. Backup & Disaster Recovery

### 9.1 What to Back Up

| Asset | Path | Frequency | Retention |
|-------|------|-----------|-----------|
| SQLite Database | `/opt/barenoc/volumes/db/barenoc.db` | Daily | 7 days |
| Environment Config | `/opt/barenoc/.env` | On change | 3 versions |
| SSH Keys | `/opt/barenoc/volumes/secrets/ssh/` | On change | 3 versions |
| Nginx Config | `/opt/barenoc/volumes/nginx/` | On change | 3 versions |
| Job History Logs | `/opt/barenoc/volumes/logs/agent/` | Weekly | 30 days |

### 9.2 Automated Backup Script

Save as `/opt/barenoc/backup.sh` and add to crontab (`0 2 * * *`):

```bash
#!/usr/bin/env bash
# BareNOC Daily Backup
set -euo pipefail

BACKUP_DIR="/opt/barenoc/backups"
DB_PATH="/opt/barenoc/volumes/db/barenoc.db"
DATE=$(date +%Y-%m-%d)

# Backup SQLite (safe copy while DB is in use)
sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/barenoc-$DATE.db'"

# Compress
gzip -f "$BACKUP_DIR/barenoc-$DATE.db"

# Rotate: keep last 7 days
find "$BACKUP_DIR" -name "barenoc-*.db.gz" -mtime +7 -delete

echo "[$(date)] Backup complete: barenoc-$DATE.db.gz"
```

### 9.3 Restore Procedure

1. Stop the stack:
   ```bash
   cd /opt/barenoc && docker compose down
   ```

2. Restore the database:
   ```bash
   gunzip /opt/barenoc/backups/barenoc-2025-07-10.db.gz
   cp /opt/barenoc/backups/barenoc-2025-07-10.db /opt/barenoc/volumes/db/barenoc.db
   chown barenoc:barenoc /opt/barenoc/volumes/db/barenoc.db
   ```

3. Restart:
   ```bash
   cd /opt/barenoc && docker compose up -d
   ```

### 9.4 Full Server Recovery

If the BareNOC host itself is lost:

1. Provision a new Ubuntu Server 24.04 instance on the management LAN with
   the appliance's static IP (e.g. `192.0.2.207`; the exact address is
   deployment-specific, not the old `10.X.10.250` placeholder).
2. Install Docker + compose v2 and the Pi Coding Agent runtime for the
   `pi-agent` user (see `02_iac_and_setup_manifests.md` §1–2).
3. Restore `/opt/barenoc` from the latest app-data backup: `.env`, compose
   file, source tree, `volumes/` (DB + secrets + certs) — `restore_app.sh`
   does this (see `operations/backup_and_restore.md`).
4. Restore the latest `barenoc-YYYY-MM-DD.db.gz` if not part of the above.
5. `docker compose up -d --build` (or re-run `deploy.sh` from the dev box).
6. Re-install the `pi-agent-runner` systemd unit + credentials if the VM is
   fresh (`scripts/setup_agent_credentials.sh` runs on deploy).
7. Verify health 200, dashboards load, and UniFi auto-sync begins.

---

## 10. Troubleshooting & FAQ

### 10.1 Common Issues

| Symptom | Probable Cause | Resolution |
|---------|---------------|------------|
| **Login page won't load** | Nginx not running, cert issue | `docker compose logs nginx` |
| **Devices show as "unknown"** | UniFi poll not running or credentials wrong | Check `docker compose logs worker`, verify UniFi creds |
| **Tickets stuck in "queued"** | Agent runner not running | `systemctl status pi-agent-runner` |
| **Tickets fail with timeout** | Pi Agent taking too long | Increase `timeout_seconds` in job settings; check model responsiveness |
| **Emails not sending** | Gmail App Password expired or wrong | Check Settings → SMTP; send test email; regenerate App Password |
| **Token cost spiking** | Too many P1/P2 tickets using reasoning model | Review ticket auto-creation thresholds; adjust health check sensitivity |
| **Database locked errors** | SQLite contention under heavy load | Restart worker; consider migrating to PostgreSQL for >100 endpoints |
| **"Permission denied" on job directory** | Agent runner user mismatch | Ensure `barenoc` user owns `/opt/barenoc/jobs/` |

### 10.2 Useful Diagnostic Commands

```bash
# Check all container status
cd /opt/barenoc && docker compose ps

# Tail all container logs
docker compose logs -f --tail=50

# Tail a specific service
docker compose logs -f worker

# Check agent runner status
systemctl status pi-agent-runner
journalctl -u pi-agent-runner -f

# Check database integrity
sqlite3 /opt/barenoc/volumes/db/barenoc.db "PRAGMA integrity_check;"

# Check LLM spend today (per-model cost from the audit trail — the LLM Monitor
# page shows this in the UI)
sqlite3 /opt/barenoc/volumes/db/barenoc.db \
  "SELECT json_extract(data,'$.model'), SUM(json_extract(data,'$.cost_usd'))
   FROM audit_log WHERE event_type='llm_request'
     AND date(timestamp)=date('now') GROUP BY json_extract(data,'$.model');"

# Check disk usage
du -sh /opt/barenoc/volumes/*
df -h /

# Test UniFi API reachability
docker exec barenoc-worker curl -sk https://10.X.10.1/proxy/network/api/s/default/stat/health
```

### 10.3 FAQ

**Q: Can I run BareNOC on a Raspberry Pi?**  
A: No. The Pi Coding Agent and Docker stack require x86-64 architecture. A Raspberry Pi is the *wrong kind* of "Pi" here — the Pi Coding Agent is an LLM-powered automation tool that runs on standard x86 Linux servers. Use a small form-factor PC or VM instead.

**Q: What happens if the internet goes down?**  
A: BareNOC cannot reach the DeepSeek API, so automated remediation stops. However:
- The web UI remains accessible on the LAN (all dashboards, settings, ticket queue)
- Health polling continues (UniFi and SNMP are local)
- Tickets queue up; they dispatch automatically when the internet returns
- Touch maintenance emails won't send until connectivity is restored (the worker retries SMTP for 24 hours)

**Q: Can BareNOC manage multiple UniFi sites?**  
A: One BareNOC instance manages one UniFi site. For multi-site MSP deployments, deploy one BareNOC instance per site and use a central monitoring dashboard (future feature).

**Q: How do I add custom health checks?**  
A: Custom health checks can be added by creating a script on the host and registering it as a custom check in Settings → Health Checks. The worker will execute it on the configured interval and generate tickets for failures.

**Q: Can I use a different LLM provider?**  
A: Yes, any OpenAI-compatible API endpoint works. Add it in Settings → LLM Providers. The worker abstracts all LLM calls behind a provider interface.

---

## 11. Maintenance Procedures

### 11.1 Updating BareNOC

```bash
cd /opt/barenoc

# Pull latest code (if using git)
git pull origin main

# Rebuild and restart
docker compose down
docker compose up -d --build

# Verify
docker compose ps
```

### 11.2 Replacing Self-Signed Cert with Let's Encrypt

If the BareNOC server has a public DNS name:

```bash
# Install certbot
apt-get install -y certbot

# Stop nginx temporarily
docker compose stop nginx

# Obtain certificate (standalone mode, port 80 must be open temporarily)
certbot certonly --standalone -d barenoc.yourdomain.com

# Copy to BareNOC cert directory
cp /etc/letsencrypt/live/barenoc.yourdomain.com/fullchain.pem \
   /opt/barenoc/volumes/nginx/certs/barenoc.crt
cp /etc/letsencrypt/live/barenoc.yourdomain.com/privkey.pem \
   /opt/barenoc/volumes/nginx/certs/barenoc.key
chown barenoc:barenoc /opt/barenoc/volumes/nginx/certs/*

# Restart nginx
docker compose start nginx

# Auto-renewal cron (monthly):
# 0 0 1 * * certbot renew --quiet && \
#   cp /etc/letsencrypt/live/barenoc.yourdomain.com/fullchain.pem \
#      /opt/barenoc/volumes/nginx/certs/barenoc.crt && \
#   cp /etc/letsencrypt/live/barenoc.yourdomain.com/privkey.pem \
#      /opt/barenoc/volumes/nginx/certs/barenoc.key && \
#   docker compose restart nginx
```

### 11.3 Rotating Secrets

Periodically rotate these secrets (recommended quarterly):

| Secret | Command/Method |
|--------|---------------|
| JWT Secret | Generate new: `openssl rand -hex 32` → update `.env` → `docker compose restart api` |
| Fernet Key | Generate new: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` → update `.env` → re-encrypt stored credentials |
| DeepSeek API Key | Rotate in platform.deepseek.com → update in Settings UI or `.env` |
| Gmail App Password | Revoke old in Google Account → generate new → update Settings UI |
| SSH Endpoint Keys | Generate new keypair → `ssh-copy-id` to all endpoints → update device credentials |

### 11.4 Log Rotation & Cleanup

Logs are automatically managed by Docker's `json-file` driver (10MB max per file, 3 files max per container). Additional cleanup:

```bash
# Purge agent logs older than 30 days
find /opt/barenoc/volumes/logs/agent -name "*.log" -mtime +30 -delete

# Purge old backup files
find /opt/barenoc/backups -name "*.db.gz" -mtime +7 -delete

# Vacuum SQLite (reclaim space)
sqlite3 /opt/barenoc/volumes/db/barenoc.db "VACUUM;"
```

### 11.5 Monitoring BareNOC Health

BareNOC monitors the network — but what monitors BareNOC?

| Check | Method | Action on Failure |
|-------|--------|-------------------|
| Docker containers running | `docker compose ps` (all "Up") | systemd watchdog restarts failed containers |
| Agent runner alive | `systemctl is-active pi-agent-runner` | systemd auto-restarts (Restart=always) |
| Disk space > 90% | `df -h /opt/barenoc` | Log cleanup alerts; email to admins |
| SQLite not corrupt | `PRAGMA integrity_check` | Restore from backup |
| API responding | `curl -k https://localhost/health` | Nginx or uvicorn restart |

Add a simple cron job on the host:

```bash
# /etc/cron.d/barenoc-self-check
*/5 * * * * root /opt/barenoc/self-check.sh
```

---

## Appendices

### A. Quick Reference Card

| Task | Where |
|------|-------|
| Create user | Settings → Users → Add User |
| Onboard device | Dashboard → Devices → Onboard Device |
| Create ticket | Dashboard → Tickets → New Ticket |
| View agent logs | Click ticket → Agent Logs tab |
| Check token budget | Dashboard → Token Budgeting |
| Send test email | Settings → SMTP → Send Test |
| Add fallback LLM | Settings → LLM Providers → Add |
| Restart stack | `cd /opt/barenoc && docker compose restart` |
| View all logs | `docker compose logs -f` |
| Backup DB | `sqlite3 /opt/barenoc/volumes/db/barenoc.db ".backup ..."` |

### B. Glossary

| Term | Definition |
|------|-----------|
| **Pi Coding Agent** | The autonomous LLM-powered agent that executes remediation tasks on the host. Named `pi`, it is invoked by the agent runner. |
| **Flash Model** | Fast/cheap LLM (`deepseek-chat`) used for routine P3/P4 tasks. |
| **Reasoning Model** | Deep/slow LLM (`deepseek-reasoner`) used for P1/P2 outages and retries. |
| **Touch Maintenance** | A task the Pi Agent cannot complete autonomously — requires a human to physically interact with hardware. |
| **Job File** | JSON document written by the worker to `/opt/barenoc/jobs/incoming/` that defines what Pi Agent should do. |
| **Token Budget** | The daily limit on LLM API token consumption, enforced at $0.50 soft cap and $2.00 hard cap. |
| **Site ID** | Integer (`0`, `1`, `2`, ...) encoding the deployment site in the VLAN IP scheme. |

### C. Support & Escalation

For issues with BareNOC itself (not the network it manages):

1. Check logs: `docker compose logs -f`
2. Check agent runner: `journalctl -u pi-agent-runner -n 200`
3. Check database: `sqlite3 /opt/barenoc/volumes/db/barenoc.db "PRAGMA integrity_check;"`
4. Restart stack: `docker compose down && docker compose up -d`
5. Full diagnostics: run `diagnostic.sh` (generated per-deployment) and attach to support ticket.

---

*End of BareNOC Post-Deployment Operational Runbook*
