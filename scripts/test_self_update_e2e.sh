#!/usr/bin/env bash
# test_self_update_e2e.sh — END-TO-END self-update regression on a TEST box.
#
# The hermetic scripts/test_self_update.sh covers the logic (no docker/root),
# but it can't catch the class of bug that shipped on 09-03: the apply step
# silently no-opped, `docker compose up --build -d` rebuilt the OLD tree and
# exited 0, and the health check rolled the update back — the running tree
# never moved. This script drives the REAL path end-to-end on a disposable
# test appliance:
#
#   request written -> barenoc-self-update.path fires -> /usr/local/bin
#   barenoc-self-update.sh downloads + verifies + applies -> `docker compose
#   up --build -d` -> VERSION-verifying health check -> update_result.json
#   ok:true -> (then a rollback request restores the original version).
#
# It builds a LOCAL release tarball from this checkout with the version
# stamped to a synthetic pre-signing version (2026.08.24.z < 2026.08.25.a),
# so the unsigned test artifact is accepted via the hash-only fallback — no
# release-signing key needed on the test box, and no dependency on
# barenoc.com. The signature path is covered separately by
# scripts/test_release_signing.sh.
#
# Run it FROM THE DEV BOX (the checkout lives here) against a test box:
#
#   bash scripts/test_self_update_e2e.sh --ssh barenoc@192.168.4.79
#
# Safety:
#   • Refuses to touch the PROD appliance (192.168.4.207) or a host whose
#     hostname contains "prod".
#   • Needs docker + root (sudo) + the barenoc-self-update.path unit on the
#     target — exactly what a real appliance has. NOT for CI (ubuntu has no
#     docker). Use scripts/test_self_update.sh in CI.
#   • Restores the box to its original version via a rollback request unless
#     --keep is passed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_VERSION="2026.08.24.z"   # pre-mandatory-signing -> unsigned artifact OK
KEEP=0
TARGET=""
TIMEOUT=360                   # seconds to wait for one self-update to finish

usage() {
  echo "usage: $0 --ssh <user>@<host> [--test-version V] [--keep] [--timeout S]" >&2
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh) TARGET="${2:-}"; shift 2 ;;
    --test-version) TEST_VERSION="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$TARGET" ] || usage

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say()  { printf '\n==> %s\n' "$*"; }
die()  { printf '\nFAIL - %s\n' "$*" >&2; exit 1; }
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "$@"; }
sudo_remote() { remote "sudo $*"; }
# ok_field <json> -> echo the JSON 'ok' field value (true/false), or '' if unparsable
ok_field() {
  printf '%s' "${1:-}" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("ok"))
except Exception:
    print("")' 2>/dev/null
}

# ── 0. safety + prerequisites ───────────────────────────────────────────────
say "checking $TARGET"
HOSTNAME="$(remote hostname)"
case "$HOSTNAME" in *prod*) die "refusing to run against a prod host: $HOSTNAME" ;; esac
remote "hostname -I" 2>/dev/null | grep -qw 192.168.4.207 && die "refusing to run against PROD (192.168.4.207)"

sudo_remote true >/dev/null 2>&1 || die "sudo -n failed on $TARGET (need passwordless sudo)"
remote "test -d /opt/barenoc && test -f /opt/barenoc/api/version.py" || die "/opt/barenoc not found on $TARGET"
remote "docker info >/dev/null 2>&1" || die "docker not usable on $TARGET"
remote "systemctl is-active barenoc-self-update.path" | grep -q active || die "barenoc-self-update.path not active on $TARGET"

V_CUR="$(remote "python3 -c \"import re;print(re.search(r'APP_VERSION\\s*=\\s*\\\"([^\\\"]+)\\\"', open('/opt/barenoc/api/version.py').read()).group(1))\"")"
[ -n "$V_CUR" ] || die "could not read the current version on $TARGET"
say "current version on $TARGET: $V_CUR (test version: $TEST_VERSION)"

# Install the SELF-UPDATE SCRIPT UNDER TEST first: the .service runs
# /usr/local/bin/barenoc-self-update.sh, so the box would otherwise exercise
# whatever OLD copy it already has — not the fix this checkout carries. The
# test tarball also ships the fixed script (so the apply step re-pins it), but
# the run that matters must be driven by the fixed entrypoint from the start.
say "installing the self-update script under test (src/scripts/barenoc-self-update.sh)"
scp -q "$ROOT/src/scripts/barenoc-self-update.sh" "$TARGET:/tmp/barenoc-self-update.sh"
sudo_remote "install -m 0755 /tmp/barenoc-self-update.sh /usr/local/bin/barenoc-self-update.sh \
  && install -m 0755 /tmp/barenoc-self-update.sh /opt/barenoc/scripts/barenoc-self-update.sh"
remote "grep -q restore_previous_release /usr/local/bin/barenoc-self-update.sh" \
  || die "fixed self-update script did not install (missing restore_previous_release)"

# ── 1. build a LOCAL release tarball stamped with the test version ─────────
say "building a local test tarball (bareNOC-$TEST_VERSION.tar.gz)"
SRC_COPY="$WORK/src-copy"
mkdir -p "$SRC_COPY" "$WORK/dist"
( cd "$ROOT" && tar cf - --exclude=.git --exclude=dist --exclude='__pycache__' \
    --exclude='*.pyc' --exclude=node_modules . ) | ( cd "$SRC_COPY" && tar xf - )
sed -i "s/APP_VERSION = \".*\"/APP_VERSION = \"$TEST_VERSION\"/" \
  "$SRC_COPY/src/api/version.py"
grep -q "APP_VERSION = \"$TEST_VERSION\"" "$SRC_COPY/src/api/version.py" \
  || die "failed to stamp the test version"
python3 "$SRC_COPY/scripts/build_release_manifest.py" \
  --source "$SRC_COPY" --out "$WORK/dist" >/dev/null
TARBALL="$WORK/dist/bareNOC-$TEST_VERSION.tar.gz"
[ -s "$TARBALL" ] || die "tarball build failed"
# the tarball must be self-sufficient: version.py at src/api/, and the worker
# shared modules side-by-side in src/worker/ (the 09-03 P0).
# NOTE: write the listing to a FILE first — `tar tzf | grep -q` under
# `set -o pipefail` trips on SIGPIPE when grep exits as soon as it matches.
tar tzf "$TARBALL" > "$WORK/tar.list"
grep -q '^src/api/version.py$' "$WORK/tar.list" || die "test tarball missing src/api/version.py"
grep -q '^src/worker/tierrouter.py$' "$WORK/tar.list" || die "test tarball missing src/worker/tierrouter.py (builder regression)"

# ── 2. push the tarball + write the update request ─────────────────────────
say "pushing tarball + writing update_request.json"
scp -q "$TARBALL" "$TARGET:/tmp/barenoc-e2e.tar.gz"
# Clear any stale result/progress so we only observe THIS run.
sudo_remote "rm -f /opt/barenoc/volumes/update_status/update_result.json \
  /opt/barenoc/volumes/update_status/progress.json \
  /opt/barenoc/volumes/update_status/rollback_request.json"
write_request() {  # write_request <version>
  sudo_remote "python3 -c \"import json;open('/opt/barenoc/volumes/update_status/update_request.json','w').write(json.dumps({'version':'$1','kind':'patch','tarball':'file:///tmp/barenoc-e2e.tar.gz','checksums':'','signature':'','requested_at':'2026-09-04T00:00:00Z','snapshot':False}))\""
}
write_request "$TEST_VERSION"

# ── 3. wait for the update to reach a terminal state ───────────────────────
wait_result() {  # wait_result -> echoes the result JSON; dies on timeout
  local i
  for i in $(seq 1 "$TIMEOUT"); do
    local r
    r="$(sudo_remote "cat /opt/barenoc/volumes/update_status/update_result.json 2>/dev/null" || true)"
    if [ -n "$r" ] && printf '%s' "$r" | python3 -c 'import json,sys;json.load(sys.stdin)' 2>/dev/null; then
      printf '%s' "$r"
      return 0
    fi
    sleep 2
  done
  die "timed out after ${TIMEOUT}s waiting for update_result.json"
}

say "waiting for the self-update to finish (this includes a real docker rebuild)"
RESULT="$(wait_result)"
echo "update_result: $RESULT"

# ── 4. assert the update actually flipped the running version ──────────────
check() {  # check <desc> <cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "ok  - $desc"; else echo "FAIL - $desc" >&2; return 1; fi
}
rc=0
[ "$(ok_field "$RESULT")" = "True" ] && echo "ok  - update reported ok:true" || { echo "FAIL - update reported ok:true (result: $RESULT)" >&2; rc=1; }
APPLIED="$(remote "python3 -c \"import re;print(re.search(r'APP_VERSION\\s*=\\s*\\\"([^\\\"]+)\\\"', open('/opt/barenoc/api/version.py').read()).group(1))\"")"
check "tree flipped to $TEST_VERSION" test "$APPLIED" = "$TEST_VERSION" || rc=1
HEALTH="$(remote "curl -sk https://127.0.0.1/api/v1/health" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("version",""))
except Exception: print("")')"
check "health endpoint reports $TEST_VERSION" test "$HEALTH" = "$TEST_VERSION" || rc=1
check "worker shared modules present (tierrouter.py)" \
  remote "test -f /opt/barenoc/worker/tierrouter.py" || rc=1
check "worker shared modules present (ratewindows.py)" \
  remote "test -f /opt/barenoc/worker/ratewindows.py" || rc=1
check "worker's own main.py intact" \
  remote "grep -q 'BareNOC Worker' /opt/barenoc/worker/main.py" || rc=1
check "request file consumed" \
  remote "test ! -f /opt/barenoc/volumes/update_status/update_request.json" || rc=1

# ── 5. restore the original version via a real rollback (unless --keep) ────
if [ "$rc" = "0" ] && [ "$KEEP" = "0" ]; then
  say "rolling back to $V_CUR (exercises the rollback path too)"
  sudo_remote "python3 -c \"import json;open('/opt/barenoc/volumes/update_status/rollback_request.json','w').write(json.dumps({'requested_at':'2026-09-04T00:00:00Z'}))\""
  sudo_remote "rm -f /opt/barenoc/volumes/update_status/update_result.json /opt/barenoc/volumes/update_status/progress.json"
  RB_RESULT="$(wait_result)"
  echo "rollback result: $RB_RESULT"
  [ "$(ok_field "$RB_RESULT")" = "True" ] && echo "ok  - rollback reported ok:true" || { echo "FAIL - rollback reported ok:true (result: $RB_RESULT)" >&2; rc=1; }
  RB_APPLIED="$(remote "python3 -c \"import re;print(re.search(r'APP_VERSION\\s*=\\s*\\\"([^\\\"]+)\\\"', open('/opt/barenoc/api/version.py').read()).group(1))\"")"
  check "tree restored to $V_CUR" test "$RB_APPLIED" = "$V_CUR" || rc=1
else
  [ "$KEEP" = "1" ] && say "--keep passed: leaving $TARGET on $TEST_VERSION"
fi

# ── 6. reconcile agent credentials ─────────────────────────────────────────
# The update's post-apply provision rotates the agent password in the DB; a
# rollback restores the OLD credential file, so re-run the (idempotent)
# provision to put file + DB back in agreement. Best-effort — never fail the
# e2e over a provisioning nicety, but log loudly so it's not silently skipped.
say "reconciling agent credentials after the test"
sudo_remote "bash /opt/barenoc/scripts/provision_agent.sh" \
  && echo "ok  - agent credentials reconciled" \
  || echo "!! provision_agent.sh post-test reconcile failed — check the box" >&2

say "final container status"
remote "cd /opt/barenoc && docker compose ps --format 'table {{.Name}}\t{{.Status}}'" || true

echo
if [ "$rc" = "0" ]; then
  if [ "$KEEP" = "1" ]; then
    SUMMARY="$V_CUR -> $TEST_VERSION (kept)"
  else
    SUMMARY="$V_CUR -> $TEST_VERSION -> $V_CUR (restored)"
  fi
  echo "e2e self-update PASSED ($TARGET: $SUMMARY)"
else
  echo "e2e self-update FAILED"
  exit 1
fi
