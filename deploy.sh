#!/usr/bin/env bash
# BareNOC deploy — sync local src/ to VM and rebuild the Docker stack.
# Usage: ./deploy.sh <user>@<host>   (e.g. ./deploy.sh barenoc@<appliance-ip>)
set -euo pipefail

VM="${1:-}"
if [ -z "$VM" ]; then
  echo "Usage: ./deploy.sh <user>@<host>" >&2
  exit 1
fi
ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/src"

echo "==> Deploying to $VM:/opt/barenoc"

# Ensure runtime directories exist (docker auto-creates mounts, but own them)
ssh "$VM" "mkdir -p /opt/barenoc/volumes/branding /opt/barenoc/volumes/backup_status /opt/barenoc/volumes/update_status /opt/barenoc/volumes/pocket-id/data /opt/barenoc/volumes/step-ca /opt/barenoc/volumes/dns"

# step-ca: bootstrap the CA password file (uid 1000 = the step container user).
# The container reads it on first boot to init the CA, then keeps its own copy.
ssh "$VM" "chown 1000:1000 /opt/barenoc/volumes/step-ca; [ -s /opt/barenoc/volumes/step-ca/password-in ] || { umask 077; openssl rand -base64 24 | tr '+/' '_-' > /opt/barenoc/volumes/step-ca/password-in; chown 1000:1000 /opt/barenoc/volumes/step-ca/password-in; }"

# Security: .env holds all API keys/secrets — never world-readable
ssh "$VM" "chmod 600 /opt/barenoc/.env"

# Pocket ID is served at root on 8443 (its SPA needs root-absolute paths).
# Best-effort: needs sudo on first setup; idempotent afterwards.
ssh "$VM" "sudo ufw allow 8443/tcp 2>/dev/null || true"

# DNS service (split-horizon CoreDNS): allow the LAN to reach it
ssh "$VM" "sudo ufw allow 53/udp 2>/dev/null; sudo ufw allow 53/tcp 2>/dev/null; true"

# Backup: let the backup cron read the DB dir (root-owned; root still writes fine).
# Run via the api container (root) since the barenoc user has no sudo.
ssh "$VM" 'docker ps -q -f name=^barenoc-api$ | grep -q . && docker exec barenoc-api chown -R "$(id -u barenoc):$(id -g barenoc)" /opt/barenoc/volumes/db || true'

# Backup: install app-data backup cron (every 6h, idempotent)
ssh "$VM" "crontab -l 2>/dev/null | grep -q backup_app.sh || (crontab -l 2>/dev/null; echo '0 */6 * * * /opt/barenoc/scripts/backup_app.sh >> /opt/barenoc/backups/backup.log 2>&1') | crontab -"

# Shared modules the worker image needs in its build context (see worker/Dockerfile).
# They live in api/ in the repo; copy into the worker context on the VM.
SHARED_MODULES=(action_validator.py audit.py crypto.py database.py models.py sanitizer.py schemas.py worknotes.py llm_providers.py emailer.py)

# Sync each service directory (no --delete: VM may have runtime-only files).
rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/api/"            "$VM:/opt/barenoc/api/"
rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/worker/"         "$VM:/opt/barenoc/worker/"
rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/scheduler/"      "$VM:/opt/barenoc/scheduler/"
rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/nginx/"          "$VM:/opt/barenoc/nginx/"
rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/scripts/"        "$VM:/opt/barenoc/scripts/"
rsync -rltz --no-o --no-g --exclude=__pycache__ "$ROOT/client/"       "$VM:/opt/barenoc/client/"
rsync -rltz --no-o --no-g "$SRC/docker-compose.yml" "$VM:/opt/barenoc/docker-compose.yml"

# Self-update (L3): install the host-side apply service + the .path watcher
# that fires it when the API writes an update/rollback request.
scp -q "$SRC/scripts/barenoc-self-update.sh" "$VM:/tmp/barenoc-self-update.sh"
ssh "$VM" "sudo install -m 0755 /tmp/barenoc-self-update.sh /usr/local/bin/ && \
  sudo install -m 0644 /opt/barenoc/scripts/barenoc-self-update.service /etc/systemd/system/ && \
  sudo install -m 0644 /opt/barenoc/scripts/barenoc-self-update.path /etc/systemd/system/ && \
  sudo systemctl daemon-reload && sudo systemctl enable --now barenoc-self-update.path" 2>/dev/null \
  && echo "self-update units installed" || echo "!! self-update units not installed (manual: see deploy log)"

# Agent runner: /opt/barenoc/agent/ is owned by pi-agent, so only sync when changed
if ! diff -q "$SRC/agent/runner.py" <(ssh "$VM" "cat /opt/barenoc/agent/runner.py" 2>/dev/null) >/dev/null 2>&1; then
  echo "==> Agent runner.py changed; copying via temp (needs pi-agent access)"
  scp -q "$SRC/agent/runner.py" "$VM:/tmp/runner.py"
  if ! ssh "$VM" "sudo -u pi-agent cp /tmp/runner.py /opt/barenoc/agent/runner.py && sudo systemctl restart pi-agent-runner" 2>/dev/null; then
    echo "!! Could not update agent runner (needs sudo). Deploy manually:"
    echo "   ssh $VM 'sudo -u pi-agent cp /tmp/runner.py /opt/barenoc/agent/runner.py'"
  fi
fi

for m in "${SHARED_MODULES[@]}"; do
  rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/api/$m" "$VM:/opt/barenoc/worker/$m"
done

echo "==> Rebuilding stack (docker compose up --build -d)"
ssh "$VM" "cd /opt/barenoc && docker compose up --build -d"

echo "==> Waiting for API to come up..."
for i in $(seq 1 30); do
  if ssh "$VM" "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health" | grep -q 200; then
    break
  fi
  sleep 2
done

# nginx config is a bind-mounted file — restart nginx to pick up changes (e.g. Pocket ID route)
ssh "$VM" "docker exec barenoc-nginx nginx -t 2>/dev/null && docker restart barenoc-nginx >/dev/null 2>&1 && echo 'nginx reloaded' || echo 'nginx config check skipped'"

# DNS service: render the split-horizon Corefile from the appliance identity
# settings (APPLIANCE_IP / APPLIANCE_HOST in the VM .env) + restart CoreDNS.
ssh "$VM" 'bash -s' <<'RENDER'
set -u
ENV=/opt/barenoc/.env
get() { grep -E "^$1=" "$ENV" | head -1 | cut -d= -f2-; }
IP="${APPLIANCE_IP:-$(get APPLIANCE_IP)}"; IP="${IP:-192.168.4.207}"
HOST="${APPLIANCE_HOST:-$(get APPLIANCE_HOST)}"; HOST="${HOST:-app.barenoc.com}"
mkdir -p /opt/barenoc/volumes/dns
cat > /opt/barenoc/volumes/dns/Corefile <<CORE
.:53 {
  hosts {
    $IP $HOST bareNOC.local pocket-id.barenoc.local stepca.barenoc.local
    fallthrough
  }
  forward . 1.1.1.1 8.8.8.8
  cache 30
  log
}
CORE
echo "dns Corefile rendered (IP=$IP host=$HOST)"
docker restart barenoc-dns >/dev/null 2>&1 && echo "dns restarted" || echo "dns container not up yet"
RENDER

# step-cli builds for self-service onboarding (Linux + macOS; fetched once)
ssh "$VM" 'mkdir -p /opt/barenoc/volumes/static && for p in "step:step_linux_amd64" "step-cli-darwin_amd64:step_darwin_amd64" "step-cli-darwin_arm64:step_darwin_arm64"; do out="${p%%:*}"; src="${p##*:}"; [ -s "/opt/barenoc/volumes/static/$out" ] && continue; (cd /tmp && curl -sL "https://dl.smallstep.com/gh-release/cli/gh-release-header/v0.30.2/${src}.tar.gz" -o s.tgz && tar xzf s.tgz && find "${src}" -name step -type f -exec cp {} "/opt/barenoc/volumes/static/$out" \; && chmod 755 "/opt/barenoc/volumes/static/$out" && rm -rf s.tgz "${src}" && echo "fetched $out") || true; done'

# Agent service credentials: create/rotate the `agent` service account + 0600
# credential file (needs the api container up). Idempotent.
echo "==> Provisioning agent service credentials"
ssh "$VM" "sudo bash /opt/barenoc/scripts/setup_agent_credentials.sh"

# Restart the agent runner so it picks up any runner.py changes (pi-agent-owned dir)
ssh "$VM" "sudo systemctl restart pi-agent-runner 2>/dev/null && echo 'agent runner restarted' || echo 'agent runner restart skipped (needs sudo)'"

echo "==> Container status:"
ssh "$VM" "cd /opt/barenoc && docker compose ps"
echo "==> Health:"
ssh "$VM" "curl -sk https://127.0.0.1/api/v1/health"
echo
echo "==> Done."
