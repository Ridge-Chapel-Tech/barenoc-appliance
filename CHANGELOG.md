# Changelog

All notable changes to BareNOC are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project uses **date-based versioning (CalVer)** — see
`docs/development/versioning.md`: `YYYY.MM` (major), `YYYY.MM.DD` (minor),
`YYYY.MM.DD.a` (same-day release / hotfix). Pre-CalVer history: `0.1.0`.

Categories per release:
- **Added** — new features
- **Changed** — behavior changes / improvements
- **Fixed** — bug fixes
- **Security** — hardening, credential handling
- **Docs** — documentation (local + wiki)
- **Ops** — deployment, backup, tooling

## [2026.08] — 2026-08-05

### Added
- **LLM provider failover chain** — Primary → Secondary → Tertiary
  (`LLM_PROVIDER_ORDER`): automatic failover on error / timeout
  (`LLM_TIMEOUT_S`) / repeated failures (`LLM_PROVIDER_DOWN_AFTER`);
  Hosted vs **On-prem** deployment per provider (Ollama $0); whole-chain
  outage opens a deduped **P1 ticket** + alert email, auto-closed on recovery.
- **Settings → LLM Providers UI overhaul** — failover-order selects,
  deployment selector, remove (✕) per provider, dashed "+ Add provider" card
  (secondary/tertiary/on-prem slots creatable from the UI); removed
  providers are pruned from `.env`.
- **Endpoint adoption & control** — Claim now persists SSH keys (device
  becomes SSH-controlled); new Manage-Credentials modal on Onboarded +
  Monitoring-Only cards (`Creds` / `Add SSH/SNMP`); PATCH `/devices/{id}`
  accepts `snmp_community`/`ssh_user`/`ssh_key`; agent SSH actions use each
  device's stored credentials (runner fetches via the API, 0600 temp key,
  cleaned up per job); `DISCOVERY_SUBNET` env for non-UniFi ping scans.
- **One-shot Proxmox installer** — `proxmox/barenoc-appliance.sh` (cloud
  image + cloud-init + `deploy.sh`; `--profile s|m|l|xl` sizing).
- **BareNOC ISO builder** — `proxmox/build_barenoc_iso.sh` (Ubuntu 24.04
  autoinstall remaster with the embedded app; first boot provisions + deploys).
- **One-line installer bootstrap** — `install.sh` (fetches `versions.json`,
  verifies SHA256, runs the appliance script).
- **Hardware sizing matrix** — `docs/appliance/hardware_sizing.md` (S/M/L/XL).
- **Distribution plan** — `marketing/download_distribution.md`
  (GitHub Releases + R2/B2 + `versions.json` contract).
- **USB backup stick (LUKS)** — `proxmox/setup-usb-backup.sh` + LUKS-aware
  weekly script + locked-state `usb_present` detection + app-backup sync.
- **`unifi_network_create` action** — create a new corporate VLAN/subnet on
  the UniFi controller via tickets: validator (name/vlan 1-4094/CIDR),
  `POST /unifi/networks` (admin-or-agent), agent script, runner wiring,
  judge catalog + executor + prompt action 25, readable in-thread answer.
- **Desktop chat enable toggle** — Settings → General "Enable desktop chat
  client" (`CHAT_CLIENT_ENABLED`, default true); when off, the chat API and
  Downloads return 403 so the client cannot sign in.
- **`barenoc-update.sh`** — VM-side Layer-2 update wrapper (apt + compose
  pull/up + prune + health check; `--dry-run`/`--no-apt`; never auto-reboots)
  with an optional weekly systemd timer install.
- **Settings → Backups** — schedule the encrypted USB backup (Layer 3) from
  the UI: enable / day (daily or Sun–Sat) / hour + **Run USB backup now** +
  live status (stick present, last USB backup, last VM snapshot). The
  Proxmox host reconciles its own cron from the VM every 10 min
  (`proxmox/sync-backup-schedule.sh`); saved schedule applies without
  touching the host. Default = Wednesday 2 AM — set live for tonight's run.

### Security
- **Agent least-privilege split** — new `agent` role (tier-2, distinct from
  operator): the Pi Agent Runner/scripts/scheduler reach exactly the write
  endpoints they need (device credentials, unifi sync/ensure/SSID/port
  writes) via `require_any_role("admin", "agent")`; the agent is no longer
  admin (users/settings/admin-gated routes return 403). Operators still
  CANNOT fetch decrypted SSH keys.
- **Backup archives locked to `0600`** — app-data backups contain `.env`,
  the Fernet key and the DB (password hashes) but were world-readable
  (`0644`); `backup_app.sh` now enforces `0600` + the backups dir is `0700`.
- **`deploy.sh` rsync is content-only** (`-rltz --no-o --no-g`) — a
  root-owned file on the VM (e.g. `tailscale_status.sh`) no longer aborts
  the deploy with chown/chmod EPERM; the db-chown step is best-effort.
- **Secrets & config management doc** — `docs/security/secret_management.md`
  (current posture, production-grade .env split design, rotation cadence).

### Fixed
- `devices.py` background discovery/fingerprint threads crashed (undefined
  `logger` → NameError); discovery now logs "Discovery queued N ping jobs".
- Claiming a device silently dropped the SSH key (key file never written).
- Reboot tickets always escalated: validator required a `scheduled_at` the
  reboot script never honored.
- `generate_report` action was dead end-to-end (no script, no scheduler) —
  removed; validator's `ACTION_SCRIPTS` referenced nonexistent scripts.
- Dead `_ping_device()` removed.
- Worker `use_reasoner` flag was ignored — reasoner model now actually
  selected for P1/P2 tickets.
- Chat (client): QM header shows name + role; replies labeled with her name
  instead of "Bot:"; identity questions ("what's your name?" etc.) answered
  directly — no ticket opened.
- Removed providers disappear from Settings immediately (provider registry
  reads the `.env` file only — the container's process env was keeping
  deleted providers alive until recreate).
- **Internet / ISP link monitor** — alert engine probes the LAN gateway + a
  public host every 60 s (`INTERNET_PROBE_*`): distinguishes ISP/service vs
  link/physical outages; 3 consecutive failures open a deduped **P1
  "Internet connectivity down" ticket** + alert email, auto-closed on
  recovery. (The UniFi gateway stays online during an ISP outage, so device
  monitoring alone never caught it.)
- **Ticket lifecycle (Settings → Tickets)** — per-priority, fully
  configurable: check in with a human/customer every N hours on
  escalated/customer_action tickets (P1 hourly, P2 4 h, P3/P4 daily),
  auto-close resolved tickets after N days (default 3); audit events
  `ticket_checkin` / `ticket_autoclosed`.
- **Dashboard Performance & Reporting** — action time (first response +
  resolution), escalations + rate, closures, reopens, auto-closes, LLM cost;
  Chart.js visuals (created vs resolved, priority mix, resolution by
  priority); **downloads: PDF, Excel (XLSX), OpenOffice (ODS), CSV, Google
  Sheets (clipboard + sheets.new)**.
- **Per-ticket analysis** (CSV/PDF/ODS/XLSX) — time to respond, time to
  escalate, time to close, cost to close (LLM spend), customer replies,
  check-ins, auto-closed flag, assigned-to.
- **Support cost** — AI spend (LLM) + estimated manned-NOC cost (configurable
  rate/hours) + savings.
- **Escalation rate fixed** — now % of tickets created this period that were
  escalated (bounded); raw events + distinct tickets shown separately.
- **Time-to-respond fixed** — measures to the first customer-facing response
- **Report parity** — PDF now matches the spreadsheet exports row-for-row (closed,
  escalation rate, check-ins, daily trend) with consistent "first response (min)"
  labeling.
- **Per-ticket cost** — "Cost to close" uses the authoritative ticket.llm_cost_usd
  first (audit fallback); pi/Lily-path cost metering is a noted follow-up.
  (not the internal pickup note) in minutes; was rounding sub-3-minute
  responses to 0.0 h.
- **Settings "+ Add provider" fixed** — the add card now inserts before a
  stable anchor (was grabbing the first card's Test-Connection button).

### Docs
- `docs/02_iac_and_setup_manifests.md` rewritten to match the deployed
  system; README tree, runbook, update pipeline, appliance docs, architecture
  docs and wiki (devices/discovery/getting-started) brought up to date.

## [0.1.0] — 2026-08-04

### Added
- Pi Coding Agent at the core: autonomous ticket → local agent (Lily) with
  full tools; API-written provider secret file for the runner.
- Customer-facing names: Juniper (Queue Manager) + Lily (AI assistant).
- Threaded chat follow-ups + chat-format replies + double-post guard.
- UniFi auto-adopt (default ON), auto-sync interval, merge of no-MAC
  discovered records with UniFi client identity.
- UniFi action catalog: port config/rename/bounce, client-port lookup,
  firewall rules read, restart via controller, wireless-uplink sync, SSID
  password change.
- LLM retry loop (2-min default), autonomy policy profiles
  (autonomous/balanced/strict), judge/executor two-phase pipeline (opt-in).
- Policy Settings UI + per-deployment PATCH_ALLOWLIST.
- Email: Gmail OAuth2, per-type recipients, morning digest + EOD summary.
- Timezone dropdown; settings change audit logging; GitHub/Google OAuth
  config scaffolding (flows pending credentials).
- Devices page: dashboard counts, topology view (mermaid), notify bell,
  fingerprinting (nmap), "Identified — ready to claim" grouping.
- Chat client (Tkinter), UniFi discovery/fingerprint/SNMP scripts,
  agent service account (no hardcoded admin credentials).

### Security
- `/api/v1/jobs/result` now authenticated (operator+); agent credentials file
  (0600 pi-agent) replaces plaintext admin creds; login page hint removed.

### Ops
- Backup layers 1+2 (SQLite 6h, vzdump daily) with restore drills PASSED;
  USB layer staged for hardware.

[0.1.0]: https://github.com/<org>/BareNOC/releases/tag/v0.1.0
