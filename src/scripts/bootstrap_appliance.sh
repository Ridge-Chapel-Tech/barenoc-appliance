#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC — appliance self-sufficiency bootstrap (pre-compose, idempotent).
#
#   sudo bash /opt/barenoc/scripts/bootstrap_appliance.sh     (run ON the VM)
#
# The ISO first-boot unit runs this BEFORE `docker compose up`; the shared
# agent provision step (provision_agent.sh) re-asserts it on every deploy. It
# is the single place that makes a fresh install turnkey — no manual steps:
#
#   1. Ownership — the /opt/barenoc source tree becomes barenoc-owned
#      (deploy.sh's rsync + first `mkdir -p` run AS barenoc and fail on a
#      root-owned tree; the 08-20 round-3 gap).
#   2. Passwordless sudo for barenoc (the deploy's sudo steps need it).
#   3. .env bootstrap — template + JWT/admin/encryption/appliance identity
#      (docker compose + step-ca cert issuance read it; the ISO had NO .env —
#      neither first-boot nor deploy.sh creates it, only the appliance
#      installer did).
#   4. step-ca: password-in → CA init (ca.json) → barenoc-devices provisioner,
#      BEFORE anything reads them. (The deploy's CA-init silently aborted with
#      `|| true` when password-in was missing → "there is no ca.json config
#      file" → the step-ca provisioner step failed.)
#   5. nginx certs (main + stepca vhost) from the CA — nginx crash-loops on a
#      fresh install without a cert.
#   6. CoreDNS Corefile + step-cli static fetch (self-service onboarding).
#
# Idempotent: safe to run at any point; no-ops once each artifact exists.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "!! bootstrap_appliance.sh must run as root (use sudo)" >&2
  exit 1
fi

B="/opt/barenoc"
ENV="$B/.env"
CA_VOL="$B/volumes/step-ca"

detect_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1
}
env_get() {
  grep -E "^$1=" "$ENV" 2>/dev/null | head -1 | cut -d= -f2-
}

# ── 1. ownership ───────────────────────────────────────────────────────────
echo "==> bootstrap: ownership"
# Source tree (deploy.sh rsync targets) — must be barenoc-owned so a later
# deploy can overwrite files without sudo (rsync --no-o --no-g is content-only
# but still needs write permission on the destination).
for d in api worker scheduler nginx scripts client; do
  [ -d "$B/$d" ] && chown -R barenoc:barenoc "$B/$d" 2>/dev/null || true
done
chown barenoc:barenoc "$B/deploy.sh" "$B/docker-compose.yml" 2>/dev/null || true
[ -f "$B/.env.example" ] && chown barenoc:barenoc "$B/.env.example" 2>/dev/null || true

# Runtime dirs (existence first; ownership second — install -d -o aborts when
# the target group doesn't exist, so mkdir then chown).
mkdir -p \
  "$B/volumes/db" "$B/volumes/logs/api" "$B/volumes/logs/worker" \
  "$B/volumes/logs/scheduler" "$B/volumes/logs/agent" "$B/volumes/secrets/ssh" \
  "$B/volumes/nginx/certs" "$B/volumes/branding" "$B/volumes/backup_status" \
  "$B/volumes/update_status" "$B/volumes/pocket-id/data" "$CA_VOL" \
  "$B/volumes/static" "$B/volumes/dns" "$B/backups" \
  "$B/jobs/incoming" "$B/jobs/running" "$B/jobs/completed" "$B/pi-work" "$B/agent"
chown -R barenoc:docker \
  "$B/volumes/db" "$B/volumes/logs" "$B/volumes/secrets" "$B/volumes/nginx" \
  "$B/volumes/branding" "$B/volumes/backup_status" "$B/volumes/update_status" \
  "$B/volumes/pocket-id" "$B/volumes/static" "$B/volumes/dns" "$B/backups" 2>/dev/null || true
# install/mkdir only chowns the FINAL dir — fix the intermediates (08-07 lesson #7)
chown barenoc:docker "$B/volumes" "$B/volumes/logs" "$B/volumes/secrets" \
  "$B/volumes/nginx" "$B/volumes/pocket-id" "$B/jobs" 2>/dev/null || true
# step-ca volume is owned by uid 1000 (the container's user), NOT barenoc
chown 1000:1000 "$CA_VOL" 2>/dev/null || true
# pi-agent writable trees (runner + job queue + its log)
chown -R pi-agent:pi-agent "$B/jobs" "$B/volumes/logs/agent" "$B/pi-work" "$B/agent" 2>/dev/null || true

# worker build context: the worker image COPYs the shared modules that live in
# api/ (deploy.sh copies them into worker/ before compose up — the ISO tarball
# ships them side-by-side, so first-boot must do the same or the worker build
# fails with '"/emailer.py": not found').
echo "==> bootstrap: worker shared modules"
# ⚠️ KEEP THIS LIST IN SYNC with deploy.sh SHARED_MODULES + barenoc-self-update.sh —
# adding a module to api/ requires updating ALL THREE (the .30.b self-update bug).
for m in action_validator.py audit.py audit_catalog.py crypto.py database.py models.py \
         sanitizer.py schemas.py worknotes.py queue_status.py tone_pool.py \
         llm_providers.py emailer.py ratewindows.py tierrouter.py; do
  [ -f "$B/api/$m" ] && cp -f "$B/api/$m" "$B/worker/$m"
done
chown -R barenoc:barenoc "$B/worker" 2>/dev/null || true

# ── 2. passwordless sudo for barenoc ───────────────────────────────────────
echo "==> bootstrap: barenoc passwordless sudo"
if [[ ! -f /etc/sudoers.d/barenoc ]] || ! grep -q 'NOPASSWD:ALL' /etc/sudoers.d/barenoc; then
  ( umask 022; printf 'barenoc ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/barenoc )
  chmod 440 /etc/sudoers.d/barenoc
  if ! visudo -c >/dev/null 2>&1; then
    rm -f /etc/sudoers.d/barenoc
    echo "!! sudoers write failed visudo validation" >&2
    exit 1
  fi
  echo "bootstrap: sudoers written"
fi

# ── 3. .env bootstrap ──────────────────────────────────────────────────────
echo "==> bootstrap: .env"
if [[ ! -s "$ENV" ]]; then
  if [[ ! -f "$B/.env.example" ]]; then
    echo "!! /opt/barenoc/.env.example missing — cannot bootstrap .env" >&2
    exit 1
  fi
  cp "$B/.env.example" "$ENV"
  chmod 600 "$ENV"
  JWT="$(openssl rand -hex 32)"
  ENC="$(openssl rand -hex 32)"
  ADMIN_PW=""
  [ -f "$B/.admin-seed" ] && ADMIN_PW="$(cat "$B/.admin-seed")"
  ADMIN_PW="${ADMIN_PW:-$(openssl rand -base64 12 | tr '+/' '_-')}"
  IP="$(detect_ip)"; IP="${IP:-192.0.2.207}"
  GW="$(ip route | awk '/default/{print $3; exit}')"
  GW="${GW:-$(echo "$IP" | awk -F. '{print $1"."$2"."$3".1"}')}"
  SUBNET="$(echo "$IP" | awk -F. '{print $1"."$2"."$3".0/24"}')"
  sed -i \
    "s|^JWT_SECRET=.*|JWT_SECRET=${JWT}|;
     s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PW}|;
     s|^# ENCRYPTION_KEY=.*|ENCRYPTION_KEY=${ENC}|;
     s|^# APPLIANCE_IP=.*|APPLIANCE_IP=${IP}|;
     s|^# APPLIANCE_HOST=.*|APPLIANCE_HOST=app.barenoc.com|;
     s|^# INTERNET_PROBE_GATEWAY=.*|INTERNET_PROBE_GATEWAY=${GW}|;
     s|^# DISCOVERY_SUBNETS=.*|DISCOVERY_SUBNETS=${SUBNET}|;" "$ENV"
  echo "bootstrap: .env created (appliance IP=$IP)"
  rm -f "$B/.admin-seed"
fi
# Pocket ID requires ENCRYPTION_KEY (>=16 bytes) even on a pre-existing .env.
grep -qE '^ENCRYPTION_KEY=.+' "$ENV" || { echo "ENCRYPTION_KEY=$(openssl rand -hex 32)" >> "$ENV"; echo "bootstrap: ENCRYPTION_KEY added"; }
chmod 600 "$ENV"
chown barenoc:barenoc "$ENV" 2>/dev/null || true

# ── 4. step-ca password-in + CA init ───────────────────────────────────────
echo "==> bootstrap: step-ca password + CA init"
chown 1000:1000 "$CA_VOL" 2>/dev/null || true
if [ ! -s "$CA_VOL/password-in" ]; then
  ( umask 077; openssl rand -base64 24 > "$CA_VOL/password-in" )
  chown 1000:1000 "$CA_VOL/password-in"
  chmod 600 "$CA_VOL/password-in"
  echo "bootstrap: step-ca password-in written"
fi
if [ ! -f "$CA_VOL/config/ca.json" ]; then
  ADMINPW="$(cat "$CA_VOL/password-in")"
  # The container's entrypoint runs the FULL init then keeps serving — feed the
  # prompts via stdin and bound it with timeout. `|| true` here is EXPECTED
  # (timeout kills the still-running server after the init); the REAL check is
  # the ca.json assert below (the 08-20 gap: a missing password-in made the
  # init abort silently and `|| true` masked it).
  { printf "step\n%s\n" "$ADMINPW"; sleep 85; } | timeout 90 \
    docker run --rm -i -v "$CA_VOL:/home/step" \
      -e STEPPATH=/home/step \
      -e DOCKER_STEPCA_INIT_NAME="BareNOC Internal CA" \
      -e DOCKER_STEPCA_INIT_DNS_NAMES=stepca.barenoc.local \
      -e DOCKER_STEPCA_INIT_PROVISIONER_NAME=admin \
      -e DOCKER_STEPCA_INIT_ADDRESS=:443 \
      -e DOCKER_STEPCA_INIT_ACME=true \
      -e DOCKER_STEPCA_INIT_PASSWORD_FILE=/home/step/password-in \
      smallstep/step-ca:latest >/dev/null 2>&1 || true
  if [ ! -f "$CA_VOL/config/ca.json" ]; then
    echo "!! step-ca CA init did not produce ca.json (password-in? docker pull?)" >&2
    exit 1
  fi
  echo "bootstrap: step-ca CA initialized (ca.json)"
fi

# ── 5. barenoc-devices provisioner ─────────────────────────────────────────
echo "==> bootstrap: barenoc-devices provisioner"
if [ ! -f "$CA_VOL/secrets/barenoc-devices.pem" ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out /tmp/barenoc-devices.pem
  openssl ec -in /tmp/barenoc-devices.pem -pubout -out /tmp/barenoc-devices.pub 2>/dev/null
  # bootstrap runs as root, but the step-ca container runs as uid 1000 and must
  # READ these keypair files via the bind mount (deploy.sh runs as barenoc so
  # they were uid-1000-owned there; here root-owned files 600/644 would hit
  # 'open /tmp/bd.pub failed: permission denied' inside the container).
  chown 1000:1000 /tmp/barenoc-devices.pem /tmp/barenoc-devices.pub
  chmod 600 /tmp/barenoc-devices.pem; chmod 644 /tmp/barenoc-devices.pub
  if ! docker run --rm -v "$CA_VOL:/home/step" \
    -v /tmp/barenoc-devices.pem:/tmp/bd.pem:ro -v /tmp/barenoc-devices.pub:/tmp/bd.pub:ro \
    -e STEPPATH=/home/step smallstep/step-ca:latest step ca provisioner add \
      barenoc-devices --type=JWK --private-key /tmp/bd.pem --public-key /tmp/bd.pub \
      --ca-config /home/step/config/ca.json --password-file /home/step/password-in; then
    echo "!! barenoc-devices provisioner creation failed (ca.json? password-in?)" >&2
    exit 1
  fi
  cp /tmp/barenoc-devices.pem "$CA_VOL/secrets/barenoc-devices.pem"
  chown 1000:1000 "$CA_VOL/secrets/barenoc-devices.pem"
  echo "bootstrap: barenoc-devices provisioner created"
  rm -f /tmp/barenoc-devices.pem /tmp/barenoc-devices.pub
fi

# ── 6. nginx certs (main + stepca vhost) ───────────────────────────────────
echo "==> bootstrap: nginx certs"
mkdir -p "$B/volumes/nginx/certs"
cp "$CA_VOL/certs/root_ca.crt" "$B/volumes/nginx/certs/ca-root.crt"
ADMINPW="$(cat "$CA_VOL/password-in")"
IP="$(env_get APPLIANCE_IP)"; IP="${IP:-192.0.2.207}"

CERT="$B/volumes/nginx/certs/stepca-intermediate.crt"
if [ ! -s "$CERT" ] || ! openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -q stepca.barenoc.local; then
  T=$(mktemp -d)
  openssl ec -in "$CA_VOL/secrets/intermediate_ca_key" -passin pass:"$ADMINPW" -out "$T/int.key" 2>/dev/null
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$T/server.key" -out "$T/server.csr" -subj "/CN=stepca.barenoc.local" 2>/dev/null
  printf "subjectAltName=DNS:stepca.barenoc.local,DNS:app.barenoc.com,DNS:bareNOC.local,IP:%s\n" "$IP" > "$T/sans.cnf"
  openssl x509 -req -in "$T/server.csr" \
    -CA "$CA_VOL/certs/intermediate_ca.crt" -CAkey "$T/int.key" -CAcreateserial -days 3650 -sha256 \
    -extfile "$T/sans.cnf" -out "$T/server.crt" 2>/dev/null
  cat "$T/server.crt" "$CA_VOL/certs/intermediate_ca.crt" > "$CERT"
  cp "$T/server.key" "$B/volumes/nginx/certs/stepca-intermediate.key"
  chmod 0644 "$CERT" "$B/volumes/nginx/certs/ca-root.crt"
  chmod 0640 "$B/volumes/nginx/certs/stepca-intermediate.key"
  rm -rf "$T"
  echo "bootstrap: stepca vhost cert issued (SANs incl. appliance IP)"
else
  echo "bootstrap: stepca vhost cert present (SANs) — keeping"
fi

CERT="$B/volumes/nginx/certs/barenoc.crt"
KEY="$B/volumes/nginx/certs/barenoc.key"
ISSUER_HASH="$(openssl x509 -in "$CERT" -noout -issuer_hash 2>/dev/null || true)"
SUBJECT_HASH="$(openssl x509 -in "$CERT" -noout -subject_hash 2>/dev/null || true)"
if [ ! -s "$CERT" ] || [ ! -s "$KEY" ] || [ "$ISSUER_HASH" = "$SUBJECT_HASH" ] || \
   ! openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -qF "$IP" || \
   ! openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null 2>&1; then
  T=$(mktemp -d)
  openssl ec -in "$CA_VOL/secrets/intermediate_ca_key" -passin pass:"$ADMINPW" -out "$T/int.key" 2>/dev/null
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$T/server.key" -out "$T/server.csr" -subj "/CN=app.barenoc.com" 2>/dev/null
  printf "subjectAltName=DNS:app.barenoc.com,DNS:bareNOC.local,DNS:pocket-id.barenoc.local,IP:%s\n" "$IP" > "$T/sans.cnf"
  openssl x509 -req -in "$T/server.csr" \
    -CA "$CA_VOL/certs/intermediate_ca.crt" -CAkey "$T/int.key" -CAcreateserial -days 3650 -sha256 \
    -extfile "$T/sans.cnf" -out "$T/server.crt" 2>/dev/null
  cat "$T/server.crt" "$CA_VOL/certs/intermediate_ca.crt" > "$CERT"
  cp "$T/server.key" "$KEY"
  chmod 0644 "$CERT"
  chmod 0640 "$KEY"
  rm -rf "$T"
  echo "bootstrap: main vhost cert issued (SANs incl. appliance IP)"
else
  echo "bootstrap: main vhost cert present — keeping"
fi

# ── 7. CoreDNS Corefile ────────────────────────────────────────────────────
echo "==> bootstrap: CoreDNS Corefile"
IP="$(env_get APPLIANCE_IP)"; IP="${IP:-192.0.2.207}"
HOST="$(env_get APPLIANCE_HOST)"; HOST="${HOST:-app.barenoc.com}"
mkdir -p "$B/volumes/dns"
cat > "$B/volumes/dns/Corefile" <<CORE
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
# the coredns container runs as a non-root user and must READ the Corefile
# (a leaked umask 077 from an earlier step would leave it root-only 600 ->
# 'open /config/Corefile: permission denied' crash-loop)
chmod 644 "$B/volumes/dns/Corefile"
chown barenoc:docker "$B/volumes/dns/Corefile" 2>/dev/null || true
echo "bootstrap: Corefile rendered (IP=$IP host=$HOST)"

# ── 8. step-cli static fetch (self-service onboarding; best-effort) ────────
echo "==> bootstrap: step-cli static fetch (best-effort)"
mkdir -p "$B/volumes/static" && chown barenoc:docker "$B/volumes/static" 2>/dev/null || true
for p in "step:step_linux_amd64" "step-cli-darwin_amd64:step_darwin_amd64" "step-cli-darwin_arm64:step_darwin_arm64"; do
  out="${p%%:*}"; src="${p##*:}"
  [ -s "$B/volumes/static/$out" ] && continue
  (cd /tmp && curl -sL "https://dl.smallstep.com/gh-release/cli/gh-release-header/v0.30.2/${src}.tar.gz" -o s.tgz \
    && tar xzf s.tgz && find "${src}" -name step -type f -exec cp {} "$B/volumes/static/$out" \; \
    && chmod 755 "$B/volumes/static/$out" && rm -rf s.tgz "${src}" && echo "bootstrap: fetched $out") || true
done

echo "==> bootstrap complete"
