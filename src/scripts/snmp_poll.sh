#!/bin/bash
# Safe SNMP poll script — captures system health metrics
# Usage: snmp_poll.sh <target_ip> [community]
# Returns JSON with CPU, memory, uptime, interfaces

TARGET="$1"
COMMUNITY="${2:-public}"
TIMEOUT=4

if [ -z "$TARGET" ]; then
  echo '{"error": "No target specified"}'
  exit 1
fi

if ! command -v snmpwalk &>/dev/null; then
  echo "{\"error\": \"snmpwalk not available\", \"target\": \"$TARGET\"}"
  exit 1
fi

# System description
SYS_DESCR=$(snmpwalk -v 2c -c "$COMMUNITY" -t $TIMEOUT "$TARGET" 1.3.6.1.2.1.1.1.0 2>/dev/null | grep -o 'STRING:.*' | sed 's/STRING://' | xargs)

if [ -z "$SYS_DESCR" ]; then
  echo "{\"reachable\": false, \"error\": \"SNMP timeout or no response\", \"target\": \"$TARGET\"}"
  exit 1
fi

# Uptime (hundredths of a second)
SYS_UPTIME_RAW=$(snmpwalk -v 2c -c "$COMMUNITY" -t $TIMEOUT "$TARGET" 1.3.6.1.2.1.1.3.0 2>/dev/null | grep -o '[0-9]*' | head -1)
if [ -n "$SYS_UPTIME_RAW" ]; then
  UPTIME_DAYS=$(awk "BEGIN {printf \"%.1f\", $SYS_UPTIME_RAW/8640000}")
  UPTIME_HRS=$(awk "BEGIN {printf \"%.1f\", ($SYS_UPTIME_RAW/360000) % 24}")
else
  UPTIME_DAYS="?"
  UPTIME_HRS="?"
fi

# CPU load (1-min average, OID 1.3.6.1.4.1.2021.10.1.3.1)
CPU_LOAD=$(snmpwalk -v 2c -c "$COMMUNITY" -t $TIMEOUT "$TARGET" 1.3.6.1.4.1.2021.10.1.3.1 2>/dev/null | grep -o 'Gauge32: [0-9]*' | grep -o '[0-9]*' | head -1)
if [ -z "$CPU_LOAD" ]; then CPU_LOAD="?"; fi

# Memory: total & used (OID 1.3.6.1.4.1.2021.4.5.0 total, 1.3.6.1.4.1.2021.4.6.0 free)
MEM_TOTAL=$(snmpwalk -v 2c -c "$COMMUNITY" -t $TIMEOUT "$TARGET" 1.3.6.1.4.1.2021.4.5.0 2>/dev/null | grep -o 'INTEGER: [0-9]*' | grep -o '[0-9]*' | head -1)
MEM_FREE=$(snmpwalk -v 2c -c "$COMMUNITY" -t $TIMEOUT "$TARGET" 1.3.6.1.4.1.2021.4.6.0 2>/dev/null | grep -o 'INTEGER: [0-9]*' | grep -o '[0-9]*' | head -1)

if [ -n "$MEM_TOTAL" ] && [ -n "$MEM_FREE" ] && [ "$MEM_TOTAL" != "0" ]; then
  MEM_USED=$(awk "BEGIN {printf \"%.1f\", (($MEM_TOTAL - $MEM_FREE) / $MEM_TOTAL) * 100}")
else
  MEM_USED="?"
fi

echo "{\"reachable\": true, \"sys_descr\": \"$SYS_DESCR\", \"uptime_days\": \"$UPTIME_DAYS\", \"uptime_hrs\": \"$UPTIME_HRS\", \"cpu_load\": \"$CPU_LOAD\", \"mem_used_pct\": \"$MEM_USED\", \"target\": \"$TARGET\"}"
