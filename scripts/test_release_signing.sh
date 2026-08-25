#!/usr/bin/env bash
# test_release_signing.sh — tests for the detached-signature verify path
# (src/scripts/verify_release_signature.sh) + the release manifest signing:
#   • good-sig: a tarball signed by the PINNED key verifies (exit 0);
#   • bad-sig: a tampered tarball fails closed (exit 1);
#   • wrong-key: a signature by a DIFFERENT key fails closed (exit 1) — the
#     verify path trusts ONLY the pinned key file, never the system keyring;
#   • missing-sig: a pre-mandatory release (>= 2026.08.25.a is mandatory)
#     falls back to hash-only (exit 3), a mandatory release fails closed
#     (exit 1);
#   • missing pinned key → fail closed (exit 4);
#   • scripts/build_release_manifest.py --sign adds assets.signature and
#     --require-sign fails closed when the key is absent;
#   • docs/security/release-signing.pub == src/scripts/release-signing.pub.
#
# A THROWAWAY gpg key is generated inside the test in an isolated GNUPGHOME —
# the REAL release-signing key is never touched or referenced. Runs on CI
# (ubuntu) and the VM host; needs gpg + python3.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERIFY="$ROOT/src/scripts/verify_release_signature.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

command -v gpg >/dev/null 2>&1 || { echo "gpg required" >&2; exit 1; }

# Isolated keyring — never the user's real ~/.gnupg.
GNUPGHOME="$TMP/gnupg"
mkdir -p -m 700 "$GNUPGHOME"
export GNUPGHOME

# ── throwaway signing key + an unrelated throwaway key ─────────────────────
gpg --batch --passphrase '' --quick-generate-key \
  "bareNOC test signer <test-signer@example.com>" ed25519 sign 0 2>/dev/null \
  || { echo "could not generate throwaway signing key" >&2; exit 1; }
gpg --batch --passphrase '' --quick-generate-key \
  "bareNOC unrelated <unrelated@example.com>" ed25519 sign 0 2>/dev/null

gpg --batch --armor --export "test-signer@example.com" > "$TMP/signer.pub"
gpg --batch --armor --export "unrelated@example.com" > "$TMP/unrelated.pub"

# ── fixture tarball (arbitrary bytes — gpg signs bytes) ────────────────────
printf 'bareNOC release payload v1\n' > "$TMP/release.tar.gz"
gpg --batch --yes --armor --detach-sign \
  --local-user "test-signer@example.com" \
  --output "$TMP/release.tar.gz.sig" "$TMP/release.tar.gz" 2>/dev/null

fail=0
check() {  # check <desc> <cmd...>  (passes on exit 0)
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "ok  - $desc"
  else
    echo "FAIL - $desc" >&2
    fail=1
  fi
}
check_rc() {  # check_rc <desc> <expected-rc> <cmd...>
  local desc="$1" want="$2"; shift 2
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  if [ "$got" = "$want" ]; then
    echo "ok  - $desc"
  else
    echo "FAIL - $desc (want rc=$want got rc=$got)" >&2
    fail=1
  fi
}

# ── 1. good-sig: pinned key verifies the signed tarball ────────────────────
check_rc "good-sig verifies (exit 0)" 0 \
  "$VERIFY" "$TMP/release.tar.gz" "$TMP/release.tar.gz.sig" "$TMP/signer.pub" "2026.08.25.a"

# ── 2. bad-sig (tampered tarball): fail closed ─────────────────────────────
printf 'tampered payload\n' > "$TMP/tampered.tar.gz"
check_rc "bad-sig tampered tarball fails (exit 1)" 1 \
  "$VERIFY" "$TMP/tampered.tar.gz" "$TMP/release.tar.gz.sig" "$TMP/signer.pub" "2026.08.25.a"

# ── 3. bad-sig (untrusted key): never consult the ambient keyring ──────────
gpg --batch --yes --armor --detach-sign \
  --local-user "unrelated@example.com" \
  --output "$TMP/unrelated.sig" "$TMP/release.tar.gz" 2>/dev/null
check_rc "bad-sig untrusted key fails (exit 1)" 1 \
  "$VERIFY" "$TMP/release.tar.gz" "$TMP/unrelated.sig" "$TMP/signer.pub" "2026.08.25.a"

# ── 4. missing-sig: pre-mandatory release → hash-only fallback (exit 3) ────
check_rc "missing-sig pre-mandatory falls back (exit 3)" 3 \
  "$VERIFY" "$TMP/release.tar.gz" "" "$TMP/signer.pub" "2026.08.24.b"

# ── 5. missing-sig: mandatory release → fail closed (exit 1) ───────────────
check_rc "missing-sig mandatory fails closed (exit 1)" 1 \
  "$VERIFY" "$TMP/release.tar.gz" "" "$TMP/signer.pub" "2026.08.25.a"

# ── 6. missing pinned key → fail closed (exit 4) ───────────────────────────
check_rc "missing pinned key fails (exit 4)" 4 \
  "$VERIFY" "$TMP/release.tar.gz" "$TMP/release.tar.gz.sig" "$TMP/nope.pub" "2026.08.25.a"

# ── 7. canonical + runtime pubkeys are byte-identical ──────────────────────
check "docs/security/release-signing.pub == src/scripts/release-signing.pub" \
  cmp -s "$ROOT/docs/security/release-signing.pub" "$ROOT/src/scripts/release-signing.pub"

# ── 8. build_release_manifest.py --sign adds assets.signature ──────────────
OUT8="$TMP/dist8"
python3 "$ROOT/scripts/build_release_manifest.py" --out "$OUT8" --source "$ROOT" \
  --sign --signing-email "test-signer@example.com" >/dev/null 2>&1
check "manifest --sign: versions.json gains assets.signature" python3 - "$OUT8/versions.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
sig = (m.get("assets") or {}).get("signature", "")
assert sig.endswith(".tar.gz.sig"), sig
PY
check "manifest --sign: .sig written next to the tarball" python3 - "$OUT8/versions.json" "$OUT8" <<'PY'
import json, os, sys
m = json.load(open(sys.argv[1]))
ver = m["version"]
assert os.path.isfile(os.path.join(sys.argv[2], f"bareNOC-{ver}.tar.gz.sig")), "sig missing"
PY
# the produced .sig verifies against the throwaway key
SIGNED_TARBALL="$(python3 -c "import json;print('$OUT8/' + 'bareNOC-' + json.load(open('$OUT8/versions.json'))['version'] + '.tar.gz')")"
check_rc "manifest --sign: produced .sig verifies (exit 0)" 0 \
  "$VERIFY" "$SIGNED_TARBALL" "$SIGNED_TARBALL.sig" "$TMP/signer.pub" "2026.08.25.a"

# ── 9. --require-sign fails closed when the key is absent ──────────────────
EMPTY="$TMP/emptygnupg"; mkdir -p -m 700 "$EMPTY"
check_rc "manifest --require-sign without key fails (exit 1)" 1 \
  env GNUPGHOME="$EMPTY" python3 "$ROOT/scripts/build_release_manifest.py" \
    --out "$TMP/dist9" --source "$ROOT" --require-sign

echo
if [ "$fail" = "0" ]; then echo "all release_signing tests passed"; else echo "TESTS FAILED"; exit 1; fi
