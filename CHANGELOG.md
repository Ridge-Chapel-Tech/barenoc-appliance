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

## [2026.08.30.b] — 2026-08-30

### Added
- **Offsite / remote backup (Layer 4):** a new **Settings → Backups → Offsite**
  section with one S3-compatible transport and two flavors — **BareNOC-managed**
  (subscription, plan-key gated) and **bring-your-own** (any R2/B2/MinIO/Synology
  endpoint). The appliance generates a per-install data-encryption key, encrypts
  the app-data archive **before** upload (AES-256-GCM), and shows a **recovery key
  once** (losing it = unrecoverable offsite copy). A daily offsite schedule (its
  own host cron, self-gating on the conf) uploads the latest archive with a
  dependency-light AWS SigV4 signed-request client (no boto3), prunes by retention
  (managed 30d beta), and writes a status record (last ok/failed/size/next-run)
  the Backups UI renders. Restore is a browser **Download a copy** + local decrypt
  with `decrypt_remote_backup.py`. Managed mode is gated by an offline-verifiable
  beta static plan key (`BARENOC_BETA_PLAN_KEY`); the Stripe→webhook→plan-key
  automation stays a separate later lane. Ships `proxmox/setup_omv_remote_backup.sh`
  (MinIO on the OMV box + per-customer bucket/key strategy — gate-run, not executed
  by the worker) and `docs/remote-backup.md`. See the Backups wiki page.
- **Honest AI support spend (cost metering):** the reports KPI now meters pi/Lily
  sessions — the agent runner sums pi's persisted per-message token usage from the
  session JSONL and reports it in the job result, and the API prices it via the
  provider registry (`llm_providers`) into the same `llm_request` audit events the
  catalog path writes. Where pi exposes no usage, a documented chars/4 estimate is
  reported and clearly labeled (never a silent $0.00).

### Changed
- **Reports "AI support spend" KPI:** follows the days dropdown, aggregates metered
  + estimated spend, and counts/labels tickets whose AI work was never metered
  (legacy pi sessions) instead of understating them as $0.00. Ticket detail views
  now show "unknown" (not $0.00) for un-metered cost and " est." for estimates.
- **Direct-LLM token accounting (worker/llm_client.py):** repair-retry calls now
  accumulate their tokens, ticket `llm_cost_usd` accumulates across re-dispatches
  instead of overwriting, and the judge's own LLM call is metered alongside the
  executor's.

### Fixed
- **Unknown hosted models priced at $0.00:** `resolve_prices` no longer returns a
  silent zero for a hosted model it doesn't recognize — it uses a documented
  blended fallback and marks the cost as an estimate; on-prem (local) inference
  stays a true $0.

### Ops
- **publish_release.sh --sign hardened (08-30):** the .sig upload could land on the
  WRONG repo (gh resolves to the dev repo's tag-triggered release unless pinned
  with `--repo`) and was never verified — after this fix every gh call pins the
  public repo, the script waits for the tag's release workflow to go green
  before uploading, and it VERIFIES the .sig is byte-exact served from GitHub
  AND on barenoc.com (the Hostinger 404 page is an HTML 200; PGP header check)
  before declaring success, failing loudly with the manual fix otherwise.

## [2026.08.30.a] — 2026-08-30

### Fixed

- **Worker hotfix — audit_catalog.py missing from the worker image (INCIDENT):**
  the .27.a audit-catalog change made `audit.py` import `audit_catalog`, but the
  module was missing from the worker build chain (deploy.sh `SHARED_MODULES` +
  `worker/Dockerfile` COPY list) → worker crash-looped `ModuleNotFoundError` on
  deploy. Both spots updated; the rebuilt worker starts clean.

## [2026.08.27.a] — 2026-08-27

- **Light / dark / auto theme (web UI):** a theme toggle in the nav (Light ·
  Dark · Auto — default **Auto** = `prefers-color-scheme`), persisted in
  `localStorage` and applied **before first paint** by a tiny inline no-flash
  script in `<head>` (the `.dark` class goes on `<html>`). Tailwind's
  `darkMode: 'class'` is enabled, and a new `src/api/static/dark.css` override
  layer maps the high-frequency light utility classes (`bg-white`/`bg-gray-50`/
  `text-gray-*`/`border-gray-*`/`shadow`/status tints) to a WCAG-AA slate
  palette for near-free coverage; key surfaces (sidebar, cards, tables, modals,
  wiki prose + mermaid diagrams) carry explicit dark styling. Covers every
  base.html page, the login page, the setup wizard, the web chat, and the user
  wiki. The tkinter desktop chat client (`client/`) is **out of scope**
  (separate follow-up).

## [2026.08.26.d] — 2026-08-26

- **Readable ticket threads (formatting)** — long agent answers (`agent_completed`
  notes + final answers) no longer land as a wall of text. The web chat + tickets
  pages now render note text with markdown-lite on the RENDER side only: `- `/`* `/`• `
  bullet lines and `1. `/`1) ` numbered lines become real lists, blank lines +
  indentation are preserved, note types are visually distinct (progress = subtle,
  final answer = message bubble, check-in = notice), and notes longer than 12
  lines collapse behind "Show more". Note HTML is always escaped — never trusted.
  The jobs-result formatter normalizes the pi final answer's lists (grouped,
  line-per-item, short section headers) and reuses the shared word-boundary
  ellipsis rule (…) for the answer detail/error paths, so a truncation is never
  a silent mid-list cut.

- **Uplink / ISP card moves to Devices:** the Starlink Link Health card moved
  from System → Devices as a vendor-agnostic "Uplink / ISP" card. Starlink
  owners keep the dish stats (same badge + spark + refresh); everyone else gets
  a gateway/ISP uplink card from UniFi WAN health (`wan1`/`wan2` +
  `stat/health` — ISP name + WAN IP + link health + latency/throughput/uptime
  where the controller exposes them), falling back to the appliance's own
  egress probe (gateway + 8.8.8.8). The System Starlink card + its JS are
  removed; no new settings; admin + non-admin staff visible.

### Security
- **Agent identity hygiene (infosec-appliance):** the pi agent can no longer
  read or re-surface the developer/owner identity. The injected ticket
  context and task are now redacted for known personal identifiers before the
  agent sees them, a hard sysctx **identity protection rule** forbids
  seeking/referencing/retaining any personal identity (its own work notes
  included), and the progress-note + final-answer filters scrub the known
  identifiers (handle, name, email, tailnet login) while leaving generic
  customer names and emails untouched. Closes the TKT-20260823-4534
  ("developer is yery") and 08-26 `yery.odell@` leaks.

- **Audit event catalog (compliance inventory, 2026-08-26):** the audit trail
  is now a complete, framework-mapped catalog (SOC 2 / PCI DSS / HIPAA style)
  instead of "the events we happened to add". New `src/api/audit_catalog.py`
  is the single source of truth (event type → required fields → retention
  class → framework), surfaced in the attestation export ("N audit event
  types active, hash-chained, retention X").
- **New audit events (the gaps):** `credential_access` (a stored SSH/SNMP
  secret was decrypted/fetched — records actor/device/type/action, never the
  secret), `export_download` (support bundle / audit-log / attestation),
  `backup_start|success|failure|restore`, `scheduler_health` (sustained
  scheduler API-auth failure + recovery — the 08-26 missing-agent-creds class
  is now visible in-app), and `update_schedule_change`. Compliance toggles +
  baseline apply and the setup sweep are also audited.
- **Volume honesty:** audit payloads are capped (~300 B target, hard ceiling
  enforced) and secret-looking fields are redacted before hashing; the audit
  table gains (event_type, actor) + timestamp indexes for the viewer.

## [2026.08.26.c] — 2026-08-26

- **Service Checks (ping/TCP/HTTP monitors → tickets):** Settings → Service
  Checks defines per-endpoint monitors — a host/IP or a linked device (its
  current IP is always used). `fail_threshold` consecutive failures open a
  **P2** ticket; a sustained outage (&gt;10 min) escalates the **same** ticket
  to **P1**; `recovery_ok` consecutive successes auto-close it with a summary
  note. Restart-safe `service_check_episodes` prevent duplicate tickets after
  a scheduler/API restart, and deleting a device disables its monitors. The
  per-monitor 🔔 toggle gates only the email/push layer — tickets are always
  created. The scheduler polls on a configurable cadence (`SERVICE_CHECK_*`
  env knobs, default every 5 min).

- **Post-update "what changed" note:** after an auto-update applies a new
  version, the appliance now emails the owner a short friendly summary of
  what's new (the top bullets from the sanitized GitHub Release notes) plus a
  link to the full changelog — via the existing alert/notify channel. One
  note per applied version; silently skipped when no recipients are
  configured; never blocks the update.

## [2026.08.26.b] — 2026-08-26

### Fixed
- **Privacy: no more tailnet identities in AI replies** — the on-appliance
  agent's progress notes and final answers strip tailnet account logins
  ("name.name@", e.g. from `tailscale status`); the sysctx forbids them and
  the filters back that up (real emails are untouched).
- **Starlink phantom dish finally purged (forum 9eaa106e):** the cleanup's
  keep rule treated the default dish address (192.168.100.1) as a real dish,
  so no-dish boxes kept the fabricated record forever. The rule is now
  evidence-based: a dish record is kept only when it has telemetry within the
  last 7 days (a phantom never does; a real dish mid-outage is kept).

### Changed
- **Starlink Link Health card** in System now renders only on boxes with a
  live dish (no more "Starlink" UI on boxes that don't have one).

### Added
- **Support SSH over the tailnet:** with Remote support on, the BareNOC
  support team can SSH into the appliance (dedicated key, key-only, tailnet —
  removed the moment Remote support is off; your own keys untouched). Gives
  beta/GA appliances direct validation + troubleshooting access.

## [2026.08.26.a] — 2026-08-26

### Fixed
- **Wiki mermaid rendering (10.9.8):** the Getting Started diagram's
  `Queue Manager (Juniper)` node failed to parse — unquoted parentheses
  inside a `[...]` label are rejected by the wiki's mermaid 10.9.8 (mermaid
  11 tolerates them, which hid it). The label is now quoted.

### Changed
- **Setup card copy:** "Setup complete — your network is being watched"
  → "Setup complete — your network is in good hands."

### Docs
- Remote-access wording: "Starlink/CGNAT" → "CGNAT" (settings Support copy,
  wiki, installer comment, deployment guide).

## [2026.08.25.d] — 2026-08-25

### Added
- **Devices — online-only toggle + online-first sort:** a one-click
  **Show online only / Show all** toggle on the Devices page (works in both the
  Onboarded grid and the Unclaimed groups) narrows to `status == online` and
  composes as an AND with the existing search/type/group/monitor filters.
  Device lists now sort online-first in every rendered group/list (the
  Onboarded grid included); endpoints keep their type order within the
  online/offline buckets. The status select gained an **Unknown** option.
  `GET /api/v1/devices?status=` filters both claimed and unclaimed (route
  binding covered by tests).
### Changed
- **Auto-update on by default:** a fresh install (or a box that updates to this
  release without ever touching the schedule) now gets a default weekly update
  schedule — **Sunday 03:00 local time** — written at API startup and by the
  setup-wizard completion sweep. The schedule conf's existence is the permanent
  opt-out marker: an existing conf (enabled or explicitly disabled) is never
  overwritten. Opt out is one click in System → Updates (the **Auto-update**
  toggle). Safe because releases ≥ v2026.08.25.a are GPG-signed and verified
  before apply (fail-closed).

## [2026.08.25.c] — 2026-08-25

### Added
- **Express setup wizard (home track):** the first-run wizard now defaults to a
  4-step express path (admin account → network/UniFi with an auto-discover
  preview → name & share the chat → done) instead of the 9-step flow. Every
  skipped step writes a correct home default at `/setup/complete` (cloud LLM
  egress, autonomous + `PI_AGENT_ENABLED`, UniFi auto-sync/auto-adopt on,
  backups on local, email off until a recipient is added, browser-detected
  timezone). The "Advanced setup" expander restores the full 9-step path.
- **Settings grouped for home:** Settings tabs are now a flat, small set
  (General · Network · Devices · Tickets · Updates · Backups) with everything
  else (LLM providers, Email, Identity, Restrictions, Security, Support, Users)
  under an **Advanced** sub-tab — no functionality lost, all reachable in ≤2
  clicks.
- **Setup-complete reassurance card:** a one-time "Setup complete — your network
  is being watched" card on first dashboard login after the wizard (links to
  the getting-started guide).

### Changed
- **Home defaults sweep:** `LLM_POLICY_PROFILE=autonomous`,
  `PI_AGENT_ENABLED=true` and `UNIFI_AUTOSYNC_ENABLED=true` now ship active in
  `.env.example` (fresh installs start autonomous + auto-discovering); the
  UniFi auto-sync default is on when unset across settings, sync, and scheduler.

## [2026.08.25.b] — 2026-08-25

### Added
- **Compliance controls (toggleable security/governance panel + attestation):**
  a new Settings → Security panel with 8 individually-toggleable controls
  (LLM egress cloud/local, MFA enforcement with TOTP, telemetry off, remote
  support consent, retention policy, audit log, session policy, data deletion),
  a one-click **Compliance baseline** preset, and an **attestation snapshot**
  export (every control's state + enabled-since provenance + settings hash +
  appliance version + audit-log export link). Home UX keeps its streamlined
  defaults. The always-on floor (self-protection, mTLS identity, encryption at
  rest, update signing, 3-layer backups) is listed read-only and is never
  toggleable.
- **LLM egress enforcement (local-only):** the worker chain + pi provider are
  filtered to on-prem (Ollama/LM Studio) endpoints when egress is local;
  saving a hosted/cloud API key is refused with a policy message; the wizard
  LLM step offers a "local-only (no data leaves your network)" option.
- **Audit log viewer:** new /audit page (hash-chain verify + JSON export) with
  chain-integrity checking (`audit.verify_chain`).
- **Session policy:** idle timeout + login lockout (strict profile), and
  per-user purge + factory reset for the data-deletion control.

### Security
- **MFA enforcement gate:** password-only admin/operator sign-in requires a
  TOTP second factor while enforcement is on (passkey-first via Pocket ID;
  TOTP fallback via pyotp).

## [2026.08.25.a] — 2026-08-24

### Security
- **Detached-signature release signing:** releases now ship a detached GPG
  signature (`bareNOC-<ver>.tar.gz.sig`) made by the `bareNOC release signing`
  key (held `0600` on the gate machine, never in the repo, looked up by email).
  The appliance verifies it against the **pinned** public key
  (`docs/security/release-signing.pub`, installed at
  `/opt/barenoc/scripts/release-signing.pub`) before applying an update,
  breaking the single trust chain (manifest + tarball + site all in one
  pipeline). Pre-signing releases fall back to hash-only with a warning; a
  signature becomes **mandatory (fail-closed) at v2026.08.25.a**.
  See `docs/security/release-signing.md`.
- **Token revocation (P0):** logins now mint a revocable refresh token (session
  row per sign-in); `/logout` revokes it instantly; `/refresh` validates the
  session (exists / unrevoked / unexpired / version match) before issuing a new
  access token. Password changes bump a per-user token version that invalidates
  **every** outstanding access + refresh token immediately.
- **Fail-closed JWT (P0):** only `access`-type JWTs authenticate API paths — a
  refresh or flow token can never pass as an access token; every decode site
  rejects missing / malformed / expired / revoked tokens by default.
- **Cookie hardening (CSRF review):** auth cookies are `SameSite=Lax` and marked
  `Secure` when served over HTTPS; the refresh cookie is HttpOnly. Confirmed no
  `Form()` endpoints and no CORS middleware (form-CSRF is blocked at
  content-type; same-origin only).

## [2026.08.24.b] — 2026-08-24

### Fixed
- **Starlink phantom dish survives updates on boxes with the collector disabled
  (08-24):** the 08-20 phantom purge only ran inside the telemetry collector
  loop — a box with `STARLINK_ENABLED=false` never purged, so the fabricated
  'Starlink Dish' record persisted across updates (forum thread 9eaa106e).
  The API now runs an unconditional startup sweep (`purge_phantom_dish_at_startup`)
  that removes no-config dish records on every boot — real configured dishes in
  an outage keep their record. Startup-purge test added.

## [2026.08.24.a] — 2026-08-24

### Security
- **Privacy sanitization (2026-08-24):** scrubbed all personal/private
  identifiers from the shipped (public) tree — local developer-machine paths
  in `validate_wiki_mermaid.mjs/.md` and
  `docs/operations/update_pipeline.md`, real home-network topology embedded
  in test fixtures (real MACs/IPs/device names — `test_devices_polish.py`,
  `test_network_opt.py`, `test_unifi_sync.py`, `test_network_scope.py`,
  `test_alerting.py`, `test_link_monitor.py`, agent + worker tests), the real
  Starlink WAN IP (now an RFC 6598 example), real VLAN names/subnets
  (now a generic private-range scheme), the hardcoded prod
  appliance IP (now an RFC 5737 placeholder), and real device
  names in code comments/docstrings (now generic). Public-repo publish commits now
  use a neutral
  `bareNOC release bot <release@barenoc.com>` identity. The website repo
  (bareNOC.com) is scrubbed in a parallel change (no personal social/email).

### Fixed
- **Root-trust anchored the WRONG certs (issue #105):** the root-trust opt-in
  (agent_install.sh + the served /onboard scripts) previously anchored
  whatever `/onboard/root-ca.crt` returned without verifying it — on a box
  where that was an unrelated root or a leaf, the store ended up with the
  wrong anchors while the real signing root (`BareNOC Internal CA Root CA`,
  the step-ca root that signs the served web cert chain) was missing, so
  Chrome/curl kept rejecting `https://<appliance>`. The installer now verifies
  the candidate is a self-signed CA root that actually chains to the served
  web cert (never an unrelated root or a leaf), removes any stale
  `barenoc-root*.crt` anchors a previous version added, installs into the
  correct store (`update-ca-trust` on Fedora/RHEL, `update-ca-certificates` on
  Debian/Ubuntu), and verifies the trust lands (`openssl verify` + `curl`
  without `-k`) — no more “installed but still red”.
- **Ordering (issue #105):** the “installation complete” confirmation now comes
  AFTER the cert-accept/root-trust step — completion is the last step and the
  flow no longer suggests the device is done until the trust step finishes
  (the served onboarding script previously popped the “onboarded” dialog before
  the trust prompt).

## [2026.08.21.a] — 2026-08-21

### Added
- **Multi-source APPLY (08-21):** `apply_updates` — the gated counterpart to the
  multi-source update check. The NOC_Agent gains an `apply_updates` action
  (confirm-gated on BOTH the appliance and the agent — customer-requested only,
  never autonomous-unprompted) that runs `src/scripts/apply_updates.sh` on the
  endpoint: it re-runs the read-only check for a fresh per-source picture, then
  applies each NON-zero source — the OS package manager
  (`dnf -y update` / `apt-get -y upgrade` / yum / apk / zypper) + flatpak +
  firmware (fwupd) + snap + rpm-ostree (the SAME multi-source family the check
  explores). The result reports per-source applied counts + a `reboot_needed`
  flag for kernel/atomic updates — the script **never reboots** (the flag is
  surfaced; the customer decides when). The PATCH_ALLOWLIST stays the appliance's
  firmware-ID gate; this is the endpoint OS apply (no overlap).
- **Appliance dispatch:** `AGENT_ACTIONS` += `apply_updates` (with the
  `confirm=true` validation gate) so the chat/pi flow can enqueue the apply for
  agent devices, and the result formatter renders the applied counts +
  reboot-needed flag.

### Changed
- **NOC_Agent `apply_updates`** runs `/opt/noc-agent/scripts/apply_updates.sh`
  (installed root-owned next to the check script by `agent_install.sh`, embedded
  heredoc = the canonical script — a CI drift test pins it). The sudoers
  allowlist needs **no new tools** for apply: the .e grant already covers the
  per-OS package managers + flatpak/fwupdmgr/snap/rpm-ostree at the tool level
  (e.g. `dnf check-update` for the check and `dnf -y update` for the apply).

## [2026.08.20.e] — 2026-08-20

### Added
- **Multi-source update check (08-20):** `src/scripts/check_updates_multi.sh` —
  the update engine now explores ALL update sources, not just the OS package
  manager (the App Center aggregates rpm + flatpak + firmware, which the engine
  could not see). In addition to apt/dnf/yum/apk/zypper — now run WITH metadata
  refresh so stale metadata never hides updates — it checks **flatpak**
  (`flatpak remote-ls --updates`), **firmware** (`fwupdmgr get-updates`), **snap**
  (`snap refresh --list`), and **rpm-ostree** (`rpm-ostree upgrade --check`,
  atomic distros). The result reports a machine-readable per-source shape
  (`sources` counts + `total` + `updates_available` + a base64 detail), where
  "updates available" = any source non-zero. Read-only — apply stays a separate
  gated action (the queued OS-aware lane).

### Changed
- **`apply_patch.sh`** delegates to the multi-source check (the SSH path relays
  the per-source report), and the ticket/chat formatter surfaces the per-source
  breakdown (e.g. "OS packages: 2, Firmware: 1").
- **NOC_Agent `check_updates`** runs the same multi-source check via
  `/opt/noc-agent/scripts/check_updates.sh` (installed by `agent_install.sh`)
  instead of the apt-only `apt-get -s upgrade`.
- **sudoers** (the appliance's `barenoc` grant + the NOC_Agent's `nocagent`
  grant) now include the per-OS package managers and the other update sources
  (flatpak/fwupdmgr/snap/rpm-ostree), gated to those tools only (never `ALL`).

## [2026.08.20.d] — 2026-08-20

### Fixed
- **Starlink phantom device fabrication (08-20):** `ensure_dish_device` created + CLAIMED
  a "Starlink Dish" device on every appliance (the monitor defaulted to enabled + the
  the dish's factory-default address) even with no Starlink on the network. The record is now
  created only after a real dish snapshot succeeds, and a purge removes phantom dish
  records on boxes with no explicit `STARLINK_ADDRESS` (a configured dish in an outage
  keeps its record). Found via loompafoo's report (the "Starlink Dish" he doesn't have).
- **Post-update verification grace (08-20):** the tailscale check right after the
  provision could catch the join mid-flight → false "verification failed" auto-reports
  on boxes that joined fine a minute later. The probe now retries up to ~90s before
  declaring failure.

## [2026.08.20.b] — 2026-08-20

### Added
- **Post-update verification suite** (`scripts/verify_post_update.sh`) — runs after EVERY
  self-update: (a) entitlement check (the `support_grant` beta gate — an entitled box
  proceeds; an unentitled box reports state + skips the tailscale requirement), (b) tailscale
  check + self-heal (install → seed key → join → verify the node is Online + tagged), and
  (c) a result JSON the auto-report hook reads. Idempotent + safe — a healthy entitled box
  sees no action beyond the checks; `--dry-run` for read-only testing.
- **Auto-report hook** (`AUTO_REPORT_POST_UPDATE` env knob, default ON) — when a post-update
  check FAILS (or the update itself rolls back), the appliance files a bug through the
  existing in-app Submit-Report path (forum thread + redacted support bundle) titled
  "Post-update verification failed" with the stage + evidence. Only real failures report.

### Changed
- **Self-update health check is now VERSION-verifying** — after the rebuild the script reads
  the live `/api/v1/health` JSON and compares `version` to the requested version; a mismatch
  is treated as a failure → restore `.previous` (the existing rollback path). Actual +
  expected are logged (a failed rebuild left the old stack serving 200 — the 08-20 buddy bug).
- **Build output is no longer masked** — the compose build streams to
  `/var/log/barenoc-self-update-build.log` and the self-update script's own log; never fully
  silent.
- **Post-apply provision** — the update runs `provision_agent.sh` after a successful apply, so
  existing boxes updating now get the full provision pass (tailscale, agent creds, notify,
  remote support) automatically.

### Fixed
- **Self-update shared-module list matches deploy.sh's 12** — added `queue_status.py` +
  `tone_pool.py` to the worker build-context copy (was 10) so worker builds no longer fail on
  updated boxes.
- **Tailscale install is repo-correct** — the remote-support provision now configures the
  Tailscale apt repo via the official installer (`apt-get install tailscale` alone fails on
  existing boxes with no pre-configured repo); falls back to a plain apt install.

## [2026.08.20.a] — 2026-08-20

### Fixed
- **Tailscale remote-support join used the wrong CLI flag** (`--tags` vs the real
  `--advertise-tags`) — every tagged join failed at runtime; the idempotency check also
  only tested "interface up" (a broken half-join skipped re-joins forever). The join now
  verifies the node is ONLINE + carrying the tag, tears down any unhealthy state, and
  re-joins cleanly. (Found + fixed live 08-20: prod joined as `bareNOC-<id>`,
  `tag:appliance`, CGNAT IP.)

## [2026.08.19.e] — 2026-08-19

### Added
- **Vendor-managed email (out-of-the-box)** — the appliance now has a third
  transport (`vendor`) that POSTs alert/digest/EOD/check-in email to the
  vendor `notify` Supabase edge function (shared `NOTIFY_TOKEN`, the
  forum-submit pattern), which sends via **Resend** from
  `noreply@notify.barenoc.com`. The display name is the appliance's site name;
  the REPLY-TO is configurable. Emails land in inboxes with SPF/DKIM/DMARC on
  the vendor domain.
- **Settings → Email/Notifications** gains the transport choice
  (vendor-managed vs your own SMTP, with the privacy note), the notify URL +
  token (0600 `notify.json`, the Settings → Support pattern), a REPLY-TO field,
  and the Test email button covers the vendor transport.
- **`smtp_configured()` now counts vendor-managed as configured** — the alert
  engine's email half (down/recovery, P1/P2, digests, EOD, check-ins) runs
  out-of-the-box instead of sitting dead when SMTP is unconfigured.
- New `supabase/functions/notify` edge fn (BareNOC-Forum repo): token-gated,
  Resend send, rate-limited, nonce idempotency (in-isolate dedupe + Resend
  Idempotency-Key).

### Changed
- `emailer.send_email` transport resolution: explicit `EMAIL_TRANSPORT` wins;
  when unset, your own SMTP/Gmail overrides the vendor default (so existing
  self-hosted setups are unchanged).
- **Remote support (Tailscale zero-touch onboarding + customer toggle):** the
  appliance joins a vendor support tailnet via a tagged, expiring, revocable
  auth key (provision step: `apt install tailscale` + tagged join, idempotent +
  graceful), and Settings → Support gains a **Remote support** toggle (default
  OFF) that runs `tailscale up/down` and shows the node identity + tailnet
  status. Scoped by Tailscale ACLs (appliance nodes only, never the customer
  LAN); audit-logged.
- **Support gate (`report_gate.py`):** `support` mode now reads an expiring
  beta `support_grant` (0600 secret, forum-submit pattern) — beta-open while
  the grant is active, then the GA entitlement stub. Both the remote-support
  toggle and the submit-report path check it.

### Fixed
- **Ping-sweep no-hang:** whole-subnet sweeps are now parallel + capped with
  short timeouts, progress notes, and CGNAT exclusion; the runner streams
  `PROGRESS:` notes and aborts sweeps cleanly at the job timeout (a /24
  finishes in seconds, never hangs the worker/pi session).
- **CGNAT/Tailscale discovery exclusion:** 100.64.0.0/10 (RFC 6598 CGNAT +
  Tailscale overlay) is never scanned, discovered, claimed, or adopted — a
  Starlink CGNAT-link case is pinned in tests.
## [2026.08.20.c] — 2026-08-20

### Added
- **Settings → Support → Support key input** (the customer flow for remote
  support): a password-style **Support key** field lets the customer (or the
  owner on their behalf) paste the vendor's Tailscale auth key instead of
  hand-editing `/opt/barenoc/volumes/secrets/tailscale.json` over SSH. The API
  writes the key to the 0600 secret (merging `tailnet`/`tags`/`hostname_prefix`/
  `appliance_id`) and kicks the host reconciler immediately (the 60s timer stays
  as the backstop) → the node joins within a minute. The toggle (ON/OFF) is
  unchanged; the key field only appears when the box is entitled (the gate is
  open).
- **Remote-support status in the UI** — Settings → Support now shows a human
  status: **Connected** (online + tailnet IP) / **Connecting…** / **Not
  connected**, plus the tailnet IP and a human error when the join fails
  ("Invalid key — check with your provider." / "No support key set…"). The API
  reads the same state the reconciler writes (`remote_support.desired` +
  `remote_support.json` + `self.json`).

### Fixed
- **ISO install is now self-sufficient (zero hands) — `proxmox/build_barenoc_iso.sh` + new `scripts/bootstrap_appliance.sh`**
  (the 08-20 round-3 gap: a fresh ISO box needed manual remediation before
  `deploy.sh` could run). The first-boot flow now makes the box turnkey on its
  own:
  - `/opt/barenoc` source tree lands `barenoc`-owned (deploy.sh's rsync + first
    `mkdir -p` run AS barenoc and failed on a root-owned tree).
  - `barenoc` gets passwordless sudo (the deploy's sudo steps need it).
  - `.env` is bootstrapped (template + JWT/admin/encryption/appliance identity)
    — previously neither first-boot nor deploy.sh created it (only the
    appliance installer did).
  - `step-ca/password-in` is written BEFORE the CA-init, and the CA init now
    FAILS LOUDLY if `ca.json` isn't produced (the deploy's `|| true` silently
    masked the missing-password-file abort → "there is no ca.json config
    file" → the step-ca provisioner step failed).
  - nginx certs (main + stepca vhost) + CoreDNS Corefile + the
    barenoc-devices provisioner are all created pre-compose, so `nginx` no
    longer crash-loops on a fresh install.
  - the worker build context gets the shared api modules copied in (deploy.sh
    does this before compose; the ISO tarball ships them side-by-side so
    first-boot must too — otherwise the worker build dies with
    `"/emailer.py": not found`).
  - `provision_agent.sh` re-asserts the same bootstrap on every deploy (the
    appliance-installer / ISO / manual-deploy paths now converge).

## [2026.08.19.d] — 2026-08-19

### Added
- **NetOpt device identification WITHOUT packet capture** (`feat/netopt-device-id`):
  the scan now identifies what is on each switch port from the signals the
  controller already exposes — no mirror/native tcpdump needed (packet capture
  moves to the future SOC appliance).
  - **Traffic archetype** per port: `router_ap` (1 learned MAC + heavy
    bidirectional traffic/multicast — an edge router/AP), `switch` (many MACs),
    `host` (1 MAC, modest), `dead_end`, `unused`, `down`, and a conservative
    `unknown` that is never guessed.
  - **Conservative rule:** a `router_ap` or `unknown` port is NEVER told to
    change networks — its suggested action is "likely a router/AP — left on the
    default network; verify before any change" (a Google/Nest WAN port regression, which previously guessed "switch" → Management).
  - **OUI best-effort:** a small embedded vendor table (Google/Nest, Apple,
    Ubiquiti, + common top OUIs) maps a learnable port MAC to "likely X gear",
    failing gracefully when the firmware hides port→MAC.
  - **DHCP hostname attempt:** surfaces a connected client's hostname from the
    client list (rest/user + stat/sta; `stat/dhcpd_lease` 404s on this build) —
    fail-soft.
  - **Capability declaration (light):** the run summary declares the
    capture/mirror capability state (`packet_capture.available: false` on this
    gear) for the future SOC-appliance integration.

### Fixed
- **False "no assigned network" on an overridden port** — the UniFi collector
  now reads each port's EFFECTIVE native/tagged/name from its `port_overrides`
  entry when set (a port has native=Default via its override), and
  the no-profile rule treats a native (including the untagged Default) or a
  tagged set as a working profile — configured ports are no longer findings.

## [2026.08.19.c] — 2026-08-19

### Added
- **In-app "Submit Report" → forum bug thread + bundle** (`feat/report-submit`):
  System → Support / Bug Report gains a **Submit Report** button beside the
  existing Download support bundle. The flow requires a mandatory comment,
  vets it with one LLM call (bug / not-bug / unclear — not-a-bug explains
  inline, unclear prompts once then submits flagged), then creates a forum bug
  thread on forum.barenoc.com attributed to the logged-in user and uploads the
  redacted support bundle to the forum's private `session-logs` bucket.
  - New modules: `report_gate.py` (`REPORT_GATE` env — open during beta,
    Support-subscription gate stubs in for GA), `report_vet.py` (single-LLM
    classification reusing the provider registry), `report_submit.py` (the
    forum-submit client; token stored 0600 in Settings → Support).
  - `routes/report.py` (`POST /api/v1/report/vet` + `/submit`); `support.py`
    refactored to expose `build_bundle()` for reuse by the submit flow.
  - Settings gains a **Support** section (forum-submit URL + token).
  - Forum repo (`Ridge-Chapel-Tech/BareNOC-Forum`): new `forum-submit` edge
    function — token-gated, creates the bug thread + uploads the bundle.

## [2026.08.19.b] — 2026-08-19

### Added
- **Per-port discovery + dead-end/loop detection** (`feat/netopt-port-discovery`):
  the UniFi per-port snapshot now carries `mac_table_count`, `rx_packets`,
  `tx_packets`, `tx_multicast`, `stp_state`, `disabled`, and the controller's
  uplink mapping (port → known AP/switch name). Each port is classified
  best-effort as connected / dead_end / unused / down, and two new rules fire:
  `hyg.dead_end_port` (warning, high-risk — no devices learned + multicast
  flooding, a dead-end port loop signature) and
  `hyg.unused_port_up` (info — link up but no devices; disable for hygiene).
  The run detail now shows the per-port discovery alongside the findings.
- **Merge-safe port disable** — `UniFiClient.set_port_disabled` + the
  `/api/v1/unifi/ports/{mac}/{port}/disabled` endpoint + `unifi_port_disable.sh`
  / `unifi_port_enable.sh` scripts (the dead-end/loop fix path).
  `infra_checkpoint.py` now captures/restores the `disabled` state so a
  port-disable fix rolls back to re-enable.
- **VLAN/subnet-aware Network Optimization + NO-FLAT guardrail**
  (`feat/netopt-vlan-awareness`): the scan now UNDERSTANDS VLANs and
  subnetting and never recommends a flat network.
  - `unifi.py`: `get_networks()`/`get_networks_map()` surface the network
    `purpose` field alongside vlan/subnet/enabled (rest/networkconf).
  - `network_opt.py`: the UniFi collector enriches every port with
    `native_network`/`tagged_networks` names, builds a **network map**
    (`{vlan_id: subnet, purpose, enabled}`, untagged default keyed `default`),
    and the run detail persists `network_map` + per-port `vlan_context`
    ("native WiFi (10.0.5.1/24), tagged IoT(9)/Video(10)").
  - `network_opt_rules.py`: device-class→network mapping (AP→WiFi,
    gateway/router/switch→Management, host/server→Production) so the
    "port with no assigned network" finding names the CORRECT network for the
    device class (uplinks get the full trunk, never a flatten); plus the
    **NO-FLAT guardrail** — any suggested_action that would collapse VLANs/
    subnets (assign everything to one network, remove VLAN tags, flatten) is
    suppressed and flagged "design change — not recommended".
  - `routes/network_opt.py` + `netopt_tickets.py`: the run detail and Optimize
    tickets use the VLAN-aware `suggested_action_for()` accessor (dynamic
    per-finding action + guardrail applied on every path).
  - `_network_opt.html`: the run detail renders the network map + per-port
    VLAN context beside the findings.
### Changed
- **Port finding naming** (`feat/netopt-findings-naming`): port-related findings
  now use the canonical `<device> Port <idx> (<description>)` naming — the device
  name plus the UniFi port `name` field (its description) in parentheses when one
  exists — across finding titles/details and the Optimize ticket change-plan
  (e.g. "Switch-02 Port 7 (Google WAN): no assigned network" instead of
  "Port with no assigned network on Port 1"). Display-only: finding keys and the
  `interface` (port idx) field are unchanged.



## [2026.08.19.a] — 2026-08-19

### Added
- **Starlink dish gRPC telemetry + link-health monitor (P0)** — a new collector
  in the telemetry family polls the dish's local unauthenticated gRPC API
  (~60s, configurable via `STARLINK_*`) and writes
  `starlink.*` metrics (ping_ms, link_up, down/up_mbps, snr, obstructed,
  obstruction_fraction, uptime_seconds, ping_drop_rate) into the metrics store
  (device = the dish). A graduated-ticket health monitor complements the
  port-level link-flap monitor: sustained degradation → P2 "Starlink link
  degraded" (kept open), dish-reported link-down → same ticket escalates to P1
  "Starlink link outage", sustained recovery → auto-close. A lean Starlink
  link-health block (latest ping/signal/throughput + mini trend) lands on the
  System page. gRPC client = `starlink-grpc-core` (reflection client only —
  grpcio/protobuf/yagrc, no influxdb/mqtt/prometheus deps); unreachable-dish
  gaps are recorded honestly (no fabricated samples).
- **Agent foresight for infra changes** (the 08-19 Optimize-rollout incident fix):
  risk-aware recommendations + an execution contract + checkpoint/rollback so a
  port/VLAN/network change is planned and verified, never half-applied.
  - `network_opt_rules.py`: every fixable rule now carries `high_risk` +
    `blast_radius` + `plan_note` metadata; port/VLAN/uplink-changing rules are
    flagged high-risk and their `suggested_action` includes the blast radius and
    a plan-first note (e.g. unnamed uplink → "do not change the uplink";
    port with no network → "assigning a native will move connected devices —
    plan + verify"; ssh/http → safe).
  - `netopt_tickets.py`: Optimize tickets now embed a **CHANGE PLAN** artifact
    per finding (current state → proposed change → blast radius → verification
    step → rollback step) + a `change_plan` work note, so the ticket arrives
    pre-thought.
  - `src/agent/runner.py`: the pi sysctx gains an **INFRA-CHANGE CONTRACT**
    (enumerate current state first → blast-radius reasoning → capture-before →
    one-change-verify → rollback-on-failure; never change the appliance/uplink/
    management ports without reasoning + a fallback plan). A mid-flight timeout
    now reports "applied step N of M, rollback state at <path>" with the restore
    command instead of a half-applied mystery.
  - `src/scripts/infra_checkpoint.py` (new): capture/restore the full before-state
    of a UniFi switch's port table via the merge-safe appliance API.

- **Three-tier roles + requester-owned close-loop (P1)** — `user` (customer) /
  `technician` / `admin` roles with additive migration on the existing
  `profiles.role` column (`operator` stays a legacy alias for technician,
  `tenant` for user; `readonly` = read-only staff; `agent` = service identity).
  Signup/self-registration defaults to `user`; admins change roles in
  Settings → Users (the role picker now offers User / Technician / Admin).
  Requester-owned verification: a ticket's close-loop is requester-gated —
  requester closes their own, technician closes within their device-group
  scope, admin closes anything; a non-requester customer confirm is routed to
  "waiting on <requester> to verify" (Juniper + API enforcement).
- **Per-user Juniper front desk + pending-items context** — the front-desk DM is
  keyed to the authenticated user (no shared thread) and Juniper's greeting
  surfaces that user's own pending items role-aware: "You have N ticket(s)
  awaiting your verification" for everyone; "+ N escalation(s) requiring
  review" and "+ N pending action approvals in your scope" for the technician
  tier/admin (firmware visibility gated by `FIRMWARE_TECH_VISIBILITY`;
  gateway approvals admin-only). New chat intents: "pending" (list),
  "approve #<id>" / "resolve #<id>" (act on firmware pending items per role).


## [2026.08.18.h] — 2026-08-18

### Added
- **Firmware management (UniFi-only v1, autonomy-aware)** — the never-patched-home-router fix.
  New `maintenance_windows` (local-time, one-time/recurring, reusable by other
  scheduled ops — same shape as updates-schedule-v2), `device_firmware` (per
  managed device: current/previous/available version + last upgrade result),
  `firmware_upgrades` (history + in-flight state machine), and `pending_actions`
  (approvals + escalations queue with role visibility — the feed the
  roles-and-chat-context worker consumes). Upgrade engine: pre-stage → window
  gate → one-device-at-a-time → verify (version bump + device returns/informing)
  → next; halt-on-failure; rollback attempt via the controller; P1
  physical-assistance escalation with a runbook when upgrade AND rollback both
  fail. Autonomy matrix (firmware settings: autonomous/balanced/strict/off +
  technician-visibility toggle): autonomous auto-runs with a non-blocking
  "action pending" notice; balanced auto-runs APs/switches but gates the gateway
  behind admin approval; strict approves every device; off opts out. Order by
  risk APs → switches → gateway LAST. System → Firmware UI (inventory, windows,
  queue, history) + `GET/POST /api/v1/firmware/*` API.
### Fixed
- **NetOpt SNMP read the wrong system OIDs** — `network_opt.collect_snmp` read
  sysDescr / sysName / sysUpTime one OID node too deep (e.g.
  `1.3.6.1.2.1.1.1.1.0` instead of `1.3.6.1.2.1.1.1.0`), so the system
  identity + uptime block was always empty against real gear. Now aligned with
  the telemetry collector and the scheduler/`snmp_poll.sh`/`snmp_sweep.sh`
  (sysDescr `1.3.6.1.2.1.1.1.0`, sysName `1.3.6.1.2.1.1.5.0`, sysUpTime
  `1.3.6.1.2.1.1.3.0`). The ifTable and UCD-SNMP CPU/mem OIDs were already
  correct; new tests pin them.


## [2026.08.18.g] — 2026-08-18

### Added
- **Telemetry backbone (P0 time-series)** — the missing layer between NetOpt's
  point-in-time snapshots and continuous capacity/SLA trends. New `metrics`
  table (device_id, metric, ts, value) with a (device_id, metric, ts) range
  index; in-process collectors in the API container on modest configurable
  cadences (UniFi per-device/per-port counters → bandwidth rate over one
  long-lived controller session, SNMP ifOperStatus/bytes → rate + CPU/RAM/uptime,
  light capped ping → latency + packet loss); batched writes + disk-aware
  retention pruning (scheduler-owned, hourly, `TELEMETRY_*` env knobs); and an
  admin/operator-gated trends API (`GET /api/v1/metrics/trends?device=&metric=
  &from=&to=&agg=` with min/avg/max bucketing that omits empty buckets). A lean
  line-chart preview on the Dashboard Reports section proves the pipeline
  end-to-end; the full analytics UI stays out of scope.

## [2026.08.18.f] — 2026-08-18

### Fixed
- **NetOpt tuning (sync-IP refresh + controller-live authority + score calibration)** —
  the 08-18 live incident where NetOpt scored the home network 0 and flagged
  `rel.offline_gear` on an AP because the appliance's device record held
  a stale pre-VLAN-move IP. Three layers: (1) the UniFi sync now refreshes the
  controller's **LIVE IP** (`config_network.ip` / device `ip`) **and hostname**
  on every sync, not just status/last_seen; (2) for `unifi_managed` devices the
  NetOpt reachability/status source is the **controller snapshot** (live IP +
  state), never the DB record — a stale record IP can no longer produce a false
  offline_gear critical (non-UniFi devices keep the record/scan path); (3) score
  calibration pinned to gate semantics: criticals −20 stack (floor 0), warnings
  −5 stack with no cap, info findings capped at the first 5 (−2 each, absolute
  −10) so noise never tanks a healthy network; UniFi-default SSH downgrades to
  info on Ubiquiti gear; `rel.link_down_count` now warns only on repeated (>2)
  link-down transitions so a single old PoE-cycle counter can't warn forever.
  A healthy home network now scores ~90+ while real risks still bite.
- **Friendlier unknown-target wording + whole-subnet ping resilience (friend's bug #2)** —
  a "ping sweep of a /24" request no longer aborts with a cryptic
  `Unknown target: 'switch-01'. Device not in managed inventory.` when the AI
  pinned an unresolvable device name. The customer-facing message now reads as a
  product message ("I couldn't find a device named 'X' — you can ask me to check
  an IP/subnet, or adopt the device first"), the technical detail stays in the
  ticket/log (a hidden `target_validation_failed` note), and a subnet/IP scan
  request falls back to scanning the subnet with a clear name-miss note instead
  of escalating.
## [2026.08.18.e] — 2026-08-18

### Fixed
- **Updates: stable builds never re-checked for new releases (08-18).** The auto-check
  only fired when the installed build changed (`check_stale` = persisted current != live)
  — a box running a stable version could sit forever without discovering a release, and
  there was no manual control. Now: a **"Check now" button** on System → Updates, and
  `check_stale` also triggers when the last check is older than the staleness window
  (default 6 h, `UPDATES_CHECK_STALE_HOURS`), so a stable build still finds new releases.



## [2026.08.18.d] — 2026-08-18

### Fixed
- **Fresh-install agent provisioning guarantee (all install paths)** — a clean
  rebuild via `proxmox/barenoc-appliance.sh` could land with
  `/opt/barenoc/agent/credentials` MISSING → the scheduler 401-flooded and the
  runner/autonomous chat had no auth until a manual provision. Now every install
  path (appliance installer, ISO first-boot, and `deploy.sh`) converges on one
  shared, idempotent step — `src/scripts/provision_agent.sh` — that creates the
  runtime dirs, installs + enables + starts `pi-agent-runner` from the repo unit
  (single source of truth), and provisions the agent credentials with the
  api-healthy-BEFORE-creds wait + file↔DB login-200 agreement check (loud
  failure, never a silent file-without-DB-user).

### Changed
- **Scheduler health-order guard** — `deploy.sh` (and the ISO first-boot) now
  start the scheduler only AFTER agent provisioning; `scheduler/main.py` also
  waits at startup for the api to be healthy and the agent credentials to log
  in (visible retries) instead of flooding `Cannot read agent credentials` /
  401 errors every cycle.
- **Post-install verification** — `src/scripts/verify_agent_provision.sh` checks
  scheduler logs too (not just health 200 + a minted token) and surfaces the
  checklist lines: agent login verified · runner active · scheduler 0 errors.
  `deploy.sh` runs it at the end of every install/update.
### Changed
- **Autonomous tickets honor a customer's "close" (no-reinvestigate)** — a
  customer replying in a COMPLETED ticket's thread with an explicit close ask
  ("yes, please close", "close the ticket", "you can close it", "close",
  "done, thanks — close it") now closes the ticket inline (status `closed`,
  resolution + note) instead of spawning a fresh re-investigating pi session —
  the TKT-20260818-5615 incident (the customer asked twice to close; each
  reply re-dispatched a session that re-verified instead of closing). A pure
  thanks/ack on a completed ticket gets a short note (no session); a NEW
  request on a completed ticket still dispatches normally. Owner-gated: the
  requester or an admin/operator may close; a non-requester is routed
  "waiting on <requester> to verify". A mid-work ticket + "close" gets a
  polite "still open" note and is neither closed nor re-dispatched.

## [2026.08.18.c] — 2026-08-18

### Added
- **Network Optimization tab (P1 — scheduled read-only network audit/report)** —
  an admin-only Dashboard tab that runs a deterministic, rule-based audit of
  NETWORK GEAR ONLY (gateway/router/switch/AP + UniFi-managed devices; never
  endpoints/servers, and never the appliance itself — self-protection). A
  vendor-agnostic collector framework (channels: `unifi` + `snmp` + `nmap/ping`,
  extensible to Cisco/Juniper SSH config-parsing in P2) feeds ~40 stable,
  testable checks across PERFORMANCE, SECURITY, RELIABILITY and HYGIENE, each
  with a stable `finding_key`, severity (critical/warning/info), title/detail,
  and structured `evidence` JSON — no LLM in the findings path, no tickets/
  emails/actions. Results persist in new `scan_runs` + `findings` tables
  (schema-versioned, summary slot reserved for a later LLM executive summary,
  stable keys make trend/diff + finding→fix links cheap later). Scheduling
  reuses the updates-schedule-v2 local-time pattern (one-time or recurring,
  default weekly Sun 03:00 local, disabled until enabled) with progress +
  cancel and per-host concurrency. Cost knobs are first-class (`NETOPT_ENABLED`,
  `NETOPT_MAX_HOSTS`, `NETOPT_SCAN_PROFILE` (-T3/-T2, capped ports, no
  intrusive scripts), `NETOPT_CONCURRENCY`, `NETOPT_DEFAULT_SCHEDULE`). The
  `nmap` binary is now installed in the api image; scans run entirely in-process
  in the API container and never feed the pi/LLM agent loop.

### Changed
- **More varied, stage-matched chat progress notes** — the runner's friendly
  progress vocabulary grows from 5 repeated placeholders to a categorized pool
  (dozens of variants across reading/investigating, connecting, applying,
  verifying, and waiting). Selection is context-aware (keyword cues from the
  raw note map it to the matching activity category) and varied (no immediate
  repeats; a stable per-ticket seed keeps a re-read deterministic). Long pi
  tasks (>2 min with no distinct activity) now emit an elapsed-time heartbeat
  ("Still working — about 3 min in…") so a long run doesn't look hung. The
  technical-fragment safety net is unchanged and never leaks internals. The
  API-side `queue_status` "Working on it — {detail}" mapping shares the same
  category pool (`src/api/tone_pool.py`) for parity. Runner change is host-side
  (deploy.sh syncs it); `tone_pool.py` is a new shared module copied into the
  worker context.

## [2026.08.18.b] — 2026-08-18

### Added
- **Root CA browser-trust opt-in (Linux/macOS)** — the Linux agent installer
  (`agent_install.sh`) and the served `/onboard` Linux + macOS scripts now offer an
  explicit, **default-OFF** opt-in (&ldquo;Trust the BareNOC root CA for this machine's
  browsers? [y/N]&rdquo;) that installs the BareNOC root into the OS trust store (and
  Firefox's NSS store, best-effort) so `https://<appliance-ip>` and `app.<domain>` stop
  showing &ldquo;Not Secure&rdquo; for home users. Never installed silently — consent is always
  required; undo commands are documented in `docs/deployment_guide.md`.

## [2026.08.18.a] — 2026-08-18

### Added
- **Forum confirm note (GitHub → forum loop)** — when a `customer-bug` issue is
  closed, the new `.github/workflows/forum-confirm.yml` parses the version from
  the closer's `Fixed in v<version>` comment and the thread id from the issue
  body's forum link, then calls the new `forum-confirm` Supabase edge function
  (BareNOC-Forum) to post "✅ Bug confirmed. Patched in vX — please verify." to
  the forum thread. Idempotent — the edge function never double-posts.
- **Link-stability monitor (link-flap detection with graduated severity)** — a
  per-link state machine that watches monitored interfaces and turns state
  changes into a single graduated ticket: first down/up transition opens a P2
  "Link flap" ticket (30-min episode window); ≥3 flaps escalate it to P1
  "Recurring link flap"; a down that persists >10 min escalates it to P1
  "Link outage"; 30 min with no further events auto-closes it with a summary.
  Data channels: UniFi gateway WAN (stat/health) + gateway/switch port tables
  (one long-lived controller session, port tables cached ≤60s), SNMP
  ifOperStatus (snmp_configured devices), and the Device.status field as the
  fallback. The gateway WAN is always monitored; other links opt in via the
  existing `notify_state_changes` toggle. The WAN flap ticket IS the WAN
  outage ticket — the InternetMonitor probe promotes an open WAN flap ticket
  to P1 instead of opening a duplicate "Internet connectivity down" ticket.
  In-flight episodes persist in a new `link_episodes` table so a container
  restart resumes them. Env knobs: `LINK_MONITOR_ENABLED` (default true),
  `LINK_FLAP_WINDOW_MIN=30`, `LINK_FLAP_ESCALATE_COUNT=3`,
  `LINK_PERSIST_DOWN_MIN=10`, `LINK_STABLE_CLOSE_MIN=30`.
### Fixed
- **Devices topology render cascade (forum #50)** — the topology diagram could
  fail with "No diagram type detected … for text: #topo-…{font-family:…}" on
  refresh. `renderMermaid()` now renders from a stored source string (never the
  element's `textContent`, which holds the previous SVG/CSS after a render) and
  truncates error text, so a failure can never be re-parsed as a diagram; the
  mermaid CDN loader queues callbacks on a single `<script>` tag so a second
  `loadTopology()` mid-download can't double-render. Node labels also escape
  backslash + quote, and edge-port labels are digits-only (mermaid's `|…|`
  grammar rejects `|`, `"`, `(` and `)`).



## [2026.08.17.d] — 2026-08-17

### Added
- **Backups tab — remote (NAS) backup setup + guided new-USB setup** — the
  Settings → Backups tab now exposes the same network-copy form the wizard's
  Backups step has (proto cifs/nfs · host · share · user/pass → Connect /
  Disconnect → status, plus the already-mounted-folder Save/Test), reusing the
  existing `backup_net_mount` / `net-unmount` machinery (0600 creds file,
  host-mount via the privileged nsenter helper, reboot-safe remount).
- **Guided "Set up a new USB stick"** — when no encrypted USB stick is
  configured, the Backups tab (and the wizard's Backups step) offer a guided
  setup: prerequisites (stick in the appliance's Proxmox host, ≥4 GB, wiped +
  LUKS2-encrypted), a **Detect** button that lists host USB candidates, a
  confirm-to-erase gate, then the appliance drives the host-side
  `setup-usb-backup.sh` through `_host_run` (run in the host mount namespace) and
  polls the status (detected / encrypted / keyslots). The one-time recovery
  passphrase is surfaced once for the rack card; failures (no stick, missing
  host script) fall back to a clear message + the exact manual command.

### Changed
- `install.sh` now also installs `setup-usb-backup.sh` and
  `sync-backup-schedule.sh` to `/usr/local/bin` (the docs already referenced
  them there).

### Fixed
- **Agent runner no longer loses job results to the login rate limit** — the
  host-side runner logged in at every API call site with no caching, so a burst
  of verify/device work could 429 the login endpoint and send `/jobs/result`
  with an empty Bearer → 401 → the ticket never updated and the watchdog
  escalated a job that actually succeeded. The runner now reuses a cached token
  (≤5 min), retries 429/5xx logins with backoff, re-logins once on a 401 for
  the result POST, and retries that POST with backoff too. Other call sites
  (progress notes, device verify callbacks) inherit the cached login. Login
  rate-limit defaults are unchanged (prod already runs `RATE_LIMIT_LOGIN=120`).
- **`_host_run` works again on fresh installs (USB detect + NAS mount)** — the
  privileged helper ran `nsenter -t 1 -m -- chroot /host …`, but `/host` is a
  container-only bind mount that disappears once nsenter switches to the host
  mount namespace, so `chroot /host` failed with "No such file or directory"
  and USB-detect/NAS-mount fell back to the manual path on the fresh .207. The
  helper now relies on the mount-namespace switch alone (after `nsenter -t 1
  -m`, `/` IS the host rootfs — /etc, /dev, /sys, /proc are the host's) with
  `env -i` + a full host-style PATH so host binaries in /usr/sbin (e.g.
  cryptsetup) resolve, and the NAS mkdir runs in the host namespace too. Manual
  fallbacks are unchanged.
- **Setup wizard: choosing autonomy=autonomous now enables `PI_AGENT_ENABLED`** —
  the autonomy save path (`PUT /api/v1/settings/policy`, used by both the wizard
  and Settings → Autonomy) writes an active `PI_AGENT_ENABLED=true` line when the
  profile is `autonomous`, so a fresh install no longer silently degrades to the
  judge/catalog (the 08-17 Doom-uninstall escalation: autonomous was saved but pi
  stayed off). The value is written bare (no inline `#` comment, which would
  become part of the value and break `read_env_file`'s parse); non-autonomous
  profiles leave the flag untouched.
### Security
- **Main web UI cert is now signed by the BareNOC Internal CA** — `deploy.sh`
  issues the main nginx vhost's server certificate from the same CA
  intermediate as the stepca vhost (leaf + intermediate chain; SANs:
  `app.barenoc.com` + `bareNOC.local` + `pocket-id.barenoc.local` + the
  appliance IP), replacing the old self-signed cert. A browser that trusts the
  `/onboard/root-ca.crt` root now trusts `https://<appliance>` (no more
  "Not Secure"). Idempotent: the cert is regenerated only when missing, still
  self-signed, expired, or missing the appliance-IP SAN.

### Ops
- `proxmox/setup-usb-backup.sh` gains `--yes` (skip the primary-disk re-prompt
  for guided-UI flows) and prints a machine-readable `RECOVERY_PASSPHRASE="…"`
  line so the UI can surface it exactly once.

## [2026.08.17.c] — 2026-08-17

### Added
- **Updates Schedule v2 — local-time hours + one-time OR recurring schedules** —
  the schedule hour/day is now interpreted in the appliance's LOCAL timezone
  (DST-safe, `zoneinfo`); a schedule can be `recurring` (daily/weekday at a
  local hour) or `onetime` (apply the next available update at a local
  date+time, fires once then clears). `update_schedule.conf` stays
  backward-compatible. UI labels everything "local time" + shows the current
  schedule ("every <weekday> at HH:MM local" / "Scheduled for <local
  datetime>") with Cancel.

### Changed
- **Controller URL defaults to the local gateway** — when UNIFI_URL is unset,
  Settings (and /unifi/config) now default to `https://<default-route
  gateway>:443` (the router — where the UniFi controller usually lives) and
  prefill the field.

### Fixed
- **'Available' required a genuinely NEWER version** — a check that caught the
  manifest mid-propagation (latest=.a while running .b) flagged an update →
  the banner showed a downgrade as available. CalVer ordering now gates
  `available`.
- **Onboarder no longer hangs on a stale /root/.step** — `step ca bootstrap`
  now runs with `--force` (an old CA's leftover state used to open an
  interactive overwrite prompt and wait forever).
- **ISO seed: first-boot now runs end-to-end** — the fatal grub re-run was
  removed (curtin registers NVRAM itself), the first-boot unit's
  `After=cloud-init.target` was dropped (a systemd ordering cycle silently
  deleted its start job), and the provision script's crontab line no longer
  aborts under `set -e` on a fresh user.

## [2026.08.17.b] — 2026-08-17

### Added
- **Juniper closes tickets** — a new `close` directive ("close TKT-…" /
  "close the ticket", resolving to the most recent active ticket when no id
  is given) closes the ticket with the same owner/operator/admin gate as
  pause/resume (a tenant closes their own), writes a `closed` work note +
  `ticket_closed` audit event, and replies "Done — TKT-… is closed."
  Juniper's intake reply now ends with "View TKT-… →" so the opened ticket is
  one tap away.
- **Chat lives inside the app shell on desktop + Juniper fronts the chat** —
  `/chat` now renders inside the app shell (sidebar/nav intact) on wide screens
  and stays the standalone full-screen page on mobile, auto-detected by screen
  width (media query + resize listener). The sidebar reuses a new shared
  `_sidebar_nav.html` partial so `base.html` and the chat share one source of
  truth for the nav. A new chat opens at **Juniper, the Queue Manager** — her
  greeting + help, and her responder (queue/status, summarize TKT-…, intake →
  judged ticket, pause/resume/note directives) — instead of Lily; Lily remains
  the technician inside ticket threads. Existing ticket threads + the #16
  ticket-status and #17 Dashboard-button fixes stay reachable via a
  "Your tickets" tab.
- **Unified device-add model (design + scaffold)** — one coherent model for
  adding and controlling devices of ANY type: a device record now carries a
  `device_type` taxonomy (`server`/`switch`/`ap`/`router`/`camera`/`iot`/`other`)
  plus a `channels` capability list (`agent`/`vendor_api`/`ssh`/`unifi`/`snmp`/
  `monitor`). `docs/device_adoption_model.md` is the contract: fingerprint →
  suggested type + **security-first channel recommendation** (agent(mTLS) >
  vendor_api(TLS) > ssh key-only > unifi > snmpv3 > monitor-only; plaintext
  SNMPv2c/HTTP = explicit warning), one job relay with per-channel executors,
  and three unified add paths (SSH / agent / API).
- **Capability validator** — `action_validator` now declares per-action
  required channels (`reboot_device` needs `ssh|agent|snmp|vendor_api`,
  `collect_logs`/`apply_patch` need `ssh|agent`, `snmp_poll` needs `snmp`, …);
  `validate_job` enforces them when the target device has known channels.
  `ping`/`status`/`fingerprint` stay channel-less (monitor-only cameras keep
  them).
- **SNMP + vendor_api executor skeletons** — `scripts/snmp_executor.py`
  (GET/SET, v2c + v3) and `scripts/vendor_api.py` (adapter registry; Juniper/
  Cisco/HP-ONVIF/IoT stubs). Concrete vendor adapters are follow-up work.
- **Updates Schedule v2 — local-time hours + one-time OR recurring schedules**
  — the schedule hour/day is now interpreted in the appliance's LOCAL timezone
  (TZ from .env, fallback UTC) instead of container UTC, and the schedule gains
  a `mode`: `recurring` (the existing daily/weekday + hour, now local) or
  `onetime` (apply the next available update at a local date+time, fire once,
  then mark `fired` + self-disable — persisted, survives restart, cancelable).
  `update_schedule.conf` stays backward-compatible (a mode-less file = recurring).

### Changed
- **Update schedule runs in local wall-clock time** — `System → Updates →
  Schedule` labels everything "local time" and shows the current schedule
  ("every <weekday> at HH:MM local" / "Scheduled for <local datetime>") with a
  **Cancel** button for a one-time schedule. The recurring UX is otherwise
  unchanged.
- **Chat home screen reads as one history, not "multiple sessions"** — the
  `/chat` list now shows two clearly-labeled sections — **Front desk —
  Juniper** (the DM conversation) at the top and **Your tickets** (each
  ticket = one thread) below — instead of two tabbed sessions. TKT-… ids in
  Juniper's replies render as links that jump straight to the ticket thread,
  so a reply about a ticket connects to the right conversation.
- **Devices page relabel (08-17)** — top actions are now **+ Add a device
  record** · **🔐 Enroll a device — run the script on it** · **Register via
  API**; card actions are **Take ownership** (claim) vs **Enable control**
  (adopt), with one-line tooltips. Sections re-worded around "claimed (owned)"
  vs "controlled (channel connected)" vs "monitored (owned, no channel)".
  Fingerprint cards show the ranked channel recommendation + warnings.
- `GET /api/v1/devices` responses now include the device's effective `channels`.
- **Devices page two-state layout (08-17)** — the page now shows exactly two
  states: **Unclaimed** (a compact list — name/IP/vendor/type + inline
  Take ownership / Enable control / Identify / Fingerprint) and **Onboarded**
  (the main grid of EVERY claimed device, control channels or not). The
  "Monitoring Only" limbo section is gone. Onboarded cards show channel badges
  (#24 model) + control actions per channel + a **🔔 monitor toggle** that
  flips `notify_state_changes` (the alerting engine's existing opt-in) so a
  device participates in down/recovery alerts + outage management; a
  monitor-only camera/IoT device is Onboarded with just the toggle + ping/status.
  A **Monitored** filter chip lists only toggled-ON devices — a filter within
  the two-state model, not a third bucket.

### Changed
- **Updates management moved to the System tab; the Dashboard shows a release
  banner** — the full Updates controls (Update now / Schedule / Rollback /
  current version / last in-app update / live progress bar) moved
  from the Dashboard into a prominent **Updates** section on the System page
  (`/system#updates`). The Dashboard card is removed and replaced by a slim
  banner that appears only when a release is available ("📦 v… is available —
  Update", linking to the System section). Check Now is gone entirely — the
  auto-check-on-load still drives the banner; `POST /api/v1/updates/check`
  remains for tests/integrations.
- **Pi sessions now point at the sanctioned device scripts + no double-dispatch**
  — the pi system context (agent runner) now tells the agent to use the
  ready-made scripts under `/opt/barenoc/scripts` (`device_ssh.sh`,
  `ping_check.sh`, `collect_logs.sh`, …) for ANY device operation and states
  plainly that the agent API service account (`/opt/barenoc/agent/credentials`,
  `AGENT_TOKEN`, `/api/v1/auth/*`) is for the appliance's own scripts, NOT the
  pi agent — stopping the 08-17 incident where a session burned its whole run
  reverse-engineering API auth (~40 repeating notes). Ticket tasks (TKT-…)
  are answered from the ticket + work notes directly.
- **One pi session per ticket** — the worker skips a second dispatch when a
  ticket already has an active pi run (a `running/TKT-….json` job file, or an
  `auto_execute` pi-dispatch note with no terminal result after it) and posts
  “already working on this — I'll update you when it finishes”; the runner
  additionally merges duplicate pi-task launches per ticket so failover/retry
  can't double-spawn. The stalled-job watchdog still escalates a genuinely-stuck
  run, and MAX_CONCURRENT semantics for different tickets are unchanged.
- **Friendly, customer-facing chat tone** — the chat now shows short, plain,
  human progress updates instead of pi internals. The pi system context asks
  for one-sentence, customer-facing progress notes ("Let me find your
  laptop…", "Connecting now…", "Installing now…") and a direct final answer
  (what was done + how to use it) with no meta-narration. A safety net in the
  agent runner (`_post_progress`) replaces technical-looking progress notes
  (paths, `sudo`/`uid`/`NOPASSWD`/`dnf`/`apt`/`curl`, `api/`, `localhost`,
  `*.json`, backticked commands, IPs, long jargon) with a friendly generic
  before they reach the chat — the technical original stays in the pi session
  transcript and runner log. Final answers are cleaned of meta-narration
  prefixes ("Lily finished:", "Here's my final answer to the customer",
  "Here's what I found:", trailing `---` fences) before posting to the ticket
  (`src/api/tone_filter.py`).

### Fixed
- **Reports KPIs are now days-windowed + honest (08-17)** — the **Est.
  manned-NOC cost** now scales with the Last-*days dropdown (support tickets
  — manual/chat, not system `auto` tickets — created in the window × the
  configured rate/hours-per-ticket, instead of a resolved count that froze
  when every resolution landed in one week). **AI support spend** reports the
  real tracked catalog-path LLM cost at full precision (sub-cent values stay
  visible) and is labeled **"AI support spend only counts catalog-path LLM
  usage; pi/Lily sessions aren't metered yet"** — the runner still doesn't
  report pi token usage (follow-up open item, not fabricated). **Avg
  resolution time** is now the average of support tickets *closed in the
  period* (system auto-tickets and backdated/negative resolutions excluded);
  **time to first response** counts only a real customer-facing reply
  (ai_tech_feedback / agent_progress / agent_completed / customer_input /
  escalated / completed / closed) — the customer's own messages, internal
  pipeline notes and auto-closes no longer fabricate a response time.
  Definitions are shown in the KPI card tooltips + every export.
- **Chat scroll regression restored** — the Juniper front-desk conversation
  now preserves the reading position on its 4s poll exactly like ticket
  threads (stick to bottom only when already within 80px, otherwise restore
  the previous distance-from-bottom); the 08-13 fix applies to both the
  desktop (embedded shell) and mobile paths.
- **Scheduled updates fired at the wrong hour (UTC vs local)** — the scheduler
  compared `datetime.utcnow()` against the configured hour, so a "2 AM" schedule
  fired at 10 PM EDT. The hour/day and any one-time datetime are now wall-clock
  in the appliance's TZ (`.env TZ`, fallback UTC); the UTC scheduler converts
  local → UTC (DST-safe via `zoneinfo`) before comparing.
- **Pi progress notes no longer cut at 250 chars mid-sentence** — the agent
  runner's live-progress relay sliced every streamed pi message to the first
  three lines capped at 250 chars (and `_post_progress`/`add_progress_note`
  sliced again at 300), so a real answer (e.g. a `dnf check-update` result) was
  stored as a mystery fragment with no ellipsis. The cap is now 2000 chars in
  BOTH layers (`src/agent/runner.py` + `src/api/routes/tickets.py`, which must
  agree), any truncation appends a Unicode ellipsis (…) on a word boundary, and
  the runner only posts COMPLETE streamed messages (terminal `stopReason` — a
  still-streaming `pending` message is skipped, never posted mid-word). The
  final answer path (`out[:20000]`) is untouched.
- **Dashboard Updates card showed a stale version + a permanent "Complete"
  banner** — `/api/v1/updates/status` now always reports the LIVE installed
  version (a stale `status.json` check result can no longer win), and a
  terminal `progress.json` `done` stage is annotated `confirmed` so the card
  renders the steady "up to date" state instead of re-showing the
  "✅ Complete / 100%" banner on every load. The scheduler's once-per-transition
  completion email is unaffected (the raw progress stage stays visible to the
  watcher). The Dashboard card also auto-refreshes a stale persisted check on
  load and relabels the persisted result as "Last in-app update".
- **Performance & Reporting tz bug** — `_hours()` in
  `src/api/routes/dashboard.py` normalized every datetime to naive UTC before
  subtracting, fixing the `TypeError: can't subtract offset-naive and
  offset-aware datetimes` that 500'd the reports + performance widgets whenever
  a ticket had a `resolved_at` (mixed naive/aware rows).
- **Topology shows offline adopted UniFi gear** — an adopted device (e.g. a U6
  Mesh AP) that has dropped off the controller's live `stat/device` list still
  appears in the Devices topology, marked offline (red dashed styling) instead
  of disappearing.
- **Action gating on the Devices page** — "Enable control" / "Connect channel"
  are hidden when the device already has a control channel (ssh/agent/unifi/
  snmp/vendor_api), and Fingerprint is no longer offered on Onboarded devices
  (it is a discovery action for Unclaimed only). `monitor` is always present,
  so a control channel = anything beyond `monitor`
  (`device_adoption_model.md` §8).

## [2026.08.17.a] — 2026-08-17

### Added
- **`ticket_status` action — Lily answers "status on TKT-…"** — a new read-only
  catalog action (`scripts/ticket_status.sh`) looks up a ticket's live status
  by its `TKT-…` id via the existing `GET /api/v1/tickets/{id}/status`
  endpoint (derived stage + idle age + last note). A chat message that names a
  ticket with a status/where-at intent now routes deterministically to this
  action in every profile instead of spawning a device-action ticket
  (GitHub #16).

### Fixed
- **Chat ticket references (#16)** — "status on TKT-…" / "where's TKT-… at" /
  "is TKT-… done?" now answer read-only from the ticket's derived status
  instead of escalating with a confusing `device_status` error.
- **Mobile chat: Dashboard button on the list view (#17)** — the chat list
  screen now shows the Dashboard button (it was only visible inside a thread).

### Changed
- **Clearer escalation text** — the human-facing escalation line is now
  "Lily needs a human for this one: …" instead of the raw executor reason.

### Ops
- **ISO installer: grub-install late-command is now a conditional fallback
  (#19)** — curtin's own install-grub hook succeeds on current builds
  (NVRAM entry + shim-signed installed); the seed's unconditional re-run was
  failing with grub-install exit 3 and aborting installs. It now re-runs only
  when the `ubuntu` boot entry is actually missing (preserves the 08-14
  fallback for hosts where curtin can't write NVRAM). Found during the .i
  ISO revalidation.

## [2026.08.16.i] — 2026-08-16

### Added
- **Juniper — Queue Manager bot (Phase 1)** — a real chat entity (`User.is_bot`,
  idempotently seeded at startup, messageable via `/api/v1/chat`). The worker
  runs a Juniper responder alongside the ticket loop: deterministic
  status/queue snapshots, LLM ticket summaries (deterministic fallback),
  casual intake that opens a real ticket with a judged priority
  (`source=chat`), and pause/resume/note-to-tech directives the worker honors.
- **Pause directives** — `pause TKT-… until <time>` / `resume TKT-…` write
  `pause_until` / `pause_cleared` work notes; the worker skips paused tickets
  in every poll loop (including the stalled-job watchdog), and the chat thread
  renders the directive as a visible system line (⏸ / ▶️).

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
- **Scan Network scanned the wrong subnet** — discovery defaulted to a
  hardcoded /24; now derived from APPLIANCE_IP, pinned by the installer, and
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
