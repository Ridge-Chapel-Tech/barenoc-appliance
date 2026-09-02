#!/usr/bin/env bash
# agent_install.sh — one-command Linux install of NOC_Agent (P1b).
#
# Installs the agent on a Linux endpoint, enrolls a device certificate with
# step-ca, writes the config, installs the systemd service, and grants the
# capability-gated sudoers set. The agent's FIRST report auto-claims the device
# with method="agent" — no SSH control involved.
#
# Usage: sudo bash agent_install.sh [--trust-root] <appliance_url> <enrollment_token> [noc-agent-binary]
#   --trust-root      OPT-IN: anchor the appliance's SIGNING root (the step-ca
#                     root that actually signs the served web cert chain) into
#                     the OS trust store (+ Firefox NSS, best-effort) so
#                     browsers stop showing "Not Secure". The installer verifies
#                     the root signs the served chain, removes any stale wrong
#                     anchors it previously added, and verifies the trust lands
#                     (openssl verify + curl without -k). Default OFF.
#   appliance_url     = https://<appliance>  (as the endpoint reaches it)
#   enrollment_token  = one-time step-ca token minted by the appliance
#                       (POST /devices/<id>/adopt/cert, or /onboard/token?cn=...)
#   noc-agent-binary  = optional path to the built binary; defaults to
#                       ./noc-agent next to this script.
#
# The endpoint fetches step-cli + the CA root + the CA fingerprint from the
# appliance itself (no internet, no external trust), mirroring the existing
# /onboard flow and src/scripts/enroll_device.sh.
#
# Env overrides:
#   STEPCA_URL  CA base URL (default https://stepca.barenoc.local:8443)
set -euo pipefail

CA_URL="${STEPCA_URL:-https://stepca.barenoc.local:8443}"

INSTALL_DIR="/opt/noc-agent"
CERTS="${INSTALL_DIR}/certs"
BIN="${INSTALL_DIR}/noc-agent"
SERVICE_USER="nocagent"
CA_CERT_PATH="${CERTS}/ca.crt"
UNIT="/etc/systemd/system/noc-agent.service"

fail() { echo "agent_install: $*" >&2; exit 1; }
usage() {
  echo "usage: sudo $0 [--trust-root] <appliance_url> <enrollment_token> [noc-agent-binary]" >&2
  echo "  --trust-root  OPT-IN: anchor the appliance's SIGNING root (verified to" >&2
  echo "                sign the served web cert chain) into the OS trust store" >&2
  echo "                (+ Firefox NSS, best-effort). Removes stale wrong anchors" >&2
  echo "                and verifies the trust lands. Default OFF." >&2
  exit 2
}

# curl is required by every fetch below. Minimal endpoints ship without it
# (wget-only or nothing), which used to fail with a bare "command not found"
# mid-install. Auto-install it from the detected package manager.
ensure_curl() {
  if command -v curl >/dev/null 2>&1; then return 0; fi
  echo "==> curl not found — installing it"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq curl
  elif command -v dnf >/dev/null 2>&1; then
    dnf -y install curl
  elif command -v yum >/dev/null 2>&1; then
    yum -y install curl
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache curl
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install curl
  else
    fail "curl is required but not installed, and no supported package manager (apt/dnf/yum/apk/zypper) was found — install curl first"
  fi
  command -v curl >/dev/null 2>&1 || fail "curl install failed"
}

# appliance_host — the hostname/IP out of $APP (the front door we already
# reached), with any :port and path stripped. Used as the CA-bootstrap DNS
# fallback when stepca.barenoc.local does not resolve.
app_host() {
  local h="${APP#https://}"
  h="${h%%/*}"
  h="${h%%:*}"
  printf '%s' "$h"
}

# Browser trust opt-in — default OFF. Trusting this private root makes the
# appliance's HTTPS trusted; it only affects certs signed by the BareNOC CA.
# Never install silently.
TRUST_ROOT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --trust-root) TRUST_ROOT=1; shift ;;
    -h|--help) usage ;;
    --) shift; break ;;
    -*) fail "unknown option: $1 (only --trust-root is supported)" ;;
    *) break ;;
  esac
done

APP="${1:-}"
TOKEN="${2:-}"
SRC_BIN="${3:-}"

[ -n "$APP" ] && [ -n "$TOKEN" ] || usage
case "$APP" in https://*) ;; *) fail "appliance_url must be https://... (got $APP)";; esac
[[ ${EUID} -eq 0 ]] || fail "run as root (sudo $0 ...)"

# Binary: explicit path, else the built one next to the script.
if [[ -z "$SRC_BIN" ]]; then
  SRC_BIN="$(cd "$(dirname "$0")/.." && pwd)/noc-agent"
fi
[[ -f "$SRC_BIN" ]] || fail "binary not found at $SRC_BIN; build it first (cd agent-go && go build -o noc-agent ./cmd/noc-agent) and pass it as the 3rd arg"

# Stable, safe CN from the hostname (same rule as the /onboard flow).
HOST="$(hostname | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-._' | cut -c1-60)"
CN="device-${HOST:-node}"

echo "==> Fetching trust + appliance DNS mapping from $APP"
ensure_curl
INFO="$(curl -sk "$APP/onboard/info")"
APP_IP="$(echo "$INFO" | grep -o '"appliance_ip": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
CA_FP="$(echo "$INFO" | grep -o '"ca_fingerprint": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
[ -n "$CA_FP" ] || fail "could not read ca_fingerprint from $APP/onboard/info"
# DNS fallback: when the appliance doesn't publish appliance_ip, derive it from
# the URL the operator already reached the appliance at (so the stepca hosts
# entry still gets written and enrollment does not depend on split-horizon DNS).
if [ -z "$APP_IP" ]; then
  APP_IP="$(app_host)"
fi
if [ -n "$APP_IP" ] && echo "$APP_IP" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  # REPLACE any stale entry — a re-onboarded box can carry an old appliance IP
  # that silently breaks enrollment.
  sed -i '/stepca\.barenoc\.local/d' /etc/hosts
  echo "$APP_IP stepca.barenoc.local app.barenoc.com bareNOC.local" >> /etc/hosts
fi

echo "==> Installing the BareNOC CA root ($CA_CERT_PATH)"
install -d -m 0755 "$CERTS"
curl -sk "$APP/onboard/root-ca.crt" -o "$CA_CERT_PATH"
[ -s "$CA_CERT_PATH" ] || fail "empty CA root from $APP/onboard/root-ca.crt"

# ---- browser/OS trust of the signing root (opt-in, BEFORE completion) ------
# issue #105: anchor ONLY the root that signs the served web cert chain,
# self-clean stale wrong anchors the buggy installer added, and verify the
# trust actually lands. Runs BEFORE the completion message below.
TRUST_RC=0
TRUST_SCRIPT="$(mktemp)"
trap 'rm -f "$TRUST_SCRIPT"' EXIT
cat > "$TRUST_SCRIPT" <<'TRUST_ROOT'
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

TRUST_ROOT
if [[ "$TRUST_ROOT" -eq 1 ]]; then
  bash "$TRUST_SCRIPT" --yes "$APP" || TRUST_RC=$?
else
  bash "$TRUST_SCRIPT" "$APP" || TRUST_RC=$?
fi

echo "==> Installing step-cli from the appliance"
curl -sk -o /tmp/step "$APP/step-cli"
install -m 0755 /tmp/step /usr/local/bin/step

echo "==> Bootstrapping the CA by fingerprint + enrolling $CN"
export STEPPATH=/root/.step
# --force: a stale /root/.step (e.g. from a previous CA) made bootstrap open an
# interactive overwrite prompt and hang forever (08-17, twice). Idempotent re-enroll.
# DNS fallback: stepca.barenoc.local only resolves via the /etc/hosts entry we
# wrote above (or the appliance's split-horizon DNS). If bootstrap still fails,
# retry against the appliance host itself on :8443 — the step-ca listener — so
# a box whose /etc/hosts entry could not be written still enrolls.
bootstrap_ca() {
  step ca bootstrap --ca-url "$1" --fingerprint "$CA_FP" --force </dev/null >/dev/null 2>&1
}
if ! bootstrap_ca "$CA_URL"; then
  CA_FALLBACK_URL="https://$(app_host):8443"
  if [ "$CA_FALLBACK_URL" = "$CA_URL" ]; then
    fail "CA bootstrap failed (ca-url $CA_URL)"
  fi
  echo "  CA bootstrap failed via $CA_URL — retrying via $CA_FALLBACK_URL"
  bootstrap_ca "$CA_FALLBACK_URL" \
    || fail "CA bootstrap failed (tried $CA_URL and $CA_FALLBACK_URL)"
fi
step ca certificate "$CN" "$CERTS/noc-agent.crt" "$CERTS/noc-agent.key" \
  --token "$TOKEN" --root "$CA_CERT_PATH" \
  || fail "certificate enrollment failed"

echo "==> Creating $SERVICE_USER system user (if missing)"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "$INSTALL_DIR" \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Installing the binary"
install -d -m 0755 "$INSTALL_DIR"/logs "$INSTALL_DIR"/state
install -m 0755 "$SRC_BIN" "$BIN"

echo "==> Writing $INSTALL_DIR/config.json"
cat > "$INSTALL_DIR/config.json" <<EOF
{
  "appliance_url": "$APP",
  "cn": "$CN",
  "cert_file": "$CERTS/noc-agent.crt",
  "key_file": "$CERTS/noc-agent.key",
  "ca_file": "$CA_CERT_PATH",
  "state_db": "$INSTALL_DIR/state/noc-agent.db",
  "poll_interval": "30s",
  "log_level": "info"
}
EOF

echo "==> Installing the multi-source update check script (read-only)"
install -d -m 0755 "$INSTALL_DIR"/scripts
cat > "$INSTALL_DIR/scripts/check_updates.sh" <<'CHECK_UPDATES_MULTI'
#!/bin/bash
# check_updates_multi.sh — READ-ONLY multi-source update check (runs ON the endpoint).
#
# Explores ALL update sources, not just the OS package manager (the 08-20 gap:
# the App Center aggregates rpm + flatpak + firmware, while the engine only
# checked apt/dnf/yum/apk/zypper). NEVER installs anything — apply is a
# separate gated action (the queued OS-aware lane).
#
# Sources:
#   OS package manager (apt/dnf/yum/apk/zypper) — WITH metadata refresh so
#     stale metadata never hides updates.
#   flatpak    — `flatpak remote-ls --updates`
#   firmware   — `fwupdmgr get-updates` (fwupd)
#   snap       — `snap refresh --list` (snapd)
#   rpm-ostree — `rpm-ostree upgrade --check` (atomic distros only)
#
# Emits ONE JSON object on stdout; exit 0 = the check ran (even with zero
# updates), exit 1 = sudo was denied / no recognized update source:
#   {"success":true,"package_manager":"dnf","sources":{"dnf":2,"flatpak":0,
#    "firmware":1,"snap":0,"rpm_ostree":0},"total":3,"updates_available":true,
#    "updates_b64":"<base64 of the detail listing>"}
# When invoked as `bash -s <target>` (the apply_patch.sh SSH path) the same
# JSON also carries "action":"check_patches" + "target":"<target>".
#
# Run as root or as the scoped control user (barenoc / nocagent). Commands that
# need root escalate via `sudo -n`; the sudoers allowlist covers ONLY these
# tools (never ALL). A denied sudo surfaces as an error, never an empty success.

TARGET="${1:-}"

SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo -n"

DENIED=0
denied() {
  case "$1" in
    *"a password is required"*|*"not in the sudoers file"*|*"may not run sudo"*|*"is not allowed to run sudo"*|*"incorrect password"*)
      return 0 ;;
    *) return 1 ;;
  esac
}

# run_sudo CMD... — echo stdout of `[sudo -n] CMD...`; flag DENIED if sudo
# auth was refused (a sudo-auth message in the output), never swallowing it.
run_sudo() {
  local out rc
  out=$($SUDO "$@" 2>&1); rc=$?
  if [ $rc -ne 0 ] && denied "$out"; then
    DENIED=1
    return 1
  fi
  printf '%s\n' "$out"
  return $rc
}

count_lines() {
  printf '%s\n' "$1" | grep -vc '^[[:space:]]*$'
}

DETAIL=""
OS_COUNT=0
FLATPAK_COUNT=0
FW_COUNT=0
SNAP_COUNT=0
RPMOSTREE_COUNT=0

# ── OS package manager (family name for the report) ──────────────────────────
PM="unknown"
if command -v apt-get >/dev/null 2>&1; then PM="apt"
elif command -v dnf >/dev/null 2>&1; then PM="dnf"
elif command -v yum >/dev/null 2>&1; then PM="yum"
elif command -v apk >/dev/null 2>&1; then PM="apk"
elif command -v zypper >/dev/null 2>&1; then PM="zypper"
fi

# Atomic distros (Fedora Silverblue/Kinoite, etc.) update through rpm-ostree,
# not the dnf/rpm layer — `dnf check-update` only shows layered packages there.
ATOMIC=0
command -v rpm-ostree >/dev/null 2>&1 && ATOMIC=1

if [ "$ATOMIC" = "1" ]; then
  DETAIL="${DETAIL}[OS] atomic — updates flow through rpm-ostree (dnf layer skipped)
"
else
  case "$PM" in
    apt)
      # Refresh metadata first (stale metadata must never hide updates).
      run_sudo apt-get update -qq >/dev/null 2>&1
      LST=$(apt list --upgradable 2>/dev/null | grep -v '^Listing' | grep -v '^[[:space:]]*$')
      OS_COUNT=$(count_lines "$LST")
      DETAIL="${DETAIL}[apt] ${OS_COUNT} package update(s) available
"
      LISTING=$(printf '%s\n' "$LST" | head -n 30 | sed 's/^/  /')
      [ -n "$LISTING" ] && DETAIL="${DETAIL}${LISTING}
"
      ;;
    dnf)
      OUT=$(run_sudo dnf check-update --refresh -q)
      LST=$(printf '%s\n' "$OUT" | awk 'NF>=3 && $1 ~ /\./')
      OS_COUNT=$(count_lines "$LST")
      DETAIL="${DETAIL}[dnf] ${OS_COUNT} package update(s) available
"
      LISTING=$(printf '%s\n' "$LST" | head -n 30 | sed 's/^/  /')
      [ -n "$LISTING" ] && DETAIL="${DETAIL}${LISTING}
"
      ;;
    yum)
      run_sudo yum makecache -q >/dev/null 2>&1   # refresh (best-effort)
      OUT=$(run_sudo yum check-update -q)
      LST=$(printf '%s\n' "$OUT" | awk 'NF>=3 && $1 ~ /\./')
      OS_COUNT=$(count_lines "$LST")
      DETAIL="${DETAIL}[yum] ${OS_COUNT} package update(s) available
"
      LISTING=$(printf '%s\n' "$LST" | head -n 30 | sed 's/^/  /')
      [ -n "$LISTING" ] && DETAIL="${DETAIL}${LISTING}
"
      ;;
    apk)
      run_sudo apk update -q >/dev/null 2>&1   # refresh
      LST=$(apk list -u 2>/dev/null | grep -v '^WARNING\|^fetch' | grep -v '^[[:space:]]*$')
      OS_COUNT=$(count_lines "$LST")
      DETAIL="${DETAIL}[apk] ${OS_COUNT} package update(s) available
"
      LISTING=$(printf '%s\n' "$LST" | head -n 30 | sed 's/^/  /')
      [ -n "$LISTING" ] && DETAIL="${DETAIL}${LISTING}
"
      ;;
    zypper)
      run_sudo zypper refresh >/dev/null 2>&1   # refresh (best-effort)
      LST=$(run_sudo zypper list-updates | grep -E '^[a-z] *\|')
      OS_COUNT=$(count_lines "$LST")
      DETAIL="${DETAIL}[zypper] ${OS_COUNT} package update(s) available
"
      LISTING=$(printf '%s\n' "$LST" | head -n 30 | sed 's/^/  /')
      [ -n "$LISTING" ] && DETAIL="${DETAIL}${LISTING}
"
      ;;
    *)
      DETAIL="${DETAIL}[OS] no supported package manager detected
"
      ;;
  esac
fi

# ── flatpak ─────────────────────────────────────────────────────────────────
if command -v flatpak >/dev/null 2>&1; then
  LST=$(flatpak remote-ls --updates 2>/dev/null | awk '!/^[[:space:]]*$/ && !/^Application[[:space:]]+ID/')
  FLATPAK_COUNT=$(count_lines "$LST")
  DETAIL="${DETAIL}[flatpak] ${FLATPAK_COUNT} update(s) available
"
  LISTING=$(printf '%s\n' "$LST" | head -n 30 | sed 's/^/  /')
  [ -n "$LISTING" ] && DETAIL="${DETAIL}${LISTING}
"
else
  DETAIL="${DETAIL}[flatpak] flatpak not installed
"
fi

# ── firmware (fwupd) ────────────────────────────────────────────────────────
if command -v fwupdmgr >/dev/null 2>&1; then
  FWOUT=$(run_sudo fwupdmgr get-updates)
  if echo "$FWOUT" | grep -qi "no updates available"; then
    FW_COUNT=0
  else
    FW_COUNT=$(printf '%s\n' "$FWOUT" | grep -ci 'new version\|upgrade .* from .* to ' || true)
    [ "$FW_COUNT" -gt 0 ] || FW_COUNT=1
  fi
  DETAIL="${DETAIL}[firmware] ${FW_COUNT} firmware update(s) available
"
else
  DETAIL="${DETAIL}[firmware] fwupd not installed
"
fi

# ── snap ────────────────────────────────────────────────────────────────────
if command -v snap >/dev/null 2>&1; then
  LST=$(snap refresh --list 2>/dev/null | tail -n +2 | grep -v '^[[:space:]]*$' | grep -v '^All snaps up to date')
  SNAP_COUNT=$(count_lines "$LST")
  DETAIL="${DETAIL}[snap] ${SNAP_COUNT} update(s) available
"
  LISTING=$(printf '%s\n' "$LST" | head -n 30 | sed 's/^/  /')
  [ -n "$LISTING" ] && DETAIL="${DETAIL}${LISTING}
"
else
  DETAIL="${DETAIL}[snap] snapd not installed
"
fi

# ── rpm-ostree (atomic) ─────────────────────────────────────────────────────
if [ "$ATOMIC" = "1" ]; then
  OUT=$(run_sudo rpm-ostree upgrade --check)
  if echo "$OUT" | grep -qi "no updates available\|already up to date\|up to date"; then
    RPMOSTREE_COUNT=0
  elif echo "$OUT" | grep -qi "availableupdate\|update available"; then
    RPMOSTREE_COUNT=1
  else
    RPMOSTREE_COUNT=0
  fi
  DETAIL="${DETAIL}[rpm-ostree] ${RPMOSTREE_COUNT} update(s) available (atomic)
"
else
  DETAIL="${DETAIL}[rpm-ostree] not applicable (not an atomic distro)
"
fi

# ── no recognized update source at all → error (macOS/unknown must escalate,
#    not read as "up to date") ────────────────────────────────────────────────
if [ "$PM" = "unknown" ] && [ "$ATOMIC" = "0" ] \
   && ! command -v flatpak >/dev/null 2>&1 \
   && ! command -v snap >/dev/null 2>&1 \
   && ! command -v fwupdmgr >/dev/null 2>&1; then
  printf '{"success":false,"error":"no supported update source detected (apt/dnf/yum/apk/zypper/flatpak/snap/fwupd)"}'
  [ -n "$TARGET" ] && printf ',"action":"check_patches","target":"%s"' "$TARGET"
  printf '}\n'
  exit 1
fi

# ── aggregate + report ──────────────────────────────────────────────────────
TOTAL=$((OS_COUNT + FLATPAK_COUNT + FW_COUNT + SNAP_COUNT + RPMOSTREE_COUNT))
AVAIL="false"
[ "$TOTAL" -gt 0 ] && AVAIL="true"

PM_NAME="$PM"
[ "$ATOMIC" = "1" ] && PM_NAME="rpm-ostree"

DETAIL_B64=$(printf '%s' "$DETAIL" | base64 | tr -d '\n')

if [ "$DENIED" = "1" ]; then
  printf '{"success":false,"error":"sudo denied — a required update-check command is missing from the sudoers allowlist","package_manager":"%s","sources":{"%s":%d,"flatpak":%d,"firmware":%d,"snap":%d,"rpm_ostree":%d},"total":%d,"updates_available":%s,"updates_b64":"%s"' \
    "$PM_NAME" "$PM" "$OS_COUNT" "$FLATPAK_COUNT" "$FW_COUNT" "$SNAP_COUNT" "$RPMOSTREE_COUNT" "$TOTAL" "$AVAIL" "$DETAIL_B64"
  [ -n "$TARGET" ] && printf ',"action":"check_patches","target":"%s"' "$TARGET"
  printf '}\n'
  exit 1
fi

printf '{"success":true'
[ -n "$TARGET" ] && printf ',"action":"check_patches","target":"%s"' "$TARGET"
printf ',"package_manager":"%s","sources":{"%s":%d,"flatpak":%d,"firmware":%d,"snap":%d,"rpm_ostree":%d},"total":%d,"updates_available":%s,"updates_b64":"%s"}\n' \
  "$PM_NAME" "$PM" "$OS_COUNT" "$FLATPAK_COUNT" "$FW_COUNT" "$SNAP_COUNT" "$RPMOSTREE_COUNT" "$TOTAL" "$AVAIL" "$DETAIL_B64"
exit 0
CHECK_UPDATES_MULTI
chmod 0755 "$INSTALL_DIR/scripts/check_updates.sh"

cat > "$INSTALL_DIR/scripts/apply_updates.sh" <<'APPLY_UPDATES'
#!/bin/bash
# apply_updates.sh — multi-source APPLY (runs ON the endpoint).
#
# The gated counterpart to check_updates_multi.sh: it re-runs the read-only
# multi-source check for a FRESH per-source picture, then APPLIES each source
# the check reports non-zero — the SAME sources the check explores (OS package
# manager + flatpak + firmware + snap + rpm-ostree). Never applies from stale
# data, and never applies a source the check says is zero.
#
# Safety contract (the 08-21 gate):
#   • The script NEVER reboots. A kernel/OS update only surfaces the
#     `reboot_needed` flag — the operator/customer decides when to reboot.
#   • Apply is CUSTOMER-REQUESTED only: the agent catalog AND the appliance
#     both require params.confirm=true before this script can run (neither side
#     alone can widen the other). It never runs autonomous-unprompted.
#   • Least privilege: every escalated command uses `sudo -n` against the
#     scoped sudoers allowlist (the same dnf/apt/flatpak/fwupdmgr/snap/
#     rpm-ostree tools the check uses — the tool-level grant covers apply too).
#   • The firmware PATCH_ALLOWLIST (appliance-side firmware IDs) is a separate
#     gate; this script applies the ENDPOINT's own fwupd updates (LVFS) — no
#     overlap with the appliance's firmware-management lane.
#
# Emits ONE JSON object on stdout; exit 0 = apply succeeded (including the
# "nothing to apply" no-op), exit 1 = sudo denied / a source failed / no
# recognized update source:
#   {"success":true,"package_manager":"dnf","applied":{"dnf":2,"flatpak":0,
#    "firmware":0,"snap":0,"rpm_ostree":0},"total_applied":2,
#    "reboot_needed":false,"detail_b64":"<base64 of the apply log>"}
# On a partial failure the JSON also carries "failed":["<source>",…] and
# success:false. When invoked as `bash apply_updates.sh <target>` (the SSH
# relay path) the JSON also carries "target":"<target>".
#
# Run as root or as the scoped control user (barenoc / nocagent). The same
# sudoers allowlist that gates the check gates the apply — never ALL.

TARGET="${1:-}"

SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo -n"

DENIED=0
denied() {
  case "$1" in
    *"a password is required"*|*"not in the sudoers file"*|*"may not run sudo"*|*"is not allowed to run sudo"*|*"incorrect password"*)
      return 0 ;;
    *) return 1 ;;
  esac
}

# run_sudo CMD... — run [sudo -n] CMD... and echo its output; flag DENIED if
# sudo auth was refused (never swallowing it). Returns the command's exit code.
run_sudo() {
  local out rc
  out=$($SUDO "$@" 2>&1); rc=$?
  if [ $rc -ne 0 ] && denied "$out"; then
    DENIED=1
    return 1
  fi
  printf '%s\n' "$out"
  return $rc
}

# ── OS package manager detection (same rule as check_updates_multi.sh) ──────
PM="unknown"
if command -v apt-get >/dev/null 2>&1; then PM="apt"
elif command -v dnf >/dev/null 2>&1; then PM="dnf"
elif command -v yum >/dev/null 2>&1; then PM="yum"
elif command -v apk >/dev/null 2>&1; then PM="apk"
elif command -v zypper >/dev/null 2>&1; then PM="zypper"
fi

ATOMIC=0
command -v rpm-ostree >/dev/null 2>&1 && ATOMIC=1
PM_NAME="$PM"
[ "$ATOMIC" = "1" ] && PM_NAME="rpm-ostree"

# ── fresh multi-source check (the per-source JSON we apply) ─────────────────
# The check script is installed next to this one (agent_install.sh installs
# both root-owned). Re-run it so apply never works from a stale picture.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK="$SCRIPT_DIR/check_updates.sh"
CHECK_JSON=""
if [ -x "$CHECK" ]; then
  CHECK_JSON=$("$CHECK" 2>/dev/null || true)
fi

# sources object (the check emits {"sources":{"<pm>":N,"flatpak":N,…}}).
SRC=$(printf '%s\n' "$CHECK_JSON" | sed -n 's/.*"sources":{\([^}]*\)}.*/\1/p')

# get_count KEY — integer count for a source key (0 when absent/unparsable).
get_count() {
  local c
  c=$(printf '%s\n' "$SRC" | grep -o "\"$1\":[0-9][0-9]*" | head -1 | grep -o '[0-9][0-9]*')
  [ -n "$c" ] && echo "$c" || echo 0
}

OS_COUNT=$(get_count "$PM")
FLATPAK_COUNT=$(get_count "flatpak")
FW_COUNT=$(get_count "firmware")
SNAP_COUNT=$(get_count "snap")
RPMOSTREE_COUNT=$(get_count "rpm_ostree")

# Fallback when the check produced no readable counts (e.g. check script
# missing): apply the detected OS package manager directly (count unknown ⇒
# treat as 1 so the apply still happens). Never apply flatpak/snap/fwupd
# blind — those only run when the check said non-zero.
if [ -z "$SRC" ]; then
  if [ "$ATOMIC" = "1" ]; then
    RPMOSTREE_COUNT=1
  elif [ "$PM" != "unknown" ]; then
    OS_COUNT=1
  fi
fi

APPLIED_OS=0
APPLIED_FLATPAK=0
APPLIED_FW=0
APPLIED_SNAP=0
APPLIED_RPMOSTREE=0
FAILED=""
DETAIL=""

# apply_src NAME COUNT CMD... — run [sudo -n] CMD... when COUNT > 0; record the
# applied count on success, else the source name in FAILED. Returns 0 on
# success/no-op, 1 on failure.
apply_src() {
  local name="$1" count="$2"; shift 2
  [ "$count" -gt 0 ] || return 0
  local out rc
  out=$($SUDO "$@" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then
    if denied "$out"; then DENIED=1; fi
    FAILED="${FAILED}\"${name}\","
    DETAIL="${DETAIL}[${name}] apply FAILED (exit ${rc}): ${out}
"
    return 1
  fi
  DETAIL="${DETAIL}[${name}] applied (${count}): ${out}
"
  return 0
}

# ── apply each non-zero source ──────────────────────────────────────────────
DETAIL="${DETAIL}apply_updates on ${PM_NAME} endpoint
"

# OS package manager (skipped on atomic — updates flow through rpm-ostree).
if [ "$ATOMIC" = "0" ]; then
  case "$PM" in
    apt)
      if [ "$OS_COUNT" -gt 0 ]; then
        run_sudo apt-get update -qq >/dev/null 2>&1
        if apply_src apt "$OS_COUNT" apt-get -y upgrade; then APPLIED_OS=$OS_COUNT; fi
      fi
      ;;
    dnf)
      if [ "$OS_COUNT" -gt 0 ] && apply_src dnf "$OS_COUNT" dnf -y update; then APPLIED_OS=$OS_COUNT; fi
      ;;
    yum)
      if [ "$OS_COUNT" -gt 0 ] && apply_src yum "$OS_COUNT" yum -y update; then APPLIED_OS=$OS_COUNT; fi
      ;;
    apk)
      if [ "$OS_COUNT" -gt 0 ] && apply_src apk "$OS_COUNT" apk upgrade; then APPLIED_OS=$OS_COUNT; fi
      ;;
    zypper)
      if [ "$OS_COUNT" -gt 0 ] && apply_src zypper "$OS_COUNT" zypper --non-interactive update; then APPLIED_OS=$OS_COUNT; fi
      ;;
    *)
      DETAIL="${DETAIL}[OS] no supported package manager detected
"
      ;;
  esac
fi

# flatpak (user scope — the SAME scope the check lists; no sudo).
if [ "$FLATPAK_COUNT" -gt 0 ]; then
  OUT=$(flatpak update -y 2>&1); rc=$?
  if [ $rc -eq 0 ]; then
    APPLIED_FLATPAK=$FLATPAK_COUNT
    DETAIL="${DETAIL}[flatpak] applied (${FLATPAK_COUNT}): ${OUT}
"
  else
    FAILED="${FAILED}\"flatpak\","
    DETAIL="${DETAIL}[flatpak] apply FAILED (exit ${rc}): ${OUT}
"
  fi
fi

# firmware (fwupd — LVFS on the endpoint). Refresh first (best-effort), then
# update non-interactively.
if [ "$FW_COUNT" -gt 0 ]; then
  run_sudo fwupdmgr refresh --force >/dev/null 2>&1 || true
  if apply_src firmware "$FW_COUNT" fwupdmgr update --assume-yes; then APPLIED_FW=$FW_COUNT; fi
fi

# snap (snapd refreshes as root).
if [ "$SNAP_COUNT" -gt 0 ] && apply_src snap "$SNAP_COUNT" snap refresh; then APPLIED_SNAP=$SNAP_COUNT; fi

# rpm-ostree (atomic distros — the new deployment activates on the next boot).
if [ "$RPMOSTREE_COUNT" -gt 0 ] && apply_src rpm_ostree "$RPMOSTREE_COUNT" rpm-ostree upgrade --assumeyes; then
  APPLIED_RPMOSTREE=$RPMOSTREE_COUNT
fi

# ── reboot-needed flag (SURFACED ONLY — this script never reboots) ──────────
REBOOT_NEEDED="false"
# Atomic: the staged deployment activates on reboot.
[ "$APPLIED_RPMOSTREE" -gt 0 ] && REBOOT_NEEDED="true"
# Debian/Ubuntu: the package manager's own marker. Consult it ONLY when we
# actually applied an OS update this run — a stale host marker must not flip a
# zero-apply no-op to reboot_needed (hermeticity: test_apply_updates.sh's
# zero-sources case asserts reboot_needed=false even on a host carrying a real
# /var/run/reboot-required).
if [ "$APPLIED_OS" -gt 0 ] && [ -f /var/run/reboot-required ]; then
  REBOOT_NEEDED="true"
fi
# A kernel package in the pre-apply OS listing ⇒ a reboot is pending.
if [ "$APPLIED_OS" -gt 0 ]; then
  B64=$(printf '%s\n' "$CHECK_JSON" | sed -n 's/.*"updates_b64":"\([^"]*\)".*/\1/p')
  if [ -n "$B64" ] && printf '%s' "$B64" | base64 -d 2>/dev/null | grep -qi 'kernel'; then
    REBOOT_NEEDED="true"
  fi
fi

# ── no recognized update source at all → error (macOS/unknown must escalate) ─
if [ "$PM" = "unknown" ] && [ "$ATOMIC" = "0" ] \
   && ! command -v flatpak >/dev/null 2>&1 \
   && ! command -v snap >/dev/null 2>&1 \
   && ! command -v fwupdmgr >/dev/null 2>&1; then
  printf '{"success":false,"error":"no supported update source detected (apt/dnf/yum/apk/zypper/flatpak/snap/fwupd)"'
  [ -n "$TARGET" ] && printf ',"target":"%s"' "$TARGET"
  printf '}\n'
  exit 1
fi

TOTAL_APPLIED=$((APPLIED_OS + APPLIED_FLATPAK + APPLIED_FW + APPLIED_SNAP + APPLIED_RPMOSTREE))
APPLIED_JSON="\"$PM\":$APPLIED_OS,\"flatpak\":$APPLIED_FLATPAK,\"firmware\":$APPLIED_FW,\"snap\":$APPLIED_SNAP,\"rpm_ostree\":$APPLIED_RPMOSTREE"
DETAIL_B64=$(printf '%s' "$DETAIL" | base64 | tr -d '\n')

SUCCESS="true"
if [ "$DENIED" = "1" ]; then
  SUCCESS="false"
  printf '{"success":false,"error":"sudo denied — a required apply command is missing from the sudoers allowlist","package_manager":"%s","applied":{%s},"total_applied":%d,"reboot_needed":%s,"detail_b64":"%s"' \
    "$PM_NAME" "$APPLIED_JSON" "$TOTAL_APPLIED" "$REBOOT_NEEDED" "$DETAIL_B64"
  [ -n "$TARGET" ] && printf ',"target":"%s"' "$TARGET"
  printf '}\n'
  exit 1
fi

if [ -n "$FAILED" ]; then
  SUCCESS="false"
fi

printf '{"success":%s' "$SUCCESS"
[ -n "$TARGET" ] && printf ',"target":"%s"' "$TARGET"
printf ',"package_manager":"%s","applied":{%s},"total_applied":%d,"reboot_needed":%s' \
  "$PM_NAME" "$APPLIED_JSON" "$TOTAL_APPLIED" "$REBOOT_NEEDED"
if [ -n "$FAILED" ]; then
  printf ',"failed":[%s]' "${FAILED%,}"
fi
printf ',"detail_b64":"%s"}\n' "$DETAIL_B64"
[ "$SUCCESS" = "true" ] && exit 0 || exit 1
APPLY_UPDATES
chmod 0755 "$INSTALL_DIR/scripts/apply_updates.sh"

echo "==> Installing systemd unit ($UNIT)"
cat > "$UNIT" <<'UNIT'
[Unit]
Description=NOC_Agent — BareNOC endpoint agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nocagent
Group=nocagent
ExecStart=/opt/noc-agent/noc-agent -config /opt/noc-agent/config.json
Restart=on-failure
RestartSec=5
# NoNewPrivileges would block the capability-gated sudo actions
# (check_updates/apply_updates/collect_logs/reboot via the scoped sudoers
# allowlist) —
# found 08-17 on the first real agent job: 'the "no new privileges" flag is
# set, which prevents sudo from running as root'. The sudoers allowlist IS
# the control; keep the service unprivileged (User=nocagent) instead.
# NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/noc-agent
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Granting capability-gated sudoers (full paths only)"
cat > /etc/sudoers.d/noc-agent <<'SUDO'
nocagent ALL=(root) NOPASSWD: /usr/bin/systemctl status *, /usr/bin/tail *, /usr/bin/journalctl *, /sbin/reboot, /usr/bin/apt, /usr/bin/apt-get, /usr/bin/dnf, /usr/bin/yum, /usr/bin/apk, /usr/bin/zypper, /usr/bin/flatpak, /usr/bin/fwupdmgr, /usr/bin/snap, /usr/bin/rpm-ostree
SUDO
chmod 440 /etc/sudoers.d/noc-agent
visudo -cf /etc/sudoers.d/noc-agent >/dev/null || fail "sudoers syntax check failed"

echo "==> Fixing ownership/permissions (certs 0700, key 0600, owner $SERVICE_USER)"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
# The check + apply scripts stay root-owned: the agent runs them read-only,
# and it must not be able to rewrite the logic it then sudo-escalates through.
chown root:root "$INSTALL_DIR/scripts" "$INSTALL_DIR/scripts/check_updates.sh" "$INSTALL_DIR/scripts/apply_updates.sh"
chmod 0755 "$INSTALL_DIR/scripts/check_updates.sh" "$INSTALL_DIR/scripts/apply_updates.sh"

chmod 0700 "$CERTS"
chmod 0644 "$INSTALL_DIR/config.json"
chmod 0600 "$CERTS"/noc-agent.key "$CERTS"/noc-agent.crt 2>/dev/null || true
chmod 0600 "$CA_CERT_PATH" 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now noc-agent.service

echo
echo "✅ NOC_Agent installed and started."
echo "   device CN: $CN"
echo "   config:    $INSTALL_DIR/config.json"
case "$TRUST_RC" in
  0) echo "   browser trust: ✓ signing root anchored + verified ($APP shows secure)" ;;
  2) echo "   browser trust: declined/skipped — re-run with --trust-root to opt in" ;;
  *) echo "   browser trust: ✗ FAILED — $APP may still show 'Not Secure' (see above)" ;;
esac
echo "   The first report auto-claims the device with method=\"agent\" (no SSH)."
echo "   Watch it:  journalctl -u noc-agent.service -f"
