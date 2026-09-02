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
