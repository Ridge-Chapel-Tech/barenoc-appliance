# Appliance Bill of Materials — 10-Inch Rack

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Required Components

| # | Component | Model / Spec | Qty | Est. Cost | Notes |
|---|-----------|-------------|-----|-----------|-------|
| 1 | **10-Inch Rack** | 6U–8U wall-mount or desktop, steel | 1 | $40–60 | NavePoint or similar. Ensure depth ≥12" for mini PC |
| 2 | **UniFi Gateway** | UCG-Ultra or UCG-Max | 1 | $129–199 | UCG-Max has built-in 8-port switch + 2 PoE |
| 3 | **PoE Switch** | USW-Lite-8-PoE (if using UCG-Ultra) | 1 | $109 | Not needed if using UCG-Max |
| 4 | **WiFi 6 AP** | U6-Pro or U6-Lite | 1 | $99–159 | U6-Pro recommended for better range |
| 5 | **Mini PC** | GMKtec M5 Plus / Minisforum UM773 / Beelink SER5 | 1 | $300–400 | Ryzen 7, 32 GB RAM, 512 GB+ NVMe |
| 6 | **Patch Panel** | 8-port RJ45 keystone, 10-inch | 1 | $15–25 | For clean front-facing ports |
| 7 | **Keystone Jacks** | Cat6 passthrough keystones | 8 | $2 each | Color-code by VLAN if desired |
| 8 | **Shelf / Tray** | Fixed shelf for 10-inch rack | 1–2 | $15–25 | For mini PC + UCG-Ultra |
| 9 | **Power Strip** | 6-outlet slim, right-angle plug | 1 | $15 | Short power cables to keep it tidy |
| 10 | **Patch Cables** | 6" Cat6, various colors | 8 | $2 each | For internal rack patching |
| 11 | **NanoKVM** | NanoKVM / PiKVM V4 | 1 | $40–70 | Remote BIOS access (strongly recommended) |
| 12 | **Cooling Fan** | 40mm Noctua NF-A4x20 (if enclosed) | 1–2 | $15–30 | For rear exhaust if rack is enclosed |

**Estimated Total BOM:** ~$800–$1,000

---

## Optional / Future Add-ons

| Component | Purpose | Est. Cost |
|-----------|---------|----------|
| **4G USB Dongle** (connected to UCG) | Failover WAN for remote access | $50–100 |
| **Second Mini PC** | CyberOps VM host (separate hardware) | $300–400 |
| **UPS** (small, e.g., CyberPower 450VA) | Clean shutdown on power loss | $60–80 |
| **USB SSD** (1 TB) | External backup target inside rack | $60–80 |
| **LED Status Strip** | Front-panel visual health (power, VM status) | $20–30 |

---

## Vendor Links

| Component | Recommended Vendor |
|-----------|-------------------|
| 10-inch rack | Amazon / NavePoint |
| UniFi hardware | [ui.com](https://ui.com) (or B&H / Amazon) |
| Mini PC | Amazon / AliExpress (GMKtec official store) |
| NanoKVM | [sipeed.com](https://sipeed.com) or Amazon |
| Patch cables | Monoprice / Amazon (slim run cables) |

---

## Cable Length Guidelines

| Cable Run | Length | Type |
|-----------|--------|------|
| Gateway ↔ PoE switch | 6–12" | Cat6 patch |
| PoE switch ↔ Patch panel | 6–12" | Cat6 patch |
| PoE switch ↔ Mini PC | 12–18" | Cat6 patch |
| PoE switch ↔ AP | 3–10 ft | Cat6 (pre-terminated) |
| Power strip → components | 6–12" | Short IEC or figure-8 cables |
