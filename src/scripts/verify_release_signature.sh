#!/bin/bash
# verify_release_signature.sh — verify the detached GPG signature on a release
# tarball against the PINNED BareNOC release-signing public key.
#
# This is the appliance-side half of release signing (the other half is
# scripts/build_release_manifest.py, which signs the tarball at release time).
# It deliberately does NOT trust the system keyring: the public key is imported
# into a throwaway GNUPGHOME from the pinned key file only, so a signature made
# by any other key fails.
#
# Usage:
#   verify_release_signature.sh <tarball> <sig> <pinned-pubkey> <version>
#
#   <tarball>      the downloaded release tarball (bareNOC-<ver>.tar.gz)
#   <sig>          the detached signature (bareNOC-<ver>.tar.gz.sig) — may be
#                  empty/absent for pre-signing releases
#   <pinned-pubkey> the pinned public key file (docs/security/release-signing.pub)
#   <version>      the release version being applied (bareNOC CalVer, e.g.
#                  2026.08.25.a) — decides whether a signature is MANDATORY
#
# Exit codes:
#   0  signature valid
#   1  signature invalid / missing-on-mandatory / gpg failure — FAIL CLOSED
#   3  no signature and <version> predates mandatory signing — hash-only fallback
#   4  pinned public key missing / unusable — FAIL CLOSED (can't verify at all)
#   2  usage error
set -uo pipefail

MANDATORY_SIG_VERSION="2026.08.25.a"   # first signed release; sig mandatory >= this
SIGNING_EMAIL="${BARE_NOC_RELEASE_SIGNING_EMAIL:-release@barenoc.com}"

TARBALL="${1:-}"
SIG="${2:-}"
PINNED_KEY="${3:-}"
VERSION="${4:-}"

usage() {
  echo "usage: verify_release_signature.sh <tarball> <sig> <pinned-pubkey> <version>" >&2
  exit 2
}
[ -n "$TARBALL" ] && [ -n "$VERSION" ] || usage

# CalVer ordering with an optional single-letter suffix (2026.08.25.a).
# Python3 is present on the appliance (the self-update script already uses it).
version_at_least() {  # version_at_least <a> <b> -> 0 when a >= b
  python3 - "$1" "$2" <<'PY'
import re, sys
def parts(v):
    m = re.match(r'^v?(\d{4})\.(\d{2})\.(\d{2})(?:\.([a-z]+))?$', str(v).strip().lower())
    if not m:
        return None
    y, mo, d, suf = m.groups()
    # no suffix sorts before any suffix: '`' (chr 96) < 'a' (chr 97)
    return (int(y), int(mo), int(d), suf or '`')
a, b = parts(sys.argv[1]), parts(sys.argv[2])
sys.exit(0 if (a is not None and b is not None and a >= b) else 1)
PY
}

# ── no signature ───────────────────────────────────────────────────────────
if [ -z "$SIG" ] || [ ! -s "$SIG" ]; then
  if version_at_least "$VERSION" "$MANDATORY_SIG_VERSION"; then
    echo "verify_release_signature: release $VERSION REQUIRES a detached signature (mandatory since $MANDATORY_SIG_VERSION) — none provided" >&2
    exit 1
  fi
  echo "verify_release_signature: release $VERSION predates mandatory signing ($MANDATORY_SIG_VERSION) — hash-only fallback" >&2
  exit 3
fi

# ── pinned key must exist ──────────────────────────────────────────────────
if [ ! -s "$PINNED_KEY" ]; then
  echo "verify_release_signature: pinned public key not found: $PINNED_KEY" >&2
  exit 4
fi
if ! command -v gpg >/dev/null 2>&1; then
  echo "verify_release_signature: gpg not found — cannot verify" >&2
  exit 4
fi

# ── verify against ONLY the pinned key (never the system keyring) ──────────
GNUPGHOME="$(mktemp -d)"
trap 'rm -rf "$GNUPGHOME"' EXIT
chmod 700 "$GNUPGHOME"
export GNUPGHOME

if ! gpg --batch --quiet --import "$PINNED_KEY" >/dev/null 2>&1; then
  echo "verify_release_signature: failed to import the pinned public key (unusable key file?)" >&2
  exit 4
fi

if gpg --batch --quiet --verify "$SIG" "$TARBALL" 2>/dev/null; then
  echo "verify_release_signature: signature OK (pinned release-signing key)"
  exit 0
fi

echo "verify_release_signature: signature verification FAILED — tarball is tampered or signed by an untrusted key" >&2
exit 1
