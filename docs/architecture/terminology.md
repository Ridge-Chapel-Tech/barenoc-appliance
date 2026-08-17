# BareNOC Terminology

> See also `docs/device_adoption_model.md` — the unified model for adding and
> controlling devices of any type (servers, switches/APs/routers incl.
> Juniper/Cisco/HP, cameras, IoT).

**Adopt** means *added to BareNOC's monitored inventory* — its own record, with
a lifecycle (enrolled → linked → revoked). Adoption is **BareNOC's act**, never
a controller's. UniFi "adoption" is only a *discovery source* whose gear
BareNOC auto-adopts.

| Term | Definition | Channels / methods |
|------|-----------|--------------------|
| **Discover** | Active scanning to find what's on the network | nmap / ping-sweep / **SNMP sweep** / UniFi sync / DHCP leases / manual add |
| **Adopt** | Add to BareNOC's monitored inventory (its record + lifecycle + revocation) | **certificate** (step-ca, preferred), **SSH** (claimed with creds), **UniFi-managed**, **manual** (monitoring-only) |
| **Manage** | Control a device — run actions on it | **agent** (NOC_Agent mTLS poll), **SSH** (reboot / collect logs / patch / enroll), **vendor_api** (NETCONF/RESTCONF/ONVIF/HTTP), **SNMP** (v3 poll/SET), UniFi API for UniFi gear |

## How they compose

```
Discover ──► Adopt ──► Manage
  scan        BareNOC      control
  (find)      inventory    (act)
```

- A device can be **adopted without manage** (monitoring-only: ping/SNMP + UniFi status).
- **Manage requires adoption** (the record is trusted first) + a control channel (SSH creds).
- **Revocation** (revoke adoption) de-trusts a device instantly; manage dies with it.

## The adoption badge (Devices page)

- 🔐 **cert** — holds a short-lived cert from the internal CA (mTLS identity).
- **agent** — NOC_Agent daemon (mTLS poll loop; the preferred channel).
- **SSH** — BareNOC has its SSH creds (control).
- **vendor API** — managed via a vendor adapter (NETCONF/RESTCONF/HTTP).
- **SNMP** — poll/GET/SET (v3 auth+priv preferred; v2c warned).
- **UniFi** — managed via the controller (status + API control).
- **(no badge)** — manual / monitoring-only.

## Discovery sources (non-UniFi friendly)

The appliance is source-agnostic: `DISCOVERY_SUBNETS` (multi-VLAN ping sweep),
SNMP sweep (gear identifies itself), UniFi sync (when present), DHCP leases
(future), manual. Nothing requires UniFi.
