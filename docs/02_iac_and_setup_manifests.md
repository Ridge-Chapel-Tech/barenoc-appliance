# BareNOC — Infrastructure & Setup Manifests

**Version:** 2.0 (rewritten 2026-08-05 to match the deployed system)
**Requires:** Ubuntu Server 24.04 LTS, Docker Engine 26+ (29.x tested), Docker Compose v2
**Last Updated:** 2026-08-05

> This document describes the system **as deployed**. The older v1 document
> (a `prepare-host.sh` shell installer, a shell `agent-runner.sh`, a `seed.py`
> schema and `Dockerfile.api/.worker/.scheduler` with `version: "3.9"`) no
> longer exists in the repo — none of those files are present. The real
> installer is **`deploy.sh`** at the repo root; the real agent bridge is the
> Python `src/agent/runner.py` under a systemd unit.

---

## Table of Contents

1. [Install Path (what actually happens)](#1-install-path)
2. [Directory Layout on the VM](#2-directory-layout)
3. [Secrets & Configuration Files](#3-secrets--configuration)
4. [Docker Compose Manifest (`src/docker-compose.yml`)](#4-docker-compose-manifest)
5. [Nginx Reverse Proxy (`src/nginx/barenoc.conf`)](#5-nginx-reverse-proxy)
6. [Pi Agent Runner — systemd Service](#6-pi-agent-runner--systemd-service)
7. [Database Schema (SQLAlchemy)](#7-database-schema)
8. [Action Catalog](#8-action-catalog)
9. [Deployment Checklist](#9-deployment-checklist)

---

## 1. Install Path

BareNOC is a **single-node appliance**: a Proxmox VM (or bare Ubuntu server)
running a 7-container Docker stack plus one host-side systemd service.

### 1.1 VM provisioning (manual, one-time)

See `docs/appliance/*` for the hardware/Proxmox/VM guides. On a fresh Ubuntu
24.04 VM, install manually:

```bash
# Docker + compose v2
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker barenoc

# Agent tooling used by the action scripts (host-side)
sudo apt-get install -y nmap snmp snmp-mibs-downloader jq git

# Pi Coding Agent runtime (powers the autonomous "Lily" ticket mode):
# copy the pi-node runtime to /home/pi-agent/.local/share/pi-node, fix the
# `current` symlink, create the pi-agent system user, and ensure `node` is on
# PATH for that user (see SESSION_LOG 2026-08-04 "Pi Coding Agent at the core").
```

### 1.2 Deploy (the actual installer)

From the dev box, `deploy.sh` is the install/update path:

```bash
./deploy.sh [VM]          # default: barenoc@192.0.2.207
```

It (in order): ensures runtime dirs + perms → rsyncs `src/{api,worker,scheduler,
nginx,scripts}` + `client/` to `/opt/barenoc/` → copies the shared modules
(derived from `src/worker/Dockerfile`'s `COPY` lines — the single source of
truth; e.g. `sanitizer.py`, `tierrouter.py`, `ratewindows.py`)
into the worker build context → scp's `src/agent/runner.py` to
`/opt/barenoc/agent/runner.py` (via `sudo -u pi-agent`) if changed → rebuilds
the stack (`docker compose up --build -d`) → waits for `/api/v1/health` 200 →
reloads nginx → re-provisions the `agent` service-account credentials →
restarts `pi-agent-runner`.

> ⚠️ The agent-runner steps need sudo; if the deploy user has no passwordless
> sudo they print a manual-install hint and skip. Standing procedure: after any
> `runner.py` change, install it manually and verify md5s.

### 1.3 Post-install configuration

All in the web UI (Settings): UniFi controller creds + auto-sync interval +
auto-adopt, the active LLM provider (Settings → API Keys), Gmail OAuth2,
timezone, email recipients/schedule, Autonomy Policy profile, Pocket ID
identity, device groups. Every save is audit-logged and written to `.env`
(which the containers hot-read).

---

## 2. Directory Layout

```
/opt/barenoc/
├── .env                     # 0600 barenoc — ALL config/secrets; API rewrites on settings save
├── docker-compose.yml       # = src/docker-compose.yml (deploy syncs it)
├── api/                     # FastAPI app (built into barenoc-api image)
├── worker/                  # poll loop + LLM client (barenoc-worker image)
├── scheduler/               # health checks, UniFi auto-sync, digests (barenoc-scheduler)
├── nginx/barenoc.conf       # bind-mounted reverse-proxy config
├── scripts/                 # host-side + agent action scripts
├── client/                  # Tkinter chat client (served from the Downloads page)
├── agent/
│   ├── runner.py            # pi-agent-runner main (host-side, user pi-agent)
│   └── credentials          # 0600 pi-agent — agent service-account (rotated per deploy)
├── jobs/
│   ├── incoming/            # job files the worker writes, the runner polls
│   ├── running/             # jobs currently executing
│   └── completed/           # results + per-job outputs
├── pi-work/                 # pi session dirs (per-ticket, pi-agent-owned)
├── volumes/
│   ├── db/                  # barenoc.db (+ fernet.key, WAL/SHM)
│   ├── logs/{api,worker,scheduler,agent}/
│   ├── secrets/
│   │   ├── llm_provider.json   # 0640 root:pi-agent — active provider/key (API-written)
│   │   └── ssh/{name}.key      # per-device encrypted SSH private keys
│   ├── nginx/certs/         # TLS cert/key (self-signed; 0640)
│   ├── branding/            # customer logo uploads
│   ├── backup_status/       # host→VM backup health JSON (read-only mount)
│   └── pocket-id/data/      # Pocket ID state
└── backups/                 # app-data backups (cron, every 6h) + restore logs
```

---

## 3. Secrets & Configuration

| File | Perms | Owner | Purpose |
|------|--------|-------|---------|
| `/opt/barenoc/.env` | 0600 | barenoc:docker | All settings + secrets (provider keys, UniFi, email, JWT, policy). The API rewrites it on every Settings save; containers mount it and hot-read. |
| `volumes/secrets/llm_provider.json` | 0640 | root:pi-agent | Active provider/model/api-key for the pi runner; API writes it on settings save + startup. |
| `agent/credentials` | 0600 | pi-agent:pi-agent | `username=agent` / `password=…` service account; created by `scripts/setup_agent_credentials.sh`, rotated every deploy. |
| `volumes/db/fernet.key` | 0600 | root | Fernet key for credential-at-rest encryption (generated on first boot). |
| `volumes/secrets/ssh/*.key` | — | app | Per-device SSH private keys, **encrypted** with Fernet at rest; decrypted only by `GET /api/v1/devices/{id}/credentials` (admin). |
| `volumes/nginx/certs/` | 0640 | — | Self-signed TLS cert/key. |

The canonical key reference lives in **`src/.env.example`** (kept in sync with
the Settings UI). The provider registry is `LLM_PROVIDER_<NAME>_*` blocks with
`LLM_ACTIVE_PROVIDER` selecting the active one; `LLM_PROVIDER_<NAME>_THINKING`
disables chain-of-thought on reasoning models.

---

## 4. Docker Compose Manifest

Source of truth: **`src/docker-compose.yml`** (`version:` key is obsolete and
not used). Five services on `barenoc-net`:

| Service | Image | Exposes | Key mounts |
|---------|-------|---------|------------|
| `api` | built `./api` | 8000 (nginx only) | DB, secrets (rw for llm_provider.json), branding, jobs, `.env`, backups:ro, backup_status:ro, client:ro, `/var/run/docker.sock` |
| `worker` | built `./worker` | — | DB, jobs, secrets:ro, `.env:ro` |
| `scheduler` | built `./scheduler` | — | DB, jobs, `.env:ro`, `agent/credentials:ro` |
| `nginx` | `nginx:1.27-alpine` | **443**, **8443** | `nginx/barenoc.conf:ro`, certs:ro |
| `pocket-id` | `pocketid/pocket-id:v2` | none (nginx-only) | `volumes/pocket-id/data`; `APP_URL=https://pocket-id.barenoc.local:8443` |

Notes:

- The worker image **copies the shared modules from `api/`** (see §1.2) — this
  is done by `deploy.sh`, not the Dockerfile.
- `pocket-id` has no published ports; nginx serves it at **root on 8443** (its
  SPA needs root-absolute paths). UFW stays 443-only for the app.
- Secrets dir is **rw** on the api container (writes `llm_provider.json`) but
  **ro** on worker/scheduler.

### Dockerfiles

- `src/api/Dockerfile` — `python:3.12-slim`, installs `iputils-ping`,
  `uvicorn main:app --port 8000`.
- `src/worker/Dockerfile` — copies worker modules + shared modules,
  `CMD ["python", "-B", "main.py"]`.
- `src/scheduler/Dockerfile` — scheduler entrypoint.

---

## 5. Nginx Reverse Proxy

Source of truth: **`src/nginx/barenoc.conf`**. Two listeners:

- **443** — BareNOC UI + `/api/`; `resolver 127.0.0.11` + `set $api_upstream
  api:8000` so nginx re-resolves the api container's IP per request (the
  stale-upstream bug this fixed: 502s after the api container got a new IP).
- **8443** — Pocket ID at root (`pocket-id:1411`), required for the SvelteKit
  SPA's root-absolute asset paths.

TLS certs are self-signed (`volumes/nginx/certs/`); HSTS + DENY framing
headers are set. Bind-mounted, so `deploy.sh` restarts the nginx container to
pick up config changes.

---

## 6. Pi Agent Runner — systemd Service

Source of truth: **`src/agent/pi-agent-runner.service`** (installed at
`/etc/systemd/system/pi-agent-runner.service`, outside Docker).

```ini
[Service]
Type=simple
User=pi-agent
Group=pi-agent
ExecStart=/usr/bin/python3 /opt/barenoc/agent/runner.py
WorkingDirectory=/opt/barenoc
Restart=always
ProtectSystem=full
ProtectHome=no          # required: the pi runtime lives under /home/pi-agent
CapabilityBoundingSet=CAP_NET_RAW CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_RAW   # ping needs CAP_NET_RAW
```

What the runner does (per poll, 3s, 2 concurrent):

1. Reloads the managed-device cache from the API (names/IPs/MACs/device ids).
2. Picks up job files from `jobs/incoming/` (verify/claim first, then
   discovery, then the rest).
3. For SSH actions (`reboot_device`, `apply_patch`, `collect_logs`,
   `install_chat_client`) it **resolves the target device's stored SSH
   credentials** via `GET /api/v1/devices/{id}/credentials`, writes the
   decrypted key to a 0600 `/tmp/pi-agent-*.key`, and cleans it up after the
   run (explicit job params win; generic `id_ed25519` is the last-resort
   default).
4. For `pi_task` jobs it runs the Pi Coding Agent headlessly
   (`pi -p --provider … --model …`) with the provider/key read live from
   `llm_provider.json`/`.env`, per-ticket session dirs under `/opt/barenoc/pi-work`.
5. POSTs the result to `/api/v1/jobs/result` (now authenticated, operator+).

The runner reads its API credentials from `/opt/barenoc/agent/credentials`
(never hardcoded).

---

## 7. Database Schema

SQLite at `/opt/barenoc/volumes/db/barenoc.db`, defined in **`src/api/models.py`**
(SQLAlchemy; `Base.metadata.create_all` + additive migrations in
`database.py`). Live tables (verified 2026-08-05):

| Table | Purpose |
|-------|---------|
| `users` | login, roles (admin/operator/readonly), bcrypt hashes, OIDC sub, forced-password-change |
| `devices` | inventory: name/ip/mac/type/vendor/model/status, `claimed`, `unifi_managed`, `notify_state_changes`, `device_group`, tags, encrypted snmp/ssh refs, fingerprint, last_poll_data |
| `tickets` | queue: TKT-YYYYMMDD-NNN, priority, status (open/in_progress/awaiting_approval/completed/failed/escalated/closed), action, confidence, LLM metadata, work_notes JSON |
| `audit_log` | hash-chained immutable trail (event_type, actor, data JSON, prev/sha256) |
| `chat_messages` | tech-to-tech AIM-style chat |

There is **no** `token_usage` table anymore — LLM cost/usage lives in
`audit_log` (`llm_request` events) and the LLM Monitor page.

---

## 8. Action Catalog

The AI Technician may only choose from `AllowedAction` in
**`src/api/action_validator.py`** — this enum is the security boundary (the
judge's `ACTION_CATALOG`, the worker's `SYSTEM_PROMPT`, the runner's
`ACTION_SCRIPTS`, and `src/scripts/ticket_context_dump.py` mirror it). See
`docs/security/guardrails.md` for the full list; every action maps to a
host-side script under `/opt/barenoc/scripts/` (ping_check, snmp_poll,
apply_patch, reboot_device, collect_logs, discover, fingerprint, network_info,
unifi_*, install_chat_client).

---

## 9. Deployment Checklist

- [ ] VM: Ubuntu 24.04, static IP, Docker + compose v2, nmap/snmp/jq, pi-node
      runtime for `pi-agent`, `pi-agent-runner.service` installed
- [ ] `src/.env.example` copied to `/opt/barenoc/.env` and filled (or restored
      from backup); `chmod 600`
- [ ] TLS certs in `volumes/nginx/certs/`
- [ ] `./deploy.sh` completes: 7 containers up, health 200, agent runner active
- [ ] First login → change the `admin` password (Settings → Users)
- [ ] Settings: UniFi creds + auto-sync + auto-adopt, LLM provider + key, email
      Gmail OAuth2, timezone, autonomy profile, device groups
- [ ] Agent creds: `agent/credentials` exists (0600 pi-agent), `/jobs/result`
      accepts the agent token
- [ ] Backups: `backup_app.sh` cron installed (every 6h) + Proxmox snapshot
      cron + USB tier (see `docs/operations/backup_and_restore.md`)
- [ ] SAT suite: `docs/system_acceptance_test.md`
