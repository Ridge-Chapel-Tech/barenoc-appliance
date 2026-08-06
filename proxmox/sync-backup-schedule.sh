#!/bin/bash
# sync-backup-schedule.sh — reconcile the host backup cron with the BareNOC
# Settings → Backups config (the VM owns the schedule; this host executes it).
#
# The API writes /opt/barenoc/volumes/backup_status/backup_schedule.conf on
# every Settings → Backups save; this script (from cron every 10 minutes)
#   * rewrites /etc/cron.d/barenoc-backup's USB line to match, and
#   * fires the USB backup immediately when RUN_USB_BACKUP_NOW=true.
# The daily VM-snapshot line (1 AM) is always preserved.
# Non-destructive: if the VM is unreachable or the file is missing, the
# existing cron is left untouched.
set -u

LOG="/var/log/barenoc-backup.log"
CRON="/etc/cron.d/barenoc-backup"
VM_HOST="${VM_HOST:-192.0.2.207}"
VM_USER="barenoc"
CONF_PATH="/opt/barenoc/volumes/backup_status/backup_schedule.conf"
USB_SCRIPT="/usr/local/bin/backup-to-usb.sh"

log() { echo "[$(date -Is)] $*" >> "$LOG"; }

# 1. fetch the schedule from the VM
CONF="$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$VM_USER@$VM_HOST" "cat $CONF_PATH" 2>/dev/null || true)"
if [ -z "$CONF" ]; then
  log "schedule sync: VM unreachable or no schedule file — leaving cron as-is"
  exit 0
fi

val() { printf '%s\n' "$CONF" | sed -n "s/^$1=//p" | head -1 | tr -d '[:space:]'; }
USB_ENABLED="$(val USB_BACKUP_ENABLED)"
USB_DAY="$(val USB_BACKUP_DAY)"
USB_HOUR="$(val USB_BACKUP_HOUR)"
RUN_NOW="$(val RUN_USB_BACKUP_NOW)"

# 2. validate + build the USB cron line (empty = disabled)
USB_LINE=""
if [ "$USB_ENABLED" = "true" ]; then
  case "$USB_DAY" in
    daily|"") DAY_F="*" ;;
    [0-6])    DAY_F="$USB_DAY" ;;
    *) log "schedule sync: bad USB_BACKUP_DAY='$USB_DAY' — keeping cron"; exit 0 ;;
  esac
  case "$USB_HOUR" in
    ''|*[!0-9]*) log "schedule sync: bad USB_BACKUP_HOUR='$USB_HOUR' — keeping cron"; exit 0 ;;
    *) ;;
  esac
  [ "$USB_HOUR" -le 23 ] 2>/dev/null || { log "schedule sync: bad USB_BACKUP_HOUR='$USB_HOUR' — keeping cron"; exit 0; }
  USB_LINE="0 $USB_HOUR * * $DAY_F root $USB_SCRIPT"
fi

# 3. reconcile the cron file (full rewrite, header + daily line + USB line)
NEW_CRON="# BareNOC VM backup schedule — USB line is MANAGED by Settings → Backups\n# on the VM (sync-backup-schedule.sh rewrites this every 10 min)\n0 1 * * * root /usr/local/bin/barenoc-daily-backup.sh"
if [ -n "$USB_LINE" ]; then
  NEW_CRON="$NEW_CRON\n$USB_LINE"
fi
CUR="$(cat "$CRON" 2>/dev/null || echo MISSING)"
if [ "$CUR" != "$(printf '%b\n' "$NEW_CRON")" ]; then
  printf '%b\n' "$NEW_CRON" > "$CRON"
  if [ -n "$USB_LINE" ]; then
    log "schedule sync: cron updated → USB $USB_HOUR:00 ${USB_DAY:-daily}"
  else
    log "schedule sync: cron updated → USB backup disabled"
  fi
fi

# 4. run-now flag
if [ "$RUN_NOW" = "true" ]; then
  log "schedule sync: run-now requested — starting USB backup"
  if "$USB_SCRIPT"; then
    log "schedule sync: run-now backup completed"
  else
    log "schedule sync: run-now backup FAILED (see log)"
  fi
  ssh -o ConnectTimeout=8 -o BatchMode=yes "$VM_USER@$VM_HOST" \
      "sed -i 's/^RUN_USB_BACKUP_NOW=.*/RUN_USB_BACKUP_NOW=false/' $CONF_PATH" 2>/dev/null || \
    log "schedule sync: could not clear run-now flag on the VM"
fi
