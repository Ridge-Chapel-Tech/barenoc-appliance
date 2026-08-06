# Appliance Assembly Guide

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Tools Required

- Phillips #1 screwdriver
- RJ45 crimper and pass-through connectors (if terminating custom cables)
- Velcro cable ties (6" and 12")
- Cable tester (optional but recommended)
- Label maker (e.g., Brother P-Touch) or pre-printed labels

---

## 1. Rack Layout (Top to Bottom)

```
┌────── 10-Inch Rack ──────────────────────────────────┐
│                                                       │
│  U1  [Patch Panel - 8 Port]                          │
│       Ports labeled: WAN, MGMT, DESK-1..4, AP, SPARE │
├──────────────────────────────────────────────────────-┤
│  U2  [UniFi Gateway] (on shelf)                      │
│       Front: WAN port, LAN ports                      │
│       Rear: Power, USB (for 4G backup if equipped)    │
├──────────────────────────────────────────────────────-┤
│  U3  [PoE Switch] (if separate from gateway)          │
│       Provide PoE to AP + desk runs                   │
├──────────────────────────────────────────────────────-┤
│  U4  [Shelf / Tray]                                   │
│       [Mini PC] + [NanoKVM] on shelf                  │
│       NanoKVM HDMI → Mini PC HDMI                     │
│       NanoKVM USB → Mini PC USB                       │
│       NanoKVM Ethernet → PoE switch                   │
├──────────────────────────────────────────────────────-┤
│  U5  [Power Strip] + [Cable Management]              │
│       Mounted rear-facing or flush                    │
├──────────────────────────────────────────────────────-┤
│  U6  [Vented Panel] + [Fan(s)]                       │
│       Exhaust fans if rack is enclosed                │
└──────────────────────────────────────────────────────-┘
```

---

## 2. Assembly Steps

### Step 1: Mount the Rack

If shipping to a customer — leave the rack as a standalone unit. It sits on a desk or shelf. If wall-mounting, use 4× M8 screws into studs (not drywall anchors — the rack weighs ~15 lbs loaded).

### Step 2: Install Patch Panel

1. Mount patch panel in U1 position
2. Terminate keystone jacks (T568B standard)
3. Label each port clearly on the front face

**Port Labeling Standard:**

| Port | Label | Color | Connected To |
|------|-------|-------|-------------|
| 1 | WAN IN | Blue | Customer modem/ONT → UCG WAN |
| 2 | MGMT | Green | UCG LAN → PoE switch uplink |
| 3 | DESK-1 | White | Customer workstation |
| 4 | DESK-2 | White | Customer workstation |
| 5 | DESK-3 | White | Customer workstation |
| 6 | DESK-4 | White | Customer workstation |
| 7 | AP | Yellow | PoE switch → U6-Pro |
| 8 | SPARE | Grey | Future use |

### Step 3: Mount UniFi Gateway

If using a shelf: screw shelf into rack rails, place gateway on shelf. If using bracket ears: attach ears to gateway, slide into rack.

**Uplink cabling:**
- UCG WAN port → Patch panel port 1 (WAN IN) — (blue patch cable)
- UCG LAN port → PoE switch via short cable

### Step 4: Mount PoE Switch

- Attach rack ears if available
- Connect uplink from UCG LAN to switch
- Connect switch ports to patch panel ports 2–8
- Connect switch PoE port to AP cable

### Step 5: Mount Mini PC

- Place on shelf below the switch
- Connect Ethernet (enp1s0) → PoE switch (MGMT VLAN access)
- Connect second Ethernet (enp2s0) → patch panel port 2 (MGMT) — standby/management NIC
- Connect power

### Step 6: Install NanoKVM

- Attach NanoKVM to Mini PC:
  - NanoKVM HDMI → Mini PC HDMI output
  - NanoKVM USB → Mini PC USB port
  - NanoKVM Ethernet → PoE switch (gets IP via DHCP)
- Configure NanoKVM: set static IP on MGMT VLAN, set admin password
- Verify remote KVM access before closing rack

### Step 7: Cable Management

- Route all cables along the sides of the rack
- Use velcro ties every 2–3 inches
- Keep power cables separate from data cables where possible
- Leave a service loop at each component (enough to pull the unit forward)

### Step 8: Power

- Plug all components into the power strip
- Plug power strip into wall/UPS
- Verify all LEDs illuminate on boot

### Step 9: Label the Rack Exterior

```
[BareNOC]
Model: BN-001
Firmware: v1.0
WAN: Port 1 (Blue)
MGMT: 192.0.2.95
Support: support@barenoc.io
```

---

## 3. Initial Power-On Sequence

1. Power on the Mini PC first (Proxmox needs to boot)
2. Power on the UniFi Gateway (takes ~3 min to boot)
3. Power on the PoE Switch
4. AP will power up once PoE switch is online

**After all components boot:**
1. Verify Proxmox web UI at `https://192.0.2.95:8006`
2. Verify UniFi Controller at `https://192.0.2.1:443`
3. Verify NanoKVM web UI
4. Start the BareNOC VM in Proxmox
5. Verify BareNOC web UI at `https://barenoc.local`

---

## 4. Shipping Preparation

1. **Take a Proxmox snapshot**: `qm snapshot 100 pre-ship`
2. **Back up the snapshot** to external USB: `vzdump 100 --dumpdir /mnt/usb`
3. **Shut down VM** and **shut down Proxmox host**
4. **Unplug all rear cables** (leave front patch cables in place)
5. **Remove AP** and place in foam padding
6. **Wrap rack** in padded moving blanket or place in padded Pelican case
7. **Include quick-start card**, power cable, and any additional patch cables

---

## 5. On-Site Installation (Customer Self-Setup)

1. Place rack on desk/shelf near customer modem/ONT
2. Plug blue cable from "WAN IN" port on patch panel into modem
3. Plug power strip into wall outlet
4. Power on rack (power strip switch)
5. Wait 10 minutes for all components to boot
6. Customer connects to Wi-Fi SSID "BareNOC-Setup"
7. Web UI at `http://barenoc.local`
