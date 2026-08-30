#!/usr/bin/env bash
# BareNOC app-data backup (Layer 1) — run every 6h via cron.
# Backs up: SQLite DB (WAL-safe snapshot), Fernet key, .env, compose, nginx certs.
# Retention: 30 days. Logs to /opt/barenoc/backups/backup.log
set -euo pipefail

APP_DIR="/opt/barenoc"
BACKUP_DIR="$APP_DIR/backups"
DB="$APP_DIR/volumes/db/barenoc.db"
FERN_KEY="$APP_DIR/volumes/db/fernet.key"
LOG="$BACKUP_DIR/backup.log"
RETENTION_DAYS=30

log() { echo "[$(date -Is)] $*" >> "$LOG"; }

# The WAL-safe DB snapshot needs the sqlite3 CLI; without it every run dies
# at step 1 (fresh ISO installs missed this package once). Fail clearly.
command -v sqlite3 >/dev/null 2>&1 || { log "ERROR: sqlite3 not installed (sudo apt-get install -y sqlite3)"; exit 1; }

mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

log "Starting app backup"

# 1. Consistent SQLite snapshot (WAL-safe; never a plain cp)
sqlite3 "$DB" ".backup '$TMP/barenoc.db'"

# 2. Secrets + config. Direct copies cover the common case; files that the
#    backup user cannot read (a root-owned 0600 fernet.key / nginx key — the
#    deploy's db-chown is best-effort and can be missed on fresh installs) are
#    read through the api container (root), which barenoc reaches via docker.
cp_any() { # cp_any <src> <dest-dir> [name]
  local src="$1" dst="$2" name="${3:-$(basename "$1")}"
  if [ -f "$src" ] && cp "$src" "$dst/" 2>/dev/null; then
    return 0
  fi
  if [ -f "$src" ] && docker ps -q -f name=^barenoc-api$ | grep -q . \
     && docker exec barenoc-api cat "$src" > "$dst/$name" 2>/dev/null; then
    log "container-read: $name"
    return 0
  fi
  [ -f "$src" ] || log "WARN: $name not found"
  return 0
}
mkdir -p "$TMP/config" "$TMP/secrets" "$TMP/certs"
cp_any "$APP_DIR/.env"               "$TMP/config"
cp_any "$APP_DIR/docker-compose.yml" "$TMP/config"
cp_any "$FERN_KEY"                   "$TMP/secrets"
cp_any "$APP_DIR/volumes/nginx/certs/barenoc.crt" "$TMP/certs"
cp_any "$APP_DIR/volumes/nginx/certs/barenoc.key" "$TMP/certs"

# 2b. Pocket ID (OIDC/passkeys) data — users, passkey credentials, client registrations
mkdir -p "$TMP/pocket-id"
if [ -d "$APP_DIR/volumes/pocket-id/data" ]; then
  cp -a "$APP_DIR/volumes/pocket-id/data/." "$TMP/pocket-id/" 2>/dev/null || log "WARN: pocket-id data copy failed (perms?)"
else
  log "WARN: pocket-id data dir not found"
fi

# 3. Bundle
ARCHIVE="$BACKUP_DIR/app-backup-$TS.tar.gz"
# The archive contains .env (all API keys), the Fernet key and the DB
# (password hashes) — never world-readable. 0600, owner barenoc.
umask 077
tar -czf "$ARCHIVE" -C "$TMP" .
chmod 600 "$ARCHIVE"

# 4. Sanity: verify archive + DB integrity
if ! tar -tzf "$ARCHIVE" > /dev/null 2>&1; then
  log "ERROR: archive corrupt — removing"
  rm -f "$ARCHIVE"
  exit 1
fi
sqlite3 "$TMP/barenoc.db" "PRAGMA integrity_check;" | grep -q "^ok$" \
  || log "ERROR: DB integrity check failed in snapshot"

# 5. Retention
find "$BACKUP_DIR" -name "app-backup-*.tar.gz" -mtime +"$RETENTION_DAYS" -delete

# 6. Network copy (optional): if a target folder is configured (a mounted
#    SMB/NFS share, set in the wizard/Settings → Backups), copy the archive
#    there too + prune old copies (same 30-day retention). The target lives
#    in the backup-schedule conf the Settings UI writes.
CONF="/opt/barenoc/volumes/backup_status/backup_schedule.conf"
TARGET=$(grep -E '^BACKUP_TARGET_DIR=' "$CONF" 2>/dev/null | head -1 | cut -d= -f2-)
if [ -n "$TARGET" ] && [ -d "$TARGET" ]; then
  if cp "$ARCHIVE" "$TARGET/" 2>>"$LOG"; then
    log "Remote copy: $TARGET/$(basename "$ARCHIVE")"
    find "$TARGET" -name "app-backup-*.tar.gz" -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || true
  else
    log "WARN: remote copy to $TARGET failed"
  fi
fi

log "Backup complete: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

# 7. Publish a pointer to the newest archive — the offsite (remote) backup job
#    consumes it so the daily offsite upload always encrypts + ships the exact
#    archive this run just produced. Best-effort; the offsite job falls back to
#    globbing app-backup-*.tar.gz if the pointer is missing.
echo "$ARCHIVE" > "$BACKUP_DIR/latest_archive" 2>/dev/null || true

echo "$ARCHIVE"
