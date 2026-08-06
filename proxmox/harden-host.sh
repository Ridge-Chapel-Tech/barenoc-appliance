#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Proxmox host hardening — idempotent, run once per host (as root).
#
#   scp proxmox/harden-host.sh root@<host>:/tmp/ && ssh root@<host> 'bash /tmp/harden-host.sh <pubkey-file>'
#
# What it does:
#   1. Installs the operator's SSH public key for root (key-based root login).
#   2. Creates an `ops` sudo user with the same key (daily driver — never root).
#   3. Locks ROOT PASSWORD login (PermitRootLogin prohibit-password) so the
#      root password can't be used over SSH — keys only. (Password auth for
#      non-root users unchanged; tailnet source restriction is the next layer.)
#   4. Prints the hardened posture.
#
# Prod policy (see docs/runbook/remote_access.md):
#   - BareNOC-the-app NEVER holds host credentials (one-directional status push).
#   - Ops uses the tailnet for SSH once Tailscale is up; TOTP on the web UI.
#   - The root password lives in the password manager + sealed rack card, never
#     in SESSION_LOG / .env / the repo.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

KEY_FILE="${1:-/dev/stdin}"   # public key file (or pipe it in)
KEY="$(cat "$KEY_FILE" | tr -d '\n')"
[[ "$KEY" == ssh-* ]] || { echo "ERROR: argument must be an SSH public key file (starts with ssh-)" >&2; exit 1; }

echo "==> Installing operator key for root"
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys
grep -qF "$KEY" /root/.ssh/authorized_keys || echo "$KEY" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

echo "==> Creating/updating 'ops' sudo user (same key)"
if id ops >/dev/null 2>&1; then
  install -d -m 700 /home/ops/.ssh
  touch /home/ops/.ssh/authorized_keys
  grep -qF "$KEY" /home/ops/.ssh/authorized_keys || echo "$KEY" >> /home/ops/.ssh/authorized_keys
  chmod 600 /home/ops/.ssh/authorized_keys
  chown -R ops:ops /home/ops/.ssh
else
  useradd -m -s /bin/bash ops
  echo "ops ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ops
  chmod 440 /etc/sudoers.d/ops
  install -d -m 700 /home/ops/.ssh
  echo "$KEY" > /home/ops/.ssh/authorized_keys
  chmod 600 /home/ops/.ssh/authorized_keys
  chown -R ops:ops /home/ops/.ssh
fi

echo "==> Root password login -> prohibit-password (keys only)"
if ! grep -qE '^PermitRootLogin (prohibit-password|yes)' /etc/ssh/sshd_config; then
  cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak-$(date +%Y%m%d)
fi
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin prohibit-password' >> /etc/ssh/sshd_config
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

echo
echo "==> Hardened: $(hostname)"
echo "   root: key-only SSH · ops: sudo via key · root password now inert over SSH"
echo "   verify: ssh ops@$(hostname -I | awk '{print $1}')"
