# Creating the BareNOC VM

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Prerequisites

- Proxmox VE installed and web UI accessible at `https://192.0.2.95:8006`
- Ubuntu Server 24.04 LTS ISO downloaded to Proxmox storage
- Internet connectivity on the Proxmox host

---

## Step 1: Download Ubuntu ISO

```bash
# Via Proxmox web UI: Storage → local → ISO Images → Upload
# Or via CLI:
wget -P /var/lib/vz/template/iso/ \
    https://releases.ubuntu.com/24.04/ubuntu-24.04-live-server-amd64.iso
```

---

## Step 2: Create the VM

### Via CLI

```bash
# Create VM with these specs
VM_ID=100
VM_NAME="barenoc"

qm create $VM_ID \
    --name $VM_NAME \
    --memory 4096 \
    --cores 2 \
    --cpu host \
    --net0 virtio,bridge=vmbr0 \
    --ostype l26 \
    --cdrom local:iso/ubuntu-24.04-live-server-amd64.iso \
    --scsihw virtio-scsi-pci \
    --boot order=scsi0 \
    --agent 1 \                    # QEMU guest agent
    --onboot 1                     # Auto-start on Proxmox boot

# Attach disk
qm set $VM_ID --scsi0 local:40,format=qcow2
```

### Via Web UI

```
Datacenter → [Your Node] → Create VM
  General:
    VM ID: 100
    Name:  barenoc
  OS:
    ISO:  ubuntu-24.04-live-server-amd64.iso
  System:
    Graphics:  VirtIO-GPU
    SCSI Controller:  VirtIO SCSI single
  Disks:
    Bus/Device:  SCSI 0
    Storage:     local
    Disk size:   40 GB
  CPU:
    Cores:  2
    Type:   host
  Memory:
    Memory:  4096 MB
  Network:
    Bridge:  vmbr0
    Model:   VirtIO
  Confirm
```

---

## Step 3: Install Ubuntu Server

1. **Start the VM** → click **Console** in Proxmox
2. Follow Ubuntu installer prompts:
   - Language: English
   - Keyboard: US
   - Network: DHCP (we'll set static later)
   - Storage: **Use entire disk** (40 GB)
   - Profile:
     - Name: `BareNOC Admin`
     - Server name: `barenoc`
     - Username: `barenoc`
     - Password: (generate a strong one)
   - SSH Setup: **Install OpenSSH server**
   - Import SSH key: (paste your public key)
   - No snap packages
3. Reboot when prompted

---

## Step 4: Post-Install Configuration

### SSH into the VM

```bash
# From Proxmox host
ssh barenoc@192.0.2.207

# Or from Proxmox console
qm terminal 100
```

### Set Static IP

```bash
# Edit netplan config
sudo nano /etc/netplan/01-netcfg.yaml
```

```yaml
network:
  version: 2
  ethernets:
    ens18:
      dhcp4: no
      addresses:
        - 192.0.2.207/24
      routes:
        - to: default
          via: 192.0.2.1
      nameservers:
        addresses:
          - 192.0.2.1
          - 8.8.8.8
```

```bash
sudo netplan apply
```

### Set Hostname

```bash
sudo hostnamectl set-hostname barenoc
```

### Install Docker

```bash
# Docker Engine (tested 29.x) + compose v2
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for group changes to take effect

# Agent tooling used by the action scripts (host-side)
sudo apt-get install -y nmap snmp jq git
```

### Configure Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 443/tcp
sudo ufw --force enable
```

### Install QEMU Guest Agent

```bash
sudo apt install -y qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent
```

---

## Step 5: Deploy BareNOC Stack

```bash
# As barenoc user
sudo mkdir -p /opt/barenoc && sudo chown barenoc:docker /opt/barenoc
# (deploy.sh creates the runtime subdirectories on first run)
```

The **installer is `deploy.sh` from the dev box** (not a git clone on the VM):

```bash
# On the dev box, from the repo root
./deploy.sh barenoc@192.0.2.207
```

This rsyncs `src/` + `client/`, rebuilds the 5 containers, reloads nginx, and
provisions the agent service account. **Then install the host-side agent
runner** (not in Docker):

```bash
# On the VM
sudo useradd -r -m -s /bin/bash pi-agent            # Pi Coding Agent runtime user
sudo cp src/agent/pi-agent-runner.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pi-agent-runner
# + copy the pi-node runtime to /home/pi-agent/.local/share/pi-node
#   (see SESSION_LOG 2026-08-04 "Pi Coding Agent at the core")
```

Verify: `docker compose ps` (5 containers), `systemctl status pi-agent-runner`,
`curl -sk https://127.0.0.1/api/v1/health` → 200. First login: change the
`admin` password, then configure Settings (UniFi, LLM provider, email,
timezone).

---

## Step 6: Verify

```bash
# Check web UI
curl -sk -o /dev/null -w "%{http_code}" https://127.0.0.1
# Expected: 200

# Check API health
curl -sk https://127.0.0.1/api/v1/health
# Expected: {"status": "healthy", ...}

# Check agent runner
systemctl is-active pi-agent-runner   # expected: active

# Check database
ls -lh /opt/barenoc/volumes/db/barenoc.db
```

---

## VM Spec Summary

| Resource | Value | Rationale |
|----------|-------|-----------|
| **vCPU** | 2 cores (host CPU type) | Python web app + worker — mostly I/O bound |
| **RAM** | 4 GB | Comfortable for Docker stack + SQLite |
| **Disk** | 40 GB (qcow2) | OS + Docker images + DB + logs |
| **NIC** | 1× VirtIO on vmbr0 (MGMT VLAN) | Management network access |
| **QEMU Agent** | Enabled | Clean shutdowns, IP reporting |
| **Auto-start** | Enabled | VM comes up on Proxmox boot |
