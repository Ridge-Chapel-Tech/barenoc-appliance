#!/bin/bash
# AI Technician: re-enable a UniFi switch port (merge-safe).
# Usage: unifi_port_enable.sh <switch_mac> <port_idx>
set -u
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
exec bash "$SCRIPT_DIR/unifi_port_disable.sh" "$1" "$2" enable
