# NOC Agent (endpoint agent)

**NOC Agent** is a small agent you install on an endpoint (Linux, macOS,
Windows) so BareNOC can manage it without inbound SSH and without storing any
credentials for it. The agent **dials out** to the appliance over a device
certificate (mTLS) and executes safe, approved actions locally.

## Why use it

| | SSH adoption | NOC Agent |
|---|---|---|
| Reaches the device | The appliance must reach it (breaks behind NAT / guest wifi) | The agent dials out — works anywhere |
| Credentials | SSH key stored on the appliance (encrypted) | None — identity is the device certificate |
| Health | Ping answers | Real host facts + heartbeat |
| Offline work | No | Jobs buffered until it reconnects |

## How adoption works (install = adopt)

1. On the device, run the **one-command installer** shown by the appliance (the
   same self-service flow as `/onboard` — it fetches everything from the
   appliance, no internet needed on the device).
2. The installer enrolls a **short-lived device certificate** (one-time
   enrollment token) and starts the agent as a background service.
3. The agent's **first heartbeat auto-claims** the device with
   `method="agent"` — no SSH, no manual claim step.

## Heartbeat & jobs

- The agent polls every **30 seconds** and posts a heartbeat + host facts (OS,
  kernel, IPs, disk) so the device shows **online** with real data.
- The appliance can enqueue jobs for the device; the agent pulls, runs, and
  reports each one:

| Job | What it does |
|-----|--------------|
| `collect_logs` | Gathers recent logs from the device |
| `check_updates` | Reports what could be updated (read-only — never installs) |
| `report_facts` | Re-reports host facts |
| `reboot` | Reboots the device (confirm-gated, capability-scoped) |

- Jobs are scoped to the device only, deduped, and deadline-limited. The agent
  re-validates every job against its own allowlist — neither side alone can
  widen what runs.

## Security model

- **No stored credentials** — identity is the short-lived mTLS cert (auto-renewed).
- **Least privilege** — the agent runs unprivileged, with capability-scoped
  permissions per action.
- **Revocation is instant** — revoking the device de-trusts it at the API layer
  even if its cert is still valid; the agent stops on the next 403.
- **Self-protection** — the agent is an *endpoint* agent; the appliance itself
  is never an agent target.

## Adoption methods compared

| Method | Control path | Credentials on appliance |
|--------|--------------|--------------------------|
| **agent** | Endpoint daemon (mTLS, dials out) | None |
| **cert** | mTLS report/heartbeat | None (optional control key) |
| **ssh** | Stored SSH key | Yes (encrypted at rest) |
| **unifi** | Controller | Controller creds |
| **monitor** | Ping/status only | None |

## Who can do this

- **Admin / operator** can mint an enrollment token and adopt a device.
- **Self-service**: a device user can run the installer from `/onboard` — the
  first heartbeat claims it (`method="agent"`).
