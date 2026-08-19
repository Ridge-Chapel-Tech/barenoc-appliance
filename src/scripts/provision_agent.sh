#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC — shared agent provisioning step (the GUARANTEE for every install
# path: appliance installer, ISO first-boot, manual deploy).
#
#   sudo bash /opt/barenoc/scripts/provision_agent.sh     (run ON the VM)
#
# After ANY fresh install this is the single step that makes the box
# self-sufficient. It is IDEMPOTENT (safe on every deploy) and does, in order:
#
#   1. Runtime dirs + ownership (agent dir, job queue, pi-work, agent log).
#   2. Installs + enables the pi-agent-runner systemd unit. Single source of
#      truth = /opt/barenoc/agent/pi-agent-runner.service (synced by deploy.sh
#      / the ISO tarball); a canonical inline fallback is kept for a defensive
#      manual install.
#   3. Provisions the `agent` service account + 0600 credential file — with the
#      api-healthy-BEFORE-creds wait and the file↔DB login-200 agreement check
#      (the 08-14 pattern; loud failure, never a silent file-without-DB-user).
#   4. Starts the runner (now that runner.py + credentials exist).
#
# The scheduler health-order guard lives in two places: deploy.sh / the ISO
# first-boot start the scheduler only AFTER this step succeeds, and
# scheduler/main.py waits for api-health + credentials at startup.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "!! provision_agent.sh must run as root (use sudo)" >&2
  exit 1
fi

AGENT_DIR="/opt/barenoc/agent"
UNIT_SRC="$AGENT_DIR/pi-agent-runner.service"
UNIT_DST="/etc/systemd/system/pi-agent-runner.service"

echo "==> Agent provision: runtime dirs + ownership"
mkdir -p \
  "$AGENT_DIR" \
  /opt/barenoc/volumes/logs/agent \
  /opt/barenoc/jobs/incoming \
  /opt/barenoc/jobs/running \
  /opt/barenoc/jobs/completed \
  /opt/barenoc/pi-work
# pi-agent owns its writable trees (job queue + log + runner dir). It is
# deliberately NOT in the docker group (docker membership = self-harm vector);
# traversal of /opt/barenoc is granted via o+x on the path instead.
chown -R pi-agent:pi-agent \
  "$AGENT_DIR" \
  /opt/barenoc/volumes/logs/agent \
  /opt/barenoc/jobs \
  /opt/barenoc/pi-work 2>/dev/null || true
for d in /opt/barenoc /opt/barenoc/volumes /opt/barenoc/volumes/logs \
         /opt/barenoc/volumes/secrets /opt/barenoc/scripts; do
  [ -d "$d" ] && chmod o+x "$d" 2>/dev/null || true
done

echo "==> Agent provision: pi-agent-runner unit"
if [[ -f "$UNIT_SRC" ]]; then
  install -m 0644 "$UNIT_SRC" "$UNIT_DST"
else
  # Defensive fallback (manual installs where the repo unit wasn't synced).
  cat > "$UNIT_DST" <<'UNIT'
[Unit]
Description=BareNOC Pi Agent Runner
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=pi-agent
Group=pi-agent
ExecStart=/usr/bin/python3 /opt/barenoc/agent/runner.py
WorkingDirectory=/opt/barenoc
Restart=always
RestartSec=5
StandardOutput=append:/opt/barenoc/volumes/logs/agent/agent.log
StandardError=append:/opt/barenoc/volumes/logs/agent/agent.log
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=no
CapabilityBoundingSet=CAP_NET_RAW CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
UNIT
fi
systemctl daemon-reload
systemctl enable pi-agent-runner >/dev/null 2>&1 || true

echo "==> Agent provision: credentials (api-healthy-before-creds + login-200 agreement)"
bash /opt/barenoc/scripts/setup_agent_credentials.sh

echo "==> Agent provision: forum-submit capability (in-app bug reports)"
# Shared beta capability token — every install gets it automatically so the
# in-app Submit Report flow works out of the box (no manual setup). The token
# is semi-public in the release tree (it only gates forum thread creation);
# the Support-subscription entitlement + per-appliance tokens tighten this at
# GA. The Settings → Support field remains the per-install override.
FORUM_SUBMIT_SECRET_DIR="/opt/barenoc/volumes/secrets"
mkdir -p "$FORUM_SUBMIT_SECRET_DIR"
cat > "$FORUM_SUBMIT_SECRET_DIR/forum_submit.json" <<JSON
{"url":"https://eqivajpnvansfpxkegpr.supabase.co/functions/v1/forum-submit","token":"46162a72e84f50d413c0970f49ce3b7eecfbb31e4b75c3d323645457f6e7df76"}
JSON
chmod 600 "$FORUM_SUBMIT_SECRET_DIR/forum_submit.json"
chown root:root "$FORUM_SUBMIT_SECRET_DIR/forum_submit.json"

echo "==> Agent provision: start runner"
systemctl restart pi-agent-runner
# give the runner a moment to pass its startup path (job-dir recovery + login)
sleep 2
if ! systemctl is-active --quiet pi-agent-runner; then
  echo "!! pi-agent-runner failed to stay active after restart" >&2
  journalctl -u pi-agent-runner -n 20 --no-pager >&2 || true
  exit 1
fi

echo "==> Agent provision complete: credentials=$AGENT_DIR/credentials runner=$(systemctl is-active pi-agent-runner)"
