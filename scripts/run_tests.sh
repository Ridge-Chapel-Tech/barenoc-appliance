#!/usr/bin/env bash
# BareNOC test gates — single source of truth for what must stay green.
# Used by CI (.github/workflows/ci.yml) and on the VM host.
# On the dev box (no pip) use the in-container commands from CONTRIBUTING.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> py_compile (whole tree)"
find src -name '*.py' -not -path '*/__pycache__/*' -print0 | xargs -0 python3 -m py_compile

echo "==> api tests"
( cd src/api && python3 -m unittest test_devices test_device_agent test_admin test_settings test_alerting test_unifi_sync test_chat_juniper 2>&1 | tail -3 )

echo "==> worker tests"
( cd src/worker && python3 -m unittest test_judge test_juniper test_integration 2>&1 | tail -3 )

echo "==> runner tests"
( cd src/agent && python3 -m unittest test_runner 2>&1 | tail -3 )

echo "==> bash syntax (scripts + proxmox)"
for f in deploy.sh proxmox/*.sh src/scripts/*.sh; do bash -n "$f"; done
echo "all bash OK"

echo "==> agent-go tests (go build/vet/test)"
GO_BIN="$(command -v go || true)"
[ -z "$GO_BIN" ] && [ -x "$HOME/.local/share/go/go/bin/go" ] && GO_BIN="$HOME/.local/share/go/go/bin/go"
if [ -z "$GO_BIN" ]; then
  echo "!! go not found — agent-go tests skipped (CI installs it via setup-go)"
else
  ( cd agent-go && "$GO_BIN" build ./... && "$GO_BIN" vet ./... && "$GO_BIN" test ./... 2>&1 | tail -2 )
fi
