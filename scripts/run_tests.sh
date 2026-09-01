#!/usr/bin/env bash
# BareNOC test gates — single source of truth for what must stay green.
# Used by CI (.github/workflows/ci.yml) and on the VM host.
# On the dev box (no pip) use the in-container commands from CONTRIBUTING.md.
set -euo pipefail
# General suite: the API rate limiter (300/min) trips once the suite
# exceeds ~300 requests in a minute; test_rate_limit sets its own env.
export RATE_LIMIT_ENABLED=false

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Run a unittest group. On success we print only the summary tail; on failure
# we ALSO surface the ERROR/FAIL blocks (test name + full traceback) that the
# old `tail -3` hid — which is exactly how api-suite errors went unseen in CI.
_run_unittest() {
  local dir="$1"; shift
  local log rc
  log="$(mktemp)"
  rc=0
  ( cd "$dir" && python3 -m unittest "$@" >"$log" 2>&1 ) || rc=$?
  tail -3 "$log"
  if [ "$rc" -ne 0 ]; then
    echo "--- failing tests (name + traceback) ---"
    grep -n -A 40 -E '^ERROR:|^FAIL:' "$log" || true
  fi
  rm -f "$log"
  return "$rc"
}

echo "==> py_compile (whole tree)"
find src -name '*.py' -not -path '*/__pycache__/*' -print0 | xargs -0 python3 -m py_compile

echo "==> scripts tests"
_run_unittest scripts test_forum_confirm
( bash scripts/test_check_updates_multi.sh )
( bash scripts/test_apply_updates.sh )
( bash scripts/test_trust_root.sh )
( bash scripts/test_release_signing.sh )
( bash scripts/test_support_ssh.sh )

echo "==> api tests"
( cd src/api && python3 -m unittest test_onboard test_devices test_device_agent test_device_agent_jobs test_device_channels test_device_layout test_devices_polish test_dashboard test_admin test_settings test_backups_setup test_alerting test_link_monitor test_service_checks test_revoke_integrity test_starlink test_unifi_sync test_chat_juniper test_chat_shell test_updates test_tickets test_tone_filter test_network_opt test_telemetry test_wiki test_firmware test_roles test_auth_sessions test_support test_report_submit test_emailer test_jobs_format test_jobs_metering test_ticket_formatting test_compliance_controls test_attestation test_audit_log test_audit_chain test_audit_catalog test_audit_events test_change_log test_setup_express test_front_door test_remote_backup 2>&1 | tail -3 )
( cd src/api && python3 -m unittest test_devices test_device_agent test_device_channels test_device_layout test_devices_polish test_dashboard test_admin test_settings test_backups_setup test_alerting test_link_monitor test_service_checks test_revoke_integrity test_starlink test_unifi_sync test_chat_juniper test_chat_shell test_updates test_tickets test_tone_filter test_network_opt test_network_scope test_discover_sweep test_telemetry test_wiki test_firmware test_roles test_support test_report_submit test_remote_backup 2>&1 | tail -3 )
_run_unittest src/api test_onboard test_devices test_device_agent test_device_certs test_device_agent_jobs test_device_channels test_device_layout test_devices_polish test_dashboard test_admin test_settings test_backups_setup test_alerting test_link_monitor test_service_checks test_starlink test_unifi_sync test_discovery_dedupe test_chat_juniper test_chat_shell test_updates test_tickets test_tone_filter test_network_opt test_telemetry test_wiki test_firmware test_roles test_auth_sessions test_support test_report_submit test_emailer test_jobs_format test_jobs_metering test_ticket_formatting test_compliance_controls test_attestation test_audit_log test_audit_chain test_audit_catalog test_audit_events test_change_log test_setup_express test_front_door test_remote_backup
_run_unittest src/api test_devices test_device_agent test_device_certs test_device_channels test_device_layout test_devices_polish test_dashboard test_admin test_settings test_backups_setup test_alerting test_link_monitor test_service_checks test_starlink test_unifi_sync test_discovery_dedupe test_chat_juniper test_chat_shell test_updates test_tickets test_tone_filter test_network_opt test_network_scope test_discover_sweep test_telemetry test_wiki test_firmware test_roles test_support test_report_submit test_remote_backup
echo "==> worker tests"
_run_unittest src/worker test_judge test_juniper test_pi_flag test_integration test_llm_client

echo "==> scheduler tests"
_run_unittest src/scheduler test_scheduler test_retention test_audit_incident

echo "==> runner tests"
_run_unittest src/agent test_runner

echo "==> bash syntax (scripts + proxmox + agent-go)"
for f in deploy.sh proxmox/*.sh src/scripts/*.sh src/api/routes/trust_root.sh agent-go/scripts/*.sh scripts/test_check_updates_multi.sh scripts/test_apply_updates.sh scripts/test_trust_root.sh scripts/test_release_signing.sh; do bash -n "$f"; done
echo "all bash OK"

echo "==> agent-go tests (go build/vet/test)"
GO_BIN="$(command -v go || true)"
[ -z "$GO_BIN" ] && [ -x "$HOME/.local/share/go/go/bin/go" ] && GO_BIN="$HOME/.local/share/go/go/bin/go"
if [ -z "$GO_BIN" ]; then
  echo "!! go not found — agent-go tests skipped (CI installs it via setup-go)"
else
  ( cd agent-go && "$GO_BIN" build ./... && "$GO_BIN" vet ./... && "$GO_BIN" test ./... 2>&1 | tail -2 )
fi
