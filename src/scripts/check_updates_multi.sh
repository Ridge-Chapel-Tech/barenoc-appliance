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
