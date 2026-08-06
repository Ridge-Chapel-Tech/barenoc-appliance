#!/usr/bin/env bash
# Restore / verify a BareNOC app backup (Layer 1).
#
# SAFE MODE (default): restores to a scratch dir and validates contents —
# DB integrity + table row counts. Does NOT touch production.
#
# APPLY MODE (--apply): stops containers, restores DB + config, starts them.
# Use only after a successful safe-mode drill, on the same host.
#
# Usage:
#   restore_app.sh <backup.tar.gz>            # safe-mode verify
#   restore_app.sh <backup.tar.gz> --apply    # full restore
set -euo pipefail

BACKUP="${1:?usage: restore_app.sh <backup.tar.gz> [--apply]}"
MODE="${2:-}"
APP_DIR="/opt/barenoc"

if [ ! -f "$BACKUP" ]; then
  echo "ERROR: backup file not found: $BACKUP"
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "==> Extracting $BACKUP"
tar -xzf "$BACKUP" -C "$TMP"

DB="$TMP/barenoc.db"
if [ ! -f "$DB" ]; then
  echo "ERROR: no barenoc.db in backup"
  exit 1
fi

echo "==> SQLite integrity check"
INTEGRITY=$(sqlite3 "$DB" "PRAGMA integrity_check;")
echo "$INTEGRITY"
[ "$INTEGRITY" = "ok" ] || { echo "ERROR: integrity check failed"; exit 1; }

echo "==> Table row counts"
for t in users tickets devices audit_log; do
  cnt=$(sqlite3 "$DB" "SELECT count(*) FROM $t;" 2>/dev/null || echo "n/a")
  echo "  $t: $cnt"
done

echo "==> Backup contents:"
tar -tzf "$BACKUP" | sort

if [ "$MODE" = "--apply" ]; then
  echo "==> APPLY MODE: stopping containers and restoring production"
  ( cd "$APP_DIR" && docker compose stop )
  cp "$DB" "$APP_DIR/volumes/db/barenoc.db"
  # Clear stale WAL/SHM so SQLite doesn't replay old WAL against the restored DB
  rm -f "$APP_DIR/volumes/db/barenoc.db-wal" "$APP_DIR/volumes/db/barenoc.db-shm"
  [ -f "$TMP/secrets/fernet.key" ] && cp "$TMP/secrets/fernet.key" "$APP_DIR/volumes/db/fernet.key"
  [ -f "$TMP/config/.env" ] && cp "$TMP/config/.env" "$APP_DIR/.env" && chmod 600 "$APP_DIR/.env"
  [ -f "$TMP/config/docker-compose.yml" ] && cp "$TMP/config/docker-compose.yml" "$APP_DIR/docker-compose.yml"
  ( cd "$APP_DIR" && docker compose up -d )
  echo "==> Restore complete. Verify: docker compose ps && /api/v1/health"
else
  echo "==> Safe-mode verify complete (production untouched)."
  echo "    Run with --apply only after this drill passes."
fi
