# Remote Access Procedures

**Version:** 1.0  
**Last Updated:** 2025-07-29  
**Audience:** Internal Support / Engineering

---

## Access Methods (Ordered by Preference)

| Method | Use For | Requires | Failover |
|--------|---------|----------|---------|
| **Tailscale SSH** | Daily management, updates, troubleshooting | Tailscale installed + auth | NanoKVM |
| **NanoKVM** | BIOS recovery, kernel panic, stuck boot | NanoKVM powered + network | Physical access |
| **Physical Access** | Hardware failure, dead Mini PC | Console cable or monitor+keyboard | — |

---

## 1. Tailscale (Primary)

### Initial Setup

```bash
# Install on Proxmox host (during provisioning)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --accept-routes

# Verify
tailscale status
# Output: 100.x.x.x    root@pve    linux    -
```

### Daily Access

```bash
# SSH via Tailscale IP
ssh root@100.x.x.x

# Transfer files
scp /path/to/file root@100.x.x.x:/opt/barenoc/

# Port forward (web UI access without opening firewall)
tailscale serve --https 8006 localhost:8006
```

### Customer remote support (vendor support tailnet)

The appliance can join a **vendor support tailnet** on the customer's explicit
consent (Settings → Support → **Remote support**, default OFF). The node is
`bareNOC-<appliance-id>`, tagged `tag:appliance`, and joins via a **tagged,
expiring, revocable auth key** stored 0600 in
`/opt/barenoc/volumes/secrets/tailscale.json`.

- The provision step (`provision_agent.sh`) installs tailscale + performs the
  idempotent join on every deploy — never blocks the deploy on failure.
- The customer toggle runs `tailscale up/down` via a host-side reconciler
  (`tailscale_remote_support.sh`, a systemd timer every 60 s).
- **ACL config (tailnet admin console):** the vendor's support user may reach
  `tag:appliance` nodes ONLY — never a customer's LAN or other tags. Keep the
  support user key-only SSH + 2FA; revoke it in the tailnet console when an
  engineer rotates off support.
- **Auth-key rotation cadence:** rotate the tagged auth key at least every 90
  days (or immediately on any suspected exposure). The key is single-use by
  default — a rejoin after `tailscale down`/up is fine (the node keeps its
  identity), but a fresh key is required for a NEW appliance or after a node
  is fully removed. Rotate by replacing `auth_key` in
  `/opt/barenoc/volumes/secrets/tailscale.json` (0600) and re-running
  `tailscale_remote_support.sh reconcile`.
- **Beta grant:** the `support` gate is beta-open via an expiring
  `support_grant` (0600 in `support_grant.json`). At GA the grant expires and
  the Support-subscription entitlement check takes over.

### Managing Multiple Appliances

Create an SSH config:

```bash
# ~/.ssh/config on your dev machine
Host barenoc-customer1
    HostName 100.64.0.1
    User root
    IdentityFile ~/.ssh/barenoc_admin

Host barenoc-customer2
    HostName 100.64.0.2
    User root
    IdentityFile ~/.ssh/barenoc_admin
```

Then: `ssh barenoc-customer1`

---

## 2. NanoKVM (Backup)

### Access

1. Open browser to `https://<nanokvm-ip>:443`
2. Log in with admin credentials
3. You'll see a virtual console of the Mini PC's screen

### What You Can Do via NanoKVM

- ✅ View BIOS boot process
- ✅ Access GRUB menu
- ✅ Emergency shell (Proxmox recovery mode)
- ✅ Power cycle the Mini PC
- ✅ Mount ISO images remotely
- ✅ Send keyboard input (Ctrl+Alt+Del, etc.)

### NanoKVM Network Config

```bash
# Default IP: 10.0.10.240 (set during provisioning)
# If lost, check your router's DHCP leases
# The NanoKVM hostname usually appears as "nanokvm" or "sipeed"
```

---

## 3. Physical Access (Emergency)

### Console Access

Instruct customer or on-site technician:

```
1. Plug a monitor into the Mini PC's HDMI port
2. Plug a USB keyboard into the Mini PC
3. Power cycle the appliance

You should see:
- Proxmox boot screen (GRUB)
- After boot: Proxmox login prompt
- Or: VM console output
```

### Emergency Recovery via Console

At the Proxmox terminal:

```bash
# If Proxmox is running but VM is stuck:
qm stop 100
qm start 100

# If Proxmox won't boot:
# Reboot and hold Shift for GRUB
# Select "Advanced options" → "Recovery mode"
# Or append "init=/bin/bash" to kernel command line

# If filesystem is corrupted:
fsck /dev/rpool/ROOT/pve-1
```

---

## Access Without Internet

If the customer site has no internet, Tailscale won't work. In this case:

1. **NanoKVM** works on the local network (no internet needed)
2. **Customer can provide VPN access** into their network
3. **Physical dispatch** for critical issues

---

## Security Notes

- All remote access is logged in the **Proxmox host audit log**
- NanoKVM has its own login — do not reuse the Proxmox root password
- Tailscale nodes are tagged `tag:appliance` for organization
- Customer remote support is **default OFF** and customer-controlled; the
  toggle + audit log make every up/down visible
- The support tailnet is **ACL-scoped to appliance nodes only** — the vendor
  support user never reaches the customer's LAN
- **Disable root password SSH login** after Tailscale is set up:

```bash
# /etc/ssh/sshd_config
PermitRootLogin prohibit-password  # key-based only
PasswordAuthentication no
```

- Keep NanoKVM firmware updated (check [sipeed.com](https://sipeed.com) periodically)
