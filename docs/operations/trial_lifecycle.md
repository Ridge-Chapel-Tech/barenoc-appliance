# Trial Lifecycle

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Overview

The BareNOC appliance lifecycle for customer trials:

```
Build ──▶  Provision ──▶  Ship ──▶  Trial ──▶  Decision
  ▲                                            │
  └────────────── Return/Fail ────────────────┘
```

---

## Phase 1: Build

See [`assembly_guide.md`](../appliance/assembly_guide.md) for physical assembly.

### Build Checklist

- [ ] All BOM components acquired
- [ ] Rack assembled and cabled
- [ ] Proxmox VE installed on mini PC
- [ ] Proxmox web UI accessible
- [ ] NanoKVM installed and accessible via web
- [ ] UniFi Gateway factory reset and accessible
- [ ] PoE switch accessible via UniFi
- [ ] AP adopted and broadcasting SSID
- [ ] Power-on test: full rack boot in <10 minutes
- [ ] Thermal test: 1-hour run, no overheating
- [ ] All cables labeled

---

## Phase 2: Provision

### Step 1: Create Proxmox Snapshot (Clean State)

```bash
# After Proxmox is installed and configured, BEFORE VM creation
# This gives us a recovery baseline for the host
```

### Step 2: Create BareNOC VM

See [`barenoc_vm_create.md`](../appliance/barenoc_vm_create.md).

### Step 3: Configure for Customer

```bash
# Set site-specific parameters
export SITE_ID=1
export CUSTOMER_NAME="Acme Corp"
export MGMT_VLAN="10.1.10.0/24"
export TIMEZONE="America/Chicago"

# Run provisioning script
/root/provision-customer.sh \
    --site-id $SITE_ID \
    --customer "$CUSTOMER_NAME" \
    --mgmt-vlan $MGMT_VLAN \
    --timezone $TIMEZONE
```

### Step 4: Pre-Ship Snapshot

```bash
# After provisioning, take a clean snapshot
qm snapshot 100 pre-ship

# Verify snapshot
qm listsnapshot 100
```

### Step 5: Generate Quick-Start Card

```bash
# Print the customer-specific quick-start card
lp docs/customer/quick_start_card.md
```

---

## Phase 3: Ship

### Packaging Checklist

- [ ] Rack in padded case or box
- [ ] AP in separate foam padding
- [ ] Quick-start card included
- [ ] Power cable included
- [ ] 2× spare patch cables included
- [ ] Return shipping label included
- [ ] Support contact info included (email + phone)

### Shipping Labels

```
BARENOC TRIAL UNIT
Customer: [NAME]
Return By: [DATE + 35 DAYS]
Serial: BN-[YEAR]-[MONTH]-[NUM]

FRAGILE — ELECTRONICS
```

---

## Phase 4: Trial (30 Days)

### Day 0 — Customer Setup

Customer follows the quick-start card:
1. Plug WAN cable into port 1
2. Plug power
3. Wait 10 minutes
4. Connect to Wi-Fi SSID "BareNOC-Setup"
5. Open browser to `http://barenoc.local`

### Day 1 — Activation Check

```bash
# Remote check via Tailscale
ping 100.X.X.X  # appliance Tailscale IP
curl -s https://barenoc.local/api/v1/health
```

Verify:
- [ ] Appliance is online
- [ ] UniFi gateway is reachable
- [ ] BareNOC web UI is responsive
- [ ] First ticket(s) being processed

### Day 7 — Check-In #1

- Email customer: "How's the trial going?"
- Verify no stuck tickets or errors
- Offer configuration tweaks

### Day 14 — Mid-Trial Check

- Review audit logs for anomalies
- Check disk usage, log rotation
- Verify backups are running

### Day 21 — Pre-Decision Check

- Ensure customer has all the info they need to decide
- Offer to extend trial by 7 days if needed

### Day 28 — Decision Reminder

- Email: "Your trial ends in 2 days"
- Prepare invoice if purchasing
- Prepare return label if not

### Day 30 — Decision

| Outcome | Action |
|---------|--------|
| **Purchase** | Send invoice, leave hardware, activate support |
| **Return** | Send return label, deactivate remote access |
| **Extend** | Add 14 days, set new decision date |

---

## Phase 5: Return & Recover

### Customer Returns Hardware

```
Ship back ──▶ Inspect ──▶ Rollback ──▶ Reprovision ──▶ Re-Ship
```

### Inspection

- [ ] Visual inspection: cracks, damage, missing parts
- [ ] Power-on test: does it boot?
- [ ] NanoKVM test: remote KVM functional
- [ ] UniFi gateway: factory reset it
- [ ] PoE switch: verify ports
- [ ] AP: verify LED, test adoption

### Rollback to Factory State

```bash
# On Proxmox host

# 1. Stop current VM
qm stop 100

# 2. Delete current VM disk (customer data)
qm destroy 100 --purge

# 3. Restore from pre-ship backup
qmrestore /mnt/backup/vzdump-qemu-100-pre-ship.vma.zst 100

# 4. Start VM
qm start 100

# 5. Factory reset UniFi gateway (hold reset button 10s)
# 6. Forget and re-adopt AP and switch

# 7. Verify clean state
qm listsnapshot 100
```

### Reprovision for Next Customer

```bash
# Run provisioning script again
/root/provision-customer.sh \
    --site-id $NEXT_SITE_ID \
    --customer "$NEXT_CUSTOMER" \
    ...
```

---

## Cleanup Script

```bash
#!/bin/bash
# /usr/local/bin/prepare-for-reship.sh
# Run after receiving returned hardware

set -euo pipefail

echo "=== Preparing BareNOC appliance for re-shipment ==="

# 1. Wipe customer data from VM
echo "[1/5] Destroying customer VM..."
qm stop 100 2>/dev/null || true
qm destroy 100 --purge 2>/dev/null || true

# 2. Restore from pre-ship backup
echo "[2/5] Restoring factory state..."
BACKUP=$(ls -t /mnt/backup/*pre-ship*.vma.zst 2>/dev/null | head -1)
if [ -z "$BACKUP" ]; then
    echo "Error: No pre-ship backup found on USB"
    exit 1
fi
qmrestore $BACKUP 100 --storage local

# 3. Factory reset networking gear
echo "[3/5] Resetting UniFi gateway..."
# Manual: hold reset button on UCG for 10 seconds

# 4. Clear logs
echo "[4/5] Clearing audit logs..."
qm start 100
sleep 10
ssh barenoc@192.0.2.207 "rm -rf /opt/barenoc/volumes/logs/*"
ssh barenoc@192.0.2.207 "rm -rf /opt/barenoc/backups/*"

# 5. Take new pre-ship snapshot
echo "[5/5] Creating pre-ship snapshot..."
qm snapshot 100 pre-ship

echo "=== Appliance ready for re-shipment ==="
```
