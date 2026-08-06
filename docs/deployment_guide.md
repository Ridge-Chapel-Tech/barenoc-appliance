# BareNOC — Setup & Deployment Guide

**Version:** 2026.08 · **Applies to:** all current releases
**Audience:** installers, operators, and anyone standing up BareNOC.

---

## What BareNOC is

BareNOC is a **single-node network operations appliance**: one Linux machine
running a **5-container Docker stack** (API + web UI, poll worker, scheduler,
nginx, Pocket ID) plus **one host-side service** (`pi-agent-runner`) that
executes the action scripts (ping, SNMP, reboot, UniFi control, the Pi Coding
Agent). All state lives in SQLite + encrypted credential files under
`/opt/barenoc/` — no external services are required.

Everything below deploys **the same application**. The three tracks differ
only in *how you provision the machine* and *which backup layers apply*:

| | A — Shipped hardware | B — Your VM | C — Bare metal |
|---|---|---|---|
| Machine | The rack appliance (Mini PC, we provision it) | Your hypervisor's VM | Your own server |
| Host OS | Proxmox VE + Ubuntu 24.04 VM | Your Ubuntu 24.04 VM | Ubuntu 24.04 on the metal |
| Installer | `proxmox/barenoc-appliance.sh` (one-shot) | Manual + `deploy.sh` | Manual + `deploy.sh` |
| Docker stack | ✅ identical | ✅ identical | ✅ identical |
| Agent runner | ✅ host-side | ✅ host-side | ✅ host-side |
| VM snapshots + encrypted USB stick | ✅ (appliance only) | ➖ (use your hypervisor's snapshots) | ➖ (use restic/Borg/Timeshift) |
| App-data archive (every 6 h) | ✅ | ✅ | ✅ |

**Hardware sizing** (from `docs/appliance/hardware_sizing.md`):

| Profile | Endpoints | vCPU | RAM | Disk | Typical box |
|---|---|---|---|---|---|
| **s** | ≤10 | 1 | 2 GB | 30 GB | Mini PC (N100/N150); can run bare-metal, no Proxmox |
| **m** | ≤50 | 2 | 4 GB | 40 GB | Mini PC (Ryzen 5 / i5); the live reference config |
| **l** | ≤200 | 4 | 8 GB | 80 GB | NUC / small tower (i5/i7) |
| **xl** | ≤500 | 6 | 16 GB | 160 GB | Small tower / server |

> **Example addresses** in this guide use the reserved documentation range
> `192.0.2.0/24` (RFC 5737) — substitute your own IP plan.

---

## Part A — Shipped with hardware (the BareNOC appliance)

The rack unit ships **pre-provisioned**: Proxmox VE on the Mini PC, the
BareNOC VM, and the software already installed. What remains is power-on,
network assignment, and configuration.

### A1. Unbox & rack
Follow `docs/appliance/assembly_guide.md` (10-inch rack) — every cable is
labeled; the sealed **rack card** in the lid holds the secrets you need later
(the LUKS USB-stick passphrase and the Proxmox root password).

### A2. First boot of the Proxmox host
1. Connect the rack's uplink (the appliance LAN goes to the customer network).
2. Power on. Proxmox boots the VM automatically (auto-start is configured).
3. Find the VM's IP on your network (router DHCP table / console: `qm terminal 100`).
   The reference layout uses a static IP like `192.0.2.207`; the web UI is
   **https://<vm-ip>/**.

### A3. Provision a fresh appliance (re-image / pre-ship)
If you are standing up a *new* appliance from the repo (factory/pre-ship), run
the one-shot installer **on the Proxmox host**:

```bash
# from a checkout of the repo on the Proxmox host (or via ssh 'bash -s' < script)
bash proxmox/barenoc-appliance.sh \
  --ip 192.0.2.210 \          # required: static IP for the VM
  --ssh-key ~/.ssh/id_ed25519.pub \
  --profile m \                 # s | m | l | xl (default m)
  --admin-password 'Change-Me-Now'   # optional; auto-generated otherwise
```

What it does: downloads/caches the Ubuntu 24.04 cloud image → creates a VM
sized by `--profile` with cloud-init (static IP, `barenoc` user + your SSH
key, qemu-guest-agent) → provisions Docker, the pi-agent user + Pi Coding
Agent runtime, the agent runner service, UFW, and the `/opt/barenoc` skeleton
→ waits for the provisioning marker → runs `./deploy.sh barenoc@<ip>` to
install the application. **Result: a ready appliance at https://<ip>**, no
manual steps. (`--skip-app` provisions the OS only; run `./deploy.sh`
yourself later. A fully manual VM path — for hosts without the script — is in
`docs/appliance/barenoc_vm_create.md`.)

### A4. First login & configure
1. Log in with `admin` + the seeded password (the UI forces a change).
2. Configure in **Settings** (all audit-logged):
   - **UniFi** — controller URL/credentials, auto-sync interval, auto-adopt.
   - **API Keys** — the active LLM provider(s) (DeepSeek/Gemini/Anthropic/Ollama).
   - **Email** — Gmail OAuth2 (client id/secret/refresh token) + recipients/schedule.
   - **General** — site ID, customer name, timezone, bot names.
   - **Identity** — Pocket ID passkeys (enroll your first passkey!), device groups.
   - **Tickets / Autonomy Policy** — lifecycle + approval profile for your site.

### A5. Host-side finishing (appliance-specific)
- **Encrypted USB backup stick (Layer 3):** plug the included stick into the
  Proxmox host and run **once** per stick:

  ```bash
  # on the Proxmox host (destructive — wipes the stick)
  bash /usr/local/bin/setup-usb-backup.sh --dev /dev/sdX
  ```

  It creates the LUKS2 volume, writes the host keyfile
  (`/etc/barenoc-usb.key`, root-only) and prints a **recovery passphrase —
  write it on the sealed rack card / your password manager** (it is never
  stored on disk). Then verify **Settings → Backups** shows `🔐 LUKS2 · 2
  keyslots` and the schedule (default: weekly Wednesday 2 AM, configurable in
  Settings). First run: `bash /usr/local/bin/backup-to-usb.sh`.
- **Verify the agent runner:** `systemctl status pi-agent-runner` on the VM —
  active, runs as `pi-agent`. The autonomous "Lily" ticket mode requires
  `PI_AGENT_ENABLED=true` (set via Settings) + the Pi Coding Agent runtime
  (installed by the appliance installer).

### A6. Trial lifecycle & factory reset
Trial provisioning, conversion to paid, and wiping for the next customer:
`docs/operations/trial_lifecycle.md` and `docs/runbook/factory_reset.md`
(host-side `factory-reset.sh` restores from the pre-ship snapshot).

### A — Verification checklist
- [ ] `https://<vm-ip>/api/v1/health` → `200` (all 5 containers up)
- [ ] First login forces a password change
- [ ] UniFi sync discovers gear (`Settings → UniFi → Test connection`)
- [ ] A test ticket completes (P4 "what time is it?" → Lily/worker answers)
- [ ] `Settings → Backups` shows stick present + encrypted + last backup
- [ ] A manual `backup-to-usb.sh` run completes; the archive appears on the stick

---

## Part B — VM deployment (your own hypervisor)

You run BareNOC on a VM you create — Proxmox, ESXi, KVM, Hyper-V, cloud — or
on a VM on the same box you manage. **The application itself is identical to
Part A; you provide the platform.**

### B1. Create the VM
- **Guest OS:** Ubuntu 24.04 LTS Server (cloud image or installer ISO).
- **Sizing:** use the profile table above (`m` = 2 vCPU / 4 GB / 40 GB is the
  reference). Give the VM a **static IP** and a DNS-resolvable hostname.
- Install the **qemu-guest-agent** in the VM if your hypervisor supports it
  (snapshots + clean shutdown).

### B2. Install the host prerequisites (inside the VM)
```bash
# Docker Engine + compose v2
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"

# Agent tooling used by the action scripts
sudo apt-get install -y nmap snmp snmp-mibs-downloader jq git
```

### B3. Get the code
```bash
sudo mkdir -p /opt/barenoc && sudo chown "$USER" /opt/barenoc
# from the release tarball (recommended) or a git clone:
git clone https://github.com/<org>/BareNOC.git /opt/barenoc   # or: extract tarball here
```

### B4. Configure
```bash
cd /opt/barenoc
cp src/.env.example .env
chmod 600 .env
$EDITOR .env     # set: JWT_SECRET, ADMIN_PASSWORD (min 8 chars), the LLM
                 # provider block, UNIFI_* / GOOGLE_* if used, TZ, SITE_ID, CUSTOMER_NAME
```
`.env` holds **all** config + secrets; the Settings UI rewrites it on every
save (so most values can also be set in the web UI after first login). The
seeded `admin` login uses `ADMIN_PASSWORD`; the UI forces a change on first
login.

### B5. Deploy the application
Two equivalent paths:

**Option 1 — on the box itself (simplest for a single self-host):**
```bash
cd /opt/barenoc
docker compose up --build -d
# wait for health, then provision the agent service account:
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1/api/v1/health   # → 200
sudo bash /opt/barenoc/scripts/setup_agent_credentials.sh
```

**Option 2 — from a control box (the dev/deploy flow):**
```bash
# on your workstation: ./deploy.sh <user>@<vm-ip>
./deploy.sh barenoc@192.0.2.210
```
`deploy.sh` rsyncs the code, rebuilds the stack, reloads nginx, provisions
the agent credentials, and syncs the runner (sudo steps print a manual hint
if passwordless sudo isn't configured — see B6).

### B6. Install the host-side agent runner
```bash
# create the service account + runtime dirs
sudo useradd -r -m -s /bin/bash pi-agent
sudo mkdir -p /opt/barenoc/agent /opt/barenoc/volumes/logs/agent
sudo chown -R pi-agent:pi-agent /opt/barenoc/agent /opt/barenoc/volumes/logs/agent

# install the unit + start
sudo cp src/agent/pi-agent-runner.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pi-agent-runner
```
> **Autonomous "Lily" mode** (`PI_AGENT_ENABLED=true`) additionally needs the
> Pi Coding Agent runtime under `/home/pi-agent/.local/share/pi-node`
> (see `docs/02_iac_and_setup_manifests.md` §1.1 / the wiki autonomy page).
> Without it, the safe-action scripts (ping/SNMP/reboot/UniFi) still work —
> only the open-ended `pi_task` action is unavailable.

### B7. Backups (your hypervisor is your Layer 2)
- **App data (Layer 1)** — automatic every 6 h, 30-day retention, in
  `/opt/barenoc/backups/` (a `0600` archive with the DB, secrets, keys,
  certs). Restore anywhere: `scripts/restore_app.sh --apply <archive>`.
- **Machine level** — use **your hypervisor's snapshots** for the VM. That is
  the equivalent of the appliance's daily `vzdump`.
- **Off-site** — copy `/opt/barenoc/backups/` with your own tool (restic,
  rclone to S3, …). No Proxmox host = no encrypted USB-stick layer; the
  Settings → Backups page will correctly show the "not an appliance
  deployment" notice.

### B8. Post-install configuration
Same as A4 (UniFi, LLM, Email, General, Identity, Tickets/Autonomy).

### B — Verification checklist
- [ ] `https://<vm-ip>/api/v1/health` → `200`
- [ ] First login forces a password change
- [ ] Agent runner active (`systemctl status pi-agent-runner`)
- [ ] A test ticket completes end-to-end
- [ ] First app-data backup exists (`ls /opt/barenoc/backups/`)
- [ ] Hypervisor snapshot taken post-install (your Layer 2)

---

## Part C — Bare metal install

BareNOC runs directly on a Linux server — no VM layer. This is the lightest
option (the `s` profile explicitly calls out "can run bare-metal without
Proxmox") and suits home/small-office owners who already run a box.

### C1. Prereqs
- Ubuntu 24.04 LTS Server installed on the machine (or any distro with
  Docker + systemd; Ubuntu is the tested reference).
- Static IP, DNS, and a user with sudo.
- Sizing: profile table above (`s` = 1 vCPU / 2 GB / 30 GB is enough for a
  home network; `m` for an SMB).

### C2–C5. Identical to B2–B5
Install Docker (`get.docker.com`) + agent tooling, get the code to
`/opt/barenoc`, configure `.env`, deploy with **Option 1** (`docker compose
up --build -d` + `setup_agent_credentials.sh`) or **Option 2** (`./deploy.sh
user@<ip>` from a control box).

### C6. Agent runner — identical to B6
Same `pi-agent` user, unit file, and runtime (autonomous Lily mode optional).

### C7. Backups (no hypervisor — Layer 1 + your own tool)
- **Layer 1 app-data archive** — automatic (every 6 h, 30 days). This is your
  portable recovery: `restore_app.sh --apply` on **any** Docker box.
- **Machine level** — pick one: **restic** (local disk + S3/off-site),
  **BorgBackup**, **Timeshift** (system snapshots), or a plain nightly rsync
  of `/opt/barenoc/` + the archive. Keep a copy **off the same disk**.
- No Proxmox host → no vzdump / encrypted USB layers; Settings → Backups
  shows the BYO notice and disables the stick schedule.

### C8. Security hardening (bare metal has no Proxmox firewall by default)
```bash
sudo ufw allow OpenSSH && sudo ufw allow 443/tcp && sudo ufw allow 8443/tcp && sudo ufw enable
```
- Key-only SSH (`PermitRootLogin prohibit-password`), a non-root admin user.
- `.env` stays `0600`; backups stay `0600` (both enforced by the scripts).

### C — Verification checklist
- [ ] `https://<ip>/api/v1/health` → `200`
- [ ] UFW active, only 22/443/8443 open
- [ ] Agent runner active
- [ ] A test ticket completes
- [ ] A restic/Borg/Timeshift backup exists **and** the Layer-1 archive is
      copied off-box

---

## Common: architecture, config, updates, troubleshooting

### Services & ports
| Service | Role | Port |
|---|---|---|
| `barenoc-nginx` | TLS reverse proxy + Pocket ID at 8443 | 443, 8443 |
| `barenoc-api` | FastAPI + web UI (all Settings writes land in `.env`) | internal 8000 |
| `barenoc-worker` | ticket pipeline, LLM calls, alerting | — |
| `barenoc-scheduler` | UniFi auto-sync, periodic jobs | — |
| `barenoc-pocket-id` | passkey/SSO identity | behind nginx |
| `pi-agent-runner` | host-side job executor (systemd, user `pi-agent`) | — |

Directory layout: `/opt/barenoc/{api,worker,scheduler,nginx,scripts,agent,client}` +
`volumes/{db,logs,secrets,branding,pocket-id,backup_status}` + `jobs/` +
`backups/`.

### Config reference (`.env` — `src/.env.example` is the template)
- **Core:** `JWT_SECRET`, `ADMIN_USERNAME/PASSWORD`, `DATABASE_URL`, `TZ`,
  `SITE_ID`, `CUSTOMER_NAME`, `BOT_QUEUE_MANAGER_NAME` (Juniper),
  `BOT_ASSISTANT_NAME` (Lily), `CHAT_CLIENT_ENABLED`.
- **LLM:** `LLM_PROVIDER_<NAME>_TYPE/_API_KEY/_CHAT_MODEL/_REASONER_MODEL`,
  `LLM_PROVIDER_ORDER` (failover chain), `LLM_POLICY_*` (autonomy),
  `LLM_RETRY_*`.
- **UniFi:** `UNIFI_URL/USER/PASSWORD`, `UNIFI_AUTOSYNC_*`, `UNIFI_AUTO_ADOPT`.
- **Email:** `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN/SENDER`, `ALERT_EMAIL`,
  per-type recipients, digest/EOD schedule.
- **Pocket ID:** `APP_URL`, `OIDC_*` (set in Settings → Identity).
- **Backups:** managed in Settings → Backups (the Proxmox host reconciles its
  cron from the VM every 10 min — appliance only).

### Updating
- **Application code:** `./deploy.sh <user>@<ip>` (rsync → rebuild → health
  check → agent credentials → runner sync). Always snapshot the VM (or take
  your hypervisor's snapshot) before an update.
- **OS + Docker images on the machine:** `sudo /opt/barenoc/scripts/barenoc-update.sh`
  (`--dry-run` to preview, `--no-apt` for images only; never auto-reboots).
- **Rollback:** VM snapshot rollback (A/B), `restore_app.sh --apply` (any
  track), or rebuild from the last known-good `.env` + archive.

### First-test / smoke checklist (all tracks)
- [ ] `GET /api/v1/health` → 200
- [ ] Login → forced password change
- [ ] `Settings → UniFi → Test connection` → `connected: true`
- [ ] Ticket: *"what is the current local time?"* → answered in-thread
- [ ] `Settings → Backups` status is truthful for your deployment type
- [ ] Agent runner active; `md5sum /opt/barenoc/agent/runner.py` matches the
      repo if you changed the runner

### Troubleshooting & operations
- `docs/runbook/troubleshooting.md` — the common failure ladder.
- `docs/03_post_deployment_runbook.md` — day-2 ops, restore, recovery.
- `docs/security/secret_management.md` — credential handling + rotation.
- `docs/operations/update_pipeline.md` — the three-layer update model.

---

*End of guide. Track-specific details: `docs/appliance/*` (hardware),
`docs/02_iac_and_setup_manifests.md` (manifests), `docs/system_acceptance_test.md`
(the formal test suite).*
