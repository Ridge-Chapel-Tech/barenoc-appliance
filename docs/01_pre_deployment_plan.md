# BareNOC — Pre-Deployment Architecture Plan

**Version:** 1.0  
**Target:** Single-server Network Operations Center for residential through 50-employee SMB  
**Last Updated:** 2026-08-05

> **Reference deployment note:** this plan targets the 9-VLAN / `10.X.*`
> site-ID scheme. The live home deployment (and this repo's examples) uses a
> simpler single LAN — `192.0.2.0/24`, BareNOC VM `192.0.2.207`, UniFi
> gateway `192.0.2.1` — with the same application architecture.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Standardized VLAN & Subnet Architecture](#2-standardized-vlan--subnet-architecture)
3. [System Architecture Overview](#3-system-architecture-overview)
4. [Component Deep Dive](#4-component-deep-dive)
5. [Database Schema](#5-database-schema)
6. [Token Queue Manager — Flow & Logic](#6-token-queue-manager--flow--logic)
7. [Security Model & Firewall Requirements](#7-security-model--firewall-requirements)
8. [Hardware Sizing & Storage Layout](#8-hardware-sizing--storage-layout)
9. [Network Port & Protocol Matrix](#9-network-port--protocol-matrix)

---

## 1. Executive Summary

BareNOC is a portable, self-contained Network Operations Center that runs on a single Ubuntu Server deployed inside a client network. It autonomously monitors infrastructure, executes software patches, manages a prioritized ticket queue, and dispatches remediation jobs to a local Pi Coding Agent — all governed by a token-aware budget that routes routine work to fast/cheap LLMs and escalates outages to deep-reasoning models.

**Core Design Principles:**

| Principle | Implementation |
|-----------|---------------|
| **Infrastructure managed, nothing extra** | Only necessary services run; no bloat, no telemetry exfiltration |
| **Portable & repeatable** | Single `docker compose up -d` after `.env` configuration |
| **Token-aware economics** | Every LLM call is tracked, budgeted, and routed by cost/urgency |
| **Autonomous with human checkpoints** | P1/P2 auto-executed; P3/P4 optionally require approval; touch-maintenance alerts when physical intervention needed |
| **Single binary agent** | Pi Coding Agent is the sole execution engine for all automated remediation |

---

## 2. Standardized VLAN & Subnet Architecture

The following VLAN scheme is the BareNOC deployment standard, designed to work for both residential and small business (≤50 employees) environments. The second octet encodes the **site ID** — use `0` for the first deployment, incrementing for each additional client.

### 2.1 VLAN Assignment Table

| VLAN ID | Name | Subnet (Site `X`) | DHCP | Purpose |
|---------|------|-------------------|------|---------|
| **1** | Native/Default | — | ❌ | Disabled on all trunk ports; never used |
| **10** | `MGMT` | `10.X.10.0/24` | ✅ | UniFi gateway, switches, APs, **BareNOC host** |
| **20** | `STORAGE` | `10.X.20.0/24` | ✅ | NAS devices, backup targets, NFS/SMB shares |
| **30** | `SECURITY` | `10.X.30.0/24` | ✅ | UniFi Protect cameras, NVR, door access controllers |
| **40** | `AUTOMATION` | `10.X.40.0/24` | ✅ | IoT hubs, Home Assistant, environmental sensors, smart plugs |
| **50** | `WIRELESS-MAIN` | `10.X.50.0/24` | ✅ | Primary trusted WiFi (employees/residents); RADIUS-authenticated |
| **60** | `WIRELESS-GUEST` | `10.X.60.0/24` | ✅ | Guest isolation; internet-only; client isolation enforced; captive portal optional |
| **70** | `WIRELESS-KIDS` | `10.X.70.0/24` | ✅ | Parental controls, DNS filtering, time-based schedules |
| **80** | `WIRED-USERS` | `10.X.80.0/24` | ✅ | Desktop workstations, docking stations, printers |
| **90** | `SERVERS` | `10.X.90.0/24` | ✅ | On-prem application servers, self-hosted services (non-UniFi) |
| **99** | `TRANSIT` | `10.X.99.0/24` | ❌ (static) | Site-to-site VPN, static routes, inter-VLAN routing exceptions |

### 2.2 Inter-VLAN Firewall Rules (UniFi)

| From VLAN | To VLAN | Action | Rationale |
|-----------|---------|--------|-----------|
| `MGMT(10)` | ALL | ✅ Allow All | Management VLAN has full reachability |
| `STORAGE(20)` | `MGMT(10)`, `SERVERS(90)` | ✅ Allow | NAS access only where needed |
| `SECURITY(30)` | `MGMT(10)`, `STORAGE(20)` | ✅ Allow | Camera footage to NVR storage |
| `AUTOMATION(40)` | `MGMT(10)` | ⚠️ Allow (limited) | IoT hub reachability; block internet for noisy devices |
| `WIRELESS-MAIN(50)` | `STORAGE(20)`, `SERVERS(90)`, `WIRED-USERS(80)` | ✅ Allow | Normal user access |
| `WIRELESS-GUEST(60)` | Internet only | ✅ Allow | **Block all inter-VLAN and intra-VLAN traffic** |
| `WIRELESS-KIDS(70)` | Internet (filtered) | ✅ Allow | DNS filter via UniFi content filtering |
| `WIRED-USERS(80)` | `STORAGE(20)`, `SERVERS(90)`, `WIRELESS-MAIN(50)` | ✅ Allow | Normal workstation access |
| `SERVERS(90)` | `STORAGE(20)`, Internet (limited) | ⚠️ Allow | Server-to-NAS and controlled outbound |
| ALL | `MGMT(10):443,22` | ✅ Allow (limited) | UniFi Controller and BareNOC Web UI access for all users |

### 2.3 BareNOC Host Addressing

The BareNOC server **must** reside on the Management VLAN (10):

| Parameter | Value |
|-----------|-------|
| VLAN | 10 (MGMT) |
| Static IP | `10.X.10.250/24` |
| Gateway | `10.X.10.1` (UniFi Gateway) |
| DNS | `10.X.10.1` (or dedicated DNS at `10.X.10.2`) |
| Hostname | `barenoc` |

---

## 3. System Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                        CLIENT NETWORK BOUNDARY                        │
│                                                                       │
│  ┌─────────┐     ┌──────────────────────────────────────────────┐    │
│  │  UniFi   │     │           BARENOC HOST (Ubuntu 24.04)        │    │
│  │ Gateway  │◄───►│  ┌────────────────────────────────────────┐ │    │
│  │ (UCG)    │ SSH │  │          DOCKER COMPOSE STACK           │ │    │
│  └─────────┘     │  │  ┌──────────┐  ┌──────────┐            │ │    │
│                   │  │  │ barenoc- │  │ barenoc- │            │ │    │
│  ┌─────────┐     │  │  │   api    │  │  worker  │            │ │    │
│  │  UniFi   │     │  │  │ :8443    │  │ (queue   │            │ │    │
│  │ Switches │◄───►│  │  │ FastAPI  │  │  engine) │            │ │    │
│  │  & APs   │SNMP │  │  └────┬─────┘  └────┬─────┘            │ │    │
│  └─────────┘     │  │       │              │                   │ │    │
│                   │  │  ┌────┴──────────────┴─────┐            │ │    │
│  ┌─────────┐     │  │  │   SQLite DB + Job Queue  │            │ │    │
│  │ Endpoint│     │  │  │   /opt/barenoc/volumes/   │            │ │    │
│  │ Devices │◄───►│  │  └──────────────────────────┘            │ │    │
│  │(SSH/Win)│     │  │                                          │ │    │
│  └─────────┘     │  │  ┌──────────────────────────┐            │ │    │
│                   │  │  │    barenoc-scheduler     │            │ │    │
│                   │  │  │    (cron: digest emails) │            │ │    │
│                   │  │  └──────────────────────────┘            │ │    │
│                   │  └────────────────────────────────────────┘ │    │
│                   │                                              │    │
│                   │  ┌────────────────────────────────────────┐ │    │
│                   │  │   HOST SERVICES (systemd)              │ │    │
│                   │  │   ┌──────────────────────────────────┐ │ │    │
│                   │  │   │  pi-agent-runner.service         │ │ │    │
│                   │  │   │  Watches /opt/barenoc/jobs/      │ │ │    │
│                   │  │   │  Spawns: pi <job-context>        │ │ │    │
│                   │  │   └──────────────────────────────────┘ │ │    │
│                   │  └────────────────────────────────────────┘ │    │
│                   └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘

EXTERNAL (Internet):
  ┌──────────────┐     ┌──────────────┐
  │  DeepSeek API │     │  Gmail SMTP   │
  │  (LLM)        │     │  (Alerts)     │
  └──────────────┘     └──────────────┘
```

### 3.1 Data Flow Summary

```
1. UniFi topology poll (every 60s) ──► barenoc-worker
2. Health check results ──► Ticket auto-generation (if down)
3. Ticket Queue ──► Priority sort ──► Token budget check
4. Budget OK? ──► Select model (Flash vs Pro) ──► Write job file
5. pi-agent-runner picks up job ──► Executes `pi`
6. Pi Agent output ──► Results parsed ──► Ticket updated
7. Scheduled cron ──► Morning/EOD digest email ──► Gmail SMTP
```

---

## 4. Component Deep Dive

### 4.1 `barenoc-api` — FastAPI Web Backend

| Aspect | Detail |
|--------|--------|
| **Framework** | FastAPI (Python 3.12) |
| **Server** | Uvicorn, 4 workers, behind Nginx reverse proxy |
| **Templating** | Jinja2 server-side templates |
| **Frontend Interactivity** | HTMX + Alpine.js (no build step; delivered via CDN) |
| **CSS** | Tailwind CSS (CDN, or self-hosted for air-gapped deployments) |
| **Authentication** | Bcrypt-hashed passwords; JWT stored in `httpOnly`, `Secure`, `SameSite=Strict` cookie |
| **API Surface** | REST endpoints for dashboards, ticket CRUD, settings; WebSocket for live agent logs |
| **Port** | Internal `8000`; exposed via Nginx on `443` |

**Key Endpoints:**

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/api/auth/login` | None | Authenticate, receive JWT cookie |
| `POST` | `/api/auth/logout` | Any | Clear session |
| `GET` | `/api/dashboard/health` | Any | Infrastructure health data (JSON) |
| `GET` | `/api/dashboard/tokens` | Any | Token usage & budget data |
| `GET` | `/api/dashboard/tickets` | Any | Ticket queue data |
| `POST` | `/api/tickets` | Admin, Tech | Create manual ticket |
| `PATCH` | `/api/tickets/{id}` | Admin, Tech | Update ticket (approve/close/re-prioritize) |
| `GET` | `/api/tickets/{id}/logs` | Any | Live agent execution logs (SSE stream) |
| `GET` | `/api/settings` | Admin | Retrieve system settings |
| `PUT` | `/api/settings` | Admin | Update system settings |
| `GET` | `/api/users` | Admin | List users |
| `POST` | `/api/users` | Admin | Create user |
| `DELETE` | `/api/users/{id}` | Admin | Delete user |
| `GET` | `/api/llm-providers` | Admin | List configured LLM backends |
| `PUT` | `/api/llm-providers` | Admin | Update LLM provider config |
| `GET` | `/api/devices` | Any | Onboarded devices list |
| `POST` | `/api/devices` | Admin, Tech | Onboard new device |
| `GET` | `/ws/agent-logs/{ticket_id}` | Any | WebSocket for streaming Pi Agent output |

### 4.2 `barenoc-worker` — Queue Engine

The worker is a long-running Python process that polls the SQLite `tickets` table every 5 seconds.

**Responsibilities:**
1. **Topology Polling:** Every 60 seconds, query the UniFi Controller API for device status. If a device transitions to DOWN, auto-create a P2 ticket.
2. **Ticket Dispatch:** For each `queued` ticket, sorted by priority (P1 first) and age:
   - Check daily token budget remaining
   - Select model: `deepseek-chat` for P3/P4; `deepseek-reasoner` for P1/P2 or retries
   - Write a job JSON file to `/opt/barenoc/jobs/incoming/`
   - Update ticket status to `in_progress`
3. **Job Completion Watcher:** Monitor `/opt/barenoc/jobs/completed/` for result files. Parse output, update ticket with logs and status (`completed` or `failed`).
4. **Escalation on Failure:** If a Flash-model job fails, re-queue at Pro-model automatically (up to 2 retries total).
5. **Touch Maintenance Detection:** If Pi Agent output contains the marker `[TOUCH_MAINTENANCE]`, flag the ticket and trigger email to the on-site contact list.

### 4.3 `barenoc-scheduler` — Cron Digest Service

A lightweight container running `supercronic` (or a simple Python `schedule` loop) that fires:

| Schedule | Action |
|----------|--------|
| **07:00 daily** | **Morning Critical Systems Digest** — Queries all device statuses, lists overnight ticket activity, flags P1/P2 unresolved, sends HTML email to configured recipients |
| **18:00 daily** | **End-of-Day Operational Summary** — Resolved ticket count, token consumed today, patches applied, open items, cost incurred |
| **Every 5 min** | Token budget soft-warning check — if >80% of daily $0.50 threshold consumed, send alert email to Admins |

### 4.4 `pi-agent-runner` — Pi Agent Bridge (systemd)

Runs directly on the Ubuntu host (not containerized) to have full access to SSH keys, host networking, and the Pi Coding Agent binary.

**Operation Loop:**
```
while true:
    for job_file in /opt/barenoc/jobs/incoming/*.json:
        move job_file → /opt/barenoc/jobs/running/
        parse job_file → extract ticket_id, prompt, model, context
        execute: pi --model <model> --prompt "<prompt>" --context-file <context>
        capture stdout/stderr → write result.json to /opt/barenoc/jobs/completed/
```

---

## 5. Database Schema

All state is stored in a single SQLite file at `/opt/barenoc/volumes/db/barenoc.db`. SQLite was chosen deliberately — no external database dependency, zero-config backups, and sufficient performance for a single-server NOC managing hundreds of devices and tickets.

### 5.1 `users`

```sql
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK(role IN ('admin', 'technician', 'observer')),
    email           TEXT,
    display_name    TEXT,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
```

### 5.2 `email_recipients`

```sql
CREATE TABLE email_recipients (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER REFERENCES users(id) ON DELETE CASCADE,
    receives_morning_digest INTEGER DEFAULT 0,
    receives_eod_summary    INTEGER DEFAULT 0,
    receives_touch_maint    INTEGER DEFAULT 0,
    custom_email_override   TEXT    -- if set, use this instead of users.email
);
```

### 5.3 `devices`

```sql
CREATE TABLE devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    device_type     TEXT NOT NULL CHECK(device_type IN (
                        'gateway', 'switch', 'ap', 'camera', 'endpoint_linux',
                        'endpoint_macos', 'endpoint_windows', 'nas', 'other'
                    )),
    ip_address      TEXT,
    mac_address     TEXT,
    vendor          TEXT,
    model           TEXT,
    vlan_id         INTEGER,
    uniFi_device_id TEXT,       -- UniFi Controller's internal ID (NULL if non-UniFi)
    ssh_credential  TEXT,       -- JSON: {"username":"...", "key_path":"..."} encrypted at rest
    winrm_credential TEXT,      -- JSON: {"username":"...", "password":"..."} encrypted at rest
    status          TEXT DEFAULT 'unknown' CHECK(status IN ('online','offline','degraded','unknown')),
    last_seen       TEXT,
    onboarded_at    TEXT DEFAULT (datetime('now')),
    notes           TEXT
);

CREATE INDEX idx_devices_status ON devices(status);
CREATE INDEX idx_devices_type ON devices(device_type);
```

### 5.4 `tickets`

```sql
CREATE TABLE tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT,
    priority        TEXT NOT NULL CHECK(priority IN ('P1','P2','P3','P4')),
    status          TEXT DEFAULT 'queued' CHECK(status IN (
                        'queued', 'pending_approval', 'approved',
                        'in_progress', 'completed', 'failed', 'cancelled'
                    )),
    category        TEXT DEFAULT 'general' CHECK(category IN (
                        'outage', 'patching', 'software_install', 'config_change',
                        'health_check', 'general', 'touch_maintenance'
                    )),
    device_id       INTEGER REFERENCES devices(id),
    created_by      INTEGER REFERENCES users(id),
    assigned_model  TEXT,       -- 'deepseek-chat' or 'deepseek-reasoner'
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 2,
    token_used      INTEGER DEFAULT 0,
    cost_incurred   REAL DEFAULT 0.0,
    requires_approval INTEGER DEFAULT 0,
    approved_by     INTEGER REFERENCES users(id),
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    completed_at    TEXT
);

CREATE INDEX idx_tickets_status ON tickets(status);
CREATE INDEX idx_tickets_priority ON tickets(priority);
CREATE INDEX idx_tickets_created ON tickets(created_at);
```

### 5.5 `ticket_logs`

```sql
CREATE TABLE ticket_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
    log_level   TEXT DEFAULT 'info' CHECK(log_level IN ('debug','info','warning','error','critical')),
    log_entry   TEXT NOT NULL,
    timestamp   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_ticket_logs_ticket ON ticket_logs(ticket_id);
```

### 5.6 `token_usage`

```sql
CREATE TABLE token_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,      -- YYYY-MM-DD
    model_name      TEXT NOT NULL,
    tokens_input    INTEGER DEFAULT 0,
    tokens_output   INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0,
    ticket_id       INTEGER REFERENCES tickets(id),
    recorded_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_token_usage_date ON token_usage(date);
```

### 5.7 `llm_providers`

```sql
CREATE TABLE llm_providers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,   -- e.g., "DeepSeek Primary"
    endpoint_url    TEXT NOT NULL,
    api_key         TEXT NOT NULL,          -- Encrypted at rest
    model_name      TEXT NOT NULL,          -- e.g., "deepseek-chat"
    model_type      TEXT NOT NULL CHECK(model_type IN ('fast', 'reasoning')),
    is_primary      INTEGER DEFAULT 0,
    is_active       INTEGER DEFAULT 1,
    daily_token_cap INTEGER DEFAULT 2000000,
    created_at      TEXT DEFAULT (datetime('now'))
);
```

### 5.8 `settings`

```sql
CREATE TABLE settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Default settings seeded on first run
-- See 02_iac_and_setup_manifests.md for the seed list
```

---

## 6. Token Queue Manager — Flow & Logic

### 6.1 Ticket Lifecycle State Machine

```
                    ┌─────────┐
                    │ QUEUED  │
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
         (auto-dispatch) │     (requires_approval=1)
              │          │          │
              ▼          │          ▼
        ┌──────────┐     │    ┌──────────────────┐
        │IN_PROGRESS│     │    │PENDING_APPROVAL │
        └─────┬─────┘     │    └────────┬─────────┘
              │           │             │
      ┌───────┼───────┐   │     ┌───────┴────────┐
      │       │       │   │     │                │
      ▼       ▼       ▼   │     ▼                ▼
 ┌────────┐┌──────┐┌──────┐│ ┌──────────┐  ┌───────────┐
 │COMPLETED││FAILED││RETRY ││ │ APPROVED │  │ CANCELLED │
 └────────┘└──┬───┘└──┬───┘│ └────┬─────┘  └───────────┘
              │       │     │      │
              │  ┌────┘     │      └──► IN_PROGRESS
              │  │          │
              ▼  ▼          │
         (retry_count       │
          < max_retries)    │
              │             │
         ┌────┴────┐        │
         │ Fast→Pro│        │
         │ Escalate│        │
         └────┬────┘        │
              │             │
              ▼             │
           QUEUED           │
         (re-queued)        │
                            │
         (retries exhausted)│
              │             │
              ▼             │
           FAILED           │
         (terminal)         │
```

### 6.2 Model Selection Algorithm

```
FUNCTION select_model(ticket, daily_usage):
    IF ticket.priority IN ('P1', 'P2'):
        RETURN 'deepseek-reasoner'    // Outages always get reasoning

    IF ticket.retry_count > 0:
        RETURN 'deepseek-reasoner'    // Retries escalate

    IF daily_usage.cost_today > DAILY_SOFT_CAP * 0.9:
        RETURN 'deepseek-chat'        // Budget running low, stay fast

    IF ticket.category IN ('config_change', 'patching'):
        RETURN 'deepseek-reasoner'    // Destructive changes get review

    // Default for P3/P4 routine
    RETURN 'deepseek-chat'
```

### 6.3 Token Budget Tracking

| Threshold | Action |
|-----------|--------|
| Cost > $0.50/day | 🟡 Soft warning — admin email alert, but jobs continue |
| Cost > $2.00/day | 🔴 Hard cap — queue pauses; only P1 tickets dispatched with reasoning model |
| Tokens > 1,800,000/day (90% of 2M cap) | 🟠 All P3/P4 jobs deferred to next day |

Cost calculation uses DeepSeek's published API pricing:
- **deepseek-chat (Flash):** $0.14 / 1M input tokens, $0.28 / 1M output tokens
- **deepseek-reasoner (Pro):** $0.55 / 1M input tokens, $2.19 / 1M output tokens

The 2,000,000 token daily cap at Flash prices ≈ $0.28–$0.56/day theoretical max. Combined with the $0.50 soft warning, this keeps operations extremely lean.

---

## 7. Security Model & Firewall Requirements

### 7.1 Host Firewall (`ufw`)

The BareNOC host runs `ufw` with a **default-deny inbound** policy:

```bash
ufw default deny incoming
ufw default allow outgoing

# Management access (from MGMT VLAN only)
ufw allow from 10.X.10.0/24 to any port 22 proto tcp    # SSH

# Web UI (HTTPS only, accessible from all internal VLANs)
ufw allow from 10.X.0.0/16 to any port 443 proto tcp

# Docker internal (never exposed externally)
# No rules needed — docker0 bridge is NAT'd internally
```

### 7.2 UniFi Firewall Rules

All inter-VLAN routing must pass through the UniFi Gateway firewall. See Section 2.2 for the complete rules matrix.

### 7.3 Secrets Management

| Secret Type | Storage | Encryption |
|-------------|---------|------------|
| LLM API Keys | `settings` table in SQLite | AES-256-GCM via `cryptography` Fernet; key stored in `.env` |
| SSH Private Keys | `/opt/barenoc/volumes/secrets/ssh/` | Filesystem permissions `0600`; never stored in DB |
| Endpoint Credentials | `devices.ssh_credential` / `devices.winrm_credential` | JSON-blob encrypted with Fernet |
| JWT Signing Secret | `.env` → `JWT_SECRET` | 64-byte random; generated at deploy time |
| Gmail App Password | `.env` → `SMTP_PASSWORD` | `.env` file permissions `0640`, owned by `barenoc` user |

### 7.4 JWT Configuration

| Parameter | Value |
|-----------|-------|
| Access Token Lifetime | 60 minutes |
| Refresh Token Lifetime | 7 days |
| Cookie | `barenoc_session` — `httpOnly=True`, `Secure=True`, `SameSite=Strict` |
| Algorithm | HS256 |
| Payload | `{"sub": user_id, "role": role, "exp": ..., "iat": ...}` |

---

## 8. Hardware Sizing & Storage Layout

### 8.1 Minimum & Recommended Specs

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **CPU** | 2 vCPU (x86-64) | 4 vCPU |
| **RAM** | 4 GB | 8 GB |
| **Disk** | 40 GB SSD | 120 GB SSD |
| **Network** | 1 GbE, VLAN trunk (management VLAN native) | Same |

### 8.2 Storage Layout

```
/opt/barenoc/                       # Root (dedicated partition or directory)
├── .env                            # Environment variables (0640)
├── docker-compose.yml              # Stack definition
├── volumes/
│   ├── db/
│   │   └── barenoc.db              # SQLite database (single file)
│   ├── logs/
│   │   ├── api/                    # Uvicorn access/error logs
│   │   ├── worker/                 # Queue worker logs
│   │   ├── scheduler/              # Cron job output
│   │   └── agent/                  # Pi Agent execution logs (per-ticket)
│   ├── secrets/
│   │   └── ssh/                    # SSH keys for endpoint access (0600)
│   └── nginx/
│       ├── nginx.conf              # Main Nginx config
│       └── certs/
│           ├── barenoc.crt         # Self-signed or Let's Encrypt
│           └── barenoc.key
├── jobs/                           # Pi Agent job bridge (shared with host)
│   ├── incoming/                   # Worker writes, agent-runner picks up
│   ├── running/                    # Currently executing jobs
│   └── completed/                  # Results with exit codes & logs
└── backups/                        # Daily DB dumps (rotated, 7-day retention)
```

---

## 9. Network Port & Protocol Matrix

| Port | Protocol | Source | Destination | Purpose |
|------|----------|--------|-------------|---------|
| **22** | TCP/SSH | MGMT VLAN (`10.X.10.0/24`) | BareNOC Host | Admin SSH access |
| **443** | TCP/HTTPS | All internal VLANs (`10.X.0.0/16`) | BareNOC Host | NOC Web Portal |
| **443** | TCP/HTTPS | BareNOC Host | `api.deepseek.com` | LLM API calls |
| **443** | TCP/HTTPS | BareNOC Host | UniFi Gateway (`10.X.10.1`) | UniFi Controller REST API |
| **22** | TCP/SSH | BareNOC Host | UniFi Gateway (`10.X.10.1`) | UniFi OS SSH (backup CLI access) |
| **22** | TCP/SSH | BareNOC Host | Endpoint devices | Linux/macOS patching |
| **5986** | TCP/WinRM | BareNOC Host | Windows endpoints | Windows patching |
| **161** | UDP/SNMP | BareNOC Host | Switches, APs, printers | SNMP health polling |
| **587** | TCP/SMTP | BareNOC Host | `smtp.gmail.com` | Email alert delivery |
| **53** | UDP/DNS | BareNOC Host | `10.X.10.1` | DNS resolution |

---

## Next Steps

Proceed to **`02_iac_and_setup_manifests.md`** for the complete Docker Compose stack, environment configuration, and Pi Agent runner systemd service.
