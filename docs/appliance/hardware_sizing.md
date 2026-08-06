# Hardware Sizing Guide

**Last Updated:** 2026-08-05

BareNOC is a lightweight Python/Docker appliance — SQLite, a FastAPI web app,
a poll worker, a scheduler, and the host-side Pi agent. It is **not** the
bottleneck on any realistic network; the sizing below is about headroom,
snapshot/backup costs, and the network gear it manages.

The `--profile` flag on `proxmox/barenoc-appliance.sh` sets the VM specs
directly (vCPU / RAM / disk). The table mirrors it.

---

## Profiles

| Profile | Endpoints* | vCPU | RAM | Disk | Appliance class | Typical network gear |
|---------|-----------|------|-----|------|-----------------|----------------------|
| **s** — Home / small office | ≤10 | 1 | 2 GB | 30 GB | Mini PC (N100/N150, 8 GB), can run **bare-metal without Proxmox** | 1 UniFi gateway (UCG), 1 switch, 1–2 APs |
| **m** — SMB | ≤50 | 2 | 4 GB | 40 GB | Mini PC (Ryzen 5 7xxx / i5), **Proxmox + VM** (the live reference config: GMKtec M5 Plus, 2 vCPU/4 GB/40 GB) | UCG-Max, 2–4 switches, 3–6 APs, NAS, 10–40 clients |
| **l** — Larger SMB / multi-VLAN | ≤200 | 4 | 8 GB | 80 GB | NUC/small tower (i5/i7, 16 GB) | UniFi Dream Machine Pro, L2/L3 switches, 6–12 APs, VLANs, 50–200 clients |
| **xl** — MSP / multi-site controller | ≤500 | 6 | 16 GB | 160 GB | Small tower/server (Xeon/Ryzen, 32 GB) | Multiple sites aggregated, 12+ APs, heavy alerting + history |

\* *Endpoints ≈ adopted/managed devices + online clients. Offline clients in
the inventory don't cost much; SNMP/UniFi polling and ticket volume scale with
active endpoints.*

---

## Notes & rules of thumb

- **2 GB (profile s) is the floor** — Docker images + SQLite + the pi runtime
  fit, but leave room for apt/docker update headroom. If you're at 1 GB, use 2.
- **Disk ≥ 30 GB always** — the OS + images (~8 GB) plus daily Proxmox
  snapshots (vzdump) need space. The 40 GB reference VM uses ~26% with months
  of history.
- **Storage tiering**: put the VM on SSD/NVMe (`ssd=1` — the appliance script
  does this). Spinning disks are fine for the backup target, not the VM disk.
- **Proxmox vs bare-metal**: Proxmox earns its keep at profile **m+** — VM
  snapshots before updates, vzdump backups, USB-tier backups, easy rebuilds.
  For profile **s** a single mini PC running Ubuntu directly is simpler and
  the appliance script's `--skip-app` still applies (it assumes Proxmox `qm`
  today; bare-metal is manual per `docs/appliance/*`).
- **The appliance script's defaults** (`m`) are the proven live config —
  change them only when you know the endpoint count.
- **Network gear matters more than the box.** Sizing the UniFi stack (gateway
  model, switch PoE budget, AP count) is the real determinant of a happy
  network; see `docs/appliance/hardware_bom.md`.

---

## Reference: the live deployment (2026-08)

- Host: GMKtec M5 Plus (Ryzen 7 5825U, 32 GB, 953 GB NVMe)
- Proxmox VE 9.2, VM 100 = barenoc (2 vCPU / 4 GB / 40 GB), Ubuntu 24.04
- Network: 192.0.2.0/24 (example), UCG-Max gateway, 2 switches, 4 APs, ~35 inventory
  devices, ~9 online clients — profile **m**, ~23% memory, ~26% disk used.

## Buying guide (price class, 2026)

| Profile | Suggested hardware | Approx. cost |
|---------|-------------------|--------------|
| s | Beelink/GMKtec N100 mini PC (8 GB/500 GB) | $150–220 |
| m | GMKtec M5 Plus / Beelink SER5 (16 GB/500 GB) | $250–350 |
| l | Intel NUC 13 / Minisforum (32 GB/1 TB) | $500–800 |
| xl | Used SFF server (Xeon E-2xxx / Ryzen, 64 GB, 2× NVMe) | $800–1500 |
