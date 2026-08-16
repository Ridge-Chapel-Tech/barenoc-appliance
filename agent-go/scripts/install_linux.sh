#!/usr/bin/env bash
# NOC_Agent — minimal Linux install (P1a)
#
# Creates the dedicated /opt/noc-agent tree (config/certs/logs/state), a
# `nocagent` system user, copies the binary, and installs the systemd unit
# (noc-agent.service, User=nocagent, After=network-online.target).
#
# step-ca certificate enrollment is a P1b follow-up. For P1a the device
# certificate + key and the BareNOC CA root are provisioned OUT-OF-BAND:
# drop them at /opt/noc-agent/certs/{noc-agent.crt,noc-agent.key,ca.crt}
# (owned nocagent:nocagent, key mode 0600) before starting the service.
#
# Usage: sudo ./install_linux.sh [path/to/noc-agent-binary]
set -euo pipefail

INSTALL_DIR="/opt/noc-agent"
BIN="${INSTALL_DIR}/noc-agent"
SERVICE_USER="nocagent"
CONFIG="${INSTALL_DIR}/config.json"
UNIT="/etc/systemd/system/noc-agent.service"

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root (sudo ./install_linux.sh ...)" >&2
  exit 1
fi

SRC_BIN="${1:-}"
if [[ -z "${SRC_BIN}" ]]; then
  SRC_BIN="$(cd "$(dirname "$0")/.." && pwd)/noc-agent"
fi
if [[ ! -f "${SRC_BIN}" ]]; then
  echo "binary not found at ${SRC_BIN}; build it first (go build -o noc-agent ./cmd/noc-agent)" >&2
  exit 1
fi

echo "==> creating ${INSTALL_DIR} tree"
install -d -m 0755 "${INSTALL_DIR}"/certs "${INSTALL_DIR}"/logs "${INSTALL_DIR}"/state

echo "==> creating ${SERVICE_USER} system user (if missing)"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "${INSTALL_DIR}" \
    --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> installing binary"
install -m 0755 "${SRC_BIN}" "${BIN}"

if [[ ! -f "${CONFIG}" ]]; then
  echo "==> writing default ${CONFIG}"
  cat > "${CONFIG}" <<'EOF'
{
  "appliance_url": "https://appliance.barenoc.example",
  "cn": "device-CHANGEME",
  "cert_file": "/opt/noc-agent/certs/noc-agent.crt",
  "key_file": "/opt/noc-agent/certs/noc-agent.key",
  "ca_file": "/opt/noc-agent/certs/ca.crt",
  "poll_interval": "30s",
  "log_level": "info"
}
EOF
fi

echo "==> installing systemd unit"
cat > "${UNIT}" <<'EOF'
[Unit]
Description=NOC_Agent — BareNOC endpoint agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nocagent
Group=nocagent
ExecStart=/opt/noc-agent/noc-agent -config /opt/noc-agent/config.json
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/noc-agent
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo "==> fixing ownership/permissions (certs 0700, key 0600, owner ${SERVICE_USER})"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 0700 "${INSTALL_DIR}/certs"
chmod 0600 "${INSTALL_DIR}"/certs/* 2>/dev/null || true

systemctl daemon-reload
systemctl enable noc-agent.service

echo
echo "Done. Review ${CONFIG} (set appliance_url + cn), place the certs under"
echo "${INSTALL_DIR}/certs/, then: systemctl start noc-agent.service"
echo "Step-ca enrollment is P1b — certs are provisioned out-of-band for P1a."
