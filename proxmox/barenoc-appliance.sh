#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# BareNOC Appliance Installer — one-shot Proxmox install
#
# Run ON the Proxmox host, from a checkout of this repo (copy the repo to the
# host or run via `ssh root@<proxmox> 'bash -s' < proxmox/barenoc-appliance.sh …`).
#
#   bash proxmox/barenoc-appliance.sh --ip 192.0.2.210 --profile s
#
# What it does:
#   1. Preflight (qm, bridge, storage, cloud image — downloads Ubuntu 24.04
#      cloud image once, cached under /var/lib/vz/template/iso/).
#   2. Creates a VM sized by --profile (see PROFILE_MATRIX below) with
#      cloud-init: `barenoc` user + your SSH key, static IP, qemu-guest-agent.
#   3. Injects an OS-provisioning cloud-init user-data (Docker, pi-agent user +
#      the Pi Coding Agent runtime, pi-agent-runner.service, UFW, /opt/barenoc
#      skeleton) that runs once on first boot.
#   4. Waits for SSH + the provisioning marker, then runs ./deploy.sh
#      barenoc@<ip> to install the application (containers, agent creds,
#      runner sync) — the same single deploy path used for updates.
#
# Result: a ready appliance at https://<ip> — no manual steps in between.
# First login: admin / (the seeded password you choose with --admin-password;
# you are prompted to change it immediately). Configure Settings (UniFi, LLM
# provider, email) in the web UI.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── defaults ───────────────────────────────────────────────────────────────
VM_ID=1000
IP=""
GATEWAY=""
DNS="1.1.1.1"
PROFILE="m"
STORAGE="local-lvm"
BRIDGE="vmbr0"
HOSTNAME="barenoc"
APPLIANCE_HOST="app.barenoc.com"   # public name clients use (Corefile + nginx server_name; set APP_URL to your real domain before enrolling passkeys)
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519.pub}"
ADMIN_PASSWORD=""
ACTIVATION_KEY=""
ACTIVATION_EMAIL=""
SKIP_APP=0
BRANCH="$(git -C "$(dirname "$0")/.." rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

# ── sizing matrix (endpoints ≈ adopted/managed network devices + clients) ──
declare -A PROFILE_CPU=( [s]=1 [m]=2 [l]=4 [xl]=6 )
declare -A PROFILE_RAM=( [s]=2048 [m]=4096 [l]=8192 [xl]=16384 )   # MB
declare -A PROFILE_DISK=( [s]=30 [m]=40 [l]=80 [xl]=160 )          # GB
declare -A PROFILE_ENDPOINTS=(
  [s]="≤10 endpoints — home / small office (mini-PC class, 1× 2.5GbE)"
  [m]="≤50 endpoints — SMB (2 vCPU / 4 GB / 40 GB is the proven live config)"
  [l]="≤200 endpoints — larger SMB / multi-VLAN"
  [xl]="≤500 endpoints — MSP / multi-site controller"
)

usage() {
  cat <<'EOF'
Usage: bash proxmox/barenoc-appliance.sh [options]

Required:
  --ip <addr>            Static IP for the appliance VM (e.g. 192.0.2.210)
  --ssh-key <path>       .pub key to authorize for the `barenoc` user
                         (default: ~/.ssh/id_ed25519.pub — must exist on host)

Optional:
  --vm <id>              VMID (default 1000)
  --profile <s|m|l|xl>   Sizing (default m). Matrix:
                           s  ≤10 endpoints   (1 vCPU / 2 GB / 30 GB)
                           m  ≤50 endpoints   (2 vCPU / 4 GB / 40 GB)
                           l  ≤200 endpoints  (4 vCPU / 8 GB / 80 GB)
                           xl ≤500 endpoints  (6 vCPU / 16 GB / 160 GB)
  --gateway <addr>       Gateway (default: first usable IP of the /24)
  --dns <addr>           DNS (default 1.1.1.1)
  --hostname <name>      VM hostname (default barenoc)
  --storage <id>         Proxmox storage for the disk (default local-lvm)
  --bridge <iface>       Network bridge (default vmbr0)
  --admin-password <pw>  Seed admin password for the web UI (min 8 chars;
                         default: auto-generated, printed at the end)
  --activation-key <key>  Early-access activation key (gates updates; bind to
                         the purchase email at issue time)
  --activation-email <em> Purchase email bound to the activation key
  --skip-app             Provision the OS only; run ./deploy.sh yourself later
  --help                 This help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm) VM_ID="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --dns) DNS="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --hostname) HOSTNAME="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    --activation-key) ACTIVATION_KEY="$2"; shift 2 ;;
    --activation-email) ACTIVATION_EMAIL="$2"; shift 2 ;;
    --skip-app) SKIP_APP=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$REPO/deploy.sh"
CLOUDIMG="/var/lib/vz/template/iso/noble-server-cloudimg-amd64.img"
CLOUDIMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
SNIPPET_DIR="/var/lib/vz/snippets"
# cicustom wants Proxmox volume IDs (storage:snippets/file) + the snippets
# content type enabled on that storage — enable it if missing (idempotent).
SNIPPET_STORAGE="${SNIPPET_STORAGE:-$(pvesm status -content snippets 2>/dev/null | awk 'NR==2{print $1}')}"
SNIPPET_STORAGE="${SNIPPET_STORAGE:-local}"
CUR="$(awk -v s="$SNIPPET_STORAGE" \
  '$1 ~ /^[a-z]+:$/ && $2==s{f=1; next} f && /^[[:space:]]*content[[:space:]]/{sub(/^[[:space:]]*content[[:space:]]*/,""); print; exit}' \
  /etc/pve/storage.cfg)"
case ",${CUR}," in
  *,snippets,*) echo "snippets already enabled on $SNIPPET_STORAGE" ;;
  *) pvesm set "$SNIPPET_STORAGE" --content "${CUR},snippets" && echo "enabled snippets on $SNIPPET_STORAGE" ;;
esac
USERDATA="$SNIPPET_DIR/barenoc-${VM_ID}-user.yml"
META="$SNIPPET_DIR/barenoc-${VM_ID}-meta.yml"
CICUSTOM="user=${SNIPPET_STORAGE}:snippets/barenoc-${VM_ID}-user.yml,meta=${SNIPPET_STORAGE}:snippets/barenoc-${VM_ID}-meta.yml"

[[ "$IP" ]] || { echo "ERROR: --ip is required" >&2; usage; exit 1; }
[[ -f "$SSH_KEY" ]] || { echo "ERROR: SSH key not found: $SSH_KEY" >&2; exit 1; }
[[ " s m l xl " == *" $PROFILE "* ]] || { echo "ERROR: --profile must be s|m|l|xl" >&2; exit 1; }
[[ "$ADMIN_PASSWORD" && ${#ADMIN_PASSWORD} -lt 8 ]] && { echo "ERROR: --admin-password must be ≥8 chars" >&2; exit 1; }
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 12 | tr '+/' '_-')}"

# ── preflight ──────────────────────────────────────────────────────────────
command -v qm >/dev/null || { echo "ERROR: not on a Proxmox host (qm missing)" >&2; exit 1; }
command -v qm >/dev/null; [[ "$(id -u)" -eq 0 ]] || { echo "ERROR: run as root on the Proxmox host" >&2; exit 1; }
qm list >/dev/null 2>&1 || { echo "ERROR: cannot talk to qm — is the Proxmox API up?" >&2; exit 1; }
qm list | awk '{print $1}' | grep -qx "$VM_ID" && { echo "ERROR: VMID $VM_ID already exists" >&2; exit 1; }
ip link show "$BRIDGE" >/dev/null 2>&1 || { echo "ERROR: bridge $BRIDGE not found" >&2; exit 1; }
pvesm status -content images | awk '{print $1}' | grep -qx "$STORAGE" \
  || { echo "ERROR: storage $STORAGE not found (pvesm status -content images)" >&2; exit 1; }
GATEWAY="${GATEWAY:-$(echo "$IP" | awk -F. '{print $1"."$2"."$3".1"}')}"

echo "==> BareNOC appliance install"
echo "    VM=$VM_ID profile=$PROFILE ($(echo ${PROFILE_ENDPOINTS[$PROFILE]}))"
echo "    ip=$IP gw=$GATEWAY bridge=$BRIDGE storage=$STORAGE hostname=$HOSTNAME"
echo "    ssh-key=$SSH_KEY  app-deploy=$([ $SKIP_APP -eq 1 ] && echo SKIPPED || echo "$DEPLOY")"

# ── 1. cloud image (cached) ────────────────────────────────────────────────
if [[ ! -f "$CLOUDIMG" ]]; then
  echo "==> Downloading Ubuntu 24.04 cloud image (once)…"
  curl -fL "$CLOUDIMG_URL" -o "$CLOUDIMG.tmp"
  mv "$CLOUDIMG.tmp" "$CLOUDIMG"
fi

# ── 2. OS-provisioning user-data (runs once on first boot) ─────────────────
# The provisioning script is base64-embedded so YAML quoting can't mangle it.
# `read -d ''` returns 1 at EOF (no NUL found) — `|| true` keeps set -e happy.
read -r -d '' PROVISION <<'PROVEOF' || true
#!/bin/bash
set -euxo pipefail
exec > /var/log/barenoc-provision.log 2>&1
echo "[provision] starting $(date -Is)"

# packages (Docker Engine from the official repo; agent tooling; guest agent)
apt-get update -qq
apt-get install -y -qq ca-certificates curl jq qemu-guest-agent
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin \
  nmap snmp snmp-mibs-downloader
usermod -aG docker barenoc

# pi-agent user + Pi Coding Agent runtime (node + the pi npm package)
useradd -r -m -s /bin/bash pi-agent
PI_NODE_DIR=/home/pi-agent/.local/share/pi-node
mkdir -p "$PI_NODE_DIR"
curl -fsSL https://nodejs.org/dist/v22.23.1/node-v22.23.1-linux-x64.tar.xz -o /tmp/node.tar.xz
tar -xJf /tmp/node.tar.xz -C "$PI_NODE_DIR"
ln -sfn "$PI_NODE_DIR/node-v22.23.1-linux-x64" "$PI_NODE_DIR/current"
PATH="$PI_NODE_DIR/current/bin:$PATH" npm install -g --no-fund --no-audit @earendil-works/pi-coding-agent@0.83.0
chown -R pi-agent:pi-agent "$PI_NODE_DIR"
rm -f /tmp/node.tar.xz

# /opt/barenoc skeleton (deploy.sh fills the rest)
install -d -o barenoc -g docker -m 0750 /opt/barenoc
for d in volumes/db volumes/logs/api volumes/logs/worker volumes/logs/scheduler volumes/logs/agent volumes/secrets/ssh volumes/nginx/certs volumes/branding volumes/backup_status volumes/pocket-id/data jobs/incoming jobs/running jobs/completed backups pi-work agent; do
  install -d -o barenoc -g docker /opt/barenoc/$d
done
# install -d only chowns the FINAL dir — fix the intermediates (volumes/, jobs/,
# backups/, pi-work/) so deploy.sh (and the runner) can write into them.
chown barenoc:docker /opt/barenoc/volumes /opt/barenoc/jobs /opt/barenoc/backups /opt/barenoc/pi-work

# agent runner systemd unit (runs as pi-agent; ProtectHome=no for the pi runtime)
cat > /etc/systemd/system/pi-agent-runner.service <<'UNIT'
[Unit]
Description=BareNOC Pi Agent Runner
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=pi-agent
Group=pi-agent
ExecStart=/usr/bin/python3 /opt/barenoc/agent/runner.py
WorkingDirectory=/opt/barenoc
Restart=always
RestartSec=5
StandardOutput=append:/opt/barenoc/volumes/logs/agent/agent.log
StandardError=append:/opt/barenoc/volumes/logs/agent/agent.log
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=no
CapabilityBoundingSet=CAP_NET_RAW CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pi-agent-runner   # survive reboots

# firewall: ssh + 443 (+8443 for Pocket ID)
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 443/tcp
ufw allow 8443/tcp
ufw --force enable

# ready marker — the appliance script waits for this
touch /opt/barenoc/.provisioned
echo "[provision] done $(date -Is)"
PROVEOF
PROVISION_B64="$(printf '%s' "$PROVISION" | base64 -w0)"

mkdir -p "$SNIPPET_DIR"
cat > "$USERDATA" <<EOF
#cloud-config
users:
  - name: barenoc
    gecos: BareNOC Administrator
    shell: /bin/bash
    groups: [docker, sudo, adm]
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(tr -d '\n' < "$SSH_KEY")
    lock_passwd: true
package_update: false
runcmd:
  - [ bash, -c, "echo $PROVISION_B64 | base64 -d > /opt/barenoc-provision.sh && chmod +x /opt/barenoc-provision.sh && /opt/barenoc-provision.sh" ]
EOF
cat > "$META" <<EOF
instance-id: barenoc-${VM_ID}
local-hostname: $HOSTNAME
EOF
echo "==> cloud-init user-data written: $USERDATA"

# ── 3. create + start the VM ───────────────────────────────────────────────
DISK_GB="${PROFILE_DISK[$PROFILE]}"
echo "==> Creating VM $VM_ID (${PROFILE_CPU[$PROFILE]} vCPU / $(( ${PROFILE_RAM[$PROFILE]} / 1024 )) GB RAM / ${DISK_GB} GB)"
qm create "$VM_ID" \
  --name "$HOSTNAME" \
  --memory "${PROFILE_RAM[$PROFILE]}" \
  --cores "${PROFILE_CPU[$PROFILE]}" \
  --cpu cputype=host \
  --net0 "virtio,bridge=$BRIDGE" \
  --scsihw virtio-scsi-pci \
  --ide2 "$STORAGE:cloudinit" \
  --serial0 socket \
  --vga serial0 \
  --agent enabled=1 \
  --ostype l26 \
  --onboot 1 \
  --ipconfig0 "ip=$IP/24,gw=$GATEWAY" \
  --nameserver "$DNS" \
  --cicustom "$CICUSTOM"

echo "==> Importing cloud image (this can take a minute)…"
qm importdisk "$VM_ID" "$CLOUDIMG" "$STORAGE" 2>/dev/null
# the imported cloud image becomes scsi0 (no pre-created disk to replace);
# ide2 (cloudinit) already exists from qm create — don't re-specify it.
IMPORTED="$(qm config "$VM_ID" | awk '/unused0/ {print $2}')"
qm set "$VM_ID" --scsi0 "$IMPORTED" --boot order=scsi0
qm resize "$VM_ID" scsi0 "${DISK_GB}G"

echo "==> Starting VM $VM_ID (first boot: OS provisioning, ~2-4 min)…"
qm start "$VM_ID"

# ── 4. wait for SSH + provisioning marker ──────────────────────────────────
echo "==> Waiting for $IP:22 (ssh)…"
for _ in $(seq 1 60); do
  if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$IP/22" 2>/dev/null; then break; fi
  sleep 5
done
echo "==> Waiting for /opt/barenoc/.provisioned (base OS + pi runtime)…"
for _ in $(seq 1 60); do
  if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 "barenoc@$IP" "test -f /opt/barenoc/.provisioned" 2>/dev/null; then break; fi
  sleep 5
done

# ── 4.5 .env bootstrap ─────────────────────────────────────────────────────
# First contact with the fresh VM: drop any stale host key (an earlier
# appliance at this IP) and accept the new one so deploy.sh's ssh/scp work.
ssh-keygen -f /root/.ssh/known_hosts -R "$IP" 2>/dev/null || true
mkdir -p /root/.ssh
if ! grep -q "^Host $IP$" /root/.ssh/config 2>/dev/null; then
  cat >> /root/.ssh/config <<CONF

Host $IP
    StrictHostKeyChecking accept-new
CONF
fi
# deploy.sh expects /opt/barenoc/.env to exist (it seeds the admin user + all
# app config from it); the installer creates it from the repo template with
# the seeded admin password and the appliance identity. Everything else (LLM
# keys, UniFi, email) is set in the web UI → Settings after first login.
JWT="$(openssl rand -hex 32)"
if [[ $SKIP_APP -eq 0 ]]; then
  echo "==> Bootstrapping /opt/barenoc/.env (template + seeded admin + identity)"
  scp -q "$REPO/src/.env.example" "barenoc@$IP:/tmp/barenoc-env.example"
  ssh "barenoc@$IP" "install -m 600 /tmp/barenoc-env.example /opt/barenoc/.env && sed -i \
    's|^JWT_SECRET=.*|JWT_SECRET=${JWT}|;
     s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASSWORD}|;
     s|^# ENCRYPTION_KEY=.*|ENCRYPTION_KEY=${ENCRYPTION_KEY}|;
     s|^# APPLIANCE_IP=.*|APPLIANCE_IP=${IP}|;
     s|^# APPLIANCE_HOST=.*|APPLIANCE_HOST=${APPLIANCE_HOST}|;
     s|^# ACTIVATION_KEY=.*|ACTIVATION_KEY=${ACTIVATION_KEY}|;
     s|^# LICENSE_EMAIL=.*|LICENSE_EMAIL=${ACTIVATION_EMAIL}|' /opt/barenoc/.env && rm -f /tmp/barenoc-env.example"
fi

# ── 5. application install (same path as updates) ──────────────────────────
if [[ $SKIP_APP -eq 1 ]]; then
  echo
  echo "==> OS provisioned. Run the application install when ready:"
  echo "    cd $REPO && ./deploy.sh barenoc@$IP"
else
  [[ -x "$DEPLOY" ]] || { echo "ERROR: $DEPLOY not found — run this from a repo checkout" >&2; exit 1; }
  echo "==> Installing application (deploy.sh → containers → agent creds → runner)…"
  "$DEPLOY" "barenoc@$IP"
fi

# ── summary ────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════"
echo "  BareNOC appliance ready!"
echo "    Web UI:   https://$IP"
echo "    SSH:      ssh barenoc@$IP"
echo "    Profile:  $PROFILE — ${PROFILE_ENDPOINTS[$PROFILE]}"
echo "    Admin:    admin / $ADMIN_PASSWORD   (CHANGE IT on first login)"
echo
echo "  Next (web UI → Settings):"
echo "    · UniFi controller (URL/user/password + auto-sync + auto-adopt)"
echo "    · LLM provider + API key (Settings → API Keys)"
echo "    · Email (Gmail OAuth2), timezone, autonomy policy"
echo "════════════════════════════════════════════════════════════════"
