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

# Offsite/remote backup (Layer 4): hourly cron that self-gates on the offsite
# schedule + mode the Settings UI writes (cheap — 23/24 runs exit immediately).
# The wrapper dispatches into the api container (Python + cryptography live there).
ssh "$VM" "crontab -l 2>/dev/null | grep -q offsite_backup.sh || (crontab -l 2>/dev/null; echo '15 * * * * /opt/barenoc/scripts/offsite_backup.sh >> /opt/barenoc/backups/offsite.log 2>&1') | crontab -"

# Shared modules the worker image needs in its build context (see worker/Dockerfile).
# They live in api/ in the repo; copy into the worker context on the VM.
SHARED_MODULES=(action_validator.py audit.py audit_catalog.py crypto.py database.py models.py sanitizer.py schemas.py worknotes.py queue_status.py tone_pool.py llm_providers.py emailer.py)

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

# Agent runner + systemd unit: /opt/barenoc/agent/ is pi-agent-owned
# (deliberately NOT the docker group — see provision_agent.sh), so sync via
# temp + sudo install. The unit file here is the single source of truth for
# pi-agent-runner.service; the shared provision step installs it from here.
scp -q "$SRC/agent/runner.py" "$SRC/agent/pi-agent-runner.service" "$VM:/tmp/"
ssh "$VM" "sudo mkdir -p /opt/barenoc/agent /opt/barenoc/volumes/logs/agent && \
  sudo chown -R pi-agent:pi-agent /opt/barenoc/agent /opt/barenoc/volumes/logs/agent /opt/barenoc/jobs /opt/barenoc/pi-work 2>/dev/null || true; \
  sudo install -o pi-agent -g pi-agent -m 0644 /tmp/runner.py /opt/barenoc/agent/runner.py && \
  sudo install -o pi-agent -g pi-agent -m 0644 /tmp/pi-agent-runner.service /opt/barenoc/agent/pi-agent-runner.service && \
  rm -f /tmp/runner.py /tmp/pi-agent-runner.service"

for m in "${SHARED_MODULES[@]}"; do
  rsync -rltz --no-o --no-g --exclude=__pycache__ "$SRC/api/$m" "$VM:/opt/barenoc/worker/$m"
done

# TLS cert for the MAIN nginx vhost — issued from the BareNOC Internal CA as a
# leaf + intermediate chain (same direct-openssl issuance as the stepca vhost
# leaf above), so a browser that trusts the /onboard-distributed root CA also
# trusts the web UI (was self-signed → "Not Secure" on https://<appliance>).
# SANs: app.barenoc.com + bareNOC.local + pocket-id.barenoc.local + appliance IP.
# Generated BEFORE the first compose up (nginx crash-loops without a cert).
# Idempotent: regenerate only when missing / still self-signed / expired /
# missing the appliance-IP SAN (IP changed in .env).
# Gate verify:
#   openssl s_client -connect <appliance>:443 </dev/null 2>/dev/null | \
#     openssl x509 -noout -issuer -subject -ext subjectAltName   # issuer = BareNOC Intermediate CA
#   openssl verify -CAfile /opt/barenoc/volumes/step-ca/certs/root_ca.crt \
#     -untrusted /opt/barenoc/volumes/step-ca/certs/intermediate_ca.crt \
#     /opt/barenoc/volumes/nginx/certs/barenoc.crt                # → barenoc.crt: OK
ssh "$VM" 'set -e
  ADMINPW=$(cat /opt/barenoc/volumes/step-ca/password-in)
  sudo mkdir -p /opt/barenoc/volumes/nginx/certs
  CERT=/opt/barenoc/volumes/nginx/certs/barenoc.crt
  KEY=/opt/barenoc/volumes/nginx/certs/barenoc.key
  IP="$(grep -E "^APPLIANCE_IP=" /opt/barenoc/.env | head -1 | cut -d= -f2-)"; IP="${IP:-192.0.2.207}"
  ISSUER_HASH="$(openssl x509 -in "$CERT" -noout -issuer_hash 2>/dev/null || true)"
  SUBJECT_HASH="$(openssl x509 -in "$CERT" -noout -subject_hash 2>/dev/null || true)"
  if [ ! -s "$CERT" ] || [ ! -s "$KEY" ] || [ "$ISSUER_HASH" = "$SUBJECT_HASH" ] || \
     ! openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -qF "$IP" || \
     ! openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null 2>&1; then
    T=$(mktemp -d)
    sudo openssl ec -in /opt/barenoc/volumes/step-ca/secrets/intermediate_ca_key \
      -passin pass:"$ADMINPW" -out "$T/int.key" 2>/dev/null
    sudo openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
      -keyout "$T/server.key" -out "$T/server.csr" -subj "/CN=app.barenoc.com" 2>/dev/null
    printf "subjectAltName=DNS:app.barenoc.com,DNS:bareNOC.local,DNS:pocket-id.barenoc.local,IP:%s\n" "$IP" > "$T/sans.cnf"
    sudo openssl x509 -req -in "$T/server.csr" \
      -CA /opt/barenoc/volumes/step-ca/certs/intermediate_ca.crt \
      -CAkey "$T/int.key" -CAcreateserial -days 3650 -sha256 \
      -extfile "$T/sans.cnf" -out "$T/server.crt" 2>/dev/null
    cat "$T/server.crt" /opt/barenoc/volumes/step-ca/certs/intermediate_ca.crt | sudo tee "$CERT" >/dev/null
    sudo cp "$T/server.key" "$KEY"
    sudo chmod 0644 "$CERT"
    sudo chmod 0640 "$KEY"
    sudo rm -rf "$T"
    echo "main vhost cert issued from the BareNOC CA (SANs: app.barenoc.com + appliance IP)"
  else
    echo "main vhost CA-signed cert present (appliance-IP SAN) — keeping"
  fi'

# Health-order guard: bring up everything EXCEPT the scheduler first, so the
# shared agent provision step can create the agent credentials (which needs the
# api container) BEFORE the scheduler starts. The scheduler is started below,
# after provisioning — otherwise it 401-floods on a fresh install (08-14/08-18).
echo "==> Rebuilding stack (all but scheduler — scheduler starts after agent provisioning)"
ssh "$VM" "cd /opt/barenoc && docker compose up --build -d api worker nginx step-ca pocket-id dns"

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
  echo "!! WARNING: API not healthy after 60s — continuing, but the shared agent" >&2
  echo "   provision step below will abort loudly if it can't reach the api (fresh-install bug class)." >&2
fi

# nginx config is a bind-mounted file — restart nginx to pick up changes (e.g. Pocket ID route)
ssh "$VM" "docker exec barenoc-nginx nginx -t 2>/dev/null && docker restart barenoc-nginx >/dev/null 2>&1 && echo 'nginx reloaded' || echo 'nginx config check skipped'"

# DNS service: render the split-horizon Corefile from the appliance identity
# settings (APPLIANCE_IP / APPLIANCE_HOST in the VM .env) + restart CoreDNS.
ssh "$VM" 'bash -s' <<'RENDER'
set -u
ENV=/opt/barenoc/.env
get() { grep -E "^$1=" "$ENV" | head -1 | cut -d= -f2-; }
IP="${APPLIANCE_IP:-$(get APPLIANCE_IP)}"; IP="${IP:-192.0.2.207}"
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

# Shared agent provisioning — the single step every install path converges on
# (appliance installer, ISO first-boot, and this deploy). Does: dirs, runner
# unit install + enable + start, agent credentials (api-healthy-before-creds +
# file↔DB login-200 agreement, loud failure). Idempotent.
echo "==> Provisioning agent (credentials + runner unit + runner)"
ssh "$VM" "sudo bash /opt/barenoc/scripts/provision_agent.sh"

# Scheduler LAST — it must come up with credentials already in place.
# --force-recreate guarantees a fresh container (and a fresh log) so the
# post-install scheduler-log check below can't read stale pre-deploy errors.
echo "==> Starting scheduler (after agent provisioning)"
ssh "$VM" "cd /opt/barenoc && docker compose up --build -d --force-recreate scheduler"

# Post-install verification — scheduler logs too, not just health 200 (08-09).
echo "==> Verifying agent provisioning (login / runner / scheduler logs)"
ssh "$VM" "sudo bash /opt/barenoc/scripts/verify_agent_provision.sh" \
  || echo "!! post-install verification FAILED — see checklist above" >&2

echo "==> Container status:"
ssh "$VM" "cd /opt/barenoc && docker compose ps"
echo "==> Health:"
ssh "$VM" "curl -sk https://127.0.0.1/api/v1/health"
echo
echo "==> Done."
