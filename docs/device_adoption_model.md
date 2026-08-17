# BareNOC — Unified Device Adoption Model

> **Status:** design-first (no release). This document is the contract for the
> "how do I add a device" rebuild. A follow-up worker implements the vendor
> adapters and completes the SNMP/vendor_api executors from this spec — do not
> re-derive the model.
>
> Canonical vocabulary: **Discover** (find it) → **Adopt** (take ownership —
> it is *claimed*) → **Manage** (connect a control *channel* and act). See
> `docs/architecture/terminology.md`.

---

## 1. The problem this solves

Today "add a device" is four indistinguishable verbs on one page — **+ Add
Device**, **🔐 Onboard a device**, **Claim**, **Adopt** — and every one of
them is really an SSH-centric story. That works for servers and Linux boxes
but not for the rest of the network: a Juniper/Cisco/HP switch, a camera, an
IoT gadget can't run our agent and often has no general SSH. They speak SNMP,
vendor APIs (NETCONF/RESTCONF/HTTP/ONVIF), or nothing but ping.

This model collapses "add a device" into **one record, one relay, N channels**:

- **One record** — every device is a `devices` row with a `type`, a set of
  `channels` (how BareNOC can reach/control it), and an owner.
- **One relay** — the existing `device_jobs` + pi-agent runner is the single
  place jobs execute; the *executor* is chosen from the device's channels.
- **N channels** — `agent`, `vendor_api`, `ssh`, `unifi`, `snmp`, `monitor`.
- **Security-first** — after fingerprinting, BareNOC ranks the channels it
  found and **recommends one**, with security as the #1 factor; where every
  control channel is weak, it recommends **monitor-only** (no control beats
  weak control).

---

## 2. Device taxonomy

A device record declares one `device_type`. The taxonomy is **extensible**;
these are the canonical values (new values are additive — adding one never
breaks an existing device).

| `device_type` | Examples | Typical channels (suggested) |
|---|---|---|
| `server` | Linux/Windows/macOS boxes, NAS, hypervisors | `agent`, `ssh`, (`snmp` optional) |
| `switch` | Juniper/Cisco/HP/Aruba/UniFi switches | `vendor_api`, `ssh`, `snmp`, (`unifi`) |
| `ap` | Access points | `unifi`, `vendor_api`, `snmp`, `ssh` |
| `router` | Gateways, firewalls, edge routers | `vendor_api`, `ssh`, `snmp` |
| `camera` | IP cameras (ONVIF/RTSP/HTTP) | `monitor`, `vendor_api` (ONVIF), `snmp` |
| `iot` | Sensors, plugs, appliances, printers | `monitor`, `vendor_api` (HTTP/MQTT), `snmp` |
| `other` | Anything unknown/uncategorized | `monitor` |

**Legacy mapping (existing data keeps working):** `gateway` → `router`,
`workstation` → `server`, `printer`/`nas` → `iot`. The migration is *display
and suggestion* only — old rows are not rewritten; the validator and UI accept
both old and new spellings.

### Fingerprint → suggested type + channels

`fingerprint.sh` already emits JSON (`ip`, `mac`, `vendor`, `hostname`, `ttl`,
`os`, `os_reason`, `ssh_banner`, `open_ports[]`). The API layer maps that to a
**suggestion** (see `action_validator.suggest_from_fingerprint`), stored on the
record and shown in the UI, never applied destructively (it only fills
`device_type` when unknown and *adds* channels; it never removes anything).

Rules (in order — first match wins):

| Signal | Suggested type | Suggested channels |
|---|---|---|
| SNMP sysObjectID / sysdescr says switch/router (Cisco/Juniper/HP/MikroTik/…) | `switch` / `router` | `snmp`, `vendor_api`, (`ssh` if 22/tcp open) |
| 554/80/443 + camera vendor (Hikvision/Dahua/ONVIF) or RTSP | `camera` | `monitor`, `vendor_api` |
| 22/tcp + Linux/Unix SSH banner | `server` | `agent`, `ssh` |
| 22/tcp + Windows SSH banner | `server` | `ssh`, `agent` (once Windows agent ships) |
| 445/139 open (SMB) | `server` | `ssh` (if 22 open) else `monitor` |
| 5000/5001 (Synology/QNAP) | `server` | `ssh`, `snmp` |
| 9100 raw print port | `iot` | `monitor`, `snmp` |
| only 80/443, no SSH/SNMP | `iot` | `monitor`, `vendor_api` |
| nothing open | `other` | `monitor` |

The fingerprint result **also carries** (from the same function):
`suggested_type`, `candidate_channels` (ranked), `recommendation` (one channel
+ a one-line *why, security*), and `warnings` (plaintext SNMPv2c/HTTP, default
credentials, SSH-without-keys).

---

## 3. Control channels

A channel is **a way BareNOC reaches a device and acts on it**. A device can
expose several; `monitor` is always present (every device can at least be
pinged). Channels are stored as an explicit JSON list on the record plus
derived from existing credential columns (see §5 data model).

| Channel | Mechanics | Executor | Security posture |
|---|---|---|---|
| `agent` | NOC_Agent daemon polls `jobs/pull` → executes locally → `jobs/result` (mTLS cert). No inbound ports, no stored creds. | agent (existing P1b, in `agent-go/`) | **Best** — mTLS, poll-only, least-privilege, per-capability sudo |
| `vendor_api` | Vendor HTTP/RESTCONF/NETCONF/MQTT over TLS, token/cert auth. Adapter registry (`scripts/vendor_api.py`). | vendor_api executor (adapter-based) | Strong when TLS + token/cert; otherwise falls to monitor |
| `ssh` | Runner executes via SSH (key-only + scoped sudo). Juniper/Cisco/HP CLI via SSH. | ssh executor (existing `device_ssh.sh`/scripts) | OK **key-only + scoped sudo**; password SSH / default creds = explicit warning |
| `unifi` | Controller-managed: the existing UniFi sync/adopt + `unifi_*` actions. | unifi executor (existing, controller-side) | Delegated controller trust — no device creds held |
| `snmp` | Poll/GET/SET via SNMP v2c/v3. `snmpv3` (auth+priv) preferred; `snmpv2c` (plaintext community) is last-resort. | snmp executor (new, thin — `scripts/snmp_executor.py`) | `snmpv3` auth+priv OK; `snmpv2c` = explicit insecure warning |
| `monitor` | Ping/SNMP-status/port checks only — **no control**. | monitor (existing ping/status) | Neutral — the safe default when nothing secure exists |

---

## 4. Security-first channel recommendation

After fingerprinting, BareNOC ranks the channels it can actually reach and
**recommends exactly one**, security first. The fixed tier (highest first):

| Rank | Channel | One-line "why (security)" |
|---|---|---|
| 1 | `agent` | mTLS cert, poll-only (no exposed service), least-privilege actions |
| 2 | `vendor_api` | vendor RESTCONF/NETCONF/HTTP **over TLS** with token/cert auth |
| 3 | `ssh` | key-only + scoped sudo — **never** password SSH or default creds |
| 4 | `unifi` | delegated controller trust — no per-device creds held |
| 5 | `snmp` (v3) | auth+priv encryption for monitoring/limited SET |
| 6 | `monitor` | **recommended over insecure control** — no control beats weak control |
| — | `snmpv2c` / plaintext HTTP | **LAST RESORT** — explicit insecure warning, never auto-selected |

**Explicit warnings** the fingerprint output must carry (and the UI show):

- plaintext SNMPv2c community / HTTP (not HTTPS) control path;
- default credentials (admin/admin, public, ubnt/ubnt, root/no-pass);
- SSH exposed without a key (password auth) — recommend key-only;
- no secure channel available → **recommend monitor-only** and say why.

**UI contract:** the Add/Claim flow shows the recommendation prominently, as a
one-liner the user can read and act on, e.g.:

- "Best: **Install the BareNOC agent (mTLS)** — this device can run it."
- "Secure enough: **SSH key-only** (scoped sudo)."
- "Insecure — **use monitor-only instead** (this camera has only plaintext HTTP)."

The recommendation is **advisory, never enforced** — an operator can still pick
a lower tier (e.g. SNMPv2c on an air-gapped lab) — but the UI labels the
choice honestly and records the warning in the audit trail.

---

## 5. Capability model (config = capability, code = authority)

BareNOC's standing rule: **capability is config, authority is code.** The
action allowlist/param schemas remain law (unchanged); this model only adds a
*capability* check on top.

- The **device record** declares `type` + `channels` (+ vendor creds).
- The **action catalog** (`action_validator.py`) declares, per action, the
  **required channels** a device must expose for that action to be legal.
- The **validator** (`validate_channels`) enforces: when a job targets a
  device with known channels, the action's required set must intersect the
  device's channels. Channel-less actions (ping, status, fingerprint,
  appliance-side, UniFi-controller-side) keep today's behavior.

### Required-channel table (the scaffold ships this)

| Action | Required channels (any one) | Notes |
|---|---|---|
| `reboot_device` | `ssh` \| `agent` \| `snmp` \| `vendor_api` | a switch reboots via SNMP/vendor_api/ssh; a server via agent/ssh |
| `collect_logs` | `ssh` \| `agent` | |
| `apply_patch` | `ssh` \| `agent` | |
| `install_chat_client` | `ssh` \| `agent` | |
| `enroll_device` | `ssh` | SSH transport variant; `agent_install.sh` is the agent path |
| `snmp_poll` | `snmp` | |
| `ping_test` / `device_status` / `fingerprint_device` | *(none — always allowed)* | a `monitor`-only camera still gets ping/status |

Everything else (`unifi_*`, `network_*`, `system_time`, `ticket_status`,
`pi_task`, `batch`, …) is appliance-side or controller-side and has **no
channel requirement** — unchanged behavior.

### Enforcement points

1. **Worker** (`worker/main.py` → `validate_job`): the authoritative gate for
   AI-generated jobs. `MANAGED_DEVICES` entries now carry `channels`; a job
   targeting a known device with a channel-mismatched action is rejected with a
   clear message before the job file is written.
2. **API** (`action_validator.validate_job` is shared, so the same function
   gates both worker and any direct job submission).
3. **Agent** (`agent-go` embedded catalog) re-validates its own action set —
   unchanged; defense in depth.

When a target resolves to an IP that isn't in managed inventory, or the device
has no channel info yet, validation **passes through** (as today) and the
runner/agent is the fallback gate.

---

## 6. One job relay, per-channel executors

The existing **`device_jobs` table + pi-agent runner is the single relay** —
jobs are enqueued, validated, dispatched, executed, and audited in exactly one
place. What changes is **executor selection**: the runner/worker picks the
executor from the device's channels instead of assuming SSH.

### Executor map

| Channel | Executor | Lives | Status |
|---|---|---|---|
| `ssh` | `device_ssh.sh` + per-action scripts (`reboot_device.sh`, `collect_logs.sh`, …) | **host runner** (`/opt/barenoc/scripts`, pi-agent-runner) | existing |
| `agent` | NOC_Agent pull/execute/result loop (`agent-go/`, jobs/pull + jobs/result) | **endpoint** (the device); the appliance only enqueues `DeviceJob` rows | existing P1b |
| `snmp` | `snmp_executor.py` — GET/SET via `snmpget`/`snmpset` (v2c + v3) | **host runner** (`/opt/barenoc/scripts`) | **new, skeleton** (this task) |
| `vendor_api` | `vendor_api.py` — adapter registry (Juniper NETCONF, Cisco RESTCONF, HP/Aruba HTTP, ONVIF/HTTP cameras, MQTT/HTTP IoT) | **host runner** (`/opt/barenoc/scripts`) | **new, registry skeleton** (this task); concrete adapters = follow-up |
| `unifi` | existing `unifi_*` scripts (controller-side) | **host runner** → controller | existing |
| `monitor` | `ping_check.sh`, `snmp_poll.sh` (read-only), port checks | **host runner** | existing |
| `local` | appliance-side actions (system_time, ticket_status, network_info, …) | **host runner** (no target) | existing |

### How the channel hint is carried

In this scaffold the **channel hint is derived, not a job param**: the runner
resolves `target` → device record → `channels`, and the action's
`ACTION_SCRIPTS` mapping already encodes the executor (one action → one script).
Adding a *per-channel* script selection (e.g. `reboot_device` running
`snmp_executor.py` when the device is SNMP-only) is the documented follow-up:
the runner's `_build_cmd` branches on `channels` exactly once. No new job field
is required until then; if one becomes necessary, it is `params.channel` (a
hint, always overridable by the resolved device record).

### Placement rationale

SSH/SNMP/vendor_api/monitor executors are **host-side** (next to the runner)
because they need the appliance's stored credentials and shell; the **agent**
executor is **on the endpoint** by design (it dials out over mTLS); **unifi**
is **controller-side**. The appliance-side container never executes device
actions directly — it enqueues and audits.

---

## 7. The three add paths, unified

Every device ends up as **one record** with **ownership (claimed)** + **channels
(control)**. The three user-facing paths are:

### 7.1 Connect via SSH
For devices that expose SSH (servers, Linux boxes, and switches that accept
SSH CLI). Credentials are encrypted at rest; the runner acts key-only + scoped
sudo. Channels: `ssh` (+ `monitor`, optionally `snmp`).
- Manual: **+ Add a device record** then **Enable control → SSH** (paste the
  key or use the appliance key).
- Scripted: the existing `/onboard` one-click script (SSH variant).

### 7.2 Install the BareNOC agent (`agent_install.sh`)
For general-OS devices (Linux first; macOS/Windows follow the NOC_Agent plan).
Installing the agent **is** adoption: enroll cert → start service → first
report auto-claims with `method="agent"`. Channel: `agent` (the reference
client for everything below). This is the **highest-security** path and the
recommended one whenever the device can run it.

> `/onboard` stays the **SSH-only variant** (creates the `barenoc` control
> user + scoped sudo + a heartbeat). `agent_install.sh` is the **agent
> variant** (no SSH, mTLS poll loop). The two are siblings, not replacements:
> `/onboard` = "reach me over SSH", `agent_install.sh` = "manage me via the
> agent". The Devices page links both.

### 7.3 Register via API
For integrators and scripts: `POST /api/v1/devices` with a bearer token. The
NOC_Agent is the **reference client** of this API (it registers/reports over
mTLS). Channels arrive with the record or from fingerprinting afterward.

### Gear / cameras / IoT (the non-SSH reality)
These **mostly arrive via Discovery → fingerprint → pick channels**:

1. **Discover** (ping sweep / SNMP sweep / UniFi sync) finds them unclaimed.
2. **Fingerprint** (`fingerprint.sh`) suggests `type` + ranked channels.
3. **Pick channels** — SNMPv3, vendor API, or **monitor-only** (the
   recommended choice when only plaintext HTTP/SNMPv2c exists).

The Devices page therefore surfaces exactly three real choices at the top
(**Add a device record · Enroll a device — run the script · Register via API**)
and exactly two on each card (**Take ownership** vs **Enable control**), with
one-line tooltips so "claim vs adopt" is never ambiguous again.

---

## 8. Data model changes

`devices` gains one column (idempotent migration in `database.init_db`):

- **`channels`** (`JSON`, default `[]`) — the device's **explicit** channel
  declarations (e.g. `["vendor_api"]` for a NETCONF-managed switch, `["monitor"]`
  to force monitor-only). Auto-derived channels are **not** stored here; they
  come from existing columns:
  - `ssh` ⇐ `ssh_key_fingerprint` set
  - `snmp` ⇐ `snmp_community` set
  - `unifi` ⇐ `unifi_managed`
  - `agent` ⇐ `adoption_method == "agent"` or `agent_version` set
  - `monitor` ⇐ always present

The **effective channel set** = `derived ∪ explicit`, computed by
`action_validator.effective_channels(...)` and returned on every device read as
`channels`. This keeps the change backward-compatible: existing devices need no
backfill, and `ssh`/`snmp`/`unifi`/`agent` channel detection works off columns
that already exist. `vendor_api` (and future channels with no credential
column) is the reason the explicit `channels` column exists.

`device_type` already exists; its comment/canonical set widens to the §2
taxonomy (no schema change).

---

## 9. Out of scope (follow-up workers — implement from this spec)

1. **Concrete vendor adapters** — Juniper NETCONF (PyEZ), Cisco RESTCONF,
   HP/Aruba HTTP, ONVIF/RTSP camera, MQTT/HTTP IoT. The registry
   (`scripts/vendor_api.py`) is the landing spot; each adapter declares its
   channels + action methods.
2. **SNMP executor completion** — wire `snmp_executor.py` GET/SET into
   `snmp_poll`/`reboot_device` for SNMP-only gear, with v3 auth+priv param
   plumbing and the SNMPv2c warning surfaced end-to-end.
3. **Per-channel executor dispatch in the runner** — `_build_cmd` branches on
   the resolved device's `channels` (see §6).
4. **`controlled` SQL filter** — extend `_controlled_cond` to count `agent`/
   `vendor_api` from the channels column cross-DB (today it uses existing
   columns + `adoption_method == "agent"`).
5. **NOC_Agent live trial** — the first real adoption is the validation that
   the `agent` channel works end-to-end; until it runs, `agent` is "built,
   not yet proven" and the recommendation still prefers it on security grounds.
6. **Windows/macOS agent** — NOC_Agent P3 (MSI, elevation story).
