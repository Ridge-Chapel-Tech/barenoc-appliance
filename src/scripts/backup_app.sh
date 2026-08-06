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

mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

log "Starting app backup"

# 1. Consistent SQLite snapshot (WAL-safe; never a plain cp)
sqlite3 "$DB" ".backup '$TMP/barenoc.db'"

# 2. Secrets + config
mkdir -p "$TMP/config" "$TMP/secrets" "$TMP/certs"
cp "$APP_DIR/.env"                "$TMP/config/" 2>/dev/null || log "WARN: .env not found"
cp "$APP_DIR/docker-compose.yml"  "$TMP/config/" 2>/dev/null || log "WARN: docker-compose.yml not found"
[ -f "$FERN_KEY" ] && cp "$FERN_KEY" "$TMP/secrets/" || log "WARN: fernet.key not found"
cp "$APP_DIR/volumes/nginx/certs/barenoc.crt" "$TMP/certs/" 2>/dev/null || log "WARN: cert not found"
cp "$APP_DIR/volumes/nginx/certs/barenoc.key" "$TMP/certs/" 2>/dev/null || log "WARN: key not found"

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

log "Backup complete: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo "$ARCHIVE"
