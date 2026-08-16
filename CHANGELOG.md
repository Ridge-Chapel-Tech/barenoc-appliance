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

## [2026.08.16.h] — 2026-08-16

### Fixed
- **Stalled auto-executed jobs** — a dispatched job that never reported back
  left the ticket silently in_progress; a watchdog now escalates it after
  10 min ("the agent runner may be down"), visible in the thread and fed
  back to the AI (budget-bounded).
- **Support bundle includes the host-side agent runner log** (tail, redacted)
  — the first place to look when a job stalls; the runner log was previously
  invisible to bundles.
- **agent_failed shows in the chat thread** (was hidden).

## [2026.08.16.g] — 2026-08-16

### Added
- **Live ticket status in chat** — "🔄 Status" on an open ticket answers
  "where are you at?" (derived stage from work notes + idle time) without
  creating new work or interrupting the technician.
- **Settings → General → Parallel agent jobs** — `MAX_CONCURRENT` is now a
  UI field (1–8, default 2; applies on runner restart).
- **Update now is hidden until available** — the Updates card only shows the
  button after a Check now finds a release.

- **NOC_Agent P1b — Linux adoption + job loop** — the endpoint agent
  (`agent-go/`) now installs via a one-command `agent_install.sh` (step-ca
  enroll by fingerprint → config → systemd `noc-agent.service` →
  capability-gated sudoers) and runs a pull/execute/result job loop against a
  new appliance-side transport (`POST /api/v1/device/jobs/pull` +
  `/jobs/result`, backed by a `device_jobs` table scoped by cert CN). Action
  set: `collect_logs`, `reboot` (confirm-gated), `check_updates`
  (read-only), `report_facts`. Nonce dedupe + offline buffering via local
  SQLite state. No SSH involved.

## [2026.08.16.f] — 2026-08-16

### Fixed
- **"Error loading tickets" after approving an escalated ticket** — the
  ticket row's cost render (`(llm_cost_usd || '0').toFixed(6)`) threw a
  TypeError whenever cost was 0.0 or null, killing the whole list render.
- **Device cards show "Last seen"** (local time).
- **Chat gains a Dashboard button** in the header (mobile-friendly return).
- **LLM escalation reason** shows "(empty response)" instead of an empty
  string when the model returns no content.

## [2026.08.16.e] — 2026-08-16


### Fixed
- **UniFi settings save 422'd ("Cannot save unifi settings")** — the /config
  route decorator bound to a helper instead of set_config after a refactor;
  every save failed with `env_path required`. Route-binding regression tests
  added.
- **Scheduler 401 storm on fresh installs** — credentials file bind-mount
  went stale when the path was a directory at container start ("Is a
  directory"); now mounts the parent directory so the file resolves at
  runtime.
- **First-boot nginx cert race** — the TLS cert is now generated BEFORE the
  first `compose up` (nginx crash-looped without it: "API not healthy after
  60s" + [emerg] cert errors on fresh installs).
- **Scan Network scanned the wrong subnet** — discovery defaulted to
  192.168.0.0/24; now derived from APPLIANCE_IP, pinned by the installer, and
  configurable in Settings → General → Discovery subnets.
- **System page firmware-status 400 spam** — only queried when UniFi is
  configured.
- **Update UX** — progress bar clears after completion; version refreshes in
  the card + nav; "Update now" auto-arms on load.

## [2026.08.16.d] — 2026-08-16

### Fixed
- **Self-update checksum step broke EVERY update** — `sha256sum -c
  --ignore-missing` compared the release asset name against the
  locally-renamed `app.tar.gz` → "no file was verified" → exit 1 →
  "checksum mismatch" on every in-app update, silently on pre-.b builds
  (this is why the first in-app update attempts appeared to "do nothing").
  Now compares the downloaded file's hash directly against the sums value;
  stale update_request files are cleared on failure.
- **"Update now" button stayed disabled until a manual "Check now"** — the
  Updates card now auto-checks the manifest on load so the button arms
  itself.
- **Support-bundle download named `=.md`** — Content-Disposition filename
  parse fixed (filename*/filename= aware).

## [2026.08.16.c] — 2026-08-16

### Ops
- **Update-path verification build (v2026.08.16.c)** — first in-app
  self-update test on the reference appliance (.207): exercises versions.json
  → Update now → host self-update service → apply → version flip. No
  functional changes; the version marker (System page / Updates card /
  /api/v1/health) is the proof of apply.

## [2026.08.16.b] — 2026-08-16

### Added
- **Update progress + notifications** — the self-update flow now reports live
  stages (snapshot → download → verify → backup → apply → rebuild →
  healthcheck → done/failed) with a progress bar on the dashboard Updates
  card (polls 3 s while in flight; Update-now disabled mid-update). Email
  notification (ALERT_RECIPIENTS) on completion/failure — once per
  transition, persisted across scheduler restarts. Update results now carry
  `services_restarted` + `reboot_required` for the UI (reboot-aware hook).

## [2026.08.16.a] — 2026-08-16

### Added
- **Customer support bundle** — System page → "Support / Bug Report": export a
  redacted diagnostic markdown bundle to attach to a bug report. Contents:
  version, system snapshot, redacted app-config presence, safe-field device
  inventory, ticket summary, redacted audit trail, error-signal scan, and
  container log tails (via the Docker engine socket). Secrets are scrubbed
  before export (API keys, tokens, passwords, certs, private keys; .env is
  presence-only with a safe-value whitelist).
- **NOC_Agent P1a (Go endpoint agent skeleton)** — `agent-go/` module: mTLS
  self-report over the existing `/api/v1/device/report` channel (facts: OS,
  kernel, hostname, MACs, IPs, uptime, disk), config, Linux systemd install
  script, unit tests. Appliance-side: `device_report` accepts agent reports
  (`adoption_method="agent"`, stores `agent_version`/`agent_capabilities`/
  `facts_json`), no SSH credentials provisioned for agent devices; plain cert
  heartbeats stay `method="cert"` (backward compatible). Idempotent schema
  migration for the new Device columns.

### Ops
- **CI now gates the agent** — `setup-go` in CI + `go build/vet/test` wired
  into `run_tests.sh` (portable go discovery).

## [2026.08.16] — 2026-08-16

### Fixed
- **Installer: storage auto-detect** — `local-lvm` is no longer hard-coded.
  The installer now prefers `local-lvm` → `local-zfs` → the first storage
  that holds VM images, so ZFS-based Proxmox installs (and any other naming)
  work without editing the script (`--storage` still overrides; failures now
  list the available storages).
- **Installer: SSH key auto-detect** — `--ssh-key` is no longer required: the
  installer picks `~/.ssh/id_ed25519.pub` → `~/.ssh/id_rsa.pub` → any
  `~/.ssh/*.pub`, and tells you the `ssh-keygen` command if none exist.
- **UniFi sync no longer crashes with a cryptic "JSON.parse" error** — nginx
  `proxy_read_timeout` for `/api/` raised from the 60 s default to 300 s
  (heavy syncs can exceed the old limit and returned an HTML 504 page), and
  the Devices page now reads the response as text and surfaces the real HTTP
  status + body when it isn't JSON.

### Added
- **UniFi: remove a stored API key or password** — Settings → UniFi now has
  "Remove stored …" actions (previously an API key once set could only be
  removed by hand-editing `.env`; it also silently shadowed password auth).
- **Progress feedback on device scanning** — both "Scan Network" (ping) and
  "Sync Now" (UniFi) show a spinner + elapsed seconds, disable the button
  while running, and end in a clear ✓/✗ state.

### Changed
- **Test Connection now saves the form first** — the test endpoint reads the
  *saved* config, so a freshly typed URL/key was silently ignored before.

### Docs
- **Install guide: no GitHub CLI needed** — removed the `gh auth login` step
  (the release repo is public; a plain `git clone` works). Fixed the SSH-key
  prerequisite (any key type, auto-detected) and documented `--storage`
  auto-detection.

## [2026.08.15] — 2026-08-15

### Added
- **Paged first-run wizard (`/setup`)** — one step per page with enforced
  order (account first), Skip where applicable, self-healing sessions
  (cookie fallback + auto-heal + sign-in banner) and a 6-hour wizard token.
- **NAS backup mounts** — BareNOC mounts your SMB/NFS share itself (no root,
  no SSH): Connect/Disconnect in the wizard + Settings, credential file
  0600, reconnect on reboot, guest support. `/opt/barenoc/backups` bind is
  rw so the target test is honest.
- **Self-protection invariant** — the appliance may never harm itself or take
  itself offline, in any profile: worker patterns/devices, runner
  pre-dispatch + target deny, API creds self-block, Lily's hard rule, and
  pi-agent no longer in the docker group.
- **GUI onboarding** — Windows: self-elevating .bat driving a native WinForms
  progress dialog (no console). macOS/Linux: native result dialogs.
- **Self-verifying onboarding handshake** — the installer fetches trust + DNS
  from the appliance itself (`/onboard/info`, `/onboard/root-ca.crt`), writes
  /etc/hosts, enrolls the cert, and reports back to prove adoption
  ("✅ Handshake verified — BareNOC adopted this device as …").
- **Downloads-page onboarding** — pick your OS and download the script right
  from the Downloads page.
- **Rate limiting** (login/chat/api, env-configurable, in-memory fixed-window
  middleware) and a parallel agent runner (`MAX_CONCURRENT`).
- **Greeting handling** — chat replies conversationally to a bare hello;
  follow-ups dispatch to Lily; no-intent replies are friendly, not legalistic.

### Changed
- Fleet counts (dashboard/system) include certificate-adopted devices.
- `/login` no longer loops back to the wizard once the admin account is
  claimed; wizard-first routing requires an unclaimed account.
- Demo fleet/tickets seeded only with `SEED_DEMO=true` (appliance installs
  start clean).
- Onboarding scripts enable sshd + open the firewall (firewalld/ufw) and
  replace stale /etc/hosts entries.

### Fixed
- **stepca TLS** — the nginx stepca vhost served the SAN-less intermediate CA
  cert, breaking every device enrollment; deploy.sh now issues a leaf server
  cert with SANs (deployed to both installs).
- Agent-credentials provisioning waits for the api + verifies the login (no
  more silent 401-flood class).
- Fresh-install gaps: `pi-work` created/owned for pi-agent, `llm_provider.json`
  root:pi-agent 0640 (pi: "No API key found for deepseek"), sqlite3 in the
  provision (local backups were silently broken), cert-adopted devices now
  register the control key so the agent can SSH them.
- Chat: greeting replies render as bubbles; follow-up requests are no longer
  swallowed by a stale-title greeting.

### Security
- pi-agent removed from the docker group (self-protection); device-credential
  fetch refuses the appliance itself; appliance-identity endpoint for the
  agent.

## [2026.08.13] — 2026-08-13

### Added
- **Free & open (beta)** — licensing removed entirely: no activation keys, no
  gating. Updates are open; everyone downloads BareNOC. Paid support is the
  only thing that's separate (bareNOC.com).
- **Mobile chat front door (`/chat`)** — phone-first page for home users and
  tenants: sign in or create a guest account, chat with the Queue Manager,
  reply/retry/close tickets from your phone.
- **Tenant role** — first login = admin, every self-registered account = tenant.
  Tenants see only their own tickets and devices (hard isolation), can adopt
  their own devices, and can only close their own tickets. Admin can promote
  anyone (Settings → Users). Self-registration toggle: `TENANT_REGISTRATION_ENABLED`.
- **Restrictions (hard denies)** — Settings → Restrictions: blocked actions,
  blocked devices, and blocked request phrases. Enforced even in Autonomous
  mode, before the AI ever reads a blocked request.
- **First-run setup wizard (`/setup`)** — one-sitting onboarding: admin account,
  LLM key, timezone, site name, alert email, autonomy profile, backups, adopt
  your first device, and share the chat URL (QR included).
- **step-ca device identity (Phase F)** — internal CA, one-time enrollment
  JWTs, short-lived mTLS device certs, self-service `/onboard` portal.
- **UniFi integration** — controller login, auto-discover/adopt, clients/devices/
  ports, VLAN create, port config, firewall read — all behind the action catalog.

### Changed
- `system_time` action now emits JSON and resolves to a human-readable answer
  (timezone + uptime) in tickets instead of raw output.
- Timezone now reaches the AI runner end-to-end (runner runs as `pi-agent`,
  which can't read the 0600 `.env`; the worker carries TZ in the job file).

### Fixed
- Worker image missing `restrictions.py` in its build context (COPY trap).
- `system_time` reported "tz unset" through the pipeline even when TZ was set.

### Docs
- Deployment guide, update pipeline, and versioning docs updated for the
  open/beta model.

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

- **Device adoption via certificates (Phase F)** — adopt a device with a
  short-lived step-ca certificate: one-time enrollment token (JWT signed by
  BareNOC's provisioner key), device enrolls with step-cli, first mTLS report
  links it (badge 🔐), instant revocation (report 403s even with a valid
  cert). Internal CA + provisioner live; nginx requires client certs from the
  CA root on the device API surface.

- **`enroll_device` action** — adopt an SSH-reachable Linux device via a
  ticket: the agent mints the enrollment token, ships step-cli, enrolls the
  cert + installs a renew/report heartbeat cron; the device links itself over
  mTLS. (Phase F agent path; UI path landed earlier.)
- **DNS service + appliance identity (first-run)** — CoreDNS split-horizon
  container (`barenoc-dns`): the appliance answers `app.barenoc.com` +
  `*.barenoc.local` names from its own hosts block and forwards everything
  else; Settings → Identity block shows the hostname/IP + the rendered
  A-record, deploy.sh writes the Corefile from `APPLIANCE_IP`/`APPLIANCE_HOST`.
- **Multi-subnet + SNMP discovery** — `DISCOVERY_SUBNETS` (comma-separated
  CIDRs; legacy `DISCOVERY_SUBNET` fallback) + `DISCOVERY_MAX_HOSTS_PER_SUBNET`;
  new `snmp_sweep.sh` agent action (routers/switches/APs by community string)
  posts results to `/devices/snmp-sweep-results` (device-type guess +
  `snmp-discovered` tag). Docs: `docs/architecture/terminology.md`.
- **Devices UI self-serve** — fingerprint v2 (nmap service/version scan +
  SSH-banner OS identification + vendor + reverse-DNS + TTL fallback);
  “Generate appliance key” button + Credentials modal teaching the dedicated
  `barenoc` user and **command-scoped sudo**; status filter also applies to
  the Unclaimed section; cert-adopted devices count as *controlled*.
- **Fixed — SSH control default user**: the Claim / Add Device / Manage
  Credentials forms now default the SSH user to **`barenoc`** (the dedicated
  control account the onboarding flows create) instead of `root`; the agent
  runner's last-resort fallback matches. macOS/Windows keep their
  admin-user guidance.
- **Fixed — scoped sudo was a sudoers parse error**: the generated
  `/etc/sudoers.d/barenoc` used bare command names (`reboot, shutdown, …`)
  — sudoers requires **fully-qualified paths**, so every device onboarded via
  the UI os-setup or `/onboard` got an invalid sudoers entry (no passwordless
  sudo at all). Now fully-qualified (`/usr/bin/cp, /usr/sbin/reboot,
  /usr/sbin/shutdown, /usr/bin/apt, /usr/bin/apt-get, /usr/bin/journalctl,
  /usr/bin/install, /usr/bin/systemctl, /usr/bin/tail, /usr/bin/curl`); cp
  included so the appliance can sync its own agent runner when adopted as a
  device.
- **Fixed — SSH keys must end with a newline**: `control_key.py` stripped the
  private key, so every key served by the Credentials modal / stored by
  `_store_ssh_key` lacked the trailing newline — and ssh-keygen/ssh reject
  ed25519 keys without it (`error in libcrypto` on OpenSSL 3.0). Fixed at all
  three touchpoints (generation, storage, and the runner's temp-key write,
  which also repairs already-stored keys).
- **`src/scripts/fix-device-sudoers.sh`** — device-side repair script: rewrites
  `/etc/sudoers.d/barenoc` with fully-qualified paths for devices that got the
  old bare-name entry and validates with `visudo -c`.
- **Fixed — agent could not report job results**: `POST /jobs/result` was
  `require_role("operator")` (the Aug-5 agent-role split missed it — `agent`
  has no rank in the operator hierarchy, so every runner result post 403'd
  and dispatched tickets never left `in_progress`). Now
  `require_any_role("operator", "admin", "agent")`; verified end-to-end.
- **Changed — `collect_logs.sh`**: non-root control users (`barenoc`) now
  collect the SYSTEM journal via `sudo -n journalctl` (scoped sudo entry);
  root still connects directly; default SSH user is now `barenoc`.
- **Fixed — ticket/device timestamps shown in viewer's local time**: the
  Tickets/Dashboard/Devices pages rendered the naive-UTC `created_at` strings
  raw (e.g. 12:03 UTC read as if local). New `fmtTz()` helper renders them in
  the browser's timezone (naive UTC → `Z` → `toLocaleString()`), applied to
  ticket rows, work-note timestamps, dashboard recent tickets, and device
  detail. Verified: `2026-08-07 12:03:34` UTC → `8/7/2026 8:03:34 AM` EDT.
- **Fixed — ticket sort broke on mixed timestamp formats**: `created_at` is a
  naive string; `T`-separated ISO rows sort ahead of space-separated ones as
  strings. Lists now sort with `datetime(created_at)` (parses both), id DESC
  tiebreaker.
- **`src/scripts/device_ssh.sh`** — first-class endpoint control for the
  autonomous agent: logs in as the agent, resolves ip→device, fetches the
  DECRYPTED stored key via `GET /devices/{id}/credentials` (never touches key
  files on disk), writes a 0600 temp key (trailing newline), runs ssh.
- **`apply_patch.sh` (was `patch_debian.sh`)** — OS-flavor update check:
  detects apt/dnf/yum/apk/zypper on the target, `sudo -n` for non-root
  control users, never installs, and surfaces denied-sudo honestly (no stderr
  swallowing). Results render readably in tickets. Verified live against the
  Fedora laptop: `dnf check-update` → 256 updates listed in the ticket.
- **Scoped sudo covers every major OS flavor** — `SUDO_SCOPED` now includes
  `dnf`, `yum`, `apk`, `zypper` (package managers) and `log` (macOS), in
  `onboard.py`, the UI os-setup, `fix-device-sudoers.sh`, and the appliance's
  own sudoers.
- **Fixed — autonomous agent's device visibility**: `_device_inventory_context`
  listed only `unifi_managed` devices, so cert/SSH-adopted hosts were
  invisible to the agent (root cause of the "it's a phone" misidentification).
  Now every claimed device with its control channels ([unifi]/[ssh]/[cert]).
  The pi-task context also gains an operations guide (fetch creds via the API,
  cross-check the inventory before declaring a host unmanaged, sshd
  source-vs-destination, OS flavors, no fabricated blockers).
- **Easter egg** — Settings → Autonomy Policy → "Compliance tooling": a
  toggle that does nothing; its label tracks the profile (autonomous =
  "De-escalate Rogue AI Sentience", balanced = "Restrict Sub-Optimal User
  Behaviors", strict = "Throttle Unrealistic Request Vectors").
- **/onboard heartbeats report hostname** — adopted devices self-identify;
  `device_report` stores it (fixes the NULL-hostname case).
- **Live work notes in tickets** — while a pi task runs, the runner polls the
  agent's session transcript and relays its brief status messages to the
  ticket as `agent_progress` work notes (1-3 lines, min 8s apart, capped at
  15). New `POST /api/v1/tickets/{id}/progress` (agent-allowed) + the pi-task
  system prompt now asks the agent to keep a short live work log. The UI
  renders progress notes subtly italic. Verified live: 5 progress notes
  streamed during a real run, then the final answer.
- **Ticket assigned column shows the assistant's name** — `assigned_to` stored
  internal names (pi-agent/ai-tech/human-tech/customer/system); TicketResponse
  maps them to display names at serialization (agent → `BOT_ASSISTANT_NAME`,
  default Lily), so the UI never shows the pi-agent service account.
- **Fixed — runner orphans in-flight jobs on restart**: jobs are moved to
  `running/` before executing, but nothing scanned it at startup, so a restart
  mid-job stranded the file forever (ticket stuck `in_progress`, silent).
  Startup now re-queues any `running/*.json` back to `incoming/`. Verified
  live: a stranded pi task was recovered and completed with live notes.
- **Self-service onboard portal (`/onboard`)** — a workstation user with NO
  BareNOC access visits `/onboard` (URL or QR), downloads a per-OS
  (Linux/macOS/Windows) script, and with their own admin rights: creates the
  `barenoc` control user + authorizes the appliance key + scoped sudo,
  installs step-cli **from the appliance** (nginx static route, darwin
  arm64/amd64 builds fetched once by deploy.sh), bootstraps the CA, enrolls a
  short-lived cert via `/onboard/token` (15-min JWT, CN must match
  `device-*`), and installs a renew+report heartbeat — the device
  self-registers via its first mTLS report. No tech required per machine.

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
