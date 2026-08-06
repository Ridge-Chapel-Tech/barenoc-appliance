# Access Control

**Version:** 1.0  
**Last Updated:** 2025-07-29

---

## Access Matrix

| Interface | URL | Port | Protocol | Who Can Access | Auth Method |
|-----------|-----|------|----------|---------------|-------------|
| **Proxmox Web UI** | `https://192.0.2.95:8006` | 8006 | HTTPS | You only (admin) | Root password |
| **BareNOC Web UI** | `https://barenoc.local` | 443 | HTTPS | Customer IT staff | JWT (local accounts) |
| **UniFi Controller** | `https://192.0.2.1:443` | 443 | HTTPS | Customer IT staff | Local UniFi account |
| **SSH (Proxmox)** | `192.0.2.95:22` | 22 | SSH | You only | SSH key |
| **SSH (BareNOC VM)** | `192.0.2.207:22` | 22 | SSH | You only | SSH key |
| **NanoKVM** | `https://10.X.10.240:443` | 443 | HTTPS | You only | Local account |

---

## Authentication Methods

### Internal (BareNOC Web UI)

- JWT tokens stored in httpOnly, Secure, SameSite=Strict cookies
- Access token: 60 minute expiry
- Refresh token: 7 day expiry (rotated on use)
- Passwords hashed with bcrypt (cost factor 12)

### Remote Access (You/Admin)

#### Primary: Tailscale

```bash
# Install on Proxmox host
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh
```

- Each appliance gets a Tailscale IP (100.x.x.x)
- You SSH via: `ssh root@100.x.x.x`
- Tailscale handles auth, encryption, and NAT traversal
- No open firewall ports needed

#### Backup: NanoKVM (Out-of-Band)

- Connects to Mini PC via HDMI + USB
- Gives you full KVM (keyboard, video, mouse) over IP
- Accessible via web browser
- Use for: BIOS recovery, kernel panic, stuck boot

#### Emergency: Physical Access

- Customer can plug in a monitor + keyboard to the Mini PC
- Proxmox root password works at the console
- Instruct customer: "Plug monitor into the small black box, call support"

---

## Credential Storage

| Credential | Where It's Stored | Encryption |
|-----------|------------------|------------|
| Proxmox root password | Your password manager | — |
| UniFi admin password | `.env` file on VM | AES-256 (Fernet) |
| DeepSeek API key | `.env` file on VM | AES-256 (Fernet) |
| Gmail SMTP password | `.env` file on VM | AES-256 (Fernet) |
| SSH keys for endpoints | `/opt/barenoc/volumes/secrets/ssh/` | Encrypted at rest |
| JWT signing secret | `/opt/barenoc/volumes/secrets/jwt.key` | File permissions 0600 |

### Password Rotation

| Credential | Rotation Period | Method |
|-----------|----------------|--------|
| Proxmox root | Every 90 days | Manual via SSH |
| JWT signing secret | Every 30 days | `openssl rand -hex 32 > /opt/barenoc/volumes/secrets/jwt.key` |
| DeepSeek API key | Every 180 days | Via DeepSeek dashboard |
| Endpoint SSH keys | Per-device (when compromised) | Regenerate and redeploy |
