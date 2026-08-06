# Secrets & Config Management

**Status:** current posture documented; production-grade split is a design
(the open item in the session log). Last updated 2026-08-05.

## Current posture (live, acceptable for single-tenant demo/SMB)

| Item | State |
|------|-------|
| `.env` | single file at `/opt/barenoc/.env`, **`0600`** (deploy.sh enforces) |
| Containers | `env_file: .env` on api/worker/scheduler/pocket-id (every service sees everything) |
| Hot-reload | api/worker/scheduler mount the `.env` file (read-only for worker/scheduler) and re-read it on change — Settings changes apply without restart |
| Provider key | dedicated `volumes/secrets/llm_provider.json` (`0640 root:pi-agent`) — the pi agent never reads the whole `.env` |
| Device creds | Fernet-encrypted at rest |
| Agent identity | `agent` role + `credentials` file `0600` (least-privilege split done 2026-08-05) |
| Backups | app-data archives **`0600`** (was 0644 — fixed 2026-08-05); they contain `.env` + Fernet key + DB |

## Production-grade upgrade (design — build when a Tier-II deployment needs it)

1. **Split secrets from config**
   - `config.env` (0644): TZ, hours, names, ports, flags — nothing sensitive.
   - `secrets.env` (0600): API keys, passwords, refresh tokens, `JWT_SECRET`,
     `ENCRYPTION_KEY`, UniFi creds.
   - All env-reading code (`settings._read_env_file`, `llm_providers.read_env_file`,
     worker/policy.py, runner, scheduler) merges both files (config first,
     secrets overrides). This is the invasive part — every hot-reload path
     must read the same merged view.
   - Settings UI writes secrets → `secrets.env`, config → `config.env`.

2. **Scope secrets per container** (stop `env_file: .env` everywhere)
   - api: full (owns Settings + writes secrets).
   - worker: LLM + policy + retry + DB + secrets file path only.
   - scheduler: UniFi + DB + agent credentials path only.
   - pocket-id: `APP_URL` + `ENCRYPTION_KEY` only.
   - Keep the file mounts for hot-reload, but mount `secrets.env` with
     `:ro` (already done for worker/scheduler on `.env`).

3. **Rotation cadence** (recommended)
   - `JWT_SECRET` + `ENCRYPTION_KEY`: rotate per deployment/trial, not per
     release (rotation invalidates sessions + re-encrypts device creds — a
     maintenance-window op with a restore drill).
   - LLM/UniFi/Gmail keys: rotate on personnel change or suspected exposure;
     Settings → API Keys/Email/UniFi is the rotation UI (masked, audit-logged).
   - The `agent` password rotates automatically on **every deploy**
     (`setup_agent_credentials.sh`, `openssl rand -hex 24`).
   - Document rotation in the per-deployment runbook; the Settings audit log
     records every change (field names, never values).

4. **Archive discipline**
   - App-data backups `0600` (done). Layer-2/3 (vzdump / USB) are already
     root-only; the LUKS USB (Layer 3) keeps offsite copies encrypted at rest.

## Why the split is deferred
- Single-VM, single-tenant deployments get little practical risk reduction
  from the split (the `0600` `.env` already blocks local users; containers
  are root-controlled).
- The merge change touches every hot-reload path and the Settings writer —
  high regression risk for the live appliance; do it as its own
  release with the full test gate, not bundled with feature work.
