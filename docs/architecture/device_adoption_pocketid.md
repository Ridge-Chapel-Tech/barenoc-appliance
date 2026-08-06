# Device Adoption via Pocket ID (design)

**Status:** Design — agreed direction (2026-08-05)
**Principle:** Pocket ID (passkeys/WebAuthn) is the **gold-standard identity
layer** for device adoption. SSH remains an *optional transport/control*
channel for gear that can't do passkeys — it is not the adoption mechanism.

---

## 1. The model

Adoption = **binding a BareNOC inventory record to a verified Pocket ID
identity**. A device is "adopted" when it has a Pocket ID identity (passkey
enrolled) linked to its record. Adoption is an **identity act**, not a
credential-storage act.

```
        operator / device user            admin (BareNOC UI)
                 │                              │
                 │  enrolls passkey             │  "Adopt with Pocket ID"
                 ▼                              ▼
          ┌───────────────┐   creates device    ┌──────────────────┐
          │ Pocket ID     │◄──── identity ──────│ BareNOC          │
          │ (WebAuthn)    │                     │  devices record  │
          │ device users  │  link + verify      │  (adoption state)│
          └──────┬────────┘ ──────────────────► └──────────────────┘
                 │  adopted = verified identity bound to record
                 ▼
      device gets: group-scoped role · agent token ·
      SSH control only where passkey isn't feasible (fallback)
```

Key clarification (why this is realistic): WebAuthn passkeys are
**user-presentation** credentials — a headless switch can't tap a security key
per API call. So the gold-standard pattern is:

1. **Enrollment** (passkey-verified, human-presented): an operator/admin proves
   possession of the device + a Pocket ID passkey → BareNOC creates/locates the
   device's Pocket ID identity and **links** it to the inventory record.
2. **Runtime** (machine-to-machine): the *link* is the trust anchor. From it,
   BareNOC issues the device scoped credentials — an agent API token (scoped to
   that device's group) and, where useful, short-lived SSH certs — so headless
   operation works without passkeys per request.
3. **SSH as fallback**: devices without WebAuthn capability (switches, APs,
   printers) keep SSH keys as a transport credential — but only after adoption
   (the record is trusted), never as the adoption itself.

This matches the already-built pieces: Pocket ID groups gate **who** controls a
device, and approving a control action on a grouped device already requires
passkey auth. Device adoption extends the same identity to **what** is managed.

---

## 2. Adoption paths

| Path | Who | For |
|------|-----|-----|
| **Pocket ID (primary)** | Admin enrolls in the UI (or device operator via link) | Endpoints with passkey capability: servers, workstations, NAS, Linux boxes |
| **UniFi controller (auto)** | Auto-adopt on sync (unchanged) | UniFi gear — the controller *is* the identity for those devices |
| **SSH (fallback)** | Manual claim with SSH key (unchanged, existing UI) | Gear with no WebAuthn path (switches/APs/printers) or interim |
| **Manual (no identity)** | Claim without creds | Monitoring-only inventory (never adopted) |

A device is **Onboarded/controlled** if it has *any* of: Pocket ID identity
linked, UniFi-managed + claimed, or SSH creds — with Pocket ID shown as the
highest trust tier in the UI (badge: 🔑 Pocket ID).

---

## 3. Data model (planned)

```python
# Device additions
adoption_method = Column(String(16), default="none")  # pocket_id | unifi | ssh | manual
pocket_id_user_id = Column(String(128), nullable=True) # Pocket ID user for this device
pocket_id_enrolled_at = Column(DateTime, nullable=True)
pocket_id_last_seen = Column(DateTime, nullable=True)
# existing: claimed, unifi_managed, ssh_user, ssh_key_fingerprint, device_group
```

Pocket ID side: device identities are Pocket ID **users** named
`device-<hostname>` in a dedicated group (e.g. `bareno c-devices`), with
passkeys enrolled; they are *not* human logins (no email login, no admin UI
access). `OIDC_GROUP_DEVICE` maps them to a scoped BareNOC role.

---

## 4. API & flow (planned)

- `POST /api/v1/devices/{id}/adopt/pocket-id` (operator+) — ensures the Pocket
  ID identity exists (creates `device-<hostname>` user + group), returns an
  **enrollment URL** (BareNOC → Pocket ID passkey registration).
- `GET /api/v1/devices/{id}/adoption` — status: none / enrolling / linked /
  revoked, plus last-seen + method.
- `POST /api/v1/devices/{id}/adopt/verify` — after enrollment completes,
  BareNOC re-checks the Pocket ID userinfo (groups + sub) and **links**:
  `claimed=true`, `adoption_method=pocket_id`, `pocket_id_user_id=…`.
- `DELETE /api/v1/devices/{id}/adopt` — revoke: unlink + (optionally) disable
  the Pocket ID user; device returns to unclaimed unless another method holds.
- **Device principal auth** (later phase): a device-bound token (agent) minted
  from the linked identity — scoped to the device's group, short TTL,
  refreshable only by the linked identity.
- Agent SSH actions keep working via stored keys — but the runner can require
  `adoption_method != none` for write actions (configurable policy).

---

## 5. Phases

| Phase | Work | Blocker |
|-------|------|---------|
| **E** | Finish human passkey login E2E: register the BareNOC OIDC app in Pocket ID (`pocket-id.barenoc.local:8443` admin), set `OIDC_*` in `.env`, test login. **Provider is already provisioned** — only the app registration + keys remain. | browser at `https://192.0.2.207` (admin session) |
| **F** | Device identities: Pocket ID API/UI for device users+group; `adoption_method` + `pocket_id_user_id` columns; enroll/verify/revoke endpoints + Devices-page "Adopt with Pocket ID" button + badge. | Phase E working |
| **G** | Device principal auth: device-scoped agent tokens from the linked identity; optional SSH-cert issuance for adopted devices. | Phase F |
| **H** | Lifecycle: revocation drill, passkey rotation, offboarding (delete → unlink → re-enroll), audit events for adopt/revoke. | Phase G |

---

## 6. Security notes

- **Device users are not humans**: no password login path, no web UI access,
  only passkey + group membership. BareNOC never stores their passkeys —
  Pocket ID does.
- **Revocation is the killer feature**: unlink + disable in Pocket ID instantly
  de-trusts a device (vs SSH keys, which must be tracked down). This is the
  argument for Pocket ID as gold standard.
- Adoption events (enroll/link/revoke) are hash-chained audit entries like all
  other sensitive ops.
- SSH fallback stays but is second-class: a device with only SSH creds is
  "SSH-controlled"; Pocket ID adoption is the preferred badge.
