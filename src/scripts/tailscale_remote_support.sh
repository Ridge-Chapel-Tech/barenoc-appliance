#!/usr/bin/env bash
# BareNOC remote support — Tailscale zero-touch onboarding + customer toggle.
#
# This is the HOST-side reconciler (runs on the VM as root, NOT in a
# container). The API writes the customer's desired state to
#   /opt/barenoc/volumes/remote_access/remote_support.desired   {"enabled": bool}
# and a systemd timer (installed by provision_agent.sh) runs `reconcile`
# every minute to apply tailscale up/down and refresh self.json.
#
# Subcommands:
#   ensure-secret   write the 0600 tailscale.json if missing (empty auth key)
#   provision       apt install tailscale + one-time tagged join (idempotent,
#                   graceful — never blocks the deploy)
#   reconcile       apply desired state (up/down) + refresh status files
#   status          print the current remote-support state JSON
#
# Secrets live in /opt/barenoc/volumes/secrets/tailscale.json (0600):
#   {"auth_key": "<SUPPORT_AUTH_KEY>", "tailnet": "", "tags": "tag:appliance",
#    "hostname_prefix": "bareNOC"}
# The auth key is the tagged, expiring, revocable key that joins the vendor
# support tailnet. It is a SECRET — fill it in on the VM (or at release) and
# rotate it; never commit a live key. An empty key = zero-touch join disabled
# (the appliance stays off the support tailnet until one is set).

set -u

SECRET="/opt/barenoc/volumes/secrets/tailscale.json"
DESIRED="/opt/barenoc/volumes/remote_access/remote_support.desired"
STATE="/opt/barenoc/volumes/remote_access/remote_support.json"
TS="/usr/bin/tailscale"
ENV="/opt/barenoc/.env"

log() { echo "remote-support: $*" >&2; }

read_json_key() {
  # read_json_key <file> <key> [default]
  python3 - "$1" "$2" "${3:-}" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    d = {}
v = d.get(sys.argv[2], sys.argv[3])
print("" if v is None else str(v))
PY
}

appliance_id() {
  # 1. explicit operator override in the secret file
  local explicit
  explicit="$(read_json_key "$SECRET" appliance_id "")"
  if [ -n "$explicit" ]; then
    printf '%s' "$explicit"
    return
  fi
  # 2. SITE_ID when customized away from the shared default (1)
  local sid
  sid="$(grep -E '^SITE_ID=' "$ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')"
  if [ -n "$sid" ] && [ "$sid" != "1" ]; then
    printf '%s' "$sid"
    return
  fi
  # 3. stable per-VM id — unique across appliances on the same support tailnet
  if [ -r /etc/machine-id ]; then
    tr -d '[:space:]' < /etc/machine-id | cut -c1-12
    return
  fi
  # 4. last resort
  local aip
  aip="$(grep -E '^APPLIANCE_IP=' "$ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')"
  printf '%s' "${aip:-$(hostname 2>/dev/null || echo appliance)}"
}

node_hostname() {
  local prefix id
  prefix="$(read_json_key "$SECRET" hostname_prefix "bareNOC")"
  id="$(appliance_id)"
  printf '%s-%s' "$prefix" "$id"
}

ensure_secret() {
  mkdir -p "$(dirname "$SECRET")"
  if [ ! -f "$SECRET" ]; then
    cat > "$SECRET" <<'JSON'
{"auth_key":"","tailnet":"","tags":"tag:appliance","hostname_prefix":"bareNOC","appliance_id":""}
JSON
    chmod 600 "$SECRET"
    chown root:root "$SECRET"
    log "wrote $SECRET (0600; set auth_key to enable the zero-touch join)"
  fi
}

install_tailscale() {
  if [ -x "$TS" ]; then
    return 0
  fi
  if command -v apt-get >/dev/null 2>&1; then
    log "installing tailscale (official installer — configures the apt repo)"
    # The official installer configures the Tailscale apt repo + keyring and
    # installs the package. `apt-get install tailscale` alone FAILS on existing
    # boxes (the repo is not pre-configured) — found live 08-20. Fall back to a
    # plain apt install if the installer script can't run (e.g. offline).
    if command -v curl >/dev/null 2>&1; then
      if curl -fsSL https://tailscale.com/install.sh -o /tmp/tailscale-install.sh 2>/dev/null \
         && bash /tmp/tailscale-install.sh >/dev/null 2>&1; then
        rm -f /tmp/tailscale-install.sh
        log "tailscale installed (official installer)"
        return 0
      fi
      rm -f /tmp/tailscale-install.sh
      log "official installer failed — falling back to apt"
    fi
    apt-get update -qq >/dev/null 2>&1 || true
    if apt-get install -y tailscale >/dev/null 2>&1; then
      log "tailscale installed (apt)"
      return 0
    fi
    log "tailscale install FAILED — continuing without remote support"
    return 1
  fi
  log "no apt-get available — tailscale not installed"
  return 1
}

tailscale_running() {
  [ -x "$TS" ] || return 1
  "$TS" status --json >/tmp/ts-rs.json 2>/dev/null || return 1
  python3 -c 'import json,sys; d=json.load(open("/tmp/ts-rs.json")); sys.exit(0 if d.get("BackendState")=="Running" else 1)' 2>/dev/null
}

# The node is only "already joined" when it is ONLINE and carries the tag — a
# running-but-broken state (e.g. a failed earlier join, or an up without the
# tag) must NOT skip the join (found 08-20 live: a half-connected node left
# the timer skipping re-joins forever).
tailscale_healthy() {
  [ -x "$TS" ] || return 1
  local tags
  tags="$(read_json_key "$SECRET" tags "tag:appliance")"
  "$TS" status --json >/tmp/ts-rs.json 2>/dev/null || return 1
  python3 -c 'import json,sys
s=json.load(open("/tmp/ts-rs.json")).get("Self",{})
sys.exit(0 if (s.get("Online") and "'"$tags"'" in (s.get("Tags") or [])) else 1)' 2>/dev/null
}

join() {
  local key tags host
  key="$(read_json_key "$SECRET" auth_key "")"
  tags="$(read_json_key "$SECRET" tags "tag:appliance")"
  host="$(node_hostname)"
  if [ -z "$key" ] || [ "$key" = "CHANGE_ME" ]; then
    log "no auth key configured — zero-touch join disabled"
    return 2
  fi
  if tailscale_running && tailscale_healthy; then
    log "tailscale already running + joined — skip (idempotent)"
    return 0
  fi
  # any up-but-unhealthy state (or a plain failed join): tear down cleanly first
  "$TS" down >/dev/null 2>&1 || true
  log "joining support tailnet as $host"
  "$TS" up --auth-key="$key" --hostname="$host" --advertise-tags="$tags" >/dev/null 2>&1 || {
    log "tailscale up FAILED"
    return 1
  }
  log "joined as $host"
  return 0
}

write_state() {
  local enabled="$1" applied="$2" err="${3:-}" host="${4:-}" ip="${5:-}"
  python3 - "$STATE" "$enabled" "$applied" "$err" "$host" "$ip" <<'PY'
import json, sys
path, enabled, applied, err, host, ip = sys.argv[1:7]
json.dump({
    "enabled": enabled in ("true", "True", "1"),
    "applied": applied in ("true", "True", "1"),
    "hostname": host,
    "tailscale_ip": ip or None,
    "error": err or None,
}, open(path, "w"), indent=1)
PY
  chmod 644 "$STATE" 2>/dev/null || true
}


# ── Support SSH access (08-26): the vendor's support key rides the SAME
# consent as the tailnet — present ONLY while remote support is enabled.
# The public key ships at /opt/barenoc/scripts/support-ssh.pub (private key
# is gate-held, never shipped). We add/remove exactly our marked lines in
# the barenoc user's authorized_keys — never touching their own keys.
SUPPORT_KEY_FILE="${SUPPORT_KEY_FILE:-/opt/barenoc/scripts/support-ssh.pub}"
SUPPORT_SSH_DIR="${SUPPORT_SSH_DIR:-/home/barenoc/.ssh}"
SUPPORT_AUTHKEYS="${SUPPORT_AUTHKEYS:-/home/barenoc/.ssh/authorized_keys}"
SUPPORT_KEY_MARK="# bareNOC support key (remote-support reconciler)"

manage_support_key() {
  local mode="${1:-status}" key
  [ -f "$SUPPORT_KEY_FILE" ] || { log "support key file missing — skipping SSH key management"; return 0; }
  key="$(head -1 "$SUPPORT_KEY_FILE")"
  mkdir -p "$SUPPORT_SSH_DIR"
  chown barenoc:barenoc "$SUPPORT_SSH_DIR" 2>/dev/null || true
  chmod 700 "$SUPPORT_SSH_DIR" 2>/dev/null || true
  touch "$SUPPORT_AUTHKEYS"
  chown barenoc:barenoc "$SUPPORT_AUTHKEYS" 2>/dev/null || true
  chmod 600 "$SUPPORT_AUTHKEYS" 2>/dev/null || true

  # strip any existing managed block (ours only)
  grep -v -F "$SUPPORT_KEY_MARK" "$SUPPORT_AUTHKEYS" > "$SUPPORT_AUTHKEYS.tmp" 2>/dev/null || true
  grep -v -F "$key" "$SUPPORT_AUTHKEYS.tmp" > "$SUPPORT_AUTHKEYS.tmp2" 2>/dev/null || true
  mv "$SUPPORT_AUTHKEYS.tmp2" "$SUPPORT_AUTHKEYS"
  chown barenoc:barenoc "$SUPPORT_AUTHKEYS" 2>/dev/null || true
  chmod 600 "$SUPPORT_AUTHKEYS" 2>/dev/null || true

  if [ "$mode" = "enable" ]; then
    printf '%s\n%s\n' "$SUPPORT_KEY_MARK" "$key" >> "$SUPPORT_AUTHKEYS"
    log "support SSH key enabled (remote support on)"
  else
    log "support SSH key removed (remote support off)"
  fi
}

reconcile() {
  mkdir -p "$(dirname "$STATE")" "$(dirname "$DESIRED")"
  local raw enabled err host ip online
  raw="$(read_json_key "$DESIRED" enabled false)"
  case "$raw" in
    true|True|1) enabled=true ;;
    *) enabled=false ;;
  esac

  if ! install_tailscale; then
    write_state "$enabled" false "tailscale not installed"
    return 0
  fi

  err=""
  if [ "$enabled" = "true" ]; then
    join
    local rc=$?
    if [ "$rc" -eq 2 ]; then
      err="No support key set — paste the key from your provider, then save."
    elif [ "$rc" -ne 0 ]; then
      err="Invalid key — check with your provider."
    else
      manage_support_key enable
    fi
  else
    "$TS" down >/dev/null 2>&1 || true
    manage_support_key disable
  fi

  # Refresh self.json (node identity + tailnet status) for the API.
  bash /opt/barenoc/scripts/tailscale_status.sh >/dev/null 2>&1 || true

  host="$(node_hostname)"
  ip=""
  online=false
  if tailscale_running; then
    online=true
    ip="$(python3 -c 'import json;d=json.load(open("/tmp/ts-rs.json"));print((d.get("Self",{}).get("TailscaleIPs") or [None])[0] or "")' 2>/dev/null || true)"
  fi
  write_state "$enabled" "$online" "$err" "$host" "$ip"
}

status() {
  if [ -f "$STATE" ]; then
    cat "$STATE"
  else
    echo '{"enabled": false, "applied": false, "hostname": null, "tailscale_ip": null, "error": "no state yet"}'
  fi
}

case "${1:-status}" in
  ensure-secret) ensure_secret ;;
  provision)
    ensure_secret
    install_tailscale && join || true
    reconcile
    ;;
  reconcile) reconcile ;;
  status) status ;;
  *)
    echo "usage: $0 {ensure-secret|provision|reconcile|status}" >&2
    exit 2
    ;;
esac
