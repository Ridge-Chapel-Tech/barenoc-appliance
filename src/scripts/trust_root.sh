#!/usr/bin/env bash
# trust_root.sh — verify + install the BareNOC signing root into the OS trust store.
#
# Issue #105: the earlier root-trust step blindly anchored whatever
# /onboard/root-ca.crt returned. On a box where that endpoint served an
# unrelated root or a leaf, the store ended up with the WRONG anchors while the
# actual signing root ("BareNOC Internal CA Root CA" — the step-ca root that
# signs the served web cert chain) was missing, so Chrome/curl kept rejecting
# https://<appliance>. This script anchors ONLY the root that actually signs the
# served web cert chain, removes stale barenoc-root anchors the buggy installer
# previously added, and then PROVES the fix (openssl verify + curl without -k).
#
# Usage: sudo bash trust_root.sh [--yes] <appliance_url>
#   --yes   non-interactive opt-in (skip the [y/N] prompt)
#
# Exit codes:
#   0  root anchored + verified (or the install+verify succeeded)
#   1  failed to verify/install (the root was wrong/unrelated/leaf, or the
#      trust store could not be updated) — nothing is anchored
#   2  declined / skipped (no tty without --yes) — nothing changed
#
# Canonical source: agent_install.sh embeds this file verbatim (see the
# TRUST_ROOT heredoc) and src/api/routes/onboard.py generates an equivalent
# inline block for the served onboarding scripts.
set -euo pipefail

YES=0
case "${1:-}" in
  --yes) YES=1; shift ;;
esac
APP="${1:-}"
if [ -z "$APP" ]; then
  echo "usage: $0 [--yes] <appliance_url>" >&2
  exit 1
fi
case "$APP" in
  https://*) ;;
  *) echo "trust_root: appliance_url must be https://... (got $APP)" >&2; exit 1 ;;
esac
# Test hook (scripts/test_trust_root.sh): allow a non-root run so the suite
# can exercise the install/verify path without touching the real /etc.
if [ "${TRUST_ROOT_ALLOW_NONROOT:-0}" != "1" ] && [[ ${EUID:-$(id -u 2>/dev/null || echo 0)} -ne 0 ]]; then
  echo "trust_root: run as root (sudo bash $0 ...)" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Test hooks (scripts/test_trust_root.sh): redirect the anchor dirs so the
# suite can exercise the install/clean path without touching the real /etc.
ANCHOR_FEDORA="${TRUST_ROOT_ANCHORS_DIR:-/etc/pki/ca-trust/source/anchors}"
ANCHOR_DEBIAN="${TRUST_ROOT_CA_CERTS_DIR:-/usr/local/share/ca-certificates}"

# ---- parse host:port out of the https URL --------------------------------
url="${APP#https://}"
url="${url%%/*}"
HOST="${url%%:*}"
PORT="${url##*:}"
if [ "$PORT" = "$HOST" ]; then PORT=443; fi
[ -n "$HOST" ] || { echo "trust_root: could not parse the host out of $APP" >&2; exit 1; }

# ---- fetch the served cert chain (leaf + intermediates; root NOT served) --
# nginx serves leaf + intermediate only (the client must already have the root).
CHAIN="$(timeout 25 openssl s_client -connect "$HOST:$PORT" -servername "$HOST" -showcerts </dev/null 2>/dev/null || true)"

# split_certs <pem-text> <prefix> — write each PEM cert to <prefix>.0, .1, ...
split_certs() {
  local text="$1" prefix="$2" line n=0 in=0 buf=""
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "-----BEGIN CERTIFICATE-----" ]; then in=1; buf=""; fi
    if [ "$in" = "1" ]; then buf="${buf}${line}
"; fi
    if [ "$line" = "-----END CERTIFICATE-----" ]; then
      printf '%s' "$buf" > "${prefix}.${n}"
      n=$((n+1)); in=0; buf=""
    fi
  done <<< "$text"
}
split_certs "$CHAIN" "$TMP/served"

LEAF="$TMP/served.0"
[ -s "$LEAF" ] || { echo "trust_root: no certificate served by $APP (openssl s_client returned nothing)" >&2; exit 1; }

# Intermediates = every cert after the leaf, concatenated in order.
INTER="$TMP/intermediates.pem"
rm -f "$INTER"
i=1
while [ -s "$TMP/served.$i" ]; do
  cat "$TMP/served.$i" >> "$INTER"
  i=$((i+1))
done

# ---- fetch the candidate root from the appliance --------------------------
ROOT="$TMP/root.crt"
curl -sk "$APP/onboard/root-ca.crt" -o "$ROOT"
[ -s "$ROOT" ] || { echo "trust_root: empty root from $APP/onboard/root-ca.crt" >&2; exit 1; }

# ---- the candidate must be a SELF-SIGNED ROOT (never a leaf/intermediate) --
ROOT_SUBJ="$(openssl x509 -in "$ROOT" -noout -subject 2>/dev/null | sed 's/^subject=//' || true)"
ROOT_ISSUER="$(openssl x509 -in "$ROOT" -noout -issuer 2>/dev/null | sed 's/^issuer=//' || true)"
if [ -z "$ROOT_SUBJ" ] || [ "$ROOT_SUBJ" != "$ROOT_ISSUER" ]; then
  echo "trust_root: $APP/onboard/root-ca.crt is NOT a self-signed root (leaf or intermediate) — refusing to anchor it." >&2
  echo "  subject: ${ROOT_SUBJ:-unparsable}" >&2
  echo "  issuer:  ${ROOT_ISSUER:-unparsable}" >&2
  exit 1
fi
if ! openssl x509 -in "$ROOT" -noout -ext basicConstraints 2>/dev/null | grep -q "CA:TRUE"; then
  echo "trust_root: $APP/onboard/root-ca.crt has CA:FALSE (a leaf) — refusing to anchor it." >&2
  exit 1
fi

# ---- the candidate must ACTUALLY SIGN the served chain --------------------
# (this is what catches an unrelated-but-valid root: verify fails below)
if [ -s "$INTER" ]; then
  if ! openssl verify -CAfile "$ROOT" -untrusted "$INTER" "$LEAF" >"$TMP/verify.out" 2>&1; then
    echo "trust_root: the fetched root does NOT sign the served web cert chain — refusing to anchor it (unrelated root)." >&2
    sed 's/^/    /' "$TMP/verify.out" >&2
    exit 1
  fi
else
  if ! openssl verify -CAfile "$ROOT" "$LEAF" >"$TMP/verify.out" 2>&1; then
    echo "trust_root: the fetched root does NOT sign the served web cert — refusing to anchor it." >&2
    sed 's/^/    /' "$TMP/verify.out" >&2
    exit 1
  fi
fi

# ---- opt-in prompt (default OFF; explicit consent only) --------------------
if [ "$YES" -ne 1 ]; then
  if [ -t 0 ]; then
    echo
    echo "Optional: trust the BareNOC root CA so this machine's browsers show"
    echo "$APP as secure (no 'Not Secure' warning). This only affects"
    echo "certificates signed by the BareNOC CA — nothing else is trusted."
    printf "Trust the BareNOC root CA for this machine's browsers? [y/N] "
    read -r ANS || ANS=N
    case "$ANS" in
      y|Y|yes|YES|Yes) ;;
      *) echo "  (declined — root NOT added; re-run with --yes to opt in)"; exit 2 ;;
    esac
  else
    echo "  (browser trust skipped — pass --yes to opt in non-interactively)"
    exit 2
  fi
fi

echo "==> Trusting the BareNOC root CA (opt-in) — $APP will show as secure"
echo "    Scope: only certificates signed by the BareNOC CA."

# ---- self-clean: remove stale barenoc-root anchors the buggy installer added
# (issue #105 migration) from BOTH the Fedora/RHEL and Debian/Ubuntu anchor
# dirs, so a re-enroll clears the wrong anchors before we install the right one.
for d in "$ANCHOR_FEDORA" "$ANCHOR_DEBIAN"; do
  [ -d "$d" ] || continue
  for f in "$d"/barenoc-root*.crt; do
    [ -e "$f" ] || continue
    rm -f "$f"
    echo "  removed stale anchor: $f"
  done
done

# ---- install to the distro's trust store ----------------------------------
if command -v update-ca-trust >/dev/null 2>&1; then
  DST="$ANCHOR_FEDORA/barenoc-root.crt"
  APPLY="update-ca-trust"
elif command -v update-ca-certificates >/dev/null 2>&1; then
  DST="$ANCHOR_DEBIAN/barenoc-root.crt"
  APPLY="update-ca-certificates"
else
  echo "trust_root: no trust-store tool found (update-ca-trust / update-ca-certificates) — cannot activate the root." >&2
  exit 1
fi
install -d -m 0755 "$(dirname "$DST")"
install -m 0644 "$ROOT" "$DST"
"$APPLY" >/dev/null 2>&1 || { echo "trust_root: $APPLY failed — root copied but not activated." >&2; exit 1; }
echo "  installed: $DST ($APPLY)"
echo "  undo anytime: rm $DST && $APPLY"

# ---- Firefox (best-effort, non-fatal) --------------------------------------
# sudo resets HOME to /root — target the invoking user's home.
FF_HOME="$HOME"
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  FF_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6 || true)"
  [ -n "$FF_HOME" ] || FF_HOME="$HOME"
fi
if ! command -v certutil >/dev/null 2>&1; then
  echo "  !!! Firefox NOT covered: certutil missing (Debian/Ubuntu: apt-get install -y libnss3-tools)."
  echo "      Manual, per profile: certutil -A -n \"BareNOC Internal CA Root\" -t \"C,,\" -i $DST -d sql:<profile-dir>"
else
  FF_FOUND=0; FF_IMPORTED=0
  for INI in "$FF_HOME/.mozilla/firefox/profiles.ini" \
             "$FF_HOME/.var/app/org.mozilla.firefox/.mozilla/firefox/profiles.ini" \
             "$FF_HOME/snap/firefox/common/.mozilla/firefox/profiles.ini"; do
    [ -f "$INI" ] || continue
    FF_FOUND=1
    BASE="$(dirname "$INI")"
    while IFS='|' read -r P REL; do
      [ -n "$P" ] || continue
      if [ "$REL" = "0" ]; then D="$P"; else D="$BASE/$P"; fi
      [ -d "$D" ] || continue
      if certutil -A -n "BareNOC Internal CA Root" -t "C,," -i "$DST" -d "sql:$D" >/dev/null 2>&1; then
        echo "    Firefox: trusted in profile $D"; FF_IMPORTED=1
      else
        echo "    Firefox: import into $D failed (non-fatal)"
      fi
    done < <(awk '
      BEGIN { path=""; rel="1" }
      /^\[/ { if (path != "") print path "|" rel; path=""; rel="1"; next }
      /^Path=/ { path=substr($0,6); sub(/^[ \t]+/,"",path); sub(/[ \t\r]+$/,"",path) }
      /^IsRelative=/ { rel=substr($0,12); sub(/^[ \t]+/,"",rel); sub(/[ \t\r]+$/,"",rel) }
      END { if (path != "") print path "|" rel }
    ' "$INI")
  done
  if [ "$FF_FOUND" -eq 0 ]; then
    echo "  !!! Firefox NOT detected (no profiles.ini under $FF_HOME)."
  elif [ "$FF_IMPORTED" -eq 0 ]; then
    echo "  !!! Firefox profiles found but none imported (flatpak/snap sandbox or locked db)."
  fi
fi

# ---- verify the trust now lands (the "no more installed-but-still-red" step) --
echo "  Verifying the trust now lands (no -k):"
OK=1
if [ -s "$INTER" ]; then
  openssl verify -CAfile "$DST" -untrusted "$INTER" "$LEAF" >"$TMP/verify-after.out" 2>&1 || OK=0
else
  openssl verify -CAfile "$DST" "$LEAF" >"$TMP/verify-after.out" 2>&1 || OK=0
fi
if [ "$OK" = "1" ]; then
  echo "  ✓ openssl verify: the installed root chains to the served cert"
else
  echo "  ✗ openssl verify FAILED after install:"
  sed 's/^/    /' "$TMP/verify-after.out"
fi

CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$APP" 2>"$TMP/curl.err" || true)"
if [ "$CODE" = "200" ]; then
  echo "  ✓ curl (no -k): $APP -> HTTP 200 (trust store accepted the cert)"
else
  echo "  ✗ curl (no -k): $APP -> HTTP ${CODE:-000} (still untrusted)"
  [ -s "$TMP/curl.err" ] && sed 's/^/    /' "$TMP/curl.err"
  OK=0
fi

if [ "$OK" = "1" ]; then
  echo "  ✓ Root trust verified."
  exit 0
else
  echo "trust_root: trust did NOT verify — $APP may still show 'Not Secure'." >&2
  exit 1
fi
