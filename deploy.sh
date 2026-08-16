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
ssh "$VM" "sudo chown 1000:1000 /opt/barenoc/volumes/step-ca; [ -s /opt/barenoc/volumes/step-ca/password-in ] || { umask 077; sudo openssl rand -base64 24 | sudo tee /opt/barenoc/volumes/step-ca/password-in >/dev/null; sudo chown 1000:1000 /opt/barenoc/volumes/step-ca/password-in; }"

# step-ca: the container's entrypoint prompts for the ADMIN password (no TTY in
# a container) — run the FULL entrypoint once, feeding the prompts via stdin,
# so the CA initializes with secrets/password etc. and the real container just
# starts the server. Idempotent.
ssh "$VM" 'if [ ! -f /opt/barenoc/volumes/step-ca/config/ca.json ]; then
  ADMINPW=$(cat /opt/barenoc/volumes/step-ca/password-in)
  { printf "step\n%s\n" "$ADMINPW"; sleep 30; } | timeout 35 \
    docker run --rm -i -v /opt/barenoc/volumes/step-ca:/home/step \
      -e STEPPATH=/home/step \
      -e DOCKER_STEPCA_INIT_NAME="BareNOC Internal CA" \
      -e DOCKER_STEPCA_INIT_DNS_NAMES=stepca.barenoc.local \
      -e DOCKER_STEPCA_INIT_PROVISIONER_NAME=admin \
      -e DOCKER_STEPCA_INIT_ADDRESS=:443 \
      -e DOCKER_STEPCA_INIT_ACME=true \
      -e DOCKER_STEPCA_INIT_PASSWORD_FILE=/home/step/password-in \
      smallstep/step-ca:latest >/dev/null 2>&1 || true
  echo "step-ca CA initialized"
fi'

# barenoc-devices provisioner: the api signs device-enrollment tokens with this
# key. Generate our own keypair and register it (--private-key/--public-key).
ssh "$VM" 'if [ ! -f /opt/barenoc/volumes/step-ca/secrets/barenoc-devices.pem ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out /tmp/barenoc-devices.pem
  openssl ec -in /tmp/barenoc-devices.pem -pubout -out /tmp/barenoc-devices.pub 2>/dev/null
  docker run --rm -v /opt/barenoc/volumes/step-ca:/home/step \
    -v /tmp/barenoc-devices.pem:/tmp/bd.pem:ro -v /tmp/barenoc-devices.pub:/tmp/bd.pub:ro \
    -v /opt/barenoc/volumes/step-ca/password-in:/run/secrets/ca_password:ro \
    -e STEPPATH=/home/step smallstep/step-ca:latest step ca provisioner add \
      barenoc-devices --type=JWK --private-key /tmp/bd.pem --public-key /tmp/bd.pub \
      --ca-config /home/step/config/ca.json --password-file /run/secrets/ca_password \
    && cp /tmp/barenoc-devices.pem /opt/barenoc/volumes/step-ca/secrets/barenoc-devices.pem \
    && chown 1000:1000 /opt/barenoc/volumes/step-ca/secrets/barenoc-devices.pem \
    && echo "barenoc-devices provisioner created"
fi'

# nginx needs the CA root (device mTLS) + a proper SERVER cert for the stepca
# vhost. The step-ca intermediate CA cert has NO SANs (step ca init) — serving
# it directly makes TLS hostname verification fail for enrolling devices. Issue
# a leaf server cert (SANs: stepca.barenoc.local + appliance names) signed by
# the intermediate, and serve the chain (leaf + intermediate).
ssh "$VM" 'set -e
  ADMINPW=$(cat /opt/barenoc/volumes/step-ca/password-in)
  sudo mkdir -p /opt/barenoc/volumes/nginx/certs
  sudo cp /opt/barenoc/volumes/step-ca/certs/root_ca.crt /opt/barenoc/volumes/nginx/certs/ca-root.crt
  CERT=/opt/barenoc/volumes/nginx/certs/stepca-intermediate.crt
  if [ ! -s "$CERT" ] || ! openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -q stepca.barenoc.local; then
    IP="$(grep -E "^APPLIANCE_IP=" /opt/barenoc/.env | head -1 | cut -d= -f2-)"; IP="${IP:-127.0.0.1}"
    T=$(mktemp -d)
    sudo openssl ec -in /opt/barenoc/volumes/step-ca/secrets/intermediate_ca_key \
      -passin pass:"$ADMINPW" -out "$T/int.key" 2>/dev/null
    sudo openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
      -keyout "$T/server.key" -out "$T/server.csr" -subj "/CN=stepca.barenoc.local" 2>/dev/null
    printf "subjectAltName=DNS:stepca.barenoc.local,DNS:app.barenoc.com,DNS:bareNOC.local,IP:%s\n" "$IP" > "$T/sans.cnf"
    sudo openssl x509 -req -in "$T/server.csr" \
      -CA /opt/barenoc/volumes/step-ca/certs/intermediate_ca.crt \
      -CAkey "$T/int.key" -CAcreateserial -days 3650 -sha256 \
      -extfile "$T/sans.cnf" -out "$T/server.crt" 2>/dev/null
    cat "$T/server.crt" /opt/barenoc/volumes/step-ca/certs/intermediate_ca.crt | sudo tee "$CERT" >/dev/null
    sudo cp "$T/server.key" /opt/barenoc/volumes/nginx/certs/stepca-intermediate.key
    sudo chmod 0644 "$CERT" /opt/barenoc/volumes/nginx/certs/ca-root.crt
    sudo chmod 0640 /opt/barenoc/volumes/nginx/certs/stepca-intermediate.key
    sudo rm -rf "$T"
    echo "step-ca leaf server cert issued (SANs: stepca.barenoc.local + appliance)"
  else
    echo "stepca cert present with SANs — keeping"
  fi'

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
SHARED_MODULES=(action_validator.py audit.py crypto.py database.py models.py sanitizer.py schemas.py worknotes.py queue_status.py llm_providers.py emailer.py)

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

# Convergence: pocket-id crashes without ENCRYPTION_KEY (>=16 bytes) — add it
# to .env once if missing (Settings/other env keys untouched).
ssh "$VM" 'grep -qE "^ENCRYPTION_KEY=.+" /opt/barenoc/.env || \
  (echo "ENCRYPTION_KEY=$(openssl rand -hex 32)" >> /opt/barenoc/.env && echo "ENCRYPTION_KEY added") || true'

# Agent runner: /opt/barenoc/agent/ is owned by pi-agent, so only sync when changed
if ! diff -q "$SRC/agent/runner.py" <(ssh "$VM" "cat /opt/barenoc/agent/runner.py" 2>/dev/null) >/dev/null 2>&1; then
  echo "==> Agent runner.py changed; copying via temp (needs pi-agent access)"
  # runner identity convergence: docker-group traversal of /opt/barenoc + the
  # pi-agent-owned job queue and log (fresh installs get this via the provision;
  # existing installs converge here). pi-agent's docker group was REMOVED (see
  # setup_agent_credentials.sh — docker membership = self-harm vector).
  ssh "$VM" "sudo chown -R pi-agent:pi-agent /opt/barenoc/jobs /opt/barenoc/volumes/logs/agent 2>/dev/null; \
    sudo chown -R pi-agent:pi-agent /opt/barenoc/agent 2>/dev/null || true"
  scp -q "$SRC/agent/runner.py" "$VM:/tmp/runner.py"
  if ! ssh "$VM" "sudo -u pi-agent cp /tmp/runner.py /opt/barenoc/agent/runner.py && sudo systemctl restart pi-agent-runner" 2>/dev/null; then
    echo "!! Could not update agent runner (needs sudo). Deploy manually:"
    echo "   ssh $VM 'sudo -u pi-agent cp /tmp/runner.py /opt/barenoc/agent/runner.py'"
  fi
fi

for m in "${SHARED_MODULES[@]}"; do
  rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/api/$m" "$VM:/opt/barenoc/worker/$m"
done

# TLS cert for nginx — self-signed with the appliance SANs (app.barenoc.com +
# .local names + IP). Generated ONCE and BEFORE the first compose up: nginx
# crash-loops without it, which on fresh installs delayed the API for minutes
# ("API not healthy after 60s" + scary [emerg] cert errors in the bundle).
IP="${APPLIANCE_IP:-$(grep -E '^APPLIANCE_IP=' <(ssh "$VM" 'cat /opt/barenoc/.env') | head -1 | cut -d= -f2-)}"
ssh "$VM" "sudo mkdir -p /opt/barenoc/volumes/nginx/certs && \
  [ -s /opt/barenoc/volumes/nginx/certs/barenoc.crt ] || \
  sudo openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 \
    -keyout /opt/barenoc/volumes/nginx/certs/barenoc.key \
    -out /opt/barenoc/volumes/nginx/certs/barenoc.crt \
    -subj '/CN=app.barenoc.com' \
    -addext 'subjectAltName=DNS:app.barenoc.com,DNS:bareNOC.local,DNS:pocket-id.barenoc.local,DNS:stepca.barenoc.local,IP:${IP:-192.168.4.207}'"

echo "==> Rebuilding stack (docker compose up --build -d)"
ssh "$VM" "cd /opt/barenoc && docker compose up --build -d"

echo "==> Waiting for API to come up..."
API_UP=0
for i in $(seq 1 30); do
  if ssh "$VM" "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/health" | grep -q 200; then
    API_UP=1
    break
  fi
  sleep 2
done
if [ "$API_UP" != "1" ]; then
  echo "!! WARNING: API not healthy after 60s — continuing, but the agent-credentials" >&2
  echo "   step below will abort loudly if it can't reach the api (fresh-install bug class)." >&2
fi

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
ssh "$VM" 'sudo mkdir -p /opt/barenoc/volumes/static && sudo chown barenoc:docker /opt/barenoc/volumes/static && for p in "step:step_linux_amd64" "step-cli-darwin_amd64:step_darwin_amd64" "step-cli-darwin_arm64:step_darwin_arm64"; do out="${p%%:*}"; src="${p##*:}"; [ -s "/opt/barenoc/volumes/static/$out" ] && continue; (cd /tmp && curl -sL "https://dl.smallstep.com/gh-release/cli/gh-release-header/v0.30.2/${src}.tar.gz" -o s.tgz && tar xzf s.tgz && find "${src}" -name step -type f -exec cp {} "/opt/barenoc/volumes/static/$out" \; && chmod 755 "/opt/barenoc/volumes/static/$out" && rm -rf s.tgz "${src}" && echo "fetched $out") || true; done'

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
