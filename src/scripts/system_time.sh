#!/bin/bash
# AI Technician: report the appliance's current time + timezone (read-only) —
# answers "what time is it" / "what timezone is this appliance on" tickets.
# Usage: system_time.sh
# Output: human-readable text on a TTY; JSON otherwise (the runner parses it
# and the ticket formatter turns it into a natural-language resolution).
set -u

TZ_LINE="$(grep -E '^TZ=' /opt/barenoc/.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')"
# The runner executes as pi-agent, which cannot read the 0600 .env — the
# worker carries TZ in the job file and the runner exports it to us.
if [ -z "${TZ_LINE:-}" ] && [ -n "${TZ:-}" ]; then
    TZ_LINE="$TZ"
fi
if [ -n "${TZ_LINE:-}" ]; then
    export TZ="$TZ_LINE"
fi

if [ -t 1 ]; then
    echo "utc:        $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "local:      $(date '+%Y-%m-%d %H:%M:%S %Z (%z)')"
    echo "tz_setting: ${TZ_LINE:-<unset — system default (UTC)>}"
    echo "uptime:     $(uptime -p 2>/dev/null || uptime)"
    exit 0
fi

python3 - "$TZ_LINE" <<'PYEOF'
import json, os, subprocess, sys
tz = sys.argv[1] if len(sys.argv) > 1 else ""
if tz:
    os.environ["TZ"] = tz

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""

print(json.dumps({
    "utc": sh("date -u '+%Y-%m-%d %H:%M:%S UTC'"),
    "local": sh("date '+%Y-%m-%d %H:%M:%S %Z (%z)'"),
    "tz_setting": tz or "unset",
    "uptime": sh("uptime -p 2>/dev/null || uptime"),
}))
PYEOF
