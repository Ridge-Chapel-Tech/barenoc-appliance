#!/usr/bin/env bash
# test_trust_root.sh — tests for src/scripts/trust_root.sh (issue #105):
#   • anchors ONLY the root that signs the served web cert chain (a real
#     openssl fixture: root + intermediate + leaf) — never an unrelated root
#     or a leaf;
#   • self-cleans stale barenoc-root anchors the buggy installer left behind;
#   • supports Fedora/RHEL (update-ca-trust) AND Debian/Ubuntu
#     (update-ca-certificates);
#   • verify-after install (openssl verify + curl without -k);
#   • non-interactive runs never install without --yes.
#
# The network fetch is mocked (curl + `openssl s_client`); the crypto checks
# (openssl x509/verify) run against the REAL openssl on the host. Run on CI
# (ubuntu) or the VM host.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/src/scripts/trust_root.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

BIN="$TMP/bin"
CORE="$TMP/core"
mkdir -p "$BIN" "$CORE"

REAL_OPENSSL="$(command -v openssl)" || { echo "openssl required" >&2; exit 1; }
export REAL_OPENSSL

# Core utils come from the real host (never a mocked curl/openssl).
for c in awk sed grep cat cut dirname mkdir mktemp printf rm timeout head tail tr base64 id getent install; do
  p=$(command -v "$c") || { echo "missing core util: $c" >&2; exit 1; }
  ln -sf "$p" "$CORE/$c"
done

FIX="$TMP/fix"
mkdir -p "$FIX"
cd "$FIX"

# ── build the CA fixture with the real openssl ──────────────────────────────
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout root.key -out root.crt -days 3650 \
  -subj "/O=BareNOC Internal CA/CN=BareNOC Internal CA Root CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout int.key -out int.csr \
  -subj "/O=BareNOC Internal CA/CN=BareNOC Internal CA Intermediate CA" 2>/dev/null
openssl x509 -req -in int.csr -CA root.crt -CAkey root.key -CAcreateserial \
  -days 3650 -sha256 -extfile <(printf 'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n') \
  -out int.crt 2>/dev/null
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout leaf.key -out leaf.csr -subj "/CN=app.barenoc.com" 2>/dev/null
openssl x509 -req -in leaf.csr -CA int.crt -CAkey int.key -CAcreateserial \
  -days 3650 -sha256 -extfile <(printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:app.barenoc.com,IP:192.0.2.207\n') \
  -out leaf.crt 2>/dev/null
# unrelated root (self-signed CA, different subject) — the bug's wrong anchor.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout other.key -out other.crt -days 3650 \
  -subj "/CN=bareNOC appliance" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
# self-signed LEAF (CA:FALSE) — must be rejected too.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout selfleaf.key -out selfleaf.crt -days 3650 \
  -subj "/CN=app.barenoc.com" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "extendedKeyUsage=serverAuth" 2>/dev/null
cat leaf.crt int.crt > served-chain.pem

openssl verify -CAfile root.crt -untrusted int.crt leaf.crt >/dev/null 2>&1 \
  || { echo "fixture chain does not verify — test setup broken" >&2; exit 1; }

# ── mocks ───────────────────────────────────────────────────────────────────
reset_bin() {
  rm -f "$BIN"/*
  cat > "$BIN/curl" <<'EOF'
#!/bin/bash
if [ "$1" = "-sk" ]; then
  shift
  while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then cat "${FIXTURE_ROOT:?}" > "$2"; exit 0; fi
    shift
  done
  exit 1
fi
if [ "$1" = "-sS" ]; then
  printf '%s' "${CURL_CODE:-200}"
  exit 0
fi
exit 1
EOF
  cat > "$BIN/openssl" <<'EOF'
#!/bin/bash
if [ "$1" = "s_client" ]; then cat "${SERVED_CHAIN:?}"; exit 0; fi
exec "${REAL_OPENSSL:?}" "$@"
EOF
  cat > "$BIN/id" <<'EOF'
#!/bin/bash
echo 0
EOF
  cat > "$BIN/update-ca-trust" <<'EOF'
#!/bin/bash
echo "update-ca-trust" >> "${APPLY_LOG:?}"
exit 0
EOF
  cat > "$BIN/update-ca-certificates" <<'EOF'
#!/bin/bash
echo "update-ca-certificates" >> "${APPLY_LOG:?}"
exit 0
EOF
  chmod +x "$BIN/curl" "$BIN/openssl" "$BIN/id" "$BIN/update-ca-trust" "$BIN/update-ca-certificates"
}
fedora_bin() { reset_bin; rm -f "$BIN/update-ca-certificates"; }
debian_bin() { reset_bin; rm -f "$BIN/update-ca-trust"; }

export FIXTURE_ROOT="$FIX/root.crt"
export SERVED_CHAIN="$FIX/served-chain.pem"
export APPLY_LOG="$TMP/apply.log"
export HOME="$TMP/home"
mkdir -p "$HOME"
ANCHORS="$TMP/anchors"
CACERTS="$TMP/cacerts"
mkdir -p "$ANCHORS" "$CACERTS"

BASH_BIN="$(command -v bash)"
OUT="$TMP/out.txt"
run_trust() {  # run_trust [--yes] → runs with env + redirected stdin, output to $OUT
  : > "$APPLY_LOG"
  local rc=0
  PATH="$BIN:$CORE" \
    TRUST_ROOT_ANCHORS_DIR="$ANCHORS" TRUST_ROOT_CA_CERTS_DIR="$CACERTS" \
    TRUST_ROOT_ALLOW_NONROOT=1 \
    "$BASH_BIN" "$SCRIPT" "$@" </dev/null >"$OUT" 2>&1 && rc=0 || rc=$?
  printf '%s' "$rc" > "$OUT.rc"
  return 0
}

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
check_eq() {  # check_eq <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "ok  - $1"; else echo "FAIL - $1 (want '$2' got '$3')" >&2; fail=1; fi
}

# ── 1. correct root is anchored + verified (fedora path) ────────────────────
fedora_bin
run_trust --yes https://app.barenoc.com; rc=$(cat "$OUT.rc")
check_eq "correct root: exit 0" "0" "$rc"
check "correct root: anchored file equals the signing root" cmp -s "$ANCHORS/barenoc-root.crt" "$FIX/root.crt"
check "correct root: openssl verify reported" grep -q "openssl verify: the installed root chains" "$OUT"
check "correct root: curl (no -k) reported" grep -q "curl (no -k)" "$OUT"
check "correct root: update-ca-trust invoked" grep -q "update-ca-trust" "$APPLY_LOG"

# ── 2. unrelated root is REJECTED (never anchored) ─────────────────────────
fedora_bin
rm -f "$ANCHORS/barenoc-root.crt"
export FIXTURE_ROOT="$FIX/other.crt"
run_trust --yes https://app.barenoc.com; rc=$(cat "$OUT.rc")
export FIXTURE_ROOT="$FIX/root.crt"
check_eq "unrelated root: exit 1" "1" "$rc"
check "unrelated root: NOT anchored" sh -c "[ ! -e '$ANCHORS/barenoc-root.crt' ]"
check "unrelated root: error names the mismatch" grep -q "does NOT sign" "$OUT"

# ── 3. a leaf is REJECTED (CA:FALSE) ───────────────────────────────────────
fedora_bin
rm -f "$ANCHORS/barenoc-root.crt"
export FIXTURE_ROOT="$FIX/selfleaf.crt"
run_trust --yes https://app.barenoc.com; rc=$(cat "$OUT.rc")
export FIXTURE_ROOT="$FIX/root.crt"
check_eq "leaf-as-root: exit 1" "1" "$rc"
check "leaf-as-root: NOT anchored" sh -c "[ ! -e '$ANCHORS/barenoc-root.crt' ]"
check "leaf-as-root: error names CA:FALSE" grep -q "CA:FALSE" "$OUT"

# ── 4. self-clean: stale wrong anchors are removed before re-install ───────
fedora_bin
cp "$FIX/other.crt" "$ANCHORS/barenoc-root.crt"
cp "$FIX/other.crt" "$ANCHORS/barenoc-root-old.crt"
run_trust --yes https://app.barenoc.com; rc=$(cat "$OUT.rc")
check_eq "self-clean: exit 0" "0" "$rc"
check "self-clean: stale anchor removed" sh -c "[ ! -e '$ANCHORS/barenoc-root-old.crt' ]"
check "self-clean: replaced with the correct root" cmp -s "$ANCHORS/barenoc-root.crt" "$FIX/root.crt"
check "self-clean: reported the removal" grep -q "removed stale anchor" "$OUT"

# ── 5. non-interactive without --yes → declined, nothing installed ─────────
fedora_bin
rm -f "$ANCHORS/barenoc-root.crt"
run_trust https://app.barenoc.com; rc=$(cat "$OUT.rc")
check_eq "no --yes + no tty: exit 2" "2" "$rc"
check "no --yes + no tty: nothing anchored" sh -c "[ ! -e '$ANCHORS/barenoc-root.crt' ]"

# ── 6. debian path (update-ca-certificates) ───────────────────────────────
debian_bin
run_trust --yes https://app.barenoc.com; rc=$(cat "$OUT.rc")
check_eq "debian path: exit 0" "0" "$rc"
check "debian path: anchored under the ca-certificates dir" cmp -s "$CACERTS/barenoc-root.crt" "$FIX/root.crt"
check "debian path: update-ca-certificates invoked" grep -q "update-ca-certificates" "$APPLY_LOG"

# ── 7. the api copy stays byte-identical to the canonical script ──────────
check "src/api/routes/trust_root.sh == src/scripts/trust_root.sh" cmp -s "$ROOT/src/scripts/trust_root.sh" "$ROOT/src/api/routes/trust_root.sh"

# ── 8. agent_install.sh embeds the canonical trust_root.sh ─────────────────
check "agent_install.sh embeds the canonical trust_root.sh" python3 - "$ROOT" <<'PY'
import sys, os
root = sys.argv[1]
canon = open(os.path.join(root, "src/scripts/trust_root.sh")).read().rstrip("\n")
inst = open(os.path.join(root, "agent-go/scripts/agent_install.sh")).read()
start = inst.index("<<'TRUST_ROOT'\n") + len("<<'TRUST_ROOT'\n")
end = inst.index("\nTRUST_ROOT\n", start)
assert inst[start:end].rstrip("\n") == canon, "embedded trust_root.sh drifted from src/scripts/trust_root.sh"
PY

echo
if [ "$fail" = "0" ]; then echo "all trust_root tests passed"; else echo "TESTS FAILED"; exit 1; fi
