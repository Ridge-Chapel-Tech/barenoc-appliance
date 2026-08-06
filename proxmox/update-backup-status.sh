#!/usr/bin/env bash
# Generate host-side backup status JSON and push it to the VM.
# Called at the end of the daily + USB backup scripts.
# Installed at /usr/local/bin via proxmox/install.sh
set -euo pipefail

VM_HOST="${VM_HOST:-192.0.2.207}"
VM_USER="barenoc"
VM_DEST="/opt/barenoc/volumes/backup_status/status.json"
LOG="/var/log/barenoc-backup.log"
CONF="/etc/barenoc-usb.conf"
USB_LAST_FILE="/var/lib/barenoc-usb-last"

# defaults (overridable by /etc/barenoc-usb.conf)
USB_DEV="/dev/disk/by-label/BARENOC-BACKUP"
MOUNT="/mnt/barenoc-usb"
if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
fi

# Newest VM snapshot on local disk
NEWEST=$(ls -1t /var/lib/vz/dump/vzdump-qemu-100-*.vma.zst 2>/dev/null | head -1 || true)
vm_last="none"
if [ -n "$NEWEST" ]; then
  vm_last=$(basename "$NEWEST" .vma.zst | sed 's/^vzdump-qemu-100-//; s/_/ /g')
fi

# USB stick present? (raw LUKS partition visible even while locked)
usb_present="false"
if [ -e "$USB_DEV" ]; then usb_present="true"; fi

# Stick encryption state (readable while locked — LUKS keyslots are plaintext
# metadata). Tells the UI the stick is actually LUKS-encrypted + how many
# recovery keyslots exist (1 = host keyfile only, 2 = keyfile + rack-card
# passphrase).
usb_encrypted="false"
usb_keyslots=0
if [ "$usb_present" = "true" ] && cryptsetup isLuks "$USB_DEV" 2>/dev/null; then
  usb_encrypted="true"
  usb_keyslots="$(cryptsetup luksDump "$USB_DEV" 2>/dev/null \
    | sed -n '/Keyslots:/,/Tokens:/p' | grep -c 'luks2' || true)"
  [ -n "$usb_keyslots" ] || usb_keyslots=0
fi

# Last USB backup (persisted by backup-to-usb.sh — works whether mounted or not)
usb_last="none"
if [ -f "$USB_LAST_FILE" ]; then
  usb_last="$(cat "$USB_LAST_FILE")"
fi

cat > /var/lib/barenoc-backup-status.json <<EOF
{
  "vm_snapshot_last": "${vm_last}",
  "usb_present": ${usb_present},
  "usb_encrypted": ${usb_encrypted},
  "usb_keyslots": ${usb_keyslots},
  "usb_last_backup": "${usb_last}",
  "updated": "$(date -Is)"
}
EOF

if scp -q -o StrictHostKeyChecking=accept-new /var/lib/barenoc-backup-status.json "${VM_USER}@${VM_HOST}:${VM_DEST}" 2>/dev/null; then
  echo "[$(date -Is)] status pushed to VM" >> "$LOG"
else
  echo "[$(date -Is)] WARN: backup status push to VM failed" >> "$LOG"
  exit 1   # no silent false-positive: callers must see the failure
fi
