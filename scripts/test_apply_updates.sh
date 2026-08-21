#!/usr/bin/env bash
# test_apply_updates.sh — tests for the multi-source APPLY
# (src/scripts/apply_updates.sh):
#   • re-runs the check and applies each NON-ZERO source (mocked dnf / apt /
#     flatpak / fwupd / snap / rpm-ostree) — never applies a zero source;
#   • the per-source applied counts + reboot_needed (kernel in the listing);
#   • flatpak applies at USER scope (no sudo) while dnf/fwupd/snap/rpm-ostree
#     escalate via `sudo -n`;
#   • a denied sudo surfaces as success:false + error (never an empty success);
#   • the agent_install.sh embed stays in sync with the canonical script.
#
# Runs on CI (ubuntu) and the VM host; needs bash + python3 (both present).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/src/scripts/apply_updates.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

BIN="$TMP/bin"
CORE="$TMP/core"
mkdir -p "$BIN" "$CORE"

# Core utils the script itself uses (awk/sed/grep/head/tail/tr/base64/dirname)
# come from a minimal CORE dir so the PATH under test never leaks a REAL
# dnf/snap/flatpak/fwupd from the host into the probe.
for c in grep awk sed head tail tr base64 dirname; do
  p=$(command -v "$c") || { echo "missing core util: $c" >&2; exit 1; }
  ln -sf "$p" "$CORE/$c"
done

# Mock `sudo` (drop -n, exec the rest) and `id` (always a non-root uid so the
# sudo path is exercised). MOCK_SUDO_DENY=1 simulates a denied sudo.
reset_bin() {
  rm -f "$BIN"/*
  cat > "$BIN/sudo" <<'EOF'
#!/bin/bash
if [ "${MOCK_SUDO_DENY:-0}" = "1" ]; then
  echo "sudo: a password is required" >&2
  exit 1
fi
[ "$1" = "-n" ] && shift
echo "sudo $*" >> "${SUDO_LOG:?}"
exec "$@"
EOF
  chmod +x "$BIN/sudo"
  cat > "$BIN/id" <<'EOF'
#!/bin/bash
echo 1000
EOF
  chmod +x "$BIN/id"
}

# write_check JSON — the mock sibling check_updates.sh apply_updates.sh re-runs.
write_check() {
  cat > "$TMP/check_updates.sh" <<EOF
#!/bin/bash
printf '%s\n' '$1'
EOF
  chmod +x "$TMP/check_updates.sh"
}

BASH_BIN="$(command -v bash)"
run_apply() { PATH="$BIN:$CORE" "$BASH_BIN" "$TMP/apply_updates.sh" "$@"; }

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

# apply_updates.sh must resolve its sibling check via SCRIPT_DIR, so run a copy
# of the canonical script from $TMP (next to the mock check).
cp "$SCRIPT" "$TMP/apply_updates.sh"
chmod +x "$TMP/apply_updates.sh"

# ── 1. dnf apply: 2 OS + 1 flatpak, kernel ⇒ reboot_needed ───────────────────
reset_bin
export SUDO_LOG="$TMP/sudo.log"; : > "$SUDO_LOG"
cat > "$BIN/dnf" <<'EOF'
#!/bin/bash
echo "dnf $*" >> "${DNF_LOG:?}"
exit 0
EOF
cat > "$BIN/flatpak" <<'EOF'
#!/bin/bash
echo "flatpak $*" >> "${FLATPAK_LOG:?}"
exit 0
EOF
chmod +x "$BIN/dnf" "$BIN/flatpak"
export DNF_LOG="$TMP/dnf.log"; : > "$DNF_LOG"
export FLATPAK_LOG="$TMP/flatpak.log"; : > "$FLATPAK_LOG"
write_check '{"success":true,"package_manager":"dnf","sources":{"dnf":2,"flatpak":1,"firmware":0,"snap":0,"rpm_ostree":0},"total":3,"updates_available":true,"updates_b64":"W2RuZl0gMiBwYWNrYWdlIHVwZGF0ZShzKSBhdmFpbGFibGUKICBrZXJuZWwueDg2XzY0IDYuMTAuMC0xLmZjNDAgdXBkYXRlcwo="}'
run_apply > "$TMP/dnf-apply.json"
check "dnf: applied {dnf:2,flatpak:1}, reboot_needed=true" python3 - "$TMP/dnf-apply.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is True, d
assert d["package_manager"]=="dnf", d
a=d["applied"]
assert a["dnf"]==2 and a["flatpak"]==1, a
assert a["firmware"]==0 and a["snap"]==0 and a["rpm_ostree"]==0, a
assert d["total_applied"]==3, d
assert d["reboot_needed"] is True, d
assert "failed" not in d, d
PY
check "dnf: sudo dnf -y update invoked" grep -q "sudo dnf -y update" "$TMP/sudo.log"
check "flatpak: USER scope (no sudo)" sh -c '! grep -q "sudo flatpak" "$TMP/sudo.log"'
check "flatpak: update -y invoked" grep -q "flatpak update -y" "$TMP/flatpak.log"

# ── 2. zero sources ⇒ nothing to apply (no commands run) ────────────────────
reset_bin
export SUDO_LOG="$TMP/sudo2.log"; : > "$SUDO_LOG"
cat > "$BIN/dnf" <<'EOF'
#!/bin/bash
echo "dnf $*" >> "${DNF_LOG:?}"
exit 0
EOF
chmod +x "$BIN/dnf"
export DNF_LOG="$TMP/dnf2.log"; : > "$DNF_LOG"
write_check '{"success":true,"package_manager":"dnf","sources":{"dnf":0,"flatpak":0,"firmware":0,"snap":0,"rpm_ostree":0},"total":0,"updates_available":false,"updates_b64":""}'
run_apply > "$TMP/zero.json"
check "zero sources: success + total_applied=0 + reboot_needed=false" python3 - "$TMP/zero.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is True, d
assert d["total_applied"]==0, d
assert d["reboot_needed"] is False, d
PY
check "zero sources: dnf NOT invoked" sh -c '! grep -q "dnf" "$TMP/dnf2.log"'

# ── 3. fwupd + snap apply (sudo) ───────────────────────────────────────────
reset_bin
export SUDO_LOG="$TMP/sudo3.log"; : > "$SUDO_LOG"
cat > "$BIN/dnf" <<'EOF'
#!/bin/bash
echo "dnf $*" >> "${DNF_LOG:?}"
exit 0
EOF
cat > "$BIN/fwupdmgr" <<'EOF'
#!/bin/bash
echo "fwupdmgr $*" >> "${FW_LOG:?}"
exit 0
EOF
cat > "$BIN/snap" <<'EOF'
#!/bin/bash
echo "snap $*" >> "${SNAP_LOG:?}"
exit 0
EOF
chmod +x "$BIN/dnf" "$BIN/fwupdmgr" "$BIN/snap"
export DNF_LOG="$TMP/dnf3.log"; : > "$DNF_LOG"
export FW_LOG="$TMP/fw3.log"; : > "$FW_LOG"
export SNAP_LOG="$TMP/snap3.log"; : > "$SNAP_LOG"
write_check '{"success":true,"package_manager":"dnf","sources":{"dnf":0,"flatpak":0,"firmware":1,"snap":1,"rpm_ostree":0},"total":2,"updates_available":true,"updates_b64":"W2Zpcm13YXJlXSAxIHVwZGF0ZShzKQo="}'
run_apply > "$TMP/fwsnap.json"
check "fwupd+snap: applied {firmware:1,snap:1}" python3 - "$TMP/fwsnap.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is True, d
a=d["applied"]
assert a["firmware"]==1 and a["snap"]==1 and a["dnf"]==0, a
PY
check "fwupd: sudo fwupdmgr update --assume-yes invoked" grep -q "sudo fwupdmgr update --assume-yes" "$TMP/sudo3.log"
check "snap: sudo snap refresh invoked" grep -q "sudo snap refresh" "$TMP/sudo3.log"

# ── 4. a source failure ⇒ success:false + failed list ───────────────────────
reset_bin
export SUDO_LOG="$TMP/sudo4.log"; : > "$SUDO_LOG"
cat > "$BIN/dnf" <<'EOF'
#!/bin/bash
echo "dnf $*" >> "${DNF_LOG:?}"
exit 0
EOF
cat > "$BIN/snap" <<'EOF'
#!/bin/bash
echo "snap failed" >&2
exit 1
EOF
chmod +x "$BIN/dnf" "$BIN/snap"
export DNF_LOG="$TMP/dnf4.log"; : > "$DNF_LOG"
write_check '{"success":true,"package_manager":"dnf","sources":{"dnf":1,"flatpak":0,"firmware":0,"snap":1,"rpm_ostree":0},"total":2,"updates_available":true,"updates_b64":""}'
run_apply > "$TMP/fail.json" || true
check "partial failure: success=false + failed=[snap]" python3 - "$TMP/fail.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is False, d
assert d["failed"]==["snap"], d
assert d["applied"]["dnf"]==1, d
PY

# ── 5. denied sudo ⇒ success:false + sudo error (never an empty success) ─────
reset_bin
export SUDO_LOG="$TMP/sudo5.log"; : > "$SUDO_LOG"
cat > "$BIN/dnf" <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$BIN/dnf"
write_check '{"success":true,"package_manager":"dnf","sources":{"dnf":2,"flatpak":0,"firmware":0,"snap":0,"rpm_ostree":0},"total":2,"updates_available":true,"updates_b64":""}'
MOCK_SUDO_DENY=1 run_apply > "$TMP/denied.json" || true
check "denied sudo: success=false + sudo in error" python3 - "$TMP/denied.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is False, d
assert "sudo" in (d.get("error") or "").lower(), d
PY

# ── 6. no supported source ⇒ error (macOS/unknown escalates) ─────────────────
reset_bin
write_check '{"success":false,"error":"no supported update source detected"}'
run_apply > "$TMP/nosrc.json" || true
check "no source: success=false + no-supported-source error" python3 - "$TMP/nosrc.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is False, d
assert "no supported update source" in (d.get("error") or ""), d
PY

# ── 7. agent_install.sh embed matches the canonical script ──────────────────
check "agent_install.sh embeds the canonical apply_updates.sh" python3 - "$ROOT" <<'PY'
import sys, os
root = sys.argv[1]
canon = open(os.path.join(root, "src/scripts/apply_updates.sh")).read().rstrip("\n")
inst = open(os.path.join(root, "agent-go/scripts/agent_install.sh")).read()
start = inst.index("<<'APPLY_UPDATES'\n") + len("<<'APPLY_UPDATES'\n")
end = inst.index("\nAPPLY_UPDATES\n", start)
assert inst[start:end].rstrip("\n") == canon, "embedded apply_updates.sh drifted from src/scripts/apply_updates.sh"
PY

echo
if [ "$fail" = "0" ]; then echo "all apply_updates tests passed"; else echo "TESTS FAILED"; exit 1; fi
