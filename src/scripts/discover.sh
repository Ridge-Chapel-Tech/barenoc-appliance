#!/bin/bash
# Safe network discovery script — ping sweep a subnet to find live hosts.
# Usage: discover.sh <target>
#   target: "192.0.2.0/24" (subnet CIDR) or "192.0.2.10" (single host)
# Outputs JSON: {"network": "...", "found": [{"ip": "..."}, ...], "count": N}
#
# Note: sweeps the last octet (.1-.254) of the base IP, so it is designed for
# /24-style ranges. Read-only ICMP traffic only — no device writes happen here.

TARGET="$1"
if [ -z "$TARGET" ]; then
  echo '{"error": "No target specified", "found": [], "count": 0}'
  exit 1
fi

# Single host target
if [[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  if ping -c 1 -W 1 "$TARGET" >/dev/null 2>&1; then
    echo "{\"network\": \"$TARGET\", \"found\": [{\"ip\": \"$TARGET\"}], \"count\": 1}"
  else
    echo "{\"network\": \"$TARGET\", \"found\": [], \"count\": 0}"
  fi
  exit 0
fi

# Parse CIDR: a.b.c.d/prefix
if [[ "$TARGET" =~ ^([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)/([0-9]+)$ ]]; then
  IP="${BASH_REMATCH[1]}"
  PREFIX="${BASH_REMATCH[2]}"
else
  echo "{\"error\": \"Invalid target '$TARGET' — use a CIDR like 192.0.2.0/24 or a single IP\", \"found\": [], \"count\": 0}"
  exit 1
fi

# Safety: only accept /24 or wider ranges
if [ "$PREFIX" -gt 24 ]; then
  echo "{\"error\": \"Prefix /$PREFIX not supported — use /24 or wider (e.g. 192.0.2.0/24)\", \"found\": [], \"count\": 0}"
  exit 1
fi

BASE_IP="${IP%.*}"   # a.b.c
FOUND_FILE=$(mktemp)

for host in $(seq 1 254); do
  (
    if ping -c 1 -W 1 "${BASE_IP}.${host}" >/dev/null 2>&1; then
      echo "${BASE_IP}.${host}" >> "$FOUND_FILE"
    fi
  ) &
done
wait

COUNT=0
FOUND=""
while read -r ip; do
  [ -z "$ip" ] && continue
  COUNT=$((COUNT + 1))
  FOUND="${FOUND}{\"ip\": \"$ip\"},"
done < "$FOUND_FILE"
rm -f "$FOUND_FILE"

FOUND="${FOUND%,}"
echo "{\"network\": \"$TARGET\", \"found\": [$FOUND], \"count\": $COUNT}"
