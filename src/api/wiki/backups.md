# Backups

BareNOC backs itself up in three layers:

| Layer | What | Where | Schedule |
|-------|------|-------|----------|
| 1 — App data | SQLite DB + `.env` + Fernet key + certs (a `0600` archive) | on the VM, `/opt/barenoc/backups` | every 6 hours (30-day retention) |
| 2 — VM snapshot | full VM image (`vma.zst`, compressed) | on the Proxmox host disk | daily 1 AM (keep-last 7) |
| 3 — USB stick | Layer-2 archive **plus** the Layer-1 app backups, **LUKS2-encrypted** | on a USB stick plugged into the Proxmox host | **Settings → Backups** |

## Bring your own host (BYO) — what applies

BareNOC can be deployed on your own hardware (bare metal, your own VM, any
hypervisor) — the Layers 2 & 3 above are **appliance-only**: they run on the
Proxmox host that ships with the rack unit, so on a BYO deployment there is no
host pushing snapshots to the USB stick (Settings → Backups will show a
"not an appliance deployment" notice and the stick schedule is disabled).

**What you still get on BYO:**
- **Layer 1 app-data archive** — automatic, every 6 hours, 30-day retention
  (`/opt/barenoc/backups/app-backup-*.tar.gz`). It contains the full app
  state: the DB, `.env` (all secrets), the Fernet key, certs, compose file
  and Pocket ID data — everything needed to move or recover the appliance.
- **Restore anywhere:** `restore_app.sh` is pure Docker-Compose on any Linux
  host — copy the archive to a new machine, install Docker + the app, and
  run `restore_app.sh --apply <archive>`.

**Recommended BYO additions** (your call, host-level):
- Machine/VM-level backup with your own tool — restic, BorgBackup, Timeshift,
  or your hypervisor's snapshots. The Layer-1 archive is the portable
  artifact to include (or to keep entirely separate, e.g. restic to S3).
- Keep the Layer-1 archive off the same disk: point it at a mounted network
  share or include `/opt/barenoc/backups/` in your host backup.

## First-time stick setup (LUKS encryption)

The USB stick must be **encrypted once** before the schedule can use it. This is
an install-time operation on the Proxmox host (root), done once per stick:

```bash
# on the Proxmox host, with the stick plugged in — lists USB candidates first
bash /usr/local/bin/setup-usb-backup.sh --dev /dev/sdX
```

What it does (destructive — wipes the stick):
1. GPT partition + **LUKS2 encryption**.
2. Writes the **host keyfile** `/etc/barenoc-usb.key` (`0600`, root-only) —
   the automation path the backup schedule uses.
3. Generates a **recovery passphrase** and prints it ONCE, then locks the
   stick again.
4. Writes `/etc/barenoc-usb.conf` and runs a write/read test.

## Keys & recovery — where the secrets live

| Unlock path | Where | Used by |
|-------------|-------|---------|
| Host keyfile | `/etc/barenoc-usb.key` (0600, root) | automatic backups (cron) |
| Recovery passphrase | **printed once at setup** → seal it on the appliance's rack card (or your password manager) | disaster recovery if the host dies and the stick must be opened elsewhere |

- The passphrase is deliberately **never stored on disk** (not in the UI, not
  in logs) — the sealed rack card is the intended home. Settings → Backups
  shows the encryption state and keyslot count, not the passphrase itself.
- **Lost the passphrase?** The host keyfile still unlocks the stick. Add a new
  recovery passphrase from the host: `cryptsetup luksAddKey /dev/sdX1 --key-file /etc/barenoc-usb.key`
- **Lost the keyfile?** The rack-card passphrase unlocks it.
- **Both lost?** The stick cannot be opened — data stays encrypted. Re-run
  `setup-usb-backup.sh` for a fresh stick (old archives are lost).

## Configuring the USB backup (Layer 3)

Settings → **Backups**:

- **Enable USB backup** — the host skips the run automatically when the stick
  isn't plugged in.
- **Backup day** — a weekday (recommended, with `keep-last 4` = ~a month of
  full VM images on the stick) or **Daily**.
- **Backup hour** — local time; pick a quiet window (default 2 AM).
- **Run USB backup now** — queues an immediate run (the host starts it within
  ~10 minutes).

The schedule is written to a small file the Proxmox host reconciles against
every 10 minutes (`/usr/local/bin/sync-backup-schedule.sh`), so a save takes
effect without touching the host. The **Status** box on the same page shows
the stick's presence and the last USB backup / VM snapshot times.

## Recovery

- **App data:** `src/scripts/restore_app.sh` on the VM (safe-mode verify,
  `--apply` to restore).
- **Full VM:** `qmrestore` from the newest archive on the stick or host disk
  (see `docs/03_post_deployment_runbook.md` / `docs/runbook/*`).

## Secrets hygiene

Layer-1 archives contain secrets and are `0600`; the stick is LUKS2 with a
host keyfile (`/etc/barenoc-usb.key`, `0600`) plus a recovery passphrase
printed during setup — keep it on the sealed rack card, not in the UI or logs.
