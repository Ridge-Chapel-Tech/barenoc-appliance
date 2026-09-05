#!/usr/bin/env bash
# test_self_update.sh — regression tests for the appliance self-update path
# (src/scripts/barenoc-self-update.sh + scripts/build_release_manifest.py).
#
# The 09-03 P0: the release tarball lacked the worker's shared modules
# (tierrouter.py + ratewindows.py live in src/api/, the worker Dockerfile COPYs
# them from its build context), so `docker compose up --build -d` failed with
# '"/tierrouter.py": not found', the OLD api container kept serving 200, the
# health/version check saw the old version, and the self-update rolled back —
# every release was blocked. Two bugs fed it: the tarball builder kept a
# hand-maintained shared-module list, and the self-update script's belt-and-
# suspenders backfill kept a second hand-maintained list. Both are now DERIVED
# from src/worker/Dockerfile (the single source of truth).
#
# Tests (hermetic — no docker, no root, no network):
#   1. build_release_manifest.py ships EVERY worker Dockerfile COPY target in
#      src/worker/ inside the tarball (the tarball is self-sufficient);
#   2. barenoc-self-update.sh backfills any shared module the tarball forgot,
#      so even a BROKEN tarball yields a complete worker build context and the
#      update flips the running version;
#   3. a FAILED compose rebuild is reported as a rebuild failure (not a
#      misleading health/version mismatch) and the request file is consumed;
#   4-11. the download/extract step fails fast: a non-tarball download, a
#      wrong layout, a version mismatch (including an UNREADABLE version.py),
#      a PARTIAL layout (missing a mapped dir), a checksum download failure,
#      and a checksum mismatch are each reported as their REAL cause, consume
#      the request, and leave the running tree untouched — and a CORRECT
#      checksum still applies.
#
# Runs on CI (ubuntu) and the VM host; needs bash + python3 (both present).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
BASH_BIN="$(command -v bash)"

fail=0
check() {  # check <desc> <cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "ok  - $desc"
  else
    echo "FAIL - $desc" >&2
    fail=1
  fi
}

FAKE="$TMP/barenoc"
mkdir -p "$FAKE/volumes/update_status" "$TMP/usrlocal/bin" "$TMP/etc/systemd" \
         "$TMP/bin" "$TMP/var/log"

# ── 1. the release tarball ships EVERY worker Dockerfile COPY target ────────
DIST="$TMP/dist"
python3 "$ROOT/scripts/build_release_manifest.py" --out "$DIST" --source "$ROOT" >/dev/null 2>&1
VER="$(python3 -c "import re;print(re.search(r'APP_VERSION\s*=\s*\"([^\"]+)\"', open('$ROOT/src/api/version.py').read()).group(1))")"
check "release tarball ships every worker Dockerfile COPY target" python3 - "$DIST/bareNOC-$VER.tar.gz" "$ROOT/src/worker/Dockerfile" <<'PY'
import re, sys, tarfile
tar, dockerfile = sys.argv[1], sys.argv[2]
targets = [m.group(1) for m in re.finditer(r'^COPY\s+([A-Za-z0-9_]+\.py)\s+\.\s*$', open(dockerfile).read(), re.M)]
with tarfile.open(tar) as tf:
    names = set(tf.getnames())
missing = [t for t in targets if f"src/worker/{t}" not in names]
assert not missing, f"tarball missing worker build-context files: {missing}"
PY

# ── shared fixture: a BROKEN tarball (real src/worker has no shared modules) ─
# `src/worker/` in the repo only holds worker-local files; the shared modules
# live in src/api/ and are injected into the tarball by build_release_manifest.
# Taring the repo's src/worker/ alone reproduces the 09-03 broken-tarball shape
# exactly. The OTHER dirs are minimal placeholders: they exist so the tarball
# passes the full-layout guard, but they carry no verify_release_signature.sh
# (the hermetic test has no signing key) and keep the provision/verify stubs.
BROKEN="$TMP/broken"
mkdir -p "$BROKEN/src/scheduler" "$BROKEN/src/nginx" "$BROKEN/src/scripts" \
         "$BROKEN/src/agent" "$BROKEN/client"
cp -a "$ROOT/src/api" "$BROKEN/src/api"
cp -a "$ROOT/src/worker" "$BROKEN/src/worker"
printf 'x\n' > "$BROKEN/src/scheduler/main.py"
printf 'x\n' > "$BROKEN/src/nginx/conf"
printf 'x\n' > "$BROKEN/src/agent/placeholder"
printf 'x\n' > "$BROKEN/client/placeholder"
printf 'services:\n' > "$BROKEN/src/docker-compose.yml"
cat > "$BROKEN/src/scripts/provision_agent.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$BROKEN/src/scripts/verify_post_update.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$BROKEN/src/scripts/provision_agent.sh" "$BROKEN/src/scripts/verify_post_update.sh"
( cd "$BROKEN" && tar czf "$TMP/broken.tar.gz" src client )

# Seed the fake appliance at the OLD version (the shared modules exist in api/
# — the tarball always ships api/ correctly — but are missing from worker/).
mkdir -p "$FAKE/api" "$FAKE/worker" "$FAKE/scripts"
printf '"""BareNOC version."""\nAPP_VERSION = "2026.09.01.e"\n' > "$FAKE/api/version.py"
echo 'services:' > "$FAKE/docker-compose.yml"
cat > "$FAKE/scripts/provision_agent.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$FAKE/scripts/verify_post_update.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$FAKE/scripts/provision_agent.sh" "$FAKE/scripts/verify_post_update.sh"

write_request() {  # write_request <tarball-url> [checksums-url]
  local cs="${2:-}"
  cat > "$FAKE/volumes/update_status/update_request.json" <<EOF
{"version": "$VER", "kind": "patch", "tarball": "$1", "checksums": "$cs", "signature": "", "requested_at": "2026-09-04T00:00:00Z", "snapshot": false}
EOF
}

# Stubs — docker/systemctl/install/ssh/chown/chmod no-op successfully; curl
# serves the file:// download via the real curl and simulates the health
# endpoint by reading the CURRENT api/version.py (what a rebuilt api would do).
make_stubs() {
  local build_rc="${1:-0}"
  cat > "$TMP/bin/docker" <<EOF
#!/bin/bash
echo "docker \$*"
exit $build_rc
EOF
  cat > "$TMP/bin/systemctl" <<'EOF'
#!/bin/bash
echo "systemctl $*"
exit 0
EOF
  cat > "$TMP/bin/install" <<'EOF'
#!/bin/bash
echo "install $*"
exit 0
EOF
  cat > "$TMP/bin/ssh" <<'EOF'
#!/bin/bash
echo "ssh $*"
exit 0
EOF
  cat > "$TMP/bin/chown" <<'EOF'
#!/bin/bash
exit 0
EOF
  cat > "$TMP/bin/chmod" <<'EOF'
#!/bin/bash
exit 0
EOF
  cat > "$TMP/bin/curl" <<EOF
#!/bin/bash
for a in "\$@"; do
  case "\$a" in
    file://*) exec /usr/bin/curl "\$@" ;;
  esac
done
if printf '%s' "\$*" | grep -q -- '-o /dev/null'; then
  echo "200"
else
  python3 -c "import re;print('{\"version\": \"%s\"}' % re.search(r'APP_VERSION\s*=\s*\"([^\"]+)\"', open('$FAKE/api/version.py').read()).group(1))"
fi
EOF
  chmod +x "$TMP/bin"/*
}

# Repoint the hardcoded paths in a copy of the canonical script.
make_script() {
  sed \
    -e "s#/opt/barenoc#$FAKE#g" \
    -e "s#/var/log/barenoc-self-update.log#$TMP/var/log/self-update.log#g" \
    -e "s#/var/log/barenoc-self-update-build.log#$TMP/var/log/build.log#g" \
    -e "s#/usr/local/bin/barenoc-self-update.sh#$TMP/usrlocal/bin/barenoc-self-update.sh#g" \
    -e "s#/etc/systemd/system/pi-agent-runner.service#$TMP/etc/systemd/pi-agent-runner.service#g" \
    "$ROOT/src/scripts/barenoc-self-update.sh" > "$TMP/bin/self-update.sh"
  chmod +x "$TMP/bin/self-update.sh"
}

# run_self_update <logfile> — run the script single-shot (no download retries)
# so the failure cases below stay fast + deterministic; the retry behavior is
# covered explicitly by test 11.
run_self_update() {
  UPDATE_DL_ATTEMPTS=1 UPDATE_DL_RETRY_DELAY=0 PATH="$TMP/bin:$PATH" \
    "$BASH_BIN" "$TMP/bin/self-update.sh" >"$1" 2>&1 || true
}

# ── 2. the self-update heals a broken tarball + flips the version ───────────
make_stubs 0
write_request "file://$TMP/broken.tar.gz"
make_script
run_self_update "$TMP/run2.log"

check "self-update flips the api version" \
  sh -c "grep -q 'APP_VERSION = \"$VER\"' '$FAKE/api/version.py'"
check "self-update backfills the missing shared module (tierrouter.py)" \
  test -f "$FAKE/worker/tierrouter.py"
check "self-update backfills ratewindows.py" \
  test -f "$FAKE/worker/ratewindows.py"
check "self-update backfills emailer.py" \
  test -f "$FAKE/worker/emailer.py"
check "self-update does NOT clobber the worker's own main.py" \
  sh -c "cmp -s '$ROOT/src/worker/main.py' '$FAKE/worker/main.py'"
check "self-update reports success" \
  sh -c "grep -q '\"ok\": true' '$FAKE/volumes/update_status/update_result.json'"

# ── 3. a failed rebuild is reported as such + the request is consumed ──────
# Re-seed the OLD version first: test 2 left the appliance on the NEW version.
printf '"""BareNOC version."""\nAPP_VERSION = "2026.09.01.e"\n' > "$FAKE/api/version.py"
rm -rf "$FAKE/.previous"
make_stubs 1
write_request "file://$TMP/broken.tar.gz"
make_script
run_self_update "$TMP/run3.log"

check "failed rebuild reports 'rebuild failed' (not health/version)" \
  sh -c "grep -q 'rebuild failed' '$FAKE/volumes/update_status/update_result.json'"
check "failed rebuild consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "failed rebuild restores the previous version" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# ── 4-6. the download/extract step fails fast (no silent old-tree rebuild) ──
# A non-tarball (CDN error page), a wrong layout, and a version mismatch must
# each be reported as their REAL cause, consume the request, and leave the
# running tree untouched — not silently rebuilt and rolled back as a
# misleading "health/version" failure (the 09-03 symptom that hid the cause).
make_stubs 0
printf '<html>not a tarball</html>\n' > "$TMP/bad.html"
mkdir -p "$TMP/wronglayout" "$TMP/mismatch/src/api" "$TMP/mismatch/src/worker" \
         "$TMP/mismatch/src/scheduler" "$TMP/mismatch/src/nginx" \
         "$TMP/mismatch/src/scripts" "$TMP/mismatch/src/agent" \
         "$TMP/mismatch/client" \
         "$TMP/partial/src/api" "$TMP/partial/src/worker" \
         "$TMP/partial/src/scheduler" "$TMP/partial/src/nginx" \
         "$TMP/partial/src/scripts" "$TMP/partial/src/agent"
printf 'no release tree here\n' > "$TMP/wronglayout/README.md"
# A FULL-layout tarball whose version.py is NOT the requested release.
printf '"""BareNOC version."""\nAPP_VERSION = "2026.09.03.b"\n' > "$TMP/mismatch/src/api/version.py"
printf 'x\n' > "$TMP/mismatch/src/worker/main.py"
printf 'x\n' > "$TMP/mismatch/src/scheduler/main.py"
printf 'x\n' > "$TMP/mismatch/src/nginx/conf"
printf 'x\n' > "$TMP/mismatch/src/scripts/placeholder"
printf 'x\n' > "$TMP/mismatch/src/agent/placeholder"
printf 'x\n' > "$TMP/mismatch/client/placeholder"
printf 'services:\n' > "$TMP/mismatch/src/docker-compose.yml"
# A PARTIAL tarball — full layout MINUS client/ — matching the requested
# version, so it passes the api+worker+version checks but must still be
# rejected by the full-layout guard (apply_dir would otherwise silently skip
# client/ and leave it stale).
printf '"""BareNOC version."""\nAPP_VERSION = "%s"\n' "$VER" > "$TMP/partial/src/api/version.py"
printf 'x\n' > "$TMP/partial/src/worker/main.py"
printf 'x\n' > "$TMP/partial/src/scheduler/main.py"
printf 'x\n' > "$TMP/partial/src/nginx/conf"
printf 'x\n' > "$TMP/partial/src/scripts/placeholder"
printf 'x\n' > "$TMP/partial/src/agent/placeholder"
printf 'services:\n' > "$TMP/partial/src/docker-compose.yml"
( cd "$TMP/wronglayout" && tar czf "$TMP/wronglayout.tar.gz" README.md )
( cd "$TMP/mismatch" && tar czf "$TMP/mismatch.tar.gz" src client )
( cd "$TMP/partial" && tar czf "$TMP/partial.tar.gz" src )
# A FULL-layout tarball whose version.py EXISTS but is unreadable (empty) —
# must be rejected fail-closed; the old `[ -n "$TARVER" ]` guard passed it.
cp -a "$TMP/mismatch" "$TMP/emptyver"
: > "$TMP/emptyver/src/api/version.py"
( cd "$TMP/emptyver" && tar czf "$TMP/emptyver.tar.gz" src client )

# 4. non-tarball download -> "not a valid release tarball", tree untouched
printf '"""BareNOC version."""\nAPP_VERSION = "2026.09.01.e"\n' > "$FAKE/api/version.py"
write_request "file://$TMP/bad.html"
run_self_update "$TMP/run4.log"
check "non-tarball download reports 'not a valid release tarball'" \
  sh -c "grep -q 'not a valid release tarball' '$FAKE/volumes/update_status/update_result.json'"
check "non-tarball download consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "non-tarball download leaves the tree untouched" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# 5. wrong layout -> "tarball layout invalid", tree untouched
write_request "file://$TMP/wronglayout.tar.gz"
run_self_update "$TMP/run5.log"
check "wrong-layout tarball reports 'tarball layout invalid'" \
  sh -c "grep -q 'tarball layout invalid' '$FAKE/volumes/update_status/update_result.json'"
check "wrong-layout tarball consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "wrong-layout tarball leaves the tree untouched" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# 6. version mismatch -> "tarball version mismatch", tree untouched
write_request "file://$TMP/mismatch.tar.gz"
run_self_update "$TMP/run6.log"
check "version-mismatched tarball reports 'tarball version mismatch'" \
  sh -c "grep -q 'tarball version mismatch' '$FAKE/volumes/update_status/update_result.json'"
check "version-mismatched tarball consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "version-mismatched tarball leaves the tree untouched" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# 6b. unreadable version.py -> "tarball version mismatch", tree untouched.
# The layout check confirms src/api/version.py exists in the archive; an empty
# version here must be a mismatch, not a silent pass (the fail-closed guard).
printf '"""BareNOC version."""\nAPP_VERSION = "2026.09.01.e"\n' > "$FAKE/api/version.py"
write_request "file://$TMP/emptyver.tar.gz"
run_self_update "$TMP/run6b.log"
check "unreadable version.py reports 'tarball version mismatch'" \
  sh -c "grep -q 'tarball version mismatch' '$FAKE/volumes/update_status/update_result.json'"
check "unreadable version.py consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "unreadable version.py leaves the tree untouched" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# 7. partial layout (missing client/) -> "tarball layout invalid", tree untouched.
# A tarball with api+worker+version all correct but a missing mapped dir must
# NOT be applied — the old narrow check would have silently skipped client/.
write_request "file://$TMP/partial.tar.gz"
run_self_update "$TMP/run7.log"
check "partial-layout tarball reports 'tarball layout invalid'" \
  sh -c "grep -q 'tarball layout invalid' '$FAKE/volumes/update_status/update_result.json'"
check "partial-layout tarball consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "partial-layout tarball leaves the tree untouched" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# 8. checksum download failure -> fail CLOSED ("checksum download failed"),
# tree untouched. The old `|| true` skipped verification and let a bad tarball
# through to the apply step.
write_request "file://$TMP/broken.tar.gz" "file://$TMP/nonexistent-sums"
run_self_update "$TMP/run8.log"
check "checksum download failure reports 'checksum download failed'" \
  sh -c "grep -q 'checksum download failed' '$FAKE/volumes/update_status/update_result.json'"
check "checksum download failure consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "checksum download failure leaves the tree untouched" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# 9. checksum mismatch -> "checksum mismatch", tree untouched.
printf '0000000000000000000000000000000000000000000000000000000000000000  bareNOC-x.tar.gz\n' > "$TMP/badsums"
write_request "file://$TMP/broken.tar.gz" "file://$TMP/badsums"
run_self_update "$TMP/run9.log"
check "checksum mismatch reports 'checksum mismatch'" \
  sh -c "grep -q 'checksum mismatch' '$FAKE/volumes/update_status/update_result.json'"
check "checksum mismatch consumes the request file" \
  sh -c "test ! -f '$FAKE/volumes/update_status/update_request.json'"
check "checksum mismatch leaves the tree untouched" \
  sh -c "grep -q 'APP_VERSION = \"2026.09.01.e\"' '$FAKE/api/version.py'"

# 10. a CORRECT checksum still updates (the fail-closed change must not break
# the happy path).
printf '%s  bareNOC-x.tar.gz\n' "$(sha256sum "$TMP/broken.tar.gz" | awk '{print $1}')" > "$TMP/goodsums"
printf '"""BareNOC version."""\nAPP_VERSION = "2026.09.01.e"\n' > "$FAKE/api/version.py"
write_request "file://$TMP/broken.tar.gz" "file://$TMP/goodsums"
run_self_update "$TMP/run10.log"
check "correct checksum still applies the update" \
  sh -c "grep -q 'APP_VERSION = \"$VER\"' '$FAKE/api/version.py'"
check "correct checksum reports success" \
  sh -c "grep -q '\"ok\": true' '$FAKE/volumes/update_status/update_result.json'"

# 11. the download retries on a soft-404 (HTML 200) and still applies. The
# 09-03 P0 signature: curl "succeeded" on Hostinger's HTML 200 error page, the
# apply silently no-op'd, the OLD tree was rebuilt (exit 0), and the version
# check rolled back with a misleading message. The retry loop must re-download
# until a REAL tarball lands (the CDN propagation race), then apply.
cp "$TMP/broken.tar.gz" "$TMP/flaky.tar.gz"
printf '"""BareNOC version."""\nAPP_VERSION = "2026.09.01.e"\n' > "$FAKE/api/version.py"
rm -f "$TMP/flaky.count"
write_request "file://$TMP/flaky.tar.gz"
cat > "$TMP/bin/curl" <<EOF
#!/bin/bash
# flaky-download stub: the FIRST request for flaky.tar.gz returns an HTML 200
# soft-404 (the CDN propagation race); later requests serve the real file.
for a in "\$@"; do
  case "\$a" in
    file://*flaky.tar.gz)
      CNT="$TMP/flaky.count"
      n=0; [ -f "\$CNT" ] && n=\$(cat "\$CNT")
      n=\$((n+1)); echo "\$n" > "\$CNT"
      if [ "\$n" = "1" ]; then
        printf '<html>404 not found</html>\n' > "\${@: -1}"
        exit 0
      fi
      exec /usr/bin/curl "\$@"
      ;;
  esac
done
if printf '%s' "\$*" | grep -q -- '-o /dev/null'; then
  echo "200"
else
  python3 -c "import re;print('{\"version\": \"%s\"}' % re.search(r'APP_VERSION\s*=\s*\"([^\"]+)\"', open('$FAKE/api/version.py').read()).group(1))"
fi
EOF
chmod +x "$TMP/bin/curl"
UPDATE_DL_ATTEMPTS=3 UPDATE_DL_RETRY_DELAY=0 PATH="$TMP/bin:$PATH" \
  "$BASH_BIN" "$TMP/bin/self-update.sh" >"$TMP/run11.log" 2>&1 || true
check "soft-404 first download is retried and the update applies" \
  sh -c "grep -q 'APP_VERSION = \"$VER\"' '$FAKE/api/version.py'"
check "soft-404 retry reports success" \
  sh -c "grep -q '\"ok\": true' '$FAKE/volumes/update_status/update_result.json'"
check "soft-404 retry actually re-downloaded (>=2 attempts)" \
  sh -c "test \"\$(cat '$TMP/flaky.count')\" -ge 2"

echo
if [ "$fail" = "0" ]; then echo "all self_update tests passed"; else echo "TESTS FAILED"; exit 1; fi
