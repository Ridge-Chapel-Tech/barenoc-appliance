#!/usr/bin/env bash
# Install BareNOC host-side backup scripts + cron on the Proxmox host.
# Run as root ON THE HOST (from the repo root or from proxmox/):  ./install.sh
set -euo pipefail

cd "$(dirname "$0")"
[ -d proxmox ] && cd proxmox   # scripts live in proxmox/ when run from the repo root

install -m 0755 barenoc-daily-backup.sh /usr/local/bin/
install -m 0755 backup-to-usb.sh /usr/local/bin/
install -m 0755 update-backup-status.sh /usr/local/bin/
install -m 0755 setup-usb-backup.sh /usr/local/bin/
install -m 0755 sync-backup-schedule.sh /usr/local/bin/
install -m 0644 barenoc-backup-cron /etc/cron.d/barenoc-backup
touch /var/log/barenoc-backup.log

echo "Installed:"
echo "  /usr/local/bin/barenoc-daily-backup.sh"
echo "  /usr/local/bin/backup-to-usb.sh"
echo "  /usr/local/bin/update-backup-status.sh"
echo "  /usr/local/bin/setup-usb-backup.sh"
echo "  /usr/local/bin/sync-backup-schedule.sh"
echo "  /etc/cron.d/barenoc-backup"
echo
cat /etc/cron.d/barenoc-backup
