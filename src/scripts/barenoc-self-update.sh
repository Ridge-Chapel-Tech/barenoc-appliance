#!/bin/bash
# barenoc-self-update.sh — apply a released BareNOC version (or roll back).
#
# Runs as root via systemd (barenoc-self-update.service), fired by
# barenoc-self-update.path when the API writes a request file:
#   volumes/update_status/update_request.json  -> apply the release
#   volumes/update_status/rollback_request.json -> restore the previous one
#
# Flow (update): optional Proxmox snapshot -> download tarball -> verify
# checksum -> backup current code -> map the release tree (src/ layout) onto
# /opt/barenoc (flat layout) -> compose up --build -d -> health check -> runner
# restart. On health failure: restore the previous code (+ qm rollback when a
# snapshot was taken). NEVER touches .env, volumes/, jobs/ or backups/.
set -uo pipefail
exec >> /var/log/barenoc-self-update.log 2>&1
echo "=== barenoc-self-update $(date -Is) ==="

BASE=/opt/barenoc
STATUS_DIR="$BASE/volumes/update_status"
REQ="$STATUS_DIR/update_request.json"
RB="$STATUS_DIR/rollback_request.json"
RESULT="$STATUS_DIR/update_result.json"
PROG="$STATUS_DIR/progress.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Progress reporting — the API surfaces this in the Updates card (progress
# bar + stage message); the scheduler emails on the terminal stage.
progress() {  # progress <pct> <stage> <message>
  printf '{"stage": "%s", "pct": %s, "message": "%s", "at": "%s"}\n' \
    "$2" "$1" "$3" "$(date -Is)" > "$PROG" 2>/dev/null || true
}

# host snapshot access (optional) — read from .env
UPDATE_HOST="$(grep -E '^UPDATE_HOST=' "$BASE/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')"
UPDATE_VMID="$(grep -E '^UPDATE_VMID=' "$BASE/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')"
UPDATE_HOST_KEY="$(grep -E '^UPDATE_HOST_SSH=' "$BASE/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')"

report() { echo "$1" > "$RESULT"; }

# ── rollback ───────────────────────────────────────────────────────────────
if [ -f "$RB" ]; then
  rm -f "$RB"
  progress 5 "rollback" "restoring previous release"
  if [ ! -d "$BASE/.previous" ]; then
    progress 100 "failed" "rollback failed — no previous release found"
    report '{"ok": false, "action": "rollback", "error": "no previous release found"}'
    exit 1
  fi
  echo "==> rollback to previous release"
  for d in api worker scheduler nginx scripts agent client docker-compose.yml; do
    rm -rf "$BASE/$d"
    [ -e "$BASE/.previous/$d" ] && cp -a "$BASE/.previous/$d" "$BASE/$d"
  done
  chown -R barenoc:docker "$BASE/api" "$BASE/worker" "$BASE/scheduler" \
          "$BASE/nginx" "$BASE/scripts" "$BASE/client" 2>/dev/null
  chown -R pi-agent:pi-agent "$BASE/agent" 2>/dev/null
  progress 60 "rollback" "rebuilding stack"
  cd "$BASE" && docker compose up --build -d >/dev/null 2>&1
  sleep 8
  if curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health | grep -q 200; then
    systemctl restart pi-agent-runner 2>/dev/null || true
    progress 100 "done" "rollback complete — previous release restored"
    report "{\"ok\": true, \"action\": \"rollback\", \"version\": \"previous\", \"at\": \"$(date -Is)\"}"
    echo "==> rollback complete"
  else
    progress 100 "failed" "rollback failed — health check failed"
    report '{"ok": false, "action": "rollback", "error": "health check failed after rollback"}'
  fi
  exit 0
fi

# ── update ─────────────────────────────────────────────────────────────────
[ -f "$REQ" ] || exit 0
VERSION="$(python3 -c "import json;print(json.load(open('$REQ'))['version'])" 2>/dev/null || echo '?')"
TARBALL="$(python3 -c "import json;print(json.load(open('$REQ')).get('tarball',''))" 2>/dev/null || true)"
CHECKSUMS="$(python3 -c "import json;print(json.load(open('$REQ')).get('checksums',''))" 2>/dev/null || true)"
[ -n "$TARBALL" ] || { report '{"ok": false, "action": "update", "error": "no tarball URL in update request"}'; rm -f "$REQ"; exit 1; }

# 1. snapshot before (restricted host key — qm snapshot only, optional)
progress 8 "snapshot" "host snapshot (optional)"
if [ -n "$UPDATE_HOST" ] && [ -n "$UPDATE_HOST_KEY" ] && [ -n "$UPDATE_VMID" ]; then
  echo "==> snapshot before-$VERSION on $UPDATE_HOST"
  ssh -i "$UPDATE_HOST_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR -o ConnectTimeout=8 "$UPDATE_HOST" \
      "qm snapshot $UPDATE_VMID before-$VERSION --description 'bareNOC self-update'" \
    && echo "snapshot ok" || echo "snapshot failed — continuing without it"
fi

# 2. download + verify
progress 20 "download" "downloading release"
echo "==> fetching $TARBALL"
curl -fsSL "$TARBALL" -o "$TMP/app.tar.gz" || { rm -f "$REQ"; progress 100 "failed" "download failed"; report '{"ok": false, "action": "update", "error": "download failed"}'; exit 1; }
if [ -n "$CHECKSUMS" ]; then
  curl -fsSL "$CHECKSUMS" -o "$TMP/sums" 2>/dev/null || true
  if [ -s "$TMP/sums" ]; then
    progress 40 "verify" "verifying checksum"
    # Name-independent compare: the sums file lists the release asset name
    # (bareNOC-<ver>.tar.gz) but we download to app.tar.gz — sha256sum -c
    # --ignore-missing would verify NOTHING and exit 1 ("no file was
    # verified"), failing every update. Compare hashes directly instead.
    EXPECTED="$(awk 'NR==1{print $1}' "$TMP/sums")"
    ACTUAL="$(sha256sum "$TMP/app.tar.gz" | awk '{print $1}')"
    if [ -z "$EXPECTED" ] || [ "$EXPECTED" != "$ACTUAL" ]; then
      rm -f "$REQ"
      progress 100 "failed" "checksum mismatch"
      report '{"ok": false, "action": "update", "error": "checksum mismatch"}'
      exit 1
    fi
    echo "checksum ok ($ACTUAL)"
  fi
fi

# 3. backup current code
progress 50 "backup" "backing up current release"
echo "==> backing up current code to .previous"
rm -rf "$BASE/.previous"
mkdir -p "$BASE/.previous"
for d in api worker scheduler nginx scripts agent client docker-compose.yml; do
  [ -e "$BASE/$d" ] && cp -a "$BASE/$d" "$BASE/.previous/$d"
done

# 4. extract + map release tree (src/ layout) onto the flat /opt/barenoc layout
progress 65 "apply" "applying release"
echo "==> extracting release"
tar xzf "$TMP/app.tar.gz" -C "$TMP"
SRC="$TMP/src"
apply_dir() {
  [ -d "$1" ] || return 0
  rm -rf "$BASE/$2"
  cp -a "$1" "$BASE/$2"
}
apply_dir "$SRC/api" api
apply_dir "$SRC/worker" worker
apply_dir "$SRC/scheduler" scheduler
apply_dir "$SRC/nginx" nginx
apply_dir "$SRC/scripts" scripts
apply_dir "$TMP/client" client
apply_dir "$SRC/agent" agent
[ -f "$SRC/docker-compose.yml" ] && cp "$SRC/docker-compose.yml" "$BASE/docker-compose.yml"
[ -f "$SRC/agent/pi-agent-runner.service" ] && \
  cp "$SRC/agent/pi-agent-runner.service" /etc/systemd/system/pi-agent-runner.service

chown -R barenoc:docker "$BASE/api" "$BASE/worker" "$BASE/scheduler" \
        "$BASE/nginx" "$BASE/scripts" "$BASE/client" 2>/dev/null
chown -R pi-agent:pi-agent "$BASE/agent" 2>/dev/null

# shared modules the worker image needs in its build context (mirror deploy.sh)
for m in action_validator.py audit.py crypto.py database.py models.py \
         sanitizer.py schemas.py worknotes.py llm_providers.py emailer.py; do
  [ -f "$BASE/api/$m" ] && cp "$BASE/api/$m" "$BASE/worker/$m"
done

systemctl daemon-reload 2>/dev/null || true

# 5. rebuild + health
progress 80 "rebuild" "rebuilding containers"
echo "==> rebuilding stack"
cd "$BASE" && docker compose up --build -d >/dev/null 2>&1
progress 92 "healthcheck" "waiting for the web UI"
sleep 8
OK="$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health)"
if [ "$OK" = "200" ]; then
  systemctl restart pi-agent-runner 2>/dev/null || true
  rm -f "$REQ"
  progress 100 "done" "update complete — services restarted"
  report "{\"ok\": true, \"action\": \"update\", \"version\": \"$VERSION\", \"at\": \"$(date -Is)\", \"services_restarted\": true, \"reboot_required\": false}"
  echo "==> updated to $VERSION (health $OK)"
  exit 0
fi

progress 100 "failed" "update failed — health check; restoring previous release"

# 6. failure -> restore previous + optional qm rollback
echo "==> update failed (health $OK) — restoring previous"
for d in api worker scheduler nginx scripts agent client docker-compose.yml; do
  rm -rf "$BASE/$d"
  [ -e "$BASE/.previous/$d" ] && cp -a "$BASE/.previous/$d" "$BASE/$d"
done
chown -R barenoc:docker "$BASE/api" "$BASE/worker" "$BASE/scheduler" \
        "$BASE/nginx" "$BASE/scripts" "$BASE/client" 2>/dev/null
chown -R pi-agent:pi-agent "$BASE/agent" 2>/dev/null
cd "$BASE" && docker compose up --build -d >/dev/null 2>&1
if [ -n "$UPDATE_HOST" ] && [ -n "$UPDATE_HOST_KEY" ] && [ -n "$UPDATE_VMID" ]; then
  ssh -i "$UPDATE_HOST_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR "$UPDATE_HOST" \
      "qm rollback $UPDATE_VMID before-$VERSION 2>/dev/null || true" || true
fi
report "{\"ok\": false, \"action\": \"update\", \"version\": \"$VERSION\", \"error\": \"health check failed; previous release restored\"}"
exit 1
