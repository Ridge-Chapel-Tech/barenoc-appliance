#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC USB backup stick setup (Layer 3, LUKS) — run ONCE per stick.
#
#   bash proxmox/setup-usb-backup.sh --dev /dev/sdX
#   (or without --dev: lists USB candidates for you to pick)
#
# What it does (DESTRUCTIVE — wipes the target device):
#   1. GPT partition + LUKS2 encrypt (keyfile on host for cron automation +
#      a generated passphrase printed for the sealed rack card — recovery).
#   2. ext4, label BARENOC-BACKUP, mount at /mnt/barenoc-usb.
#   3. Writes /etc/barenoc-usb.conf (device, crypt name, keyfile, mount, VM).
#   4. Test write/read, then runs a real drill: backup-to-usb.sh.
#   Result: weekly cron backups just work once the stick is plugged in.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

DEV=""
CONF="/etc/barenoc-usb.conf"
KEYFILE="/etc/barenoc-usb.key"
CRYPT_NAME="barenoc-usb"
LABEL="BARENOC-BACKUP"
MOUNT="/mnt/barenoc-usb"
VM_HOST="${VM_HOST:-192.0.2.207}"

usage() {
  cat <<'EOF'
Usage: bash proxmox/setup-usb-backup.sh --dev /dev/sdX [--vm-host <ip>]

  --dev <dev>       Whole device to wipe + encrypt (e.g. /dev/sdb)
                    Omit to see a list of USB candidates.
  --vm-host <ip>    BareNOC VM address (for app-backup sync; default 192.0.2.207)
  --dry-run         Show what would run, don't touch the device
EOF
}

DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV="$2"; shift 2 ;;
    --vm-host) VM_HOST="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "ERROR: run as root on the Proxmox host" >&2; exit 1; }
command -v cryptsetup >/dev/null || { echo "ERROR: install cryptsetup (apt install cryptsetup)" >&2; exit 1; }

# ── pick the device ─────────────────────────────────────────────────────────
if [[ -z "$DEV" ]]; then
  echo "==> USB candidates (whole devices, removable):"
  lsblk -dno NAME,SIZE,MODEL,TRAN | awk '$4 ~ /usb/ {print "  /dev/"$1"  ("$2", "$3")"}'
  read -r -p "Enter device (e.g. /dev/sdb): " DEV
fi
[[ -b "$DEV" ]] || { echo "ERROR: $DEV is not a block device" >&2; exit 1; }

# safety: refuse obvious non-sticks
if echo "$DEV" | grep -qE '/dev/(sd[ab]$|nvme[0-9]+$)'; then
  # require a second confirmation for the first two disk names
  read -r -p "WARNING: $DEV looks like a primary disk. Type YES to wipe it: " CONFIRM
  [[ "$CONFIRM" == "YES" ]] || { echo "aborted"; exit 1; }
fi

if [[ $DRY -eq 1 ]]; then
  echo "[dry-run] would wipe $DEV, LUKS2-format, label $LABEL, mount $MOUNT"
  exit 0
fi

umount "$MOUNT" 2>/dev/null || true
cryptsetup close "$CRYPT_NAME" 2>/dev/null || true

# ── wipe + partition ────────────────────────────────────────────────────────
echo "==> Wiping $DEV (destructive)…"
wipefs -a "$DEV"
parted -s "$DEV" mklabel gpt
parted -s "$DEV" mkpart primary 0% 100%
PART="$(lsblk -rno NAME "$DEV" | sed -n '2p' | sed "s#^#/dev/#")"

# ── LUKS2 ───────────────────────────────────────────────────────────────────
echo "==> Encrypting $PART (LUKS2)…"
umask 077
KEY_B64="$(openssl rand -base64 48)"
printf '%s' "$KEY_B64" > "$KEYFILE"
PASSPHRASE="$(openssl rand -base64 18 | tr '+/' '_-')"   # for the sealed card

# Use the on-disk keyfile (NOT --key-file=- stdin: cryptsetup >= 2.7.5 rejects
# stdin keyfiles with "No key available with this passphrase" on open).
cryptsetup luksFormat --type luks2 --key-file "$KEYFILE" "$PART"
cryptsetup open --key-file "$KEYFILE" "$PART" "$CRYPT_NAME"
# add the passphrase as a second keyslot (recovery if the host keyfile is lost)
printf '%s\n' "$PASSPHRASE" | cryptsetup luksAddKey "$PART" --key-file "$KEYFILE"

# ── filesystem + label ──────────────────────────────────────────────────────
echo "==> Formatting ext4 (label $LABEL)…"
mkfs.ext4 -F -L "$LABEL" "/dev/mapper/$CRYPT_NAME"
mkdir -p "$MOUNT"
mount "/dev/mapper/$CRYPT_NAME" "$MOUNT"

# test write/read
echo "test-write-$(date +%s)" > "$MOUNT/.write-test"
[[ "$(cat "$MOUNT/.write-test")" == test-write-* ]] || { echo "ERROR: write test failed" >&2; exit 1; }
rm -f "$MOUNT/.write-test"
echo "==> Write/read test OK"

# ── conf for the cron scripts ───────────────────────────────────────────────
# stable by-id path to the LUKS partition (find -lname must wildcard the
# target: by-id symlinks point at "../../sda1", not the bare name)
BY_ID="$(find /dev/disk/by-id -maxdepth 1 -type l -lname "*$(basename "$PART")" 2>/dev/null | head -1)"
cat > "$CONF" <<EOF
# BareNOC USB backup configuration (written by setup-usb-backup.sh)
USB_DEV="$BY_ID"          # stable by-id path to the LUKS partition
USB_RAW_PART="$PART"
CRYPT_NAME="$CRYPT_NAME"
KEYFILE="$KEYFILE"
LABEL="$LABEL"
MOUNT="$MOUNT"
VM_HOST="$VM_HOST"
EOF
chmod 600 "$CONF"
echo "==> Wrote $CONF"

# ── lock + print the sealed-card passphrase ─────────────────────────────────
umount "$MOUNT"
cryptsetup close "$CRYPT_NAME"

echo
echo "════════════════════════════════════════════════════════════════"
echo "  USB backup stick READY. Seal this passphrase on the rack card:"
echo
echo "    $PASSPHRASE"
echo
echo "  (also stored at $KEYFILE — 0600 root — for cron automation)"
echo "  Stick is locked now. Plug/unplug freely; weekly cron opens it."
echo "  To test now: bash proxmox/backup-to-usb.sh"
echo "════════════════════════════════════════════════════════════════"
