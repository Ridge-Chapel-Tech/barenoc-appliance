#!/usr/bin/env python3
"""Dump ticket context BEFORE a queue wipe — so requested-but-missing
capabilities can be reviewed and built into the action catalog first.

STANDING PROCEDURE (see SESSION_LOG.md): before wiping the ticket queue,
run this, read every ticket for requests no current action covers, build
those actions into the catalog (and any scripts/routes), THEN wipe.

Usage (inside the api container, or on the VM with python + sqlite3):
    docker compose exec api python3 /app/ticket_context_dump.py [--notes]
"""

import json
import sqlite3
import sys

DB = "/opt/barenoc/volumes/db/barenoc.db"
ACTION_CATALOG = [  # keep in sync with action_validator.AllowedAction
    "ping_test", "snmp_poll", "device_status", "apply_patch", "reboot_device",
    "collect_logs", "network_discovery", "network_info", "system_time",
    "unifi_clients", "unifi_devices", "unifi_ports", "unifi_port_config",
    "unifi_client_port", "unifi_firewall_rules", "unifi_restart",
    "unifi_port_bounce", "unifi_port_rename", "unifi_ensure_wireless_uplinks",
    "unifi_set_ssid_password", "pi_task", "batch", "fingerprint_device",
    "install_chat_client", "complete_ticket", "request_customer_input",
    "escalate_human",
]

# Request shapes that frequently surface as capability gaps — flagged for review.
GAP_HINTS = [
    ("ssid|wifi password|passphrase", "SSID/password change — covered by unifi_set_ssid_password"),
    ("vlan|tag", "VLAN tagging — covered by unifi_port_config / unifi_ensure_wireless_uplinks"),
    ("reboot|restart", "reboot — unifi_restart / reboot_device"),
    ("rename", "port rename — unifi_port_rename"),
    ("firmware|update", "firmware — read via unifi_devices/firmware-status"),
    ("firewall|block", "firewall — read-only unifi_firewall_rules; writes are NOT in the catalog"),
    ("account|user|password for", "identity changes — NOT in the catalog (deliberate)"),
]


def main():
    show_notes = "--notes" in sys.argv
    db = sqlite3.connect(DB)
    rows = db.execute(
        "SELECT ticket_id, title, description, status, resolution, created_at "
        "FROM tickets ORDER BY created_at").fetchall()
    db.close()
    if not rows:
        print("No tickets in the queue — nothing to review before a wipe.")
        return
    print(f"{len(rows)} ticket(s) to review before wiping:\n")
    for r in rows:
        print(f"=== {r[0]} | {r[2]} | created {r[5]}")
        print(f"  title: {r[1]}")
        print(f"  desc:  {(r[2] or '(none)')[:300]}")
        print(f"  status: {r[3]} | resolution: {(r[4] or '(none)')[:180]}")
        if show_notes:
            try:
                notes = json.loads(r[4] and "" or "[]")
            except Exception:
                notes = []
        blob = f"{r[1]} {r[2]} {r[4]}".lower()
        flagged = [h for pat, h in GAP_HINTS if __import__("re").search(pat, blob)]
        if flagged:
            print("  ⚠ capability review: " + "; ".join(flagged))
        print()
    print("Before wiping: for any flagged request without a catalog action, build the action "
          "(validator + script + route + runner + prompts), then wipe.")


if __name__ == "__main__":
    main()
