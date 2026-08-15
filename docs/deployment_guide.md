# BareNOC — Setup & Deployment Guide

**Version:** 2026.08 · **Applies to:** all current releases
**Audience:** installers, operators, and anyone standing up BareNOC.

---

## Table of Contents

- [Who is this guide for?](#who-is-this-guide-for)
- [What BareNOC is](#what-barenoc-is)
- [Part A — Install on your existing Proxmox server](#part-a)
  - [A1. Prerequisites](#a1-prerequisites)
  - [A2. Get the release onto the host](#a2-get-the-release-onto-the-host)
  - [A3. Run the one-shot installer](#a3-run-the-one-shot-installer)
  - [A4. First login & configure](#a4-first-login-configure)
  - [A5. Verification checklist](#a5-verification-checklist)
- [Part B — Other hypervisors & cloud (manual VM install)](#part-b)
  - [B1. Create the Ubuntu 24.04 VM — per platform](#b1-create-the-ubuntu-2404-vm-per-platform)
  - [B2. Common manual install (all platforms)](#b2-common-manual-install-all-platforms)
  - [B3. Backups & post-install](#b3-backups-post-install)
  - [B — Verification checklist](#b-verification-checklist)
- [Part C — Shipped BareNOC appliance (customer quickstart)](#part-c)
  - [C1. Connect & power on](#c1-connect-power-on)
  - [C2. Find the appliance IP](#c2-find-the-appliance-ip)
  - [C3. Complete setup](#c3-complete-setup)
  - [C4. Host-side finishing (appliance-specific)](#c4-host-side-finishing-appliance-specific)
  - [C — Verification checklist](#c-verification-checklist)
- [Common — config, updates, troubleshooting](#common-config-updates-troubleshooting)
  - [Services & ports](#services-ports)
  - [Config reference (`.env` — `src/.env.example` is the template)](#config-reference)
  - [Identity & DNS (all tracks)](#identity-dns-all-tracks)
- [Updating](#updating)
  - [First-test / smoke checklist (all tracks)](#first-test-smoke-checklist-all-tracks)
  - [Troubleshooting & operations](#troubleshooting-operations)

<a id="who-is-this-guide-for"></a>
## Who is this guide for?

| Your situation | Start at |
|---|---|
| You **already run Proxmox VE** and want a BareNOC appliance VM | **Part A** — the standard install |
| You use **ESXi, KVM, Hyper-V, a cloud VM, or any plain VM** | **Part B** — manual VM install |
| You **bought a BareNOC appliance** (pre-provisioned hardware) | **Part C** — quickstart (plug in & set up) |
| You want config, identity/DNS, updates, or troubleshooting | **Common** at the end |

<a id="what-barenoc-is"></a>
## What BareNOC is

BareNOC is a **single-node network operations appliance**: one Linux machine
running a **7-container Docker stack** (api + web UI, poll worker, scheduler,
nginx, Pocket ID, the step-ca device CA, and CoreDNS split-horizon DNS) plus
one **host-side service** (`pi-agent-runner`) that executes the action scripts
(ping, SNMP, reboot, UniFi control, the Pi Coding Agent). All state lives in
SQLite + encrypted credential files under `/opt/barenoc/` — no external
services are required.

**Hardware sizing** (endpoints ≈ adopted/managed network devices + clients):

| Profile | Endpoints | vCPU | RAM | Disk | Typical box |
|---|---|---|---|---|---|
| **s** | ≤10 | 1 | 2 GB | 30 GB | Mini PC (N100/N150) |
| **m** | ≤50 | 2 | 4 GB | 40 GB | Mini PC (Ryzen 5 / i5) — the reference config |
| **l** | ≤200 | 4 | 8 GB | 80 GB | NUC / small tower (i5/i7) |
| **xl** | ≤500 | 6 | 16 GB | 160 GB | Small tower / server |

> **Example addresses** in this guide use the reserved documentation range
> `192.0.2.0/24` (RFC 5737) — substitute your own IP plan.

---

<a id="part-a"></a>
## Part A — Install on your existing Proxmox server

The standard BareNOC install. You already have a **Proxmox VE host running**;
the one-shot installer creates the appliance VM, provisions the OS + the Pi
Agent runtime, and deploys the application — one command, no manual steps in
between. All commands run **over SSH, in a terminal on the Proxmox host** (the
web UI at `https://<proxmox>:8006` is only needed to watch the VM / console).

<a id="a1-prerequisites"></a>
### A1. Prerequisites

- **Proxmox VE 8.x or newer** running (web UI at `https://<host>:8006`).
- **`git` on the host** (minimal installs lack it): `apt-get update && apt-get install -y git`
- **Host internet access** — the installer downloads the Ubuntu 24.04 cloud
  image (~600 MB, cached once) and the VM installs Docker + tooling.
- **An SSH keypair on the host**: `ls ~/.ssh/id_ed25519.pub` (create with
  `ssh-keygen -t ed25519` if missing). The host uses it to reach the VM.
- **A free static IP** for the appliance (e.g. `192.0.2.207`) + its gateway
  (default: first usable IP of the /24) and DNS (default `1.1.1.1`).
- A free VMID (the installer defaults to **1000**).

<a id="a2-get-the-release-onto-the-host"></a>
### A2. Get the release onto the host

The release repo is **private and invite-only** — you'll have received a
**collaborator invitation** on your GitHub account (check your email / GitHub
notifications; you need a free GitHub account). On the Proxmox host,
authenticate **as yourself** once and clone:

```bash
ssh root@<proxmox-ip>                    # from your workstation

# one-time auth on the host — the GitHub CLI (device flow) is easiest:
apt-get install -y gh && gh auth login   # scopes: repo

git clone https://github.com/Ridge-Chapel-Tech/barenoc-appliance.git /root/barenoc
```

> No GitHub CLI on the host? Use a **personal access token** (GitHub →
> Settings → Developer settings → PAT, read access to that repo) in the URL:
> `git clone https://<you>:<PAT>@github.com/Ridge-Chapel-Tech/barenoc-appliance.git /root/barenoc`
>
> (git itself may need installing first: `apt-get update && apt-get install -y git`)

<a id="a3-run-the-one-shot-installer"></a>
### A3. Run the one-shot installer

```bash
cd /root/barenoc
bash proxmox/barenoc-appliance.sh \
  --ip 192.0.2.207 \                  # required: static IP for the appliance
  --ssh-key ~/.ssh/id_ed25519.pub \
  --profile m \                       # s | m | l | xl (default m)
  --admin-password 'Change-Me-Now'    # optional; auto-generated otherwise
```

What it does (≈10–15 min):

1. Downloads/caches the Ubuntu 24.04 cloud image.
2. Creates the VM sized by `--profile` with cloud-init (static IP, `barenoc`
   user + your SSH key, qemu-guest-agent, boot-enabled).
3. First boot provisions: Docker, the `pi-agent` user + Pi Coding Agent
   runtime, the `pi-agent-runner` service (enabled at boot), UFW (22/443/8443),
   and the `/opt/barenoc` skeleton.
4. Bootstraps `/opt/barenoc/.env` from `src/.env.example` — your
   `--admin-password` is the seeded admin login; `JWT_SECRET`, `APPLIANCE_IP`,
   `APPLIANCE_HOST` are injected.
5. Runs `./deploy.sh barenoc@<ip>` — the same single deploy path used for
   updates — containers up, agent credentials, runner sync.

**Result:** `https://<ip>` — log in as `admin` with the seeded password (the
UI forces a change). `--skip-app` provisions the OS only; bootstrap `.env` and
run `./deploy.sh barenoc@<ip>` yourself later.

<a id="a4-first-login-configure"></a>
### A4. First login & configure

1. **Open the web UI** in a browser on the same LAN: `https://<vm-ip>/`
   — the root URL shows the login page (password login works by IP; the
   appliance cert already covers the IP, so no domain needed). Log in with
   `admin` + the seeded password (the UI forces a change on first login).
2. **First-run wizard (fresh installs):** if the dashboard shows the setup
   banner, open `https://<vm-ip>/setup` — it walks you through account →
   LLM key → timezone → site name → alert email → autonomy profile →
   backups → adopt first device → share the chat URL.
3. **No real domain?** Password-only login works as-is — you can skip
   Identity/passkeys entirely (a home user doesn't need a domain). If you
   want passkeys, see Common → Identity & DNS (a cheap domain resolved
   *internally only* is enough).
4. **Before enrolling passkeys:** set **Settings → Identity** — your real
   domain for `APP_URL`/`APPLIANCE_HOST` (passkeys require a registrable
   domain + a trusted cert; `.local`/raw IPs fail).
5. Configure in **Settings** (all audit-logged):
   - **UniFi** — controller URL/credentials, auto-sync interval, auto-adopt.
   - **LLM Providers** — the active provider(s) (DeepSeek/Gemini/Anthropic/Ollama).
   - **Email** — Gmail OAuth2 (client id/secret/refresh token) + recipients/schedule.
   - **General** — site ID, customer name, timezone, bot names.
   - **Identity** — Pocket ID passkeys (enroll your first passkey!), device groups.
   - **Tickets / Autonomy Policy** — lifecycle + approval profile for your site.
   - **Dashboard → Updates** — check for releases, **Update now / Schedule /
     Rollback** (free & open — no key needed).

<a id="a5-verification-checklist"></a>
### A5. Verification checklist

- [ ] `https://<vm-ip>/api/v1/health` → `200` (all 7 containers up)
- [ ] First login forces a password change
- [ ] `systemctl status pi-agent-runner` on the VM → active (runs as `pi-agent`)
- [ ] UniFi sync discovers gear (`Settings → UniFi → Test connection`)
- [ ] A test ticket completes (P4 "what time is it?" → Lily/worker answers)
- [ ] Hypervisor snapshot taken post-install (your Layer 2)

---

<a id="part-b"></a>
## Part B — Other hypervisors & cloud (manual VM install)

BareNOC on a VM you create — **ESXi, KVM, Hyper-V, a cloud VM, or any plain
VM**. The application is identical to Part A; **you provide the platform and
follow the common manual path below.** No one-shot installer exists for these
yet — the steps are a one-time ~15 min manual setup.

<a id="b1-create-the-ubuntu-2404-vm-per-platform"></a>
### B1. Create the Ubuntu 24.04 VM — per platform

Common to all: **Ubuntu 24.04 LTS Server** (cloud image or installer ISO),
sizing from the profile table (`m` = 2 vCPU / 4 GB / 40 GB), a **static IP**,
and the platform's guest agent.

- **ESXi** — create a VM (guest OS: Linux / Ubuntu 24.04), attach the cloud
  image or ISO, size per profile; configure the static IP via cloud-init or
  guest customization; install **open-vm-tools** in the guest.
- **KVM / libvirt** — `virt-install` (or virt-manager) with the Ubuntu 24.04
  cloud image (`--cloud-init` for user/keys/IP); install **qemu-guest-agent**
  in the guest.
- **Hyper-V** — **Generation 2** VM, Ubuntu 24.04; static IP via cloud-init;
  install the Hyper-V Linux integration services (guest agent).
- **Cloud (AWS / Azure / GCP)** — launch an Ubuntu 24.04 instance, size per
  profile, assign a **static / elastic IP**; open **22, 443, 8443** in the
  security group / NSG / firewall (the web UI is 443, Pocket ID is 8443).
- **Any other VM** — same requirements (Ubuntu 24.04, sizing, static IP,
  guest agent if available).

<a id="b2-common-manual-install-all-platforms"></a>
### B2. Common manual install (all platforms)

Run these **inside the VM** (as a sudo user):

```bash
# 1. Docker Engine + compose v2, and the agent tooling
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
sudo apt-get install -y nmap snmp snmp-mibs-downloader jq git

# 2. get the code
sudo mkdir -p /opt/barenoc && sudo chown "$USER" /opt/barenoc
git clone https://github.com/<org>/BareNOC.git /opt/barenoc   # or extract a release tarball

# 3. configure .env  (holds all config + secrets; Settings rewrites it on save)
cd /opt/barenoc
cp src/.env.example .env && chmod 600 .env
$EDITOR .env    # set: JWT_SECRET, ADMIN_PASSWORD (min 8), the LLM provider
                # block, UNIFI_* / GOOGLE_* if used, TZ, SITE_ID, CUSTOMER_NAME

# 4. deploy — Option 1 (on the box): 
docker compose up --build -d
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1/api/v1/health   # → 200
sudo bash /opt/barenoc/scripts/setup_agent_credentials.sh
#    — or Option 2 (from a control box): ./deploy.sh <user>@<vm-ip>

# 5. install the host-side agent runner
sudo useradd -r -m -s /bin/bash pi-agent
sudo mkdir -p /opt/barenoc/agent /opt/barenoc/volumes/logs/agent
sudo chown -R pi-agent:pi-agent /opt/barenoc/agent /opt/barenoc/volumes/logs/agent
sudo cp src/agent/pi-agent-runner.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pi-agent-runner
```

> **Autonomous "Lily" mode** (`PI_AGENT_ENABLED=true`) additionally needs the
> Pi Coding Agent runtime under `/home/pi-agent/.local/share/pi-node` (see
> `docs/02_iac_and_setup_manifests.md` §1.1 / the wiki autonomy page). Without
> it the safe-action scripts (ping/SNMP/reboot/UniFi) still work — only the
> open-ended `pi_task` action is unavailable.

<a id="b3-backups-post-install"></a>
### B3. Backups & post-install

- **App data (Layer 1)** — automatic every 6 h, 30-day retention, in
  `/opt/barenoc/backups/` (0600 archive with DB, secrets, keys, certs).
  Restore anywhere: `scripts/restore_app.sh --apply <archive>`.
- **Machine level** — **your hypervisor's snapshots** (the equivalent of the
  appliance's daily `vzdump`).
- **Off-site** — copy `/opt/barenoc/backups/` with your own tool (restic,
  rclone to S3, …). No Proxmox host = no encrypted USB-stick layer; Settings →
  Backups shows the "not an appliance deployment" notice.
- **Post-install config** — same as A4 (UniFi, LLM, Email, General, Identity,
  Tickets/Autonomy).

<a id="b-verification-checklist"></a>
### B — Verification checklist

- [ ] `https://<vm-ip>/api/v1/health` → `200`
- [ ] First login forces a password change
- [ ] Agent runner active (`systemctl status pi-agent-runner`)
- [ ] A test ticket completes end-to-end
- [ ] First app-data backup exists (`ls /opt/barenoc/backups/`)
- [ ] Hypervisor snapshot taken post-install (your Layer 2)

---

<a id="part-c"></a>
## Part C — Shipped BareNOC appliance (customer quickstart)

The rack unit ships **pre-provisioned**: Proxmox VE on the Mini PC, the
BareNOC VM, and the software already installed. Setup is: **connect → power on
→ open the URL → configure.**

<a id="c1-connect-power-on"></a>
### C1. Connect & power on

1. Plug the appliance's **uplink** into your router or switch (the labelled
   LAN port).
2. Power on. The Proxmox host boots the VM automatically (auto-start is
   configured; first boot takes a couple of minutes).

<a id="c2-find-the-appliance-ip"></a>
### C2. Find the appliance IP

- **The rack card (sealed card in the lid) lists the static IP** of the
  appliance and the admin credentials — use that if it's set.
- Otherwise the appliance got an IP by DHCP; find it any of:
  - your **router's DHCP lease table** (look for hostname `barenoc`), or
  - **mDNS**: `ping bareNOC.local` from any machine on the network, or
  - the **console**: on the Proxmox host, `qm terminal 100` shows the login
    banner with the IP.

<a id="c3-complete-setup"></a>
### C3. Complete setup

1. Open **`https://<appliance-ip>/`** (accept the self-signed cert).
2. Log in as `admin` with the rack card's password (the UI forces a change).
3. Configure **Settings** in the same order as Part A4 — most importantly set
   your **real domain in Identity before enrolling passkeys**.

<a id="c4-host-side-finishing-appliance-specific"></a>
### C4. Host-side finishing (appliance-specific)

- **Encrypted USB backup stick (Layer 3):** plug the included stick into the
  Proxmox host and run **once** per stick:

  ```bash
  # on the Proxmox host (destructive — wipes the stick)
  bash /usr/local/bin/setup-usb-backup.sh --dev /dev/sdX
  ```

  It creates the LUKS2 volume, writes the host keyfile (`/etc/barenoc-usb.key`,
  root-only) and prints a **recovery passphrase — write it on the sealed rack
  card / your password manager** (it is never stored on disk). Then verify
  **Settings → Backups** shows `🔐 LUKS2 · 2 keyslots` and the schedule
  (default: weekly Wednesday 2 AM). First run:
  `bash /usr/local/bin/backup-to-usb.sh`.

<a id="c-verification-checklist"></a>
### C — Verification checklist

- [ ] `https://<appliance-ip>/api/v1/health` → `200`
- [ ] First login forces a password change
- [ ] UniFi sync discovers gear (`Settings → UniFi → Test connection`)
- [ ] A test ticket completes (P4 "what time is it?" → Lily answers)
- [ ] `Settings → Backups` shows stick present + encrypted + last backup
- [ ] A manual `backup-to-usb.sh` run completes; the archive appears on the stick

> Trial lifecycle & factory reset: `docs/operations/trial_lifecycle.md` and
> `docs/runbook/factory_reset.md` (host-side `factory-reset.sh` restores from
> the pre-ship snapshot).

---

<a id="common-config-updates-troubleshooting"></a>
## Common — config, updates, troubleshooting

<a id="services-ports"></a>
### Services & ports

| Service | Role | Port |
|---|---|---|
| `barenoc-nginx` | TLS reverse proxy + Pocket ID at 8443 | 443, 8443 |
| `barenoc-api` | FastAPI + web UI (all Settings writes land in `.env`) | internal 8000 |
| `barenoc-worker` | ticket pipeline, LLM calls, alerting | — |
| `barenoc-scheduler` | UniFi auto-sync, periodic jobs | — |
| `barenoc-pocket-id` | passkey/SSO identity | behind nginx |
| `barenoc-step-ca` | short-lived device certificates (adoption) | behind nginx |
| `barenoc-dns` | CoreDNS split-horizon (appliance names + upstream forward) | 53 |
| `pi-agent-runner` | host-side job executor (systemd, user `pi-agent`) | — |

Directory layout: `/opt/barenoc/{api,worker,scheduler,nginx,scripts,agent,client}` +
`volumes/{db,logs,secrets,branding,pocket-id,backup_status}` + `jobs/` +
`backups/`.

<a id="config-reference"></a>
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

<a id="identity-dns-all-tracks"></a>
### Identity & DNS (all tracks)

**Passkeys need a real domain.** The console works by IP, but passkey login
(Pocket ID) requires a registrable hostname — Chrome/Edge/Safari refuse
passkeys on `.local`/`.lan`/raw IPs. At install (Settings → Identity →
Appliance identity & DNS) set:

- **Appliance IP** — the machine's LAN address.
- **Domain** — a real domain you own (e.g. `bareNOC.com`); it only needs to
  resolve inside your network.
- **Console hostname** — e.g. `app.bareNOC.com`.

The page shows the exact **DNS record** or **hosts line** with copy buttons,
and warns when the domain can't carry passkeys.

**The appliance serves DNS (split-horizon).** A CoreDNS service (port 53)
answers authoritatively for the appliance's own names and forwards everything
else upstream. Point your router's DNS (or a machine's resolver) at the
appliance IP as a **secondary** DNS — every machine and device then resolves
`app.<domain>` / `stepca.<domain>` automatically, no hosts files. The
appliance is never the sole resolver, so a reboot can't break the LAN.

Changing the domain later requires a redeploy + re-enrolling passkeys
(WebAuthn origin) — set it right at first run.

**No real domain?** A home user has two options:
- **Password-only (no domain at all):** skip Identity/passkeys and log in with
  the local `admin` account (and any Users you add). Passkeys are an optional
  login layer — everything else works without them.
- **Cheap/free domain, internal-only resolution:** passkeys need a
  *registrable* domain, but it never has to resolve publicly — the
  appliance's split-horizon DNS (or a hosts line) makes `app.<domain>` work
  on the LAN. A $10/yr domain or a free subdomain (e.g. `foo.duckdns.org`)
  is enough; no public DNS records are required.

<a id="updating"></a>
## Updating

- **App code (releases):** the dashboard **Updates** card checks the public
  manifest (**free & open** — updates are not key-gated; see the
  installer's `--activation-key`) and offers **Update now / Schedule /
  Rollback**. The update snapshots the VM (when the host key is configured),
  downloads the release, verifies the checksum, rebuilds, health-checks, and
  auto-restores on failure. Outage ≈ 15–45 s — schedule in a low-traffic
  window.
- **OS + Docker images:** `sudo /opt/barenoc/scripts/barenoc-update.sh`
  (`--dry-run` to preview, `--no-apt` for images only; never auto-reboots).
- **Vendor path (dev/control box):** `./deploy.sh <user>@<ip>` (rsync →
  rebuild → health check → agent credentials → runner sync). Always snapshot
  the VM before an update.
- **Rollback:** the Updates card's Rollback (restores the pre-update code
  copy), `qm rollback` of the pre-update snapshot, or `restore_app.sh --apply`
  from a Layer-1 archive.

<a id="first-test-smoke-checklist-all-tracks"></a>
### First-test / smoke checklist (all tracks)

- [ ] `GET /api/v1/health` → 200
- [ ] Login → forced password change
- [ ] `Settings → UniFi → Test connection` → `connected: true`
- [ ] Ticket: *"what is the current local time?"* → answered in-thread
- [ ] `Settings → Backups` status is truthful for your deployment type
- [ ] Agent runner active; `md5sum /opt/barenoc/agent/runner.py` matches the
      repo if you changed the runner

<a id="troubleshooting-operations"></a>
### Troubleshooting & operations

- **A fresh install stalls mid-`deploy.sh`?** The installer is idempotent
  *except* for the VM itself — if the app deploy step failed (SSH, perms,
  certs), pull the latest fixes and re-run just the deploy:
  `cd /root/barenoc && git pull && ./deploy.sh barenoc@<ip>` — it converges
  the VM (fixes ownership, generates certs/keys, restarts services).
- `docs/runbook/troubleshooting.md` — the common failure ladder.
- `docs/03_post_deployment_runbook.md` — day-2 ops, restore, recovery.
- `docs/security/secret_management.md` — credential handling + rotation.
- `docs/operations/update_pipeline.md` — the three-layer update model.

---

*End of guide. Track-specific details: `docs/appliance/*` (hardware),
`docs/02_iac_and_setup_manifests.md` (manifests), `docs/system_acceptance_test.md`
(the formal test suite).*