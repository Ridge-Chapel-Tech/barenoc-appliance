#!/usr/bin/env bash
# test_check_updates_multi.sh — tests for the multi-source update check
# (src/scripts/check_updates_multi.sh):
#   • per-source aggregation with MOCKED source commands (apt / dnf / flatpak /
#     snap / fwupd / rpm-ostree) — no real package manager is touched;
#   • the stale-metadata refresh (apt-get update / dnf --refresh are invoked);
#   • a denied sudo surfaces as an error, never an empty success;
#   • the sudoers syntax (visudo -cf on the barenoc + nocagent lines);
#   • the agent_install.sh embed stays in sync with the canonical script.
#
# Runs on CI (ubuntu) and the VM host; needs bash + python3 (both present).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/src/scripts/check_updates_multi.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

BIN="$TMP/bin"
CORE="$TMP/core"
mkdir -p "$BIN" "$CORE"

# Core utils the script itself uses (awk/sed/grep/head/tail/tr/base64) come
# from a minimal CORE dir so the PATH under test never leaks a REAL dnf/snap/
# flatpak/fwupd from the host into the probe.
for c in grep awk sed head tail tr base64; do
  p=$(command -v "$c") || { echo "missing core util: $c" >&2; exit 1; }
  ln -sf "$p" "$CORE/$c"
done

# Mock `sudo` (drop -n, exec the rest) and `id` (always a non-root uid so the
# sudo path is exercised). MOCK_SUDO_DENY=1 simulates a denied sudo.
cat > "$BIN/sudo" <<'EOF'
#!/bin/bash
if [ "${MOCK_SUDO_DENY:-0}" = "1" ]; then
  echo "sudo: a password is required" >&2
  exit 1
fi
[ "$1" = "-n" ] && shift
exec "$@"
EOF
chmod +x "$BIN/sudo"

cat > "$BIN/id" <<'EOF'
#!/bin/bash
echo 1000
EOF
chmod +x "$BIN/id"

reset_bin() {
  rm -f "$BIN"/*
  cat > "$BIN/sudo" <<'EOF'
#!/bin/bash
if [ "${MOCK_SUDO_DENY:-0}" = "1" ]; then
  echo "sudo: a password is required" >&2
  exit 1
fi
[ "$1" = "-n" ] && shift
exec "$@"
EOF
  chmod +x "$BIN/sudo"
  cat > "$BIN/id" <<'EOF'
#!/bin/bash
echo 1000
EOF
  chmod +x "$BIN/id"
}

BASH_BIN="$(command -v bash)"
run_check() { PATH="$BIN:$CORE" "$BASH_BIN" "$SCRIPT" "$@"; }

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

# ── 1. apt aggregation + refresh ────────────────────────────────────────────
reset_bin
cat > "$BIN/apt-get" <<'EOF'
#!/bin/bash
echo "apt-get $*" >> "${APT_LOG:?}"
exit 0
EOF
cat > "$BIN/apt" <<'EOF'
#!/bin/bash
[ "$1" = "list" ] && printf 'Listing... Done\npkgA/stable 1.0 amd64 [upgradable from: 0.9]\npkgB/stable 2.0 amd64 [upgradable from: 1.9]\n'
exit 0
EOF
chmod +x "$BIN/apt-get" "$BIN/apt"
APT_LOG="$TMP/apt.log" run_check > "$TMP/apt.json"
check "apt: sources.apt == 2" python3 - "$TMP/apt.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is True, d
assert d["package_manager"]=="apt", d
assert d["sources"]["apt"]==2, d
assert d["sources"]["flatpak"]==0 and d["sources"]["firmware"]==0 and d["sources"]["snap"]==0, d
assert d["total"]==2 and d["updates_available"] is True, d
PY
check "apt: refresh (apt-get update) invoked" grep -q "apt-get update" "$TMP/apt.log"

# ── 2. dnf + flatpak + snap + firmware (no updates) ─────────────────────────
reset_bin
cat > "$BIN/dnf" <<'EOF'
#!/bin/bash
echo "dnf $*" >> "${DNF_LOG:?}"
printf 'firefox.x86_64 127.0.1-1.fc40 updates\nkernel.x86_64 6.10.0-1.fc40 updates\n'
exit 100
EOF
cat > "$BIN/flatpak" <<'EOF'
#!/bin/bash
[ "$1" = "remote-ls" ] && printf 'org.gimp.GIMP stable flathub\n'
exit 0
EOF
cat > "$BIN/snap" <<'EOF'
#!/bin/bash
[ "$1" = "refresh" ] && printf 'Name Version Rev Tracking Publisher Notes\nsnapd 2.62 12345 stable canonical core\n'
exit 0
EOF
cat > "$BIN/fwupdmgr" <<'EOF'
#!/bin/bash
[ "$1" = "get-updates" ] && printf 'No updates available\n'
exit 0
EOF
chmod +x "$BIN/dnf" "$BIN/flatpak" "$BIN/snap" "$BIN/fwupdmgr"
DNF_LOG="$TMP/dnf.log" run_check > "$TMP/dnf.json"
check "dnf: per-source counts {dnf:2,flatpak:1,snap:1,firmware:0}" python3 - "$TMP/dnf.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is True, d
assert d["package_manager"]=="dnf", d
s=d["sources"]
assert s["dnf"]==2 and s["flatpak"]==1 and s["snap"]==1 and s["firmware"]==0, s
assert d["total"]==4 and d["updates_available"] is True, d
PY
check "dnf: stale-metadata refresh (--refresh) invoked" grep -q -- "--refresh" "$TMP/dnf.log"

# ── 3. fwupd reports a pending update ───────────────────────────────────────
reset_bin
cat > "$BIN/fwupdmgr" <<'EOF'
#!/bin/bash
[ "$1" = "get-updates" ] && printf 'New version:      1.2.4\n'
exit 0
EOF
chmod +x "$BIN/fwupdmgr"
run_check > "$TMP/fw.json"
check "fwupd: firmware == 1 when a pending list is present" python3 - "$TMP/fw.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is True, d
assert d["sources"]["firmware"]==1, d["sources"]
assert d["sources"]["unknown"]==0, d["sources"]  # no package manager present here
assert d["total"]==1, d
PY

# ── 4. atomic (rpm-ostree) skips the dnf layer ──────────────────────────────
reset_bin
cat > "$BIN/dnf" <<'EOF'
#!/bin/bash
echo "dnf $*" >> "${DNF_LOG:?}"
printf 'layered.x86_64 1.0-1 fedora\n'
exit 100
EOF
cat > "$BIN/rpm-ostree" <<'EOF'
#!/bin/bash
[ "$1" = "upgrade" ] && printf 'AvailableUpdate:\n        Version: 41.20240101.0\n'
exit 0
EOF
chmod +x "$BIN/dnf" "$BIN/rpm-ostree"
DNF_LOG="$TMP/atomic-dnf.log" run_check > "$TMP/atomic.json"
check "atomic: rpm_ostree==1, dnf layer skipped, package_manager=rpm-ostree" python3 - "$TMP/atomic.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is True, d
assert d["package_manager"]=="rpm-ostree", d
assert d["sources"]["rpm_ostree"]==1, d["sources"]
assert d["sources"]["dnf"]==0, d["sources"]
assert d["total"]==1, d
PY
check "atomic: dnf check-update was NOT run" sh -c '! grep -q check-update "$TMP/atomic-dnf.log"'

# ── 5. denied sudo surfaces as an error ─────────────────────────────────────
reset_bin
cat > "$BIN/apt-get" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$BIN/apt" <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$BIN/apt-get" "$BIN/apt"
MOCK_SUDO_DENY=1 run_check > "$TMP/denied.json" || true
check "denied sudo: success=false + error + exit 1" python3 - "$TMP/denied.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d["success"] is False, d
assert "sudo" in (d.get("error") or "").lower(), d
PY

# ── 6. sudoers syntax (visudo -cf on the barenoc + nocagent lines) ──────────
VISUDO=""
for c in /usr/sbin/visudo visudo; do
  if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then VISUDO="$c"; break; fi
done
if [ -n "$VISUDO" ]; then
  NOCC=$(grep -o '^nocagent ALL=(root) NOPASSWD: .*' "$ROOT/agent-go/scripts/agent_install.sh" | head -1)
  BARENOC=$(sed -n "s/^LINE='\\(.*\\)'$/\\1/p" "$ROOT/src/scripts/fix-device-sudoers.sh")
  cat > "$TMP/sudoers.d-test" <<EOF
$NOCC
$BARENOC
EOF
  if "$VISUDO" -cf "$TMP/sudoers.d-test" >/dev/null 2>&1; then
    echo "ok  - sudoers syntax (visudo -cf)"
  else
    echo "FAIL - sudoers syntax (visudo -cf)" >&2
    "$VISUDO" -cf "$TMP/sudoers.d-test" >&2 || true
    fail=1
  fi
else
  echo "skip - visudo not available (no sudoers syntax check)"
fi

# ── 7. agent_install.sh embed matches the canonical script ──────────────────
check "agent_install.sh embeds the canonical check_updates_multi.sh" python3 - "$ROOT" <<'PY'
import sys, os
root = sys.argv[1]
canon = open(os.path.join(root, "src/scripts/check_updates_multi.sh")).read().rstrip("\n")
inst = open(os.path.join(root, "agent-go/scripts/agent_install.sh")).read()
start = inst.index("<<'CHECK_UPDATES_MULTI'\n") + len("<<'CHECK_UPDATES_MULTI'\n")
end = inst.index("\nCHECK_UPDATES_MULTI\n", start)
assert inst[start:end].rstrip("\n") == canon, "embedded script drifted from src/scripts/check_updates_multi.sh"
PY

echo
if [ "$fail" = "0" ]; then echo "all check_updates_multi tests passed"; else echo "TESTS FAILED"; exit 1; fi
