#!/usr/bin/env bash
# Weekly full VM backup to USB (Layer 3, LUKS-encrypted stick) + app-data sync.
# Skips gracefully when the USB stick is not attached.
# - LUKS2, opened with the host keyfile (0600 root), closed afterwards.
# - vzdump snapshot-mode (zero-downtime, matches barenoc-daily-backup.sh): VM 100
#   -> $MOUNT/vzdump, prune keep-last=4. (NOT stop mode — stop mode wedges on
#   this host when the guest reboots instead of ACPI-powering-off: the copy
#   never starts and the task spins forever. Snapshot mode is proven here.)
# - rsyncs the VM's 6-hourly app backups (DB + .env + certs) -> $MOUNT/app-backups.
# - pushes status.json to the VM (System page indicators).
set -euo pipefail

LOG="/var/log/barenoc-backup.log"
CONF="/etc/barenoc-usb.conf"

# defaults (overridable by /etc/barenoc-usb.conf or env)
USB_DEV="${USB_DEV:-/dev/disk/by-label/BARENOC-BACKUP}"
CRYPT_NAME="barenoc-usb"
KEYFILE="/etc/barenoc-usb.key"
MOUNT="/mnt/barenoc-usb"
VM_HOST="${VM_HOST:-192.0.2.207}"
VM_USER="barenoc"

if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
fi

log() { echo "[$(date -Is)] $*" >> "$LOG"; }

# raw device present? (by-id path from setup, or by-label fallback)
if [[ ! -e "$USB_DEV" ]]; then
  log "USB backup SKIPPED: $USB_DEV not present"
  exit 0
fi

cleanup() {
  umount "$MOUNT" 2>/dev/null || true
  cryptsetup close "$CRYPT_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# ── open LUKS + mount ───────────────────────────────────────────────────────
if ! mountpoint -q "$MOUNT"; then
  if [[ ! -e "/dev/mapper/$CRYPT_NAME" ]]; then
    if [[ -f "$KEYFILE" ]]; then
      cryptsetup open --key-file="$KEYFILE" "$USB_DEV" "$CRYPT_NAME"
    else
      log "ERROR: keyfile $KEYFILE missing — cannot open LUKS stick"
      exit 1
    fi
  fi
  mkdir -p "$MOUNT"
  mount "/dev/mapper/$CRYPT_NAME" "$MOUNT"
fi

# ── 1. full VM backup (stop mode) ───────────────────────────────────────────
mkdir -p "$MOUNT/vzdump" "$MOUNT/app-backups"
log "starting weekly USB vzdump (snapshot mode)"
if ! vzdump 100 --compress zstd --mode snapshot \
     --dumpdir "$MOUNT/vzdump" --prune-backups keep-last=4 >> "$LOG" 2>&1; then
  log "ERROR: vzdump to USB failed"
  exit 1
fi

# ── 2. app-data sync (the VM's 6-hourly backups land here too) ──────────────
log "syncing app backups from $VM_HOST:/opt/barenoc/backups"
if rsync -az --delete -e "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10" \
     "$VM_USER@$VM_HOST:/opt/barenoc/backups/" "$MOUNT/app-backups/" >> "$LOG" 2>&1; then
  log "app backup sync done"
else
  log "WARN: app backup sync failed (VM unreachable?)"
fi

log "weekly USB backup done"

# ── persist last-success timestamp (System-page "USB last backup" reads this) ──
date -Is > /var/lib/barenoc-usb-last
log "last-success timestamp written to /var/lib/barenoc-usb-last"

# ── 3. status push (runs before cleanup unmounts) ───────────────────────────
[[ -x /usr/local/bin/update-backup-status.sh ]] && /usr/local/bin/update-backup-status.sh || true
