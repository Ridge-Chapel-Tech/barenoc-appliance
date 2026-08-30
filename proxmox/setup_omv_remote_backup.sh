#!/usr/bin/env bash
# setup_omv_remote_backup.sh — provision the BareNOC-managed remote-backup
# backend on an openmediavault (OMV) box.
#
# The managed backend is a MinIO deployment (S3-compatible). This script:
#   1. Installs MinIO + the MinIO client (mc) and a systemd unit (idempotent).
#   2. Creates the managed bucket `barenoc-managed` with a 30-day expiry
#      lifecycle rule (the beta managed retention).
#   3. Per-customer bucket/key strategy: `--customer <siteid>` creates a
#      dedicated bucket `barenoc-<siteid>` + a scoped user/access-key that can
#      ONLY touch that bucket (put/list/get/delete), and prints the endpoint +
#      access key + secret the gate stores as the managed profile
#      (OFFSITE_MANAGED_* on each appliance).
#   4. Prints a summary the gate copies into the appliance's .env.
#
# ⚠️ DO NOT RUN THIS AGAINST THE LIVE NAS AS PART OF THE WORKER LANE.
#    This file was written by the remote-backup worker and is EXECUTED ONLY
#    by the gate on the real OMV box after review (STANDING PROCEDURES #7:
#    vet-first). The worker lane never touches the live OMV host.
#
# Usage (root on the OMV box):
#   bash setup_omv_remote_backup.sh                     # install + managed bucket
#   bash setup_omv_remote_backup.sh --customer 7        # add/refresh customer 7
#   bash setup_omv_remote_backup.sh --customer 7 --rotate
set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
MINIO_VERSION="RELEASE.2025-04-22T22-12-26Z"   # pin for reproducibility
DATA_DIR="/srv/minio"
LISTEN_PORT="${MINIO_PORT:-9000}"
CONSOLE_PORT="${MINIO_CONSOLE_PORT:-9001}"
MANAGED_BUCKET="${MANAGED_BUCKET:-barenoc-managed}"
ROOT_USER="${MINIO_ROOT_USER:-}"
ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-}"
CUSTOMER=""
ROTATE=0
RETENTION_DAYS="${MANAGED_RETENTION_DAYS:-30}"
ENDPOINT_HOST="${MINIO_ENDPOINT_HOST:-}"        # printed endpoint host (default: detect)

# ── Args ────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --customer)      CUSTOMER="${2:?--customer needs a site id}"; shift 2 ;;
    --rotate)        ROTATE=1; shift ;;
    --data-dir)      DATA_DIR="${2:?}"; shift 2 ;;
    --port)          LISTEN_PORT="${2:?}"; shift 2 ;;
    --console-port)  CONSOLE_PORT="${2:?}"; shift 2 ;;
    --bucket)        MANAGED_BUCKET="${2:?}"; shift 2 ;;
    --root-user)     ROOT_USER="${2:?}"; shift 2 ;;
    --root-password) ROOT_PASSWORD="${2:?}"; shift 2 ;;
    --endpoint-host) ENDPOINT_HOST="${2:?}"; shift 2 ;;
    --retention-days) RETENTION_DAYS="${2:?}"; shift 2 ;;
    --version)       MINIO_VERSION="${2:?}"; shift 2 ;;
    -h|--help)
      sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

# ── OMV sanity (warn only — MinIO works on any Debian, but this targets OMV) ──
if [ -f /etc/openmediavault/config.xml ] || dpkg -l openmediavault >/dev/null 2>&1; then
  echo "==> Detected openmediavault host"
else
  echo "!! /etc/openmediavault/config.xml not found — continuing on a plain Debian host" >&2
fi

# ── 1. Install MinIO binary (official release, no apt repo needed) ──────────
ARCH="amd64"
[ "$(uname -m)" = "aarch64" ] && ARCH="arm64"
BIN="/usr/local/bin/minio"
MC_BIN="/usr/local/bin/mc"

install_minio() {
  local url="https://dl.min.io/server/minio/release/linux-${ARCH}/archive/minio.${MINIO_VERSION}"
  echo "==> Installing minio ${MINIO_VERSION} (${ARCH})"
  curl -fsSL -o /tmp/minio.download "$url"
  install -m 0755 /tmp/minio.download "$BIN"
  rm -f /tmp/minio.download
  "$BIN" --version | head -1
}

install_mc() {
  local url="https://dl.min.io/client/mc/release/linux-${ARCH}/archive/mc.${MINIO_VERSION}"
  echo "==> Installing mc ${MINIO_VERSION}"
  curl -fsSL -o /tmp/mc.download "$url"
  install -m 0755 /tmp/mc.download "$MC_BIN"
  rm -f /tmp/mc.download
  "$MC_BIN" --version | head -1
}

[ -x "$BIN" ] || install_minio
[ -x "$MC_BIN" ] || install_mc

# ── 2. User + data dir + root credentials ───────────────────────────────────
id -u minio-user >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin minio-user
mkdir -p "$DATA_DIR"
chown -R minio-user:minio-user "$DATA_DIR"

if [ -z "$ROOT_USER" ]; then ROOT_USER="barenoc-omv-admin"; fi
if [ -z "$ROOT_PASSWORD" ]; then
  ROOT_PASSWORD="$(openssl rand -base64 24 | tr '/+' '_-' | tr -d '=')"
  GENERATED_PW=1
else
  GENERATED_PW=0
fi
# The env file is root-only: it holds the root (admin) credential.
ENV_FILE="/etc/default/minio"
umask 077
cat > "$ENV_FILE" <<EOF
MINIO_ROOT_USER=${ROOT_USER}
MINIO_ROOT_PASSWORD=${ROOT_PASSWORD}
MINIO_VOLUMES=${DATA_DIR}
MINIO_OPTS="--address :${LISTEN_PORT} --console-address :${CONSOLE_PORT}"
MINIO_PROMETHEUS_AUTH_TYPE=public
EOF
chmod 600 "$ENV_FILE"

# ── 3. systemd unit (idempotent) ────────────────────────────────────────────
UNIT="/etc/systemd/system/minio.service"
cat > "$UNIT" <<'EOF'
[Unit]
Description=MinIO (BareNOC managed remote-backup backend)
Documentation=https://min.io/docs
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=minio-user
Group=minio-user
EnvironmentFile=-/etc/default/minio
ExecStart=/usr/local/bin/minio server $MINIO_VOLUMES $MINIO_OPTS
Restart=always
LimitNOFILE=65536
TasksMax=infinity
TimeoutStopSec=infinity
SendSIGKILL=no

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now minio.service

# ── 4. Wait for healthy + mc alias ──────────────────────────────────────────
echo "==> Waiting for MinIO health…"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${LISTEN_PORT}/minio/health/live" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS "http://127.0.0.1:${LISTEN_PORT}/minio/health/live" >/dev/null \
  || { echo "ERROR: MinIO did not become healthy" >&2; exit 1; }

"$MC_BIN" alias set barenoc "http://127.0.0.1:${LISTEN_PORT}" "$ROOT_USER" "$ROOT_PASSWORD" >/dev/null

# ── 5. Managed bucket + retention lifecycle (beta: 30 days) ─────────────────
if "$MC_BIN" ls "barenoc/${MANAGED_BUCKET}" >/dev/null 2>&1; then
  echo "==> Managed bucket ${MANAGED_BUCKET} already exists"
else
  "$MC_BIN" mb "barenoc/${MANAGED_BUCKET}"
  echo "==> Created managed bucket ${MANAGED_BUCKET}"
fi
# Tiered retention: a lifecycle rule expires objects after N days. The
# appliance also prunes client-side; this is the bucket-side backstop.
echo "==> Applying ${RETENTION_DAYS}d expiry lifecycle to ${MANAGED_BUCKET}"
"$MC_BIN" ilm rule add --expire-days "${RETENTION_DAYS}" "barenoc/${MANAGED_BUCKET}" >/dev/null 2>&1 \
  || "$MC_BIN" ilm add --expiry-days "${RETENTION_DAYS}" "barenoc/${MANAGED_BUCKET}" >/dev/null 2>&1 \
  || echo "!! lifecycle rule not applied (mc ilm syntax differs on this version) — set it in the MinIO console"

# ── 6. Per-customer bucket + scoped access key ──────────────────────────────
if [ -n "$CUSTOMER" ]; then
  CBUCKET="barenoc-${CUSTOMER}"
  CUSER="barenoc-${CUSTOMER}"
  if "$MC_BIN" ls "barenoc/${CBUCKET}" >/dev/null 2>&1; then
    echo "==> Customer bucket ${CBUCKET} already exists"
  else
    "$MC_BIN" mb "barenoc/${CBUCKET}"
  fi
  # Scoped policy: this key can ONLY touch its own bucket (put/list/get/delete).
  POLICY="/etc/minio/policy-${CBUCKET}.json"
  cat > "$POLICY" <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::${CBUCKET}"]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":["arn:aws:s3:::${CBUCKET}/*"]}
]}
EOF
  chmod 644 "$POLICY"
  POLICY_NAME="${CBUCKET}-policy"
  "$MC_BIN" admin policy create "barenoc" "$POLICY_NAME" "$POLICY" >/dev/null 2>&1 \
    || "$MC_BIN" admin policy add "barenoc" "$POLICY_NAME" "$POLICY" >/dev/null 2>&1 \
    || true
  "$MC_BIN" admin policy attach "barenoc" "$POLICY_NAME" --user "$CUSER" >/dev/null 2>&1 || true

  if [ "$ROTATE" -eq 1 ] || ! "$MC_BIN" admin user info "barenoc" "$CUSER" >/dev/null 2>&1; then
    CUSTOMER_SECRET="$(openssl rand -base64 24 | tr '/+' '_-' | tr -d '=')"
    if "$MC_BIN" admin user info "barenoc" "$CUSER" >/dev/null 2>&1; then
      "$MC_BIN" admin user remove "barenoc" "$CUSER" >/dev/null 2>&1 || true
    fi
    "$MC_BIN" admin user add "barenoc" "$CUSER" "$CUSTOMER_SECRET"
    "$MC_BIN" admin policy attach "barenoc" "$POLICY_NAME" --user "$CUSER" >/dev/null 2>&1 || true
  else
    CUSTOMER_SECRET="(unchanged — use --rotate to generate a new secret)"
  fi

  echo
  echo "════════════════════════════════════════════════════════════════"
  echo " Customer ${CUSTOMER} managed profile — store as OFFSITE_MANAGED_* on the appliance"
  echo "════════════════════════════════════════════════════════════════"
  echo "OFFSITE_MANAGED_ENDPOINT=http://$(hostname -I | awk '{print $1}'):${LISTEN_PORT}"
  echo "OFFSITE_MANAGED_BUCKET=${CBUCKET}"
  echo "OFFSITE_MANAGED_REGION=us-east-1"
  echo "OFFSITE_MANAGED_PREFIX="
  echo "OFFSITE_MANAGED_ACCESS_KEY=${CUSER}"
  echo "OFFSITE_MANAGED_SECRET_KEY=${CUSTOMER_SECRET}"
  echo "════════════════════════════════════════════════════════════════"
fi

# ── 7. Summary ──────────────────────────────────────────────────────────────
if [ -z "$ENDPOINT_HOST" ]; then
  ENDPOINT_HOST="$(hostname -I | awk '{print $1}')"
fi
echo
echo "✅ MinIO ready."
echo "   Endpoint:       http://${ENDPOINT_HOST}:${LISTEN_PORT}  (console: :${CONSOLE_PORT})"
echo "   Managed bucket: ${MANAGED_BUCKET} (retention ${RETENTION_DAYS}d)"
echo "   Root user:      ${ROOT_USER}"
if [ "${GENERATED_PW:-0}" -eq 1 ]; then
  echo "   Root password:  ${ROOT_PASSWORD}  (generated — stored in ${ENV_FILE}, root-only)"
fi
echo
echo "   Per-customer provisioning: bash $0 --customer <siteid> [--rotate]"
echo "   Open UFW ports on the OMV box for LAN appliances:"
echo "     ufw allow ${LISTEN_PORT}/tcp   # S3 API (appliances reach this)"
echo "     ufw allow ${CONSOLE_PORT}/tcp  # admin console (keep off / LAN-only)"
