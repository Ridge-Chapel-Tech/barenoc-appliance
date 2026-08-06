# Backup & Restore

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Backup Strategy

```
                    ┌─────────────────────────┐
                    │     Backup Schedule      │
                    ├─────────────────────────┤
                    │ Daily: VM snapshot       │
                    │ Weekly: Full VM backup   │
                    │ Continuous: DB dump      │
                    └─────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
     [Proxmox Host]    [External USB]     [Tailscale/Cloud]
     (local disk)      (in-rack USB)      (offsite)
     Retain 7 days     Retain 4 weeks     Retain 3 months
```

---

## What Gets Backed Up

| Data | Method | Frequency | Retention |
|------|--------|-----------|-----------|
| **Entire VM** | Proxmox `vzdump` snapshot | Daily | 7 days local |
| **Entire VM** | Proxmox `vzdump` stop-mode | Weekly | 4 weeks USB |
| **SQLite DB** | `sqlite3 .backup` | Every 6 hours | 30 days |
| **Application config** | `.env`, `docker-compose.yml` | Every deploy | Permanent (git) |
| **Pocket ID data** | Layer 1 app backup (`volumes/pocket-id/data`) | Every 6 hours | 30 days |
| **SSL certs** | Certbot renewal | Auto | Permanent |
| **Audit logs** | Append-only rotation | Daily | 90 days |

> **Pocket ID note:** the OIDC provider (users, passkey credentials, client
> registrations) lives in `volumes/pocket-id/data` and is included in the Layer 1
> app backup. If it is lost, users must re-enroll passkeys (recovery codes remain
> valid) — so this inclusion is mandatory.

---

## Backup Procedures

### Automated Daily VM Backup (Proxmox Host Cron)

```bash
# /etc/cron.d/barenoc-backup
# Daily snapshot backup at 1 AM
0 1 * * * root vzdump 100 --compress zstd --mode snapshot \
    --dumpdir /var/lib/vz/dump --remove 0 --rotate 7 \
    --notes "Daily BareNOC VM backup"
```

### Weekly Full VM Backup to USB

```bash
# /etc/cron.d/barenoc-backup-usb
# Weekly full backup at 2 AM Sunday
0 2 * * 0 root /usr/local/bin/backup-to-usb.sh
```

```bash
# /usr/local/bin/backup-to-usb.sh
#!/bin/bash
USB_MOUNT="/mnt/backup"
USB_DEV="/dev/sdb1"  # verify with lsblk

# Mount USB if not mounted
if ! mountpoint -q $USB_MOUNT; then
    mount $USB_DEV $USB_MOUNT
fi

# Run backup
vzdump 100 --compress zstd --mode stop \
    --dumpdir $USB_MOUNT \
    --remove 0 \
    --notes "Weekly BareNOC VM backup $(date +%F)"

# Cleanup backups older than 4 weeks
find $USB_MOUNT -name "*.vma.*" -mtime +28 -delete

# Unmount USB
umount $USB_MOUNT
```

### Continuous SQLite Backup

```bash
# /etc/cron.d/barenoc-db-backup
# DB backup every 6 hours
*/6 * * * * root /usr/local/bin/backup-db.sh
```

```bash
# /usr/local/bin/backup-db.sh
#!/bin/bash
DB="/opt/barenoc/volumes/db/barenoc.db"
BACKUP_DIR="/opt/barenoc/backups"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p $BACKUP_DIR

# Use sqlite3 .backup for safe online backup
sqlite3 $DB ".backup $BACKUP_DIR/barenoc-$TIMESTAMP.db"

# Compress
gzip $BACKUP_DIR/barenoc-$TIMESTAMP.db

# Keep 30 days of DB backups
find $BACKUP_DIR -name "barenoc-*.db.gz" -mtime +30 -delete
```

---

## Restore Procedures

### Restore Entire VM from Latest Snapshot

```bash
# On Proxmox host

# List available backups
pvesm list local --content backup

# Restore from latest backup
qmrestore /var/lib/vz/dump/vzdump-qemu-100-*.vma.zst 100 \
    --storage local

# Start the restored VM
qm start 100
```

### Restore from USB Backup

```bash
# Mount USB
mount /dev/sdb1 /mnt/backup

# Find the backup file
ls -lh /mnt/backup/*.vma.zst

# Restore
qmrestore /mnt/backup/vzdump-qemu-100-YYYY_MM_DD-HH_MM_SS.vma.zst 100

# Unmount USB
umount /mnt/backup
```

### Restore SQLite Database Only

```bash
# Stop services
docker compose down

# Find the backup to restore
ls -lh /opt/barenoc/backups/

# Restore
gunzip -c /opt/barenoc/backups/barenoc-20250729-060000.db.gz > \
    /opt/barenoc/volumes/db/barenoc.db

# Fix permissions
chown barenoc:barenoc /opt/barenoc/volumes/db/barenoc.db

# Restart services
docker compose up -d
```

### Factory Reset (for Re-Shipment)

See [`../runbook/factory_reset.md`](../runbook/factory_reset.md).

---

## Disaster Recovery Scenarios

| Scenario | Recovery Time | Procedure |
|----------|--------------|-----------|
| VM corrupted (software) | 15 minutes | Roll back to last Proxmox snapshot |
| VM corrupted (no snapshot) | 30 minutes | Restore from vzdump backup |
| Proxmox host failed | 2 hours | Reinstall Proxmox, restore VM from USB backup |
| Mini PC failed (hardware) | 4 hours | Replace mini PC, reinstall Proxmox, restore from backup |
| Complete rack destroyed | 1–2 days | Build new rack from spare parts, restore from cloud backup |
