# Update & Patch Pipeline

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Overview

The appliance has three layers that need updates:

```
Layer 1: Proxmox Host ─── APT (kernel, Proxmox packages, firmware)
Layer 2: BareNOC VM  ─── APT (Ubuntu) + Docker images (application)
Layer 3: Application  ─── Python code, HTML templates, scripts
```

Each layer has its own update procedure and risk profile.

---

## Layer 1: Proxmox Host Updates

### Automated (Cron)

```bash
# /etc/cron.weekly/proxmox-update
#!/bin/bash
LOG="/var/log/proxmox-update.log"
echo "[$(date)] Starting Proxmox host update..." >> $LOG
apt update -qq >> $LOG 2>&1
apt upgrade -y -qq >> $LOG 2>&1
apt autoremove -y -qq >> $LOG 2>&1
echo "[$(date)] Proxmox host update complete" >> $LOG

# Notify via email (if configured)
if [ -f /usr/bin/mail ]; then
    tail -5 $LOG | mail -s "Proxmox Update: $(hostname)" admin@example.com
fi
```

```bash
chmod +x /etc/cron.weekly/proxmox-update
```

### Manual (SSH)

```bash
ssh root@192.0.2.95
apt update && apt upgrade -y
apt autoremove -y
systemctl reboot  # if kernel updated
```

### Risk: Kernel Updates

Proxmox kernel updates require a reboot. For field units:

1. Schedule maintenance window (e.g., 3 AM Sunday)
2. Run updates: `apt update && apt upgrade -y`
3. Notify via BareNOC ticket: "Proxmox host requires reboot"
4. Reboot: `systemctl reboot`
5. Verify all VMs restart automatically (Proxmox auto-start)

---

## Layer 2: BareNOC VM Updates

### Automated (systemd Timer)

```ini
# /etc/systemd/system/barenoc-update.service
[Unit]
Description=BareNOC VM Update

[Service]
Type=oneshot
ExecStart=/usr/local/bin/barenoc-update
User=root

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/barenoc-update.timer
[Unit]
Description=Weekly BareNOC update

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
# /usr/local/bin/barenoc-update
#!/bin/bash
LOG="/var/log/barenoc-update.log"

echo "[$(date)] Starting BareNOC VM update..." >> $LOG

# Step 1: Ubuntu OS updates
apt update -qq >> $LOG 2>&1
apt upgrade -y -qq >> $LOG 2>&1

# Step 2: Update Docker Compose images
cd /opt/barenoc
docker compose pull >> $LOG 2>&1
docker compose up -d >> $LOG 2>&1

# Step 3: Clean up old Docker images
docker image prune -f >> $LOG 2>&1

# Step 4: Prune old snapshots
# (Proxmox host handles this via vzdump rotation)

echo "[$(date)] BareNOC VM update complete" >> $LOG
```

### Manual Update

```bash
# SSH into the BareNOC VM (via Proxmox host)
ssh barenoc@192.0.2.207   # the appliance's static IP

# Or direct from host console
qm terminal 100  # if VM
```

Then inside the VM:

```bash
/usr/local/bin/barenoc-update   # NOT YET BUILT (M4 backlog) — see below
```

> ⚠️ `barenoc-update` is now BUILT: `src/scripts/barenoc-update.sh` (VM-side,
> Layer 2: apt + compose pull/up + prune + health check; `--dry-run` to
> preview, `--no-apt` for images only; never auto-reboots — it prints a
> REBOOT REQUIRED notice when /var/run/reboot-required exists). Install it
> to `/usr/local/bin/barenoc-update` + a weekly systemd timer per the script
> header. Layer 1 (Proxmox host) stays a host cron; Layer 3 stays `deploy.sh`
> from the dev box.
> The real update path for APP CODE is **Layer 3 below** (`deploy.sh` from the dev box).

---

## Layer 3: Application Updates

### The real deploy path (from the dev box)

```bash
cd <bareNOC-repo>
git pull           # get the latest code
./deploy.sh        # rsync → rebuild → health-check → agent creds → runner restart
```

`deploy.sh` is the single update mechanism: it rsyncs `src/` + `client/` to
`/opt/barenoc`, rebuilds the containers, reloads nginx, re-provisions the
agent service-account credentials, and (when sudo is available) installs
`src/agent/runner.py` + restarts `pi-agent-runner`. The agent runner is
host-side — if deploy's sudo step is skipped, install it manually:

```bash
scp src/agent/runner.py barenoc@192.0.2.207:/tmp/
ssh barenoc@192.0.2.207 "sudo -u pi-agent cp /tmp/runner.py /opt/barenoc/agent/runner.py && sudo systemctl restart pi-agent-runner"
md5sum src/agent/runner.py   # must match the VM's md5
```

### Versioning Scheme

```
vYYYY.MM.PATCH
v2025.07.1  — first release in July 2025
v2025.07.2  — second release in July 2025
v2025.08.1  — first release in August 2025
```

### Release manifest + open self-updates (free & open, beta)

Releases publish `versions.json` + the release tarball to
**https://barenoc.com/downloads/** (via the BareNOC-Website push-to-deploy
repo). The manifest and **tarball are public — updates are OPEN, no key
required** (BareNOC is free & open in beta; paid support is the only thing
that's separate).

**Update now / Schedule / Rollback** (dashboard → Updates):
- `GET/POST /api/v1/updates/…` — status, check, now, rollback, schedule
  (the scheduler applies scheduled updates at the configured
day/hour).
- The api writes a trigger file → the host-side `barenoc-self-update.path`
  systemd unit runs `barenoc-self-update.sh` as root: optional Proxmox
  snapshot (restricted `qm snapshot`/`qm rollback` key) → download the tarball
  → verify SHA256 → back up the current code to `.previous` → map the release
tree onto `/opt/barenoc` → `compose up --build -d` → health check → runner
restart. On health failure: restore `.previous` (+ `qm rollback`). `.env`,
`volumes/`, `jobs/` and `backups/` are never touched.
- **Outage:** only the container recreate phase (~15–45 s); schedule in a
  low-traffic window. Blue/green (clone + promote) is the GA upgrade path.

---

## Update Rollback Procedure

If an update causes issues:

### VM Rollback (Proxmox Snapshot)

```bash
# On Proxmox host
# List snapshots
qm listsnapshot 100

# Rollback to pre-update snapshot
qm rollback 100 pre-update-2025-07-29

# Start VM
qm start 100
```

### Docker Rollback

```bash
# Inside BareNOC VM — rebuild from the last known-good source instead
cd /opt/barenoc && docker compose up --build -d
# (or restore /opt/barenoc from the latest app-data backup + re-deploy)
```

### File-based Rollback

```bash
# If using git
cd /opt/barenoc
git reflog
git reset --hard HEAD@{1}
```

---

## Update Testing Checklist

Before applying updates to customer-facing appliances:

| Check | Description |
|-------|-------------|
| Test on dev unit | Apply update to your lab rack first |
| Verify web UI loads | `curl -s -o /dev/null -w "%{http_code}" https://localhost` |
| Verify API responds | `curl -s https://localhost/api/v1/health` |
| Check DB migration | Run `docker exec barenoc-api python3 -c "from database import Base; import models"` (schema auto-migrates on boot) |
| Run integration tests | `docker exec barenoc-api python3 -m unittest test_devices test_admin test_settings test_alerting test_unifi_sync` + `docker exec barenoc-worker python3 -m unittest test_judge test_integration` |
| Create Proxmox snapshot | `qm snapshot 100 pre-update-$(date +%F)` |
| Deploy to customer | Apply update + verify health check passes |
