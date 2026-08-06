#!/usr/bin/env bash
# Daily Proxmox snapshot backup of the BareNOC VM (Layer 2, local disk).
# 7-day retention, zstd compressed. Installed at /usr/local/bin via install.sh
set -euo pipefail

LOG="/var/log/barenoc-backup.log"
DUMPDIR="/var/lib/vz/dump"

echo "[$(date -Is)] starting daily vzdump snapshot" >> "$LOG"
vzdump 100 --compress zstd --mode snapshot \
  --dumpdir "$DUMPDIR" --prune-backups keep-last=7 >> "$LOG" 2>&1
echo "[$(date -Is)] daily vzdump done" >> "$LOG"
/usr/local/bin/update-backup-status.sh
