#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC — post-update verification suite (runs after EVERY self-update).
#
#   sudo bash /opt/barenoc/scripts/verify_post_update.sh [--dry-run]
#
# Idempotent + safe: a healthy entitled box sees NO action beyond the checks.
#
#   1. Entitlement — is this box authorized for remote support? Reads the beta
#      `support_grant` 0600 secret (the report_gate `support` pattern). An
#      ENTITLED box proceeds to the tailscale guarantee; an UNENTITLED box
#      reports the state and SKIPS the tailscale requirement (not a failure).
#   2. Tailscale guarantee (entitled only) — if tailscale is not installed /
#      not online / not joined (the healthy() check: Online + tag present),
#      FIX it: install (repo-correct), seed the 0600 secret, join the support
#      tailnet, verify. The remote-support reconciler already heals minor
#      drift; this is the update-time guarantee.
#   3. Write the result JSON the API + scheduler read for the auto-report hook.
#
# Exits non-zero ONLY on a real failure (entitled + tailscale could not be
# healed). The auto-report knob (AUTO_REPORT_POST_UPDATE) is enforced by the
# API's /updates/auto-report endpoint — this script only reports state.
#
# --dry-run: run the checks read-only (no install/join) and report what would
# happen. Never mutates anything.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

BASE="${VERIFY_BASE:-/opt/barenoc}"
SECRETS="$BASE/volumes/secrets"
STATUS_DIR="$BASE/volumes/update_status"
RESULT="$STATUS_DIR/verify_post_update.json"
REMOTE_SCRIPT="$BASE/scripts/tailscale_remote_support.sh"
TS="/usr/bin/tailscale"
DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

mkdir -p "$STATUS_DIR" 2>/dev/null || true
say() { echo "verify-post-update: $*"; }

# ── entitlement (the support_grant beta gate pattern) ──────────────────────
# Mirrors report_gate.support_grant_active, PLUS the placeholder guard: the
# provision step ships a CHANGE-ME sentinel grant which is NOT a real
# entitlement (a real grant is rotated in before/at GA).
is_entitled() {
  python3 - "$SECRETS/support_grant.json" <<'PY'
import json, sys
from datetime import datetime, timezone
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
grant = ((d or {}).get("grant") or "").strip()
if not grant or "CHANGE-ME" in grant.upper():
    sys.exit(1)
exp = ((d or {}).get("expires_at") or "").strip()
if exp:
    try:
        when = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= when:
            sys.exit(1)
    except ValueError:
        pass
sys.exit(0)
PY
}

if ! is_entitled; then
  say "not entitled for remote support — reporting state + skipping tailscale"
  python3 - "$RESULT" "$DRY_RUN" <<'PY'
import json, sys
from datetime import datetime, timezone
json.dump({
    "ran_at": datetime.now(timezone.utc).isoformat(),
    "entitled": False,
    "ok": True,
    "dry_run": sys.argv[2] == "true",
    "tailscale": {
        "required": False, "installed": False, "online": False,
        "joined": False, "action": "skipped",
        "error": "remote support not required (no active support_grant)",
    },
}, open(sys.argv[1], "w"), indent=1)
PY
  echo "==> post-update verification: OK (remote support not required on this box)"
  exit 0
fi

say "entitled for remote support — running the tailscale guarantee"

# ── self-heal (install -> seed key -> join -> verify) ──────────────────────
# `provision` is idempotent + graceful: ensure-secret + repo-correct install +
# tagged join + reconcile. In --dry-run we skip it and only probe read-only.
if [ "$DRY_RUN" = "true" ]; then
  say "--dry-run: skipping install/join (would run: $REMOTE_SCRIPT provision)"
else
  bash "$REMOTE_SCRIPT" provision >/dev/null 2>&1 || true
fi

# ── verify + write result ──────────────────────────────────────────────────
python3 - "$RESULT" "$SECRETS/tailscale.json" "$DRY_RUN" "$TS" <<'PY'
import json, os, subprocess, sys
from datetime import datetime, timezone

result_path = sys.argv[1]
secret_path = sys.argv[2]
dry_run = sys.argv[3] == "true"
ts = sys.argv[4]

def read_key(path, key, default=""):
    try:
        d = json.load(open(path))
    except Exception:
        d = {}
    v = d.get(key, default)
    return "" if v is None else str(v)

tags = read_key(secret_path, "tags", "tag:appliance")
state = {
    "required": True,
    "installed": False,
    "online": False,
    "joined": False,
    "action": "failed",
    "error": None,
}

if os.path.isfile(ts) and os.access(ts, os.X_OK):
    state["installed"] = True
    try:
        out = subprocess.check_output([ts, "status", "--json"], timeout=20).decode()
        d = json.loads(out)
        self_ = d.get("Self", {}) or {}
        state["online"] = bool(self_.get("Online"))
        state["joined"] = bool(tags in (self_.get("Tags") or []))
    except Exception as e:
        state["error"] = "tailscale status unavailable: %s" % e
else:
    state["error"] = "tailscale not installed"

if state["installed"] and state["online"] and state["joined"]:
    state["action"] = "ok"
    state["error"] = None
elif not state["error"]:
    if not state["online"]:
        state["error"] = "tailscale backend not online"
    else:
        state["error"] = "tailscale node not joined/tagged (missing %s)" % tags

ok = state["action"] == "ok"
result = {
    "ran_at": datetime.now(timezone.utc).isoformat(),
    "entitled": True,
    "ok": ok,
    "dry_run": dry_run,
    "tailscale": state,
}
json.dump(result, open(result_path, "w"), indent=1)

if dry_run:
    print("verify-post-update: DRY-RUN tailscale %s" % ("healthy" if ok else ("would heal (current: %s)" % state["error"])))
    sys.exit(0)
print("verify-post-update: tailscale %s" % ("healthy (online + tagged)" if ok else ("FAILED: %s" % state["error"])))
sys.exit(0 if ok else 1)
PY
