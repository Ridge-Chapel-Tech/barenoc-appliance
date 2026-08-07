#!/bin/bash
# fix-device-sudoers.sh — repair /etc/sudoers.d/barenoc on THIS device.
#
# Older onboarding (and any old saved copy of the os-setup commands) wrote
# bare command names ("reboot, shutdown, apt, ..."). sudoers requires
# FULLY-QUALIFIED paths — a bare-name entry is a parse error, so the barenoc
# user gets NO passwordless sudo at all (every control action fails with
# "sudo: a password is required").
#
# This rewrites the entry with full paths and validates it. Safe to re-run
# (idempotent). Keep SUDO_SCOPED in src/api/routes/onboard.py in sync.
#
# Run on the device:  sudo bash fix-device-sudoers.sh
set -e

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash fix-device-sudoers.sh" >&2; exit 1; }

LINE='barenoc ALL=(ALL) NOPASSWD: /usr/bin/cp, /usr/sbin/reboot, /usr/sbin/shutdown, /usr/bin/apt, /usr/bin/apt-get, /usr/bin/dnf, /usr/bin/yum, /usr/bin/apk, /usr/bin/zypper, /usr/bin/journalctl, /usr/bin/log, /usr/bin/install, /usr/bin/systemctl, /usr/bin/tail, /usr/bin/curl'

printf '%s\n' "$LINE" > /etc/sudoers.d/barenoc
chmod 440 /etc/sudoers.d/barenoc
chown root:root /etc/sudoers.d/barenoc

visudo -c
echo "OK — /etc/sudoers.d/barenoc is valid. barenoc can now sudo the scoped commands passwordlessly."
