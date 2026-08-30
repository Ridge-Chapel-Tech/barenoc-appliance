#!/usr/bin/env bash
# BareNOC offsite/remote backup (Layer 4) — host-side cron wrapper.
#
# The host cron fires this hourly (see deploy.sh); the job self-gates on the
# offsite schedule + mode written by Settings → Backups → Offsite, so a save
# takes effect without touching the host. The heavy lifting (archive encryption
# + S3-compatible upload + pruning) runs INSIDE the api container where Python,
# cryptography and the bind-mounted volumes live — this wrapper just dispatches.
#
# Usage: offsite_backup.sh [--force]
set -euo pipefail

LOG="/opt/barenoc/backups/offsite.log"
log() { echo "[$(date -Is)] $*" >> "$LOG"; }

if ! docker ps -q -f name=^barenoc-api$ | grep -q . 2>/dev/null; then
  log "ERROR: barenoc-api container not running — skipping offsite backup"
  exit 1
fi

log "Dispatching offsite job (${1:-scheduled})"
docker exec barenoc-api python3 /app/offsite_job.py "$@"
rc=$?
if [ "$rc" -ne 0 ]; then
  log "offsite job exited non-zero (rc=$rc) — see /opt/barenoc/volumes/backup_status/offsite_status.json"
fi
exit "$rc"
