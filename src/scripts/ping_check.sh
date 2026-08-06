#!/bin/bash
# Safe ping check script
# Usage: ping_check.sh <target_ip>
# Returns JSON: {"reachable": true/false, "latency_ms": float, "packet_loss": int}

TARGET="$1"
if [ -z "$TARGET" ]; then
  echo '{"error": "No target specified", "reachable": false}'
  exit 1
fi

RESULT=$(ping -c 4 -W 2 "$TARGET" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  # Extract latency
  LATENCY=$(echo "$RESULT" | grep "rtt" | awk -F'/' '{print $5}')
  LOSS=$(echo "$RESULT" | grep "packet loss" | awk -F' ' '{print $6}' | tr -d '%')
  echo "{\"reachable\": true, \"latency_ms\": $LATENCY, \"packet_loss\": ${LOSS:-0}, \"target\": \"$TARGET\"}"
else
  echo "{\"reachable\": false, \"latency_ms\": null, \"packet_loss\": 100, \"target\": \"$TARGET\"}"
fi

exit $EXIT_CODE
