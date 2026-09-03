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
# /opt/barenoc (flat layout) -> compose up --build -d (visible build log) ->
# VERSION-verifying health check -> post-apply provision -> post-update
# verification -> runner restart. On health/version failure: restore the
# previous code (+ qm rollback when a snapshot was taken). NEVER touches .env,
# volumes/, jobs/ or backups/.
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
BUILD_LOG="/var/log/barenoc-self-update-build.log"
: > "$BUILD_LOG" 2>/dev/null || true

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

# probe_health — read the live health JSON (HTTP code + version). The version
# is what proves the NEW build is actually serving: a failed rebuild leaves the
# OLD stack serving 200, which a bare HTTP check would wrongly call "updated"
# (the 08-20 buddy incident).
probe_health() {
  HEALTH_CODE="$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health 2>/dev/null)"
  HEALTH_VERSION="$(curl -sk https://127.0.0.1/api/v1/health 2>/dev/null | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("version", ""))
except Exception:
    print("")' 2>/dev/null)"
}

# rebuild_stack — compose up --build with VISIBLE output. The build log goes to
# $BUILD_LOG AND this script's own log; never fully silent (08-20 root cause #2).
rebuild_stack() {
  cd "$BASE" || return 1
  docker compose up --build -d 2>&1 | tee -a "$BUILD_LOG"
  echo "==> compose rebuild exit: ${PIPESTATUS[0]:-?}"
}

# refresh_host_self_update — systemd runs /usr/local/bin/barenoc-self-update.sh
# (installed by deploy.sh / the installer). Keep it in sync with the just-
# applied /opt/barenoc/scripts copy so self-update LOGIC changes (e.g. release
# signature verification) actually reach self-updating boxes — the apply step
# below only updates /opt/barenoc/scripts/.
refresh_host_self_update() {
  install -m 0755 "$BASE/scripts/barenoc-self-update.sh" /usr/local/bin/barenoc-self-update.sh \
    2>/dev/null || true
}

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
  refresh_host_self_update
  progress 60 "rollback" "rebuilding stack"
  rebuild_stack
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
SIGNATURE="$(python3 -c "import json;print(json.load(open('$REQ')).get('signature',''))" 2>/dev/null || true)"
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

# 2b. verify the detached release signature BEFORE applying — breaks the
# single-chain assumption (manifest + tarball + site are one compromised
# pipeline): a tampered tarball also needs a valid signature from the pinned
# release-signing key, which lives only on the gate machine.
progress 45 "verify" "verifying release signature"
if [ -n "$SIGNATURE" ]; then
  curl -fsSL "$SIGNATURE" -o "$TMP/app.tar.gz.sig" 2>/dev/null || true
fi

VERIFY_SH="$BASE/scripts/verify_release_signature.sh"
PINNED_KEY="$BASE/scripts/release-signing.pub"
# The PINNED key (installed by the previous release) is the trust anchor. On a
# bootstrap box with no pinned key yet, fall back to the key shipped inside
# the release tree (trust-on-first-use) so the first signed release can pin.
SIG_ARG=""; KEY_ARG=""
[ -s "$TMP/app.tar.gz.sig" ] && SIG_ARG="$TMP/app.tar.gz.sig"
if [ -s "$PINNED_KEY" ]; then
  KEY_ARG="$PINNED_KEY"
else
  tar xzf "$TMP/app.tar.gz" -C "$TMP" src/scripts/release-signing.pub \
    docs/security/release-signing.pub 2>/dev/null || true
  [ -s "$TMP/src/scripts/release-signing.pub" ] && KEY_ARG="$TMP/src/scripts/release-signing.pub"
fi

if [ -x "$VERIFY_SH" ]; then
  SIG_RC=0
  "$VERIFY_SH" "$TMP/app.tar.gz" "$SIG_ARG" "$KEY_ARG" "$VERSION" || SIG_RC=$?
  case "$SIG_RC" in
    0) echo "release signature verified" ;;
    3) echo "!! no release signature (pre-mandatory release) — hash-only fallback" ;;
    *)
      rm -f "$REQ"
      progress 100 "failed" "release signature verification failed"
      report '{"ok": false, "action": "update", "version": "'"$VERSION"'", "error": "release signature verification failed"}'
      exit 1
      ;;
  esac
else
  # Bootstrap: this host's self-update predates release signing (the helper
  # ships alongside the self-update script, so they install together). A
  # signed release can only be hash-verified here.
  echo "!! verify_release_signature.sh not installed — hash-only fallback (pre-signing host)"
fi

# 3. backup current code
progress 50 "backup" "backing up current release"
echo "==> backing up current code to .previous"
rm -rf "$BASE/.previous"
mkdir -p "$BASE/.previous"
chmod 700 "$BASE/.previous"  # root-only rollback snapshot (infosec 08-27: the
                              # agent must not read pre-update tree artifacts)
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
# ⚠️ KEEP THIS LIST IN SYNC with deploy.sh SHARED_MODULES + bootstrap_appliance.sh —
# adding a module to api/ requires updating ALL THREE (the .30.b self-update bug).
for m in action_validator.py audit.py audit_catalog.py crypto.py database.py models.py \
         sanitizer.py schemas.py worknotes.py queue_status.py tone_pool.py \
         llm_providers.py emailer.py ratewindows.py tierrouter.py; do
  [ -f "$BASE/api/$m" ] && cp "$BASE/api/$m" "$BASE/worker/$m"
done

systemctl daemon-reload 2>/dev/null || true

# 5. rebuild + health (VERSION-verifying — 08-20 root cause #1)
progress 80 "rebuild" "rebuilding containers"
echo "==> rebuilding stack (build log: $BUILD_LOG)"
rebuild_stack
progress 92 "healthcheck" "waiting for the web UI"
sleep 8
probe_health
REQV="$(printf '%s' "$VERSION" | sed 's/^[vV]//')"
GOTV="$(printf '%s' "$HEALTH_VERSION" | sed 's/^[vV]//')"
if [ "$HEALTH_CODE" = "200" ] && [ -n "$REQV" ] && [ "$REQV" = "$GOTV" ]; then
  systemctl restart pi-agent-runner 2>/dev/null || true
  echo "==> updated to $VERSION (health $HEALTH_CODE, version $GOTV)"
  refresh_host_self_update

  # 6. post-apply provision — existing boxes updating must get the full pass
  #    too (tailscale install/seed, agent creds, notify, remote support) —
  #    08-20 root cause #4.
  echo "==> post-apply provision"
  if ! bash /opt/barenoc/scripts/provision_agent.sh; then
    echo "!! provision_agent.sh reported a problem — verify_post_update.sh re-checks"
  fi

  # 6b. Recreate the scheduler — the agent password rotates on every
  #  provision, and a scheduler started before it holds a STALE bind mount
  #  (it keeps reading an empty/old /opt/barenoc/agent → every API call 401s
  #  → scheduled updates silently never fire). Validated 08-26 on a customer
  #  box whose daily auto-update was dead for days for exactly this. The api
  #  is healthy at this point (the health/version check above), so this is
  #  safe; best-effort.
  echo "==> recreating scheduler (fresh agent credentials mount)"
  if docker rm -f barenoc-scheduler >/dev/null 2>&1; then
    ( cd /opt/barenoc && docker compose up -d scheduler ) >/dev/null 2>&1 || true
    echo "==> scheduler recreated"
  fi

  # 7. post-update verification suite (entitlement + tailscale self-heal).
  echo "==> post-update verification"
  if bash /opt/barenoc/scripts/verify_post_update.sh; then
    rm -f "$REQ"
    progress 100 "done" "update complete — services restarted"
    report "{\"ok\": true, \"action\": \"update\", \"version\": \"$VERSION\", \"at\": \"$(date -Is)\", \"services_restarted\": true, \"reboot_required\": false}"
    echo "==> updated to $VERSION (health $HEALTH_CODE, version $GOTV)"
    exit 0
  fi

  # Verification failed — the update itself is healthy, so DO NOT roll back.
  # Surface the failure; the scheduler auto-reports it when enabled.
  rm -f "$REQ"
  progress 100 "failed" "post-update verification failed — see verify_post_update.json"
  report "{\"ok\": false, \"action\": \"update\", \"version\": \"$VERSION\", \"applied\": true, \"error\": \"post-update verification failed\", \"at\": \"$(date -Is)\"}"
  echo "==> update applied ($VERSION) but post-update verification FAILED"
  exit 1
fi

progress 100 "failed" "update failed — health/version check; restoring previous release"

# 6. failure -> restore previous + optional qm rollback
echo "==> update failed (health $HEALTH_CODE, expected $REQV, got $GOTV) — restoring previous"
for d in api worker scheduler nginx scripts agent client docker-compose.yml; do
  rm -rf "$BASE/$d"
  [ -e "$BASE/.previous/$d" ] && cp -a "$BASE/.previous/$d" "$BASE/$d"
done
chown -R barenoc:docker "$BASE/api" "$BASE/worker" "$BASE/scheduler" \
        "$BASE/nginx" "$BASE/scripts" "$BASE/client" 2>/dev/null
chown -R pi-agent:pi-agent "$BASE/agent" 2>/dev/null
refresh_host_self_update
rebuild_stack
if [ -n "$UPDATE_HOST" ] && [ -n "$UPDATE_HOST_KEY" ] && [ -n "$UPDATE_VMID" ]; then
  ssh -i "$UPDATE_HOST_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR "$UPDATE_HOST" \
      "qm rollback $UPDATE_VMID before-$VERSION 2>/dev/null || true" || true
fi
report "{\"ok\": false, \"action\": \"update\", \"version\": \"$VERSION\", \"error\": \"health/version check failed (expected $REQV, got $GOTV); previous release restored\"}"
exit 1
