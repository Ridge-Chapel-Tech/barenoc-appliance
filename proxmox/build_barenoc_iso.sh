#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC ISO builder — remasters the official Ubuntu Server 24.04 ISO with
# the BareNOC autoinstall seed + embedded application tarball.
#
#   bash proxmox/build_barenoc_iso.sh --base-iso ubuntu-24.04.2-live-server-amd64.iso
#
# Produces dist/barenoc-<version>.iso — boot a fresh VM with it attached and
# the appliance installs itself (base OS + Docker + pi runtime + app), no
# manual steps. Needs internet on first install (base OS packages + pip deps);
# afterwards the VM's disk image is the portable artifact (or snapshot it).
#
# Requirements (host): xorriso, openssl, base Ubuntu 24.04 live-server ISO.
#   sudo apt install xorriso
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

BASE_ISO=""
BASE_ISO_URL="https://releases.ubuntu.com/noble/ubuntu-24.04.2-live-server-amd64.iso"
SSH_KEY="${SSH_KEY:-}"        # optional .pub to inject for the barenoc user
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"  # optional; auto-generated if unset

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(python3 -c "import sys; sys.path.insert(0,'$REPO/src/api'); import version; print(version.APP_VERSION)")"
OUT_DIR="$REPO/dist"
OUT_ISO="$OUT_DIR/barenoc-$VERSION.iso"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

usage() { cat <<EOF
Usage: bash proxmox/build_barenoc_iso.sh [--base-iso <path-or-url>] [--ssh-key <pub>] [--admin-password <pw>]

  --base-iso         Path or URL to ubuntu-24.04 live-server amd64 ISO
                     (default: download $BASE_ISO_URL)
  --ssh-key          .pub key to authorize for the barenoc user (optional)
  --admin-password   Seed admin password (default: auto-generated, printed)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-iso) BASE_ISO="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

command -v xorriso >/dev/null || { echo "ERROR: xorriso not installed (sudo apt install xorriso)" >&2; exit 1; }
[[ -f "$SSH_KEY" || -z "$SSH_KEY" ]] || { echo "ERROR: ssh key not found: $SSH_KEY" >&2; exit 1; }
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 12 | tr '+/' '_-')}"

# ── 1. base ISO ────────────────────────────────────────────────────────────
if [[ -z "$BASE_ISO" ]]; then
  BASE_ISO="$OUT_DIR/$(basename "$BASE_ISO_URL")"
  mkdir -p "$OUT_DIR"
  [[ -f "$BASE_ISO" ]] || { echo "==> Downloading base Ubuntu ISO (≈2.6 GB, once)…"; curl -fL "$BASE_ISO_URL" -o "$BASE_ISO"; }
elif [[ "$BASE_ISO" == http* ]]; then
  mkdir -p "$OUT_DIR"
  curl -fL "$BASE_ISO" -o "$OUT_DIR/$(basename "$BASE_ISO")"
  BASE_ISO="$OUT_DIR/$(basename "$BASE_ISO")"
fi
[[ -f "$BASE_ISO" ]] || { echo "ERROR: base ISO not found: $BASE_ISO" >&2; exit 1; }

# ── 2. app tarball (embedded in the ISO, extracted on first boot) ──────────
APP_TARBALL="$WORK/barenoc-app.tar.gz"
tar czf "$APP_TARBALL" -C "$REPO" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='client/build' --exclude='client/dist' \
  --exclude='.git' src client deploy.sh
APP_TARBALL_B64="$(base64 -w0 "$APP_TARBALL")"

# ── 3. provisioning script (first boot, after OS install) ──────────────────
read -r -d '' PROVISION <<'PROVEOF' || true
#!/bin/bash
set -euxo pipefail
exec > /var/log/barenoc-provision.log 2>&1
echo "[provision] starting $(date -Is)"

# Docker Engine (official repo) + agent tooling + guest agent
apt-get update -qq
apt-get install -y -qq ca-certificates curl jq qemu-guest-agent
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin \
  nmap snmp snmp-mibs-downloader
usermod -aG docker barenoc

# pi-agent + Pi Coding Agent runtime
useradd -r -m -s /bin/bash pi-agent
PI_NODE_DIR=/home/pi-agent/.local/share/pi-node
mkdir -p "$PI_NODE_DIR"
curl -fsSL https://nodejs.org/dist/v22.23.1/node-v22.23.1-linux-x64.tar.xz -o /tmp/node.tar.xz
tar -xJf /tmp/node.tar.xz -C "$PI_NODE_DIR"
ln -sfn "$PI_NODE_DIR/node-v22.23.1-linux-x64" "$PI_NODE_DIR/current"
PATH="$PI_NODE_DIR/current/bin:$PATH" npm install -g --no-fund --no-audit @earendil-works/pi-coding-agent@0.83.0
chown -R pi-agent:pi-agent "$PI_NODE_DIR"
rm -f /tmp/node.tar.xz

# firewalls: ssh + 443 (+8443 Pocket ID)
ufw --force reset; ufw default deny incoming; ufw default allow outgoing
ufw allow 22/tcp; ufw allow 443/tcp; ufw allow 8443/tcp; ufw --force enable

# ready marker (the first-boot unit also deploys the app, see below)
touch /opt/barenoc/.provisioned
echo "[provision] done $(date -Is)"
PROVEOF
PROVISION_B64="$(printf '%s' "$PROVISION" | base64 -w0)"

# ── 4. extract + inject the autoinstall seed ───────────────────────────────
echo "==> Extracting base ISO (≈1 min)…"
xorriso -osirrox on -indev "$BASE_ISO" -extract / "$WORK/isofs" >/dev/null
chmod -R +w "$WORK/isofs"
mkdir -p "$WORK/isofs/autoinstall"

SSH_LINE=""
if [[ -n "$SSH_KEY" ]]; then SSH_LINE="    ssh_authorized_keys: [ $(tr -d '\n' < "$SSH_KEY") ]"; fi
PW_HASH="$(openssl passwd -6 "$ADMIN_PASSWORD")"

cat > "$WORK/isofs/autoinstall/user-data" <<EOF
#cloud-config
autoinstall:
  version: 1
  locale: en_US.UTF-8
  keyboard:
    layout: us
  identity:
    hostname: barenoc
    username: barenoc
    password: "$PW_HASH"
  ssh:
    install-server: true
    allow-pubkey: true
$SSH_LINE
  packages:
    - openssh-server
  late-commands:
    # embedded provisioning script (base OS bits) — runs on first boot
    - curtin in-target -- bash -c "echo '$PROVISION_B64' | base64 -d > /opt/barenoc-provision.sh && chmod +x /opt/barenoc-provision.sh"
    # embedded application tarball → /opt/barenoc (deploy.sh contents)
    - curtin in-target -- bash -c "install -d -o barenoc -g barenoc /opt/barenoc && echo '$APP_TARBALL_B64' | base64 -d > /tmp/barenoc-app.tar.gz && tar xzf /tmp/barenoc-app.tar.gz -C /opt/barenoc && rm -f /tmp/barenoc-app.tar.gz"
    # first-boot unit: run provisioning + app deploy once (needs network)
    - curtin in-target -- bash -c "cat > /etc/systemd/system/barenoc-firstboot.service <<'UNIT'
[Unit]
Description=BareNOC first-boot (provision + app deploy)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'test -f /opt/barenoc/.provisioned || bash /opt/barenoc-provision.sh; cd /opt/barenoc && docker compose up --build -d && bash scripts/setup_agent_credentials.sh && systemctl enable --now pi-agent-runner'

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable barenoc-firstboot.service"
EOF

# grub kernel line: add autoinstall + NoCloud seed path
python3 - "$WORK/isofs" <<'PYEOF'
import os, sys, re
root = sys.argv[1]
grub = os.path.join(root, "boot/grub/grub.cfg")
s = open(grub).read()
extra = " autoinstall ds=nocloud\\;s=/cdrom/autoinstall/"
if "autoinstall" not in s:
    s = s.replace("quiet", "quiet" + extra)
    s = s.replace("splash", "splash" + extra)
    open(grub, "w").write(s)
    print("grub.cfg patched")
else:
    print("grub.cfg already has autoinstall")
PYEOF
# keep the md5 tree consistent so grub's find-based loopback works
(cd "$WORK/isofs" && find . -type f -print0 | xargs -0 md5sum > md5sum.txt)

# ── 5. remaster ────────────────────────────────────────────────────────────
mkdir -p "$OUT_DIR"
echo "==> Building $OUT_ISO (≈3 GB, ~2-4 min)…"
xorriso -as mkisofs -r -V "BARENOC-$VERSION" -o "$OUT_ISO" \
  --modification-date="$(date +%Y%m%d%H%M%S00)" \
  --grub2-mbr "$WORK/isofs/usr/lib/grub/i386-pc/boot_hybrid.img" \
  -partition_offset 16 --mbr-force-bootable \
  -append_partition 2 28712ac1-11ff-d211-77f0-da4ac1ce14e1 \
  -appended_part_type 21686148-6449-6e6f-744e-6564-4546-464655 \
  -eltorito-alt-boot -e '--interval:appended_partition_2:all::' -no-emul-boot \
  -boot-load-size 4 -boot-info-table \
  "$WORK/isofs"

echo
echo "════════════════════════════════════════════════════════════════"
echo "  ISO built: $OUT_ISO"
echo "  sha256:    $(sha256sum "$OUT_ISO" | awk '{print $1}')"
echo "  Admin:     admin / $ADMIN_PASSWORD   (CHANGE IT on first login)"
echo
echo "  Try it:"
echo "    qm create 1001 --memory 4096 --cores 2 --net0 virtio,bridge=vmbr0"
echo "    qm set 1001 --ide2 local:iso/barenoc-$VERSION.iso,media=cdrom"
echo "    qm set 1001 --boot order=ide2 --scsi0 local-lvm:40"
echo "    qm start 1001"
echo "  (or boot it in any VM/hypervisor — install is fully unattended)"
echo "════════════════════════════════════════════════════════════════"
