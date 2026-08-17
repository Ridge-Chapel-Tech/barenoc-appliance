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
BASE_ISO_URL="https://releases.ubuntu.com/noble/ubuntu-24.04.4-live-server-amd64.iso"
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
# The remaster needs -appended_part_as_gpt (the modern hybrid-ISO layout; the
# old -appended_part_type option doesn't exist in this xorriso).
if ! xorriso -as mkisofs -help 2>&1 | grep -q 'appended_part_as_gpt'; then
  echo "ERROR: xorriso is too old (needs -appended_part_as_gpt, >= 1.5.6). " >&2
  echo "       Build on Ubuntu 24.04 (apt install xorriso) or upgrade xorriso." >&2
  exit 1
fi
[[ -f "$SSH_KEY" || -z "$SSH_KEY" ]] || { echo "ERROR: ssh key not found: $SSH_KEY" >&2; exit 1; }
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 12 | tr '+/' '_-')}"
# print early: a later build step (xorriso MISHAP under set -e) can abort
# before the final banner, which would lose the auto-generated password.
echo "Admin password: $ADMIN_PASSWORD"

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

# The tarball is ~1MB base64 — a single `echo '<b64>'` shell arg exceeds the
# kernel ARG_MAX and aborts the install (E2BIG "Argument list too long:
# systemd-cat" — found 08-14). Split into 60KB chunks; each chunk is a
# whole number of base64 groups (60000 % 4 == 0), decoded and appended
# in-target, so the final /tmp/barenoc-app.tar.gz is the full tarball.
CHUNK_CMDS=""
APP_B64_REMAIN="$APP_TARBALL_B64"
while [[ -n "$APP_B64_REMAIN" ]]; do
  CHUNK="${APP_B64_REMAIN:0:60000}"
  APP_B64_REMAIN="${APP_B64_REMAIN:60000}"
  CHUNK_CMDS+="    - curtin in-target -- bash -c \"echo '$CHUNK' | base64 -d >> /tmp/barenoc-app.tar.gz\""$'\n'
done

# ── 3. provisioning script (first boot, after OS install) ──────────────────
read -r -d '' PROVISION <<'PROVEOF' || true
#!/bin/bash
set -euxo pipefail
exec > /var/log/barenoc-provision.log 2>&1
echo "[provision] starting $(date -Is)"

# Force IPv4 for apt: on IPv4-only LANs (no IPv6 route) apt can stall on the
# mirrors' AAAA records before falling back (seen 08-14: the installer's
# security-updates stage took ~30 min; the ISO kernel arg ipv6.disable=1
# covers the installer env, this covers the installed system).
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

# Docker Engine (official repo) + agent tooling + guest agent
apt-get update -qq
apt-get install -y -qq ca-certificates curl jq qemu-guest-agent sqlite3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin \
  nmap snmp snmp-mibs-downloader sqlite3
usermod -aG docker barenoc

# /opt/barenoc landed root-owned during install (the barenoc user is created
# by cloud-init at first boot, AFTER the installer's late-commands ran); hand
# the tree over now that the user exists.
chown -R barenoc:barenoc /opt/barenoc || true

# app-data backup: the appliance keeps its own data safe automatically
# (settings, tickets, config) every 6h — the deploy installs this cron on
# existing installs; a fresh ISO install gets it here. No host machinery
# needed (that's only for USB/snapshot layers, Settings → Backups).
mkdir -p /opt/barenoc/backups
crontab -u barenoc -l 2>/dev/null | grep -q backup_app.sh || \
  (crontab -u barenoc -l 2>/dev/null; echo '0 */6 * * * /opt/barenoc/scripts/backup_app.sh >> /opt/barenoc/backups/backup.log 2>&1') | crontab -u barenoc -
chmod 700 /opt/barenoc/backups

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

# nocloud seed needs meta-data alongside user-data: DataSourceNoCloud FAILS
# without it (found 08-14 live: cloud-init logged "Getting data from
# DataSourceNoCloud failed / Used fallback datasource" -> subiquity saw no
# autoinstall in the merged cloud-config -> interactive language screen).
cat > "$WORK/isofs/autoinstall/meta-data" <<EOF
instance-id: barenoc-iso-${VERSION}
local-hostname: barenoc
EOF

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
    # embedded application tarball → /opt/barenoc (deploy.sh contents),
    # in 60KB base64 chunks (single-arg embedding hit ARG_MAX/E2BIG)
${CHUNK_CMDS}    - curtin in-target -- bash -c "mkdir -p /opt/barenoc && tar xzf /tmp/barenoc-app.tar.gz -C /opt/barenoc && rm -f /tmp/barenoc-app.tar.gz"
    # Bootable under OVMF: the installer chroot has no efivarfs, so grub cannot
    # write an NVRAM boot entry — and OVMF refuses to boot a fixed disk without
    # one (found 08-14: installs completed but every reboot fell back to the
    # ISO; the disk was bootable, the firmware just had no entry). Bind-mount
    # the live env's efivars into the target and re-run grub-install so the
    # 'ubuntu' entry is registered in the VM's NVRAM; --removable also leaves
    # EFI/BOOT/BOOTX64.EFI on the ESP as a firmware fallback.
    #
    # 2026-08-17: this re-install is now CONDITIONAL — curtin's own
    # install-grub hook succeeds on current builds (grub-efi-amd64-signed +
    # shim-signed + NVRAM entries registered) and the unconditional re-run was
    # failing with exit 3 (grub-install error), killing the install. Fall back
    # to the 08-14 re-install only when the 'ubuntu' NVRAM entry is missing.
    - mkdir -p /target/sys/firmware/efi/efivars && mount --bind /sys/firmware/efi/efivars /target/sys/firmware/efi/efivars
    - curtin in-target -- bash -c "if ! efibootmgr 2>/dev/null | grep -qi ubuntu; then grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --removable && grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu && update-grub; fi"
    # first-boot unit: run provisioning + app deploy once (needs network)
    # NOTE: After=cloud-init.target — the barenoc identity user is created by
    # cloud-init at first boot, not during install (late-commands found no
    # such user — 08-14); the provision script's usermod/chown need it.
    # NOTE: YAML literal block (- |) — the unit file contains line breaks;
    # a plain scalar would break YAML (found 08-14: bad seed => subiquity
    # silently fell back to the interactive language screen).
    - |
      curtin in-target -- bash -c "cat > /etc/systemd/system/barenoc-firstboot.service <<'UNIT'
      [Unit]
      Description=BareNOC first-boot (provision + app deploy)
      After=network-online.target docker.service cloud-init.target
      Wants=network-online.target

      [Service]
      Type=oneshot
      RemainAfterExit=yes
      ExecStart=/bin/bash -c 'test -f /opt/barenoc/.provisioned || bash /opt/barenoc-provision.sh; cd /opt/barenoc && docker compose up --build -d && bash scripts/setup_agent_credentials.sh && systemctl enable --now pi-agent-runner && install -m 0755 /opt/barenoc/scripts/barenoc-self-update.sh /usr/local/bin/barenoc-self-update.sh && install -m 0644 /opt/barenoc/scripts/barenoc-self-update.service /etc/systemd/system/ && install -m 0644 /opt/barenoc/scripts/barenoc-self-update.path /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now barenoc-self-update.path'

      [Install]
      WantedBy=multi-user.target
      UNIT
      systemctl enable barenoc-firstboot.service"
EOF

# grub kernel line: add autoinstall + NoCloud seed path. NOTE: the
# 24.04.2/.4 live-server grub.cfg has NO quiet/splash — it uses bare
# `linux /casper/vmlinuz  ---` lines, so a quiet/splash replace was a silent
# no-op (found 08-14: first ISO boot went interactive instead of autoinstall
# and never installed). Patch the vmlinuz lines directly, inserting the args
# before the `---` separator.
python3 - "$WORK/isofs" <<'PYEOF'
import os, sys, re
root = sys.argv[1]
grub = os.path.join(root, "boot/grub/grub.cfg")
s = open(grub).read()
extra = "autoinstall ds=nocloud\\;s=/cdrom/autoinstall/ ipv6.disable=1"
if "autoinstall" not in s:
    n = len(re.findall(r"linux\s+/casper/[a-z-]*vmlinuz\s+---", s))
    s = re.sub(r"(linux\s+/casper/[a-z-]*vmlinuz\s+)---", r"\1%s ---" % extra, s)
    open(grub, "w").write(s)
    print("grub.cfg patched (%d vmlinuz entries)" % n)
else:
    print("grub.cfg already has autoinstall")
PYEOF
# keep the md5 tree consistent so grub's find-based loopback works
(cd "$WORK/isofs" && find . -type f -print0 | xargs -0 md5sum > md5sum.txt)

# ── 5. remaster ────────────────────────────────────────────────────────────
mkdir -p "$OUT_DIR"
# The Ubuntu live-server ISO carries its EFI System Partition as a HIDDEN
# El-Torito image inside the ISO (not as an appended partition). Extract that
# image with dd (start/size reported by xorriso) and append it as the new
# ISO's partition 2 so EFI firmware can boot the remaster. (The old recipe
# rebuilt the boot structure by hand with --grub2-mbr + -appended_part_type;
# that option doesn't exist and the grub2-mbr partition math failed.)
ET="$(xorriso -indev "$BASE_ISO" -report_el_torito 2>&1 | \
  grep 'EFI image start and size' | head -1)"
EFI_START="$(echo "$ET" | sed -E 's/.*size: ([0-9]+) \* 2048.*/\1/')"
EFI_SIZE="$(echo "$ET" | sed -E 's/.*\* 2048 , ([0-9]+) \* 512.*/\1/')"
if [[ -z "$EFI_START" || -z "$EFI_SIZE" ]]; then
  echo "ERROR: could not locate the hidden EFI image in the base ISO" >&2
  exit 1
fi
dd if="$BASE_ISO" of="$WORK/efi.img" bs=2048 skip="$EFI_START" count=$((EFI_SIZE / 4)) 2>/dev/null
echo "==> Building $OUT_ISO (≈3 GB, ~2-4 min)…"
xorriso -as mkisofs -r -V "BARENOC-$VERSION" -o "$OUT_ISO" \
  --modification-date="$(date +%Y%m%d%H%M%S00)" \
  -partition_offset 16 \
  -append_partition 2 28712ac1-11ff-d211-77f0-da4ac1ce14e1 "$WORK/efi.img" \
  -appended_part_as_gpt \
  -eltorito-alt-boot -e '--interval:appended_partition_2:all::' -no-emul-boot \
  -boot-load-size 4 \
  --protective-msdos-label \
  "$WORK/isofs"

echo
echo "════════════════════════════════════════════════════════════════"
echo "  ISO built: $OUT_ISO"
echo "  sha256:    $(sha256sum "$OUT_ISO" | awk '{print $1}')"
echo "  Admin:     admin / $ADMIN_PASSWORD   (CHANGE IT on first login)"
echo
echo "  Try it (UEFI/OVMF REQUIRED — the remaster has no legacy BIOS boot path):"
echo "    qm create 1001 --memory 4096 --cores 2 --net0 virtio,bridge=vmbr0 --bios ovmf --efidisk0 local-lvm:4,efitype=4m,pre-enrolled-keys=0"
echo "    qm set 1001 --ide2 local:iso/barenoc-$VERSION.iso,media=cdrom"
echo "    qm set 1001 --boot order=ide2 --scsi0 local-lvm:40"
echo "    qm start 1001"
echo "  After the install completes: detach the ISO (qm set 1001 --ide2 none) and reboot."
echo "  (or boot it in any UEFI VM/hypervisor — install is fully unattended)"
echo "════════════════════════════════════════════════════════════════"
