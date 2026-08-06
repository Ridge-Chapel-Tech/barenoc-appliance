# Workflows

This page maps the core BareNOC workflows. Every diagram is interactive in your
browser — the same flows drive the appliance day-to-day.

## The three roles

BareNOC separates the people/agents in the loop:

```mermaid
flowchart LR
    U["Customer / Operator"] -->|"chat client · web · (future) email"| QM["Queue Manager (Juniper)"]
    QM -->|"open · assign · prioritize · close · status"| T[(Ticket Queue)]
    T --> AI["AI Technician"]
    AI -->|"legal + doable, high confidence"| ACT["Run approved action"]
    AI -->|"needs approval or blocked"| HT["Human Tech"]
    HT -->|"approve · guide · act"| AI
    ACT -->|"ask customer to verify"| U
    AI -->|"needs more information"| U
```

| Role | Does | Never does |
|------|------|-----------|
| **Queue Manager (Juniper)** (chat persona) | Answers ticket/queue questions; opens, assigns, prioritizes, closes tickets; status updates | Pretends to know network facts — everything else becomes a ticket |
| **AI Technician** (worker) | Reads tickets, judges "legal/doable", runs approved actions, asks the customer to verify, escalates to human tech | Guesses or acts outside the approved action list |
| **Human Tech** (you) | Approves, retries, or closes escalated tickets; guides the AI | — |
| **Lily** (autonomous, experimental) | Reads tickets and works them with full tools (bash, API, controller) — diagnosis, investigation, multi-step tasks — then writes the outcome into the ticket | Only used in **Autonomous** mode with `PI_AGENT_ENABLED`; nothing is gated in this mode (see the Autonomy Policy) |

> How much the AI may do *without* a human is a per-deployment choice — see the
> **[Autonomy Policy](/wiki/autonomy)** (autonomous / balanced / strict), including
> the experimental **Lily** mode.

## Ticket lifecycle

```mermaid
flowchart LR
    A["Open<br/>created by user / alert"] -->|"AI Tech picks up"| B["In Progress<br/>AI working"]
    B -->|"needs human approval / blocked"| C["Escalated<br/>human tech"]
    C -->|"approved"| B
    B -->|"action done — ask customer to verify"| D["Customer Action<br/>waiting on customer"]
    D -->|"customer replies / confirms"| B
    B -->|"customer confirms ok-to-close"| E["Closed"]
    C -->|"human closes"| E
    D -->|"human closes"| E
```

## AI Technician pipeline

```mermaid
flowchart LR
    T[Ticket] --> SAN[Sanitizer]
    SAN --> LLM[LLM judgment]
    LLM -->|"not legal / not doable"| ESC[Escalated — human tech]
    LLM --> GATE{Confidence gate}
    GATE -->|"≥0.95 (or read-only ≥0.80)"| EXEC[Auto-execute action]
    GATE -->|"0.80–0.95"| APPR[Escalated — approval]
    GATE -->|"<0.80"| ESC
    EXEC --> DONE[Action runs]
    DONE --> FEED[Customer Action — verify]
    FEED -->|"confirmed"| CLOSE[Closed]
    APPR -->|"approved"| EXEC
```

## Discovery & device onboarding

```mermaid
flowchart LR
    UNIFI["UniFi controller"] -->|"sync every 5 min"| INV[(Inventory)]
    SCAN["Ping scan"] -->|"unclaimed"| INV
    INV -->|"fingerprint (nmap)"| ID["Identified<br/>vendor · OS · open ports"]
    ID -->|"claim + configure"| M[Managed device]
    M -->|"monitor · poll · act"| NOC[NOC / AI Tech]
```

## Management-plane onboarding lockdown

```mermaid
flowchart LR
    DEPLOY["Fresh deployment<br/>Management ACCESS = open"] --> ADOPT["Admin logs in, adopts endpoints"]
    ADOPT --> DESIGNATE["Designate admin terminals<br/>DHCP-reserve → address group → allow rule"]
    DESIGNATE --> LOCK["Lock management<br/>firewall rules deny other VLANs"]
    LOCK --> VERIFY["BareNOC reads posture<br/>verifies lockdown"]
```

## Backups

```mermaid
flowchart LR
    DB[(SQLite + config + secrets)] -->|"every 6h, 30-day retention"| APP[App backup]
    VM[VM snapshot] -->|"daily 1 AM, keep 7"| HOST[(Proxmox host)]
    HOST -->|"weekly Sun, keep 4"| USB[USB drive — LUKS]
    APP --> USB
    USB -->|"drill"| RESTORE[Restore drills]
```

## Chat answer-or-escalate

```mermaid
flowchart LR
    MSG[Chat message] --> Q{ticket/queue question?}
    Q -->|yes| ANS[Answer from the queue]
    Q -->|no| TK[Ticket — new or appended]
    TK --> AI[AI Technician]
    AI -->|"answers / acts"| FEED[Customer Action — verify]
    FEED -->|"confirmed"| CLOSE[Closed]
```

## Monitoring & alerts

- **Device reachability** — the scheduler health-checks the fleet (ping/SNMP/
  UniFi status); devices you opt in via the 🔔 bell get DOWN / RECOVERED
  **emails** (no ticket — by design).
- **Internet / ISP link** — the alert engine probes the LAN gateway + a public
  host (`1.1.1.1`) every minute. The UniFi gateway stays ONLINE during an ISP
  outage (only WAN dies), so device monitoring alone would never catch it.
  On 3 consecutive probe failures it opens a **P1 "Internet connectivity down"
  ticket** + alert email (distinguishes ISP/service vs link/physical), and
  auto-closes it when connectivity returns. Config: `INTERNET_PROBE_*` in
  `.env` (Settings-independent for now).
- **LLM provider outage** — if the whole provider chain is down, the worker
  opens a deduped **P1 ticket** + alert email (see [Autonomy](/wiki/autonomy)).
- **Ticket lifecycle** (Settings → Tickets) — tickets handed to a human
  (escalated) or waiting on the customer get a **check-in** note + email every
  per-priority interval (P1 hourly, P2 4 h, P3/P4 daily); tickets the AI
  resolved are **auto-closed** after a per-priority hold (default 3 days). Both
  are fully configurable per priority, and the audit trail records
  `ticket_checkin` / `ticket_autoclosed`.
- **Reporting** — the Dashboard's **Performance & Reporting** section tracks
  action time (first response + resolution), escalations, closures, reopens,
  auto-closes and LLM cost; charts (created vs resolved, priority mix,
  resolution time) and **CSV/PDF downloads** for business reporting.

---
*See [Tickets](/wiki/tickets) for status details, [Chat Client](/wiki/chat-client)
for the messaging flow, and [Security](/wiki/security) for the lockdown design.*
