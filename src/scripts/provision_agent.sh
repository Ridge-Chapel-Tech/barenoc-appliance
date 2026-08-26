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

# Self-sufficiency wiring (ISO round 4): ownership / passwordless sudo /
# .env bootstrap / step-ca password-in + CA init / nginx certs / Corefile.
# The ISO first-boot runs this bootstrap BEFORE `docker compose up`; re-running
# it here makes EVERY path (appliance installer, ISO, manual deploy) converge
# on the same turnkey state — a box that was installed before this wiring
# self-heals on the next deploy. Idempotent (no-ops once artifacts exist).
echo "==> Agent provision: self-sufficiency bootstrap"
if [[ -f /opt/barenoc/scripts/bootstrap_appliance.sh ]]; then
  bash /opt/barenoc/scripts/bootstrap_appliance.sh
else
  echo "!! bootstrap_appliance.sh not found — skipping (pre-bootstrap tree)" >&2
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

echo "==> Agent provision: vendor-managed email (notify capability — out-of-the-box alerts)"
# Same shared-token pattern as forum-submit: the appliance POSTs alert/digest
# email to the vendor `notify` edge function (Resend) so email works with zero
# SMTP setup. The token is semi-public in the release tree (it only gates the
# vendor's rate-limited Resend sends); Settings → Email remains the per-install
# override, and the vendor can rotate the token at any time.
cat > "$FORUM_SUBMIT_SECRET_DIR/notify.json" <<JSON
{"url":"https://eqivajpnvansfpxkegpr.supabase.co/functions/v1/notify","token":"80e5e5af4993d65714f7145827bdc600b2dc32aac8af8278a1ebfd5bd7419c97"}
JSON
chmod 600 "$FORUM_SUBMIT_SECRET_DIR/notify.json"
chown root:root "$FORUM_SUBMIT_SECRET_DIR/notify.json"
echo "==> Agent provision: remote support (tailscale zero-touch onboarding + beta grant)"
# Tailscale = the remote-support mechanism (identity mTLS, outbound-only,
# works through CGNAT). The appliance joins the VENDOR support
# tailnet via a tagged, expiring, revocable auth key in a 0600 secret file
# (same pattern as forum_submit.json above). The customer controls it with
# the Settings → Support → "Remote support" toggle (default OFF) — the API
# writes a desired-state flag and a host timer applies tailscale up/down.
#
# The join is idempotent + fails GRACEFULLY: a no-tailscale host, a missing
# auth key, or a failed join must never block the deploy.
SUPPORT_SECRET_DIR="/opt/barenoc/volumes/secrets"
mkdir -p "$SUPPORT_SECRET_DIR"
# Beta support grant — the report_gate.py `support` mode reads this expiring
# key (semi-public beta pattern like the forum-submit token). ROTATE it and
# set a new expiry before/at GA; when it expires the Support-subscription
# entitlement check takes over.
cat > "$SUPPORT_SECRET_DIR/support_grant.json" <<JSON
{"grant":"support-grant-beta-2026-CHANGE-ME","expires_at":"2026-12-31T23:59:59Z","note":"beta remote-support grant — ROTATE before GA"}
JSON
chmod 600 "$SUPPORT_SECRET_DIR/support_grant.json"
chown root:root "$SUPPORT_SECRET_DIR/support_grant.json"

# Install the remote-support reconciler (systemd timer) + perform the
# idempotent apt install / tagged join. All failures are non-fatal here.
# The install is repo-correct (tailscale_remote_support.sh uses the official
# installer to configure the Tailscale apt repo) — 08-20: a bare
# `apt-get install tailscale` fails on existing boxes with no pre-configured
# repo. verify_post_update.sh is the update-time guarantee (post-apply).
install -m 0644 /opt/barenoc/scripts/barenoc-remote-support.service /etc/systemd/system/ 2>/dev/null || true
install -m 0644 /opt/barenoc/scripts/barenoc-remote-support.timer /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
systemctl enable --now barenoc-remote-support.timer >/dev/null 2>&1 || true
bash /opt/barenoc/scripts/tailscale_remote_support.sh provision || true


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
