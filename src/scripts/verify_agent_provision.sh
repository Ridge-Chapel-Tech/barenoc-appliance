#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC — post-install agent verification (the 08-09 lesson).
#
# End-of-install must check SCHEDULER LOGS, not just health 200 + a minted
# token — a fresh box can look healthy while the scheduler 401-floods because
# the credentials file never agreed with the DB (or never existed).
#
#   sudo bash /opt/barenoc/scripts/verify_agent_provision.sh    (run ON the VM)
#
# Surfaces the checklist lines: "agent login verified", "runner active",
# "scheduler 0 errors". Exits non-zero when any check fails (loud, never
# silent).
# ═══════════════════════════════════════════════════════════════════════════
set -u

CREDS_FILE="/opt/barenoc/agent/credentials"
FAIL=0

# 1. agent login verified — the credential FILE must agree with the DB.
#    (setup_agent_credentials.sh already proves this; re-assert for the
#    checklist so a later rotation/mismatch can't slip through silently.)
if [[ -f "$CREDS_FILE" ]]; then
  USERNAME="$(grep -E '^username=' "$CREDS_FILE" | head -1 | cut -d= -f2-)"
  PASSWORD="$(grep -E '^password=' "$CREDS_FILE" | head -1 | cut -d= -f2-)"
  CODE="$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
    https://127.0.0.1/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}")"
  if [[ "$CODE" == "200" ]]; then
    echo "✓ agent login verified (200)"
  else
    echo "✗ agent login FAILED (HTTP ${CODE:-?}) — credentials file and DB are out of sync"
    FAIL=1
  fi
else
  echo "✗ agent credentials file MISSING: $CREDS_FILE"
  FAIL=1
fi

# 2. runner active — the systemd unit must be installed + running.
if systemctl is-active --quiet pi-agent-runner 2>/dev/null; then
  echo "✓ runner active ($(systemctl is-active pi-agent-runner))"
else
  echo "✗ runner inactive — pi-agent-runner.service is not running"
  FAIL=1
fi

# 3. scheduler 0 errors — check the recent scheduler log tail for the
#    fresh-install auth/DNS/connection failure signatures. A healthy provision
#    starts the scheduler AFTER credentials exist, so this must be clean.
if docker ps -q -f name=^barenoc-scheduler$ | grep -q .; then
  ERRORS="$(docker logs barenoc-scheduler --tail 100 2>&1 | \
    grep -cE 'Cannot read agent credentials|Scheduler error:|HTTP Error 401|Connection refused|Name or service not known' || true)"
  if [[ "${ERRORS:-0}" -eq 0 ]]; then
    echo "✓ scheduler 0 errors"
  else
    echo "✗ scheduler has ${ERRORS} error(s) in the recent log tail:"
    docker logs barenoc-scheduler --tail 100 2>&1 | \
      grep -E 'Cannot read agent credentials|Scheduler error:|HTTP Error 401|Connection refused|Name or service not known' | tail -10
    FAIL=1
  fi
else
  echo "✗ scheduler container not running"
  FAIL=1
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "==> agent provisioning verified: OK"
else
  echo "==> agent provisioning verified: FAILED" >&2
fi
exit "$FAIL"
