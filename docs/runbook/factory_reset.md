# Factory Reset Procedure

**Version:** 1.0  
**Last Updated:** 2025-07-29  
**Audience:** Internal Support / Engineering

---

## When to Factory Reset

- **Customer returns** appliance after trial (re-ship to new customer)
- **Pre-ship prep** — wiping device before first shipment
- **Corruption recovery** — when rollback/restore fails

---

## What a Factory Reset Wipes

| Data | Wiped? | Notes |
|------|--------|-------|
| BareNOC VM | ✅ | Destroyed and restored from pre-ship backup |
| Customer DB (tickets, logs, config) | ✅ | All customer data |
| Audit logs | ✅ | Except those archived off-device |
| UniFi gateway config | ✅ | Factory reset required separately |
| UniFi switch config | ✅ | Reset required separately |
| AP config | ✅ | Reset required separately |

| Data | Preserved? | Notes |
|------|-----------|-------|
| Proxmox host OS | ✅ | Reused |
| Proxmox VM templates | ✅ | |
| Backup USB (if present) | ✅ | Manual intervention needed to reuse |
| Pre-ship VM snapshot | ✅ | This is what we restore from |

---

## Step-by-Step

### Step 1: Take Pre-Wipe Snapshot (Safety Net)

```bash
qm snapshot 100 pre-wipe-$(date +%F)
```

### Step 2: Export Audit Logs (If Needed)

If the customer had a security incident and you need logs for investigation:

```bash
# Copy audit logs off-device
scp -r barenoc@192.0.2.207:/opt/barenoc/volumes/logs/ ~/customer-audit-logs/
```

### Step 3: Stop and Destroy Customer VM

```bash
qm stop 100
qm destroy 100 --purge
```

### Step 4: Restore from Pre-Ship Backup

```bash
# Find the pre-ship backup
ls -lh /var/lib/vz/dump/*pre-ship*
ls -lh /mnt/backup/*pre-ship*

# If on local storage:
qmrestore /var/lib/vz/dump/vzdump-qemu-100-pre-ship.vma.zst 100 \
    --storage local

# If on USB:
qmrestore /mnt/backup/vzdump-qemu-100-pre-ship.vma.zst 100 \
    --storage local
```

### Step 5: Start Fresh VM

```bash
qm start 100
```

### Step 6: Verify Clean State

```bash
# Check VM is running
qm status 100

# SSH in and verify
ssh barenoc@192.0.2.207 "
    echo '=== VM Hostname ==='
    hostnamectl
    
    echo '=== Disk Usage ==='
    df -h
    
    echo '=== Docker Status ==='
    docker compose ps
    
    echo '=== DB Size ==='
    ls -lh /opt/barenoc/volumes/db/barenoc.db
    
    echo '=== Audit Logs ==='
    ls /opt/barenoc/volumes/logs/audit/
"
```

### Step 7: Factory Reset Network Hardware

**UniFi Gateway (UCG-Ultra):**

1. Locate the reset pinhole on the front panel
2. Insert a paperclip and hold for 10 seconds
3. Wait for the LED to flash rapidly, then release
4. Gateway will reboot to factory defaults

**UniFi PoE Switch:**

1. Hold reset button for 10 seconds
2. Wait for all LEDs to flash
3. Release — switch will reboot

**UniFi AP:**

1. Hold reset button on the AP for 10 seconds
2. LED will turn off and back on
3. AP is now in factory-default, unadopted state

### Step 8: Re-provision for Next Customer

```bash
# Run the provisioning script
/root/provision-customer.sh \
    --site-id $NEXT_SITE_ID \
    --customer "$NEXT_CUSTOMER" \
    --timezone "$TIMEZONE"
```

### Step 9: Take New Pre-Ship Snapshot

```bash
qm snapshot 100 pre-ship
qm listsnapshot 100
```

---

## Automated Factory Reset Script

```bash
#!/bin/bash
# /usr/local/bin/factory-reset.sh
# Run as root on Proxmox host

set -euo pipefail

echo "=== BareNOC Factory Reset ==="
echo "WARNING: This will destroy all customer data!"
read -p "Type 'RESET' to confirm: " confirm
if [ "$confirm" != "RESET" ]; then
    echo "Aborted."
    exit 1
fi

# 1. Snapshot current state (just in case)
SNAPSHOT_NAME="pre-wipe-$(date +%F-%H%M)"
echo "Taking pre-wipe snapshot: $SNAPSHOT_NAME"
qm snapshot 100 $SNAPSHOT_NAME

# 2. Destroy VM
echo "Destroying customer VM..."
qm stop 100 2>/dev/null || true
qm destroy 100 --purge

# 3. Restore from backup
BACKUP=$(ls -t /mnt/backup/*pre-ship*.vma.zst 2>/dev/null | head -1)
if [ -z "$BACKUP" ]; then
    BACKUP=$(ls -t /var/lib/vz/dump/*pre-ship*.vma.zst 2>/dev/null | head -1)
fi

if [ -z "$BACKUP" ]; then
    echo "ERROR: No pre-ship backup found!"
    exit 1
fi

echo "Restoring from: $BACKUP"
qmrestore $BACKUP 100 --storage local

# 4. Start VM
echo "Starting VM..."
qm start 100
sleep 10

# 5. Verify
echo "Verifying..."
if [ "$(qm status 100)" = "status: running" ]; then
    echo "✅ VM restored and running"
else
    echo "❌ VM failed to start"
    exit 1
fi

# 6. Create new pre-ship snapshot
qm snapshot 100 pre-ship

echo ""
echo "=== Factory reset complete ==="
echo "Next steps:"
echo "  1. Factory reset UniFi gateway (hold reset 10s)"
echo "  2. Factory reset PoE switch (hold reset 10s)"
echo "  3. Factory reset AP (hold reset 10s)"
echo "  4. Run provisioning script for new customer"
```
