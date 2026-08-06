#!/bin/bash
# barenoc-update — VM-side update wrapper (MILESTONES M4 Layer 2).
#
# Layer map (see docs/operations/update_pipeline.md):
#   Layer 1 (Proxmox host APT)  -> host cron / manual on the Proxmox box
#   Layer 2 (this script)       -> Ubuntu OS + Docker images inside the VM
#   Layer 3 (app code)          -> ./deploy.sh from the dev box (git + rsync)
#
# What it does (Layer 2):
#   1. apt update + apt upgrade -y (Ubuntu OS packages)
#   2. docker compose pull + up -d (fresh app images from the registry)
#   3. docker image prune -f (old image garbage)
#   4. health check: GET https://127.0.0.1/api/v1/health must return 200
#   5. reports whether a reboot is required (NEVER reboots on its own)
#
# Usage:
#   sudo /opt/barenoc/scripts/barenoc-update.sh            # real run
#   sudo /opt/barenoc/scripts/barenoc-update.sh --dry-run  # preview only
#   sudo /opt/barenoc/scripts/barenoc-update.sh --no-apt   # images only
#
# Install as a weekly timer (optional):
#   sudo cp /opt/barenoc/scripts/barenoc-update.sh /usr/local/bin/barenoc-update
#   sudo tee /etc/systemd/system/barenoc-update.service >/dev/null <<'EOF'
#   [Unit]
#   Description=BareNOC VM update (Layer 2)
#   [Service]
#   Type=oneshot
#   ExecStart=/usr/local/bin/barenoc-update
#   EOF
#   sudo tee /etc/systemd/system/barenoc-update.timer >/dev/null <<'EOF'
#   [Unit]
#   Description=Weekly BareNOC update
#   [Timer]
#   OnCalendar=Sun 03:00
#   Persistent=true
#   [Install]
#   WantedBy=timers.target
#   EOF
#   sudo systemctl daemon-reload && sudo systemctl enable --now barenoc-update.timer
set -u

LOG="${BARENOC_UPDATE_LOG:-/var/log/barenoc-update.log}"
APP_DIR="${BARENOC_APP_DIR:-/opt/barenoc}"
DRY=0
APT=1

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --no-apt)  APT=0 ;;
    *) echo "unknown arg: $arg (use --dry-run / --no-apt)" >&2; exit 2 ;;
  esac
done

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
run() {
  log "> $*"
  if [ "$DRY" -eq 1 ]; then return 0; fi
  "$@"
}

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 1; }
[ -d "$APP_DIR" ] || { echo "APP_DIR $APP_DIR not found" >&2; exit 1; }

log "=== barenoc-update start (dry_run=$DRY apt=$APT) ==="

if [ "$APT" -eq 1 ]; then
  run apt update -qq
  run apt upgrade -y -qq
  run apt autoremove -y -qq
else
  log "apt steps skipped (--no-apt)"
fi

if [ -f "$APP_DIR/docker-compose.yml" ]; then
  run docker compose -f "$APP_DIR/docker-compose.yml" pull
  run docker compose -f "$APP_DIR/docker-compose.yml" up -d
  run docker image prune -f
else
  log "no docker-compose.yml — skipping compose steps"
fi

# Health check: the API must answer 200 within ~60s.
log "health check: https://127.0.0.1/api/v1/health"
ok=0
if [ "$DRY" -eq 0 ]; then
  for i in $(seq 1 30); do
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://127.0.0.1/api/v1/health 2>/dev/null || true)
    if [ "$code" = "200" ]; then ok=1; break; fi
    sleep 2
  done
  if [ "$ok" -eq 1 ]; then
    log "health: OK (200)"
  else
    log "health: FAILED (last code=$code) — roll back per docs/operations/update_pipeline.md"
    exit 1
  fi
else
  log "health: (dry-run, not checked)"
fi

if [ -f /var/run/reboot-required ]; then
  log "REBOOT REQUIRED (kernel/os packages updated) — schedule a maintenance window."
  log "   ssh barenoc@<vm> && sudo systemctl reboot   # pick the window"
else
  log "no reboot required."
fi

log "=== barenoc-update complete ==="
