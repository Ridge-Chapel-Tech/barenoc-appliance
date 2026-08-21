#!/usr/bin/env bash
# agent_install.sh — one-command Linux install of NOC_Agent (P1b).
#
# Installs the agent on a Linux endpoint, enrolls a device certificate with
# step-ca, writes the config, installs the systemd service, and grants the
# capability-gated sudoers set. The agent's FIRST report auto-claims the device
# with method="agent" — no SSH control involved.
#
# Usage: sudo bash agent_install.sh [--trust-root] <appliance_url> <enrollment_token> [noc-agent-binary]
#   --trust-root      OPT-IN: install the BareNOC root CA into the OS trust
#                     store (+ Firefox NSS, best-effort) so browsers stop
#                     showing "Not Secure" on the appliance. Default OFF.
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
  echo "  --trust-root  OPT-IN: also install the BareNOC root CA into the OS trust" >&2
  echo "                store (+ Firefox NSS, best-effort) so browsers stop showing" >&2
  echo "                'Not Secure' on the appliance. Default OFF." >&2
  exit 2
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

# Firefox trust — best-effort, non-fatal. Finds profiles via profiles.ini
# (standard ~/.mozilla, flatpak, and snap locations) and imports the root
# into each profile's NSS db with certutil.
trust_firefox() {
  local root="$1"
  # sudo resets HOME to /root on Linux — target the invoking user's home.
  local ff_home="$HOME"
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    local su_home
    su_home="$(getent passwd "$SUDO_USER" | cut -d: -f6 || true)"
    [[ -n "$su_home" ]] && ff_home="$su_home"
  fi
  if ! command -v certutil >/dev/null 2>&1; then
    echo "  !!! Firefox NOT covered: certutil missing (Debian/Ubuntu: apt-get install -y libnss3-tools)."
    echo "      Manual, per profile: certutil -A -n \"BareNOC Internal CA Root\" -t \"C,,\" -i ${root} -d sql:<profile-dir>"
    return 0
  fi
  local ini base found=0 imported=0 path rel d
  local candidates=(
    "$ff_home/.mozilla/firefox/profiles.ini"
    "$ff_home/.var/app/org.mozilla.firefox/.mozilla/firefox/profiles.ini"
    "$ff_home/snap/firefox/common/.mozilla/firefox/profiles.ini"
  )
  for ini in "${candidates[@]}"; do
    [[ -f "$ini" ]] || continue
    found=1
    base="$(dirname "$ini")"
    while IFS='|' read -r path rel; do
      [[ -n "$path" ]] || continue
      if [[ "$rel" == "0" ]]; then d="$path"; else d="$base/$path"; fi
      [[ -d "$d" ]] || continue
      if certutil -A -n "BareNOC Internal CA Root" -t "C,," -i "$root" -d "sql:$d" >/dev/null 2>&1; then
        echo "    Firefox: trusted in profile ${d}"
        imported=1
      else
        echo "    Firefox: import into ${d} failed (non-fatal)"
      fi
    done < <(awk '
      BEGIN { path=""; rel="1" }
      /^\[/ { if (path != "") print path "|" rel; path=""; rel="1"; next }
      /^Path=/ { path=substr($0,6); sub(/^[ \t]+/,"",path); sub(/[ \t\r]+$/,"",path) }
      /^IsRelative=/ { rel=substr($0,12); sub(/^[ \t]+/,"",rel); sub(/[ \t\r]+$/,"",rel) }
      END { if (path != "") print path "|" rel }
    ' "$ini")
  done
  if [[ $found -eq 0 ]]; then
    echo "  !!! Firefox NOT detected (no profiles.ini under ${ff_home}/.mozilla/firefox/)."
    echo "      Manual: certutil -A -n \"BareNOC Internal CA Root\" -t \"C,,\" -i ${root} -d sql:<profile-dir>"
  elif [[ $imported -eq 0 ]]; then
    echo "  !!! Firefox profiles found but none imported (flatpak/snap sandbox or locked db)."
    echo "      Manual: certutil -A -n \"BareNOC Internal CA Root\" -t \"C,,\" -i ${root} -d sql:<profile-dir>"
  fi
}

# Opt-in: install the BareNOC root CA into the OS trust store + Firefox.
trust_root_linux() {
  local root="$1" dst="/usr/local/share/ca-certificates/barenoc-root.crt"
  if [[ "$TRUST_ROOT" -ne 1 ]]; then
    if [[ -t 0 ]]; then
      echo
      echo "Optional: trust the BareNOC root CA so this machine's browsers show"
      echo "$APP as secure (no 'Not Secure' warning). This only affects"
      echo "certificates signed by the BareNOC CA — nothing else is trusted."
      printf "Trust the BareNOC root CA for this machine's browsers? [y/N] "
      read -r ans || true
      case "$ans" in
        y|Y|yes|YES|Yes) ;;
        *) echo "  (declined — root NOT added; re-run with --trust-root to opt in)"; return 0 ;;
      esac
    else
      echo "  (browser trust skipped — pass --trust-root to opt in non-interactively)"
      return 0
    fi
  fi

  echo "==> Trusting the BareNOC root CA (opt-in) — $APP will show as secure"
  echo "    Scope: only certificates signed by the BareNOC CA. Undo anytime with:"
  echo "      rm ${dst} && update-ca-certificates"

  install -d -m 0755 "$(dirname "$dst")"
  install -m 0644 "$root" "$dst"
  if command -v update-ca-certificates >/dev/null 2>&1; then
    update-ca-certificates >/dev/null 2>&1 || echo "  !!! update-ca-certificates failed (non-fatal)"
  else
    echo "  !!! update-ca-certificates not found — root copied to ${dst} but not activated"
  fi

  trust_firefox "$root"

  echo "  Verify (should print 'HTTP/1.1 200 OK' with no -k):"
  curl -sI "$APP" 2>/dev/null | head -1 || echo "  (could not reach $APP over HTTPS)"
}

# Binary: explicit path, else the built one next to the script.
if [[ -z "$SRC_BIN" ]]; then
  SRC_BIN="$(cd "$(dirname "$0")/.." && pwd)/noc-agent"
fi
[[ -f "$SRC_BIN" ]] || fail "binary not found at $SRC_BIN; build it first (cd agent-go && go build -o noc-agent ./cmd/noc-agent) and pass it as the 3rd arg"

# Stable, safe CN from the hostname (same rule as the /onboard flow).
HOST="$(hostname | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-._' | cut -c1-60)"
CN="device-${HOST:-node}"

echo "==> Fetching trust + appliance DNS mapping from $APP"
INFO="$(curl -sk "$APP/onboard/info")"
APP_IP="$(echo "$INFO" | grep -o '"appliance_ip": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
CA_FP="$(echo "$INFO" | grep -o '"ca_fingerprint": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
[ -n "$CA_FP" ] || fail "could not read ca_fingerprint from $APP/onboard/info"
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

trust_root_linux "$CA_CERT_PATH"

echo "==> Installing step-cli from the appliance"
curl -sk -o /tmp/step "$APP/step-cli"
install -m 0755 /tmp/step /usr/local/bin/step

echo "==> Bootstrapping the CA by fingerprint + enrolling $CN"
export STEPPATH=/root/.step
# --force: a stale /root/.step (e.g. from a previous CA) made bootstrap open an
# interactive overwrite prompt and hang forever (08-17, twice). Idempotent re-enroll.
step ca bootstrap --ca-url "$CA_URL" --fingerprint "$CA_FP" --force </dev/null >/dev/null 2>&1 \
  || fail "CA bootstrap failed (ca-url $CA_URL)"
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
# (check_updates/collect_logs/reboot via the scoped sudoers allowlist) —
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
# The check script stays root-owned: the agent runs it read-only, and it
# must not be able to rewrite the logic it then sudo-escalates through.
chown root:root "$INSTALL_DIR/scripts" "$INSTALL_DIR/scripts/check_updates.sh"
chmod 0755 "$INSTALL_DIR/scripts/check_updates.sh"

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
echo "   The first report auto-claims the device with method=\"agent\" (no SSH)."
echo "   Watch it:  journalctl -u noc-agent.service -f"
