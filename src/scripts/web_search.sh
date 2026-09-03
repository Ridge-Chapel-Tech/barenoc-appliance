#!/usr/bin/env bash
# web_search.sh "<query>" [count] — read-only, opt-in-egress web search for Lily.
# Thin wrapper over web_research.py (the single source of truth for the SSRF
# guard, egress gate and per-topic cache). Read-only: HTTP GET only.
set -uo pipefail
exec python3 "$(dirname "$0")/web_research.py" search "$@"
