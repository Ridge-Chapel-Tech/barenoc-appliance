# Proxmox VE Setup

**Version:** 1.0  
**Last Updated:** 2025-07-29  
**Target:** Mini PC with Proxmox VE 9.2 installed

---

## Prerequisites

- Proxmox VE 9.2 installed on the target machine (see `hardware_bom.md` for hardware)
- Root access to Proxmox host via SSH or web UI
- Network: Static IP configured on the MGMT VLAN

---

## 1. Post-Install Verification

After Proxmox installation, verify:

```bash
# Check Proxmox version
pveversion

# Check networking
ip a show vmbr0
cat /etc/network/interfaces

# Check storage
zpool status
pvesm status

# Check web UI access
# Open browser to https://192.0.2.95:8006
```

---

## 2. Network Configuration

Proxmox creates a `vmbr0` bridge during installation. Verify it:

```bash
# /etc/network/interfaces should look like:
auto lo
iface lo inet loopback

auto enp1s0
iface enp1s0 inet manual

auto vmbr0
iface vmbr0 inet static
    address 192.0.2.95/24
    gateway 192.0.2.1
    bridge-ports enp1s0
    bridge-stp off
    bridge-fd 0
```

If you need a second bridge for the MGMT VLAN (separate from the Proxmox management IP):

```bash
# Add to /etc/network/interfaces:
auto enp2s0
iface enp2s0 inet manual

auto vmbr1
iface vmbr1 inet manual
    bridge-ports enp2s0
    bridge-stp off
    bridge-fd 0
```

This allows VMs on `vmbr1` to access the MGMT VLAN directly.

---

## 3. Storage Configuration

### ZFS Pool

The installer sets up a ZFS pool named `rpool`. Verify:

```bash
zpool list
zfs list

# Enable compression (already on by default in Proxmox 8+)
zfs get compression rpool

# Enable auto-snapshots
pveam update
```

### Additional Storage (Optional)

If you added an external USB SSD for backups:

```bash
# Mount USB drive
mkdir -p /mnt/backup
mount /dev/sdb1 /mnt/backup

# Add to Proxmox storage
pvesm add dir backup --path /mnt/backup --content backup
```

---

## 4. Proxmox Optimization for the Appliance

### Disable Enterprise Repository

```bash
# Comment out enterprise repo
sed -i 's/^deb/#deb/' /etc/apt/sources.list.d/pve-enterprise.list

# Add community (no-subscription) repo
echo "deb http://download.proxmox.com/debian/pve bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve-no-subscription.list

# Update
apt update && apt upgrade -y
```

### Set Timezone

```bash
timedatectl set-timezone America/New_York  # adjust for customer region
timedatectl set-ntp true
```

### Set Up Email Alerts (Proxmox Host)

```bash
# Install Postfix for local delivery
apt install -y postfix

# Configure to relay through BareNOC's SMTP (or directly via Gmail)
# Edit /etc/postfix/main.cf:
# relayhost = 192.0.2.207:587
```

### Enable Automatic Updates (Optional)

```bash
# Install unattended-upgrades
apt install -y unattended-upgrades
dpkg-reconfigure --priority=low unattended-upgrades
```

---

## 5. Create the BareNOC VM

See [`barenoc_vm_create.md`](./barenoc_vm_create.md) for full VM provisioning steps.

### Quick Reference

```bash
# Download Ubuntu Server 24.04 cloud image
wget -P /var/lib/vz/template/iso/ \
    https://releases.ubuntu.com/24.04/ubuntu-24.04-live-server-amd64.iso

# Create VM (adjust IDs as needed)
qm create 100 \
    --name barenoc \
    --memory 4096 \
    --cores 2 \
    --net0 virtio,bridge=vmbr0 \
    --ostype l26 \
    --cdrom local:iso/ubuntu-24.04-live-server-amd64.iso \
    --scsihw virtio-scsi-pci \
    --boot order=scsi0

# Attach disk (40 GB)
qm set 100 --scsi0 local:40,format=qcow2

# Start VM and install Ubuntu via VNC console
qm start 100
```

---

## 6. Snapshot Management

Snapshots are critical for the trial lifecycle:

```bash
# Before shipping
qm snapshot 100 pre-ship

# After customer returns, roll back
qm rollback 100 pre-ship

# Delete old snapshots
qm delsnapshot 100 pre-ship

# List snapshots
qm listsnapshot 100
```

---

## 7. Backup Schedule

Add to Proxmox host crontab:

```cron
# Daily VM backup at 2 AM, keep 7 days
0 2 * * * vzdump 100 --dumpdir /var/lib/vz/dump --mode snapshot --compress zstd --remove 0 --rotate 7

# Weekly full backup to external USB
0 3 * * 0 vzdump 100 --dumpdir /mnt/backup --mode stop --compress zstd
```

---

## 8. Firewall

Proxmox comes with its own firewall. At minimum:

```bash
# Enable Proxmox firewall
pve-firewall start
systemctl enable pve-firewall

# Allow web UI and SSH from MGMT VLAN only
# Edit /etc/pve/firewall/host.fw:
# [RULES]
# IN SSH(ACCEPT) -source 10.X.10.0/24
# IN HTTPS(ACCEPT) -source 10.X.0.0/16
```
