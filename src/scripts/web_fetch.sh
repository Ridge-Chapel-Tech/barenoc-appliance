#!/usr/bin/env bash
# web_fetch.sh <url> — read-only, opt-in-egress page fetch for Lily.
# Thin wrapper over web_research.py (SSRF-guarded, read-only HTTP GET, cached
# per URL). Use on search hits / vendor docs / release notes / changelogs.
set -uo pipefail
exec python3 "$(dirname "$0")/web_research.py" fetch "$@"
