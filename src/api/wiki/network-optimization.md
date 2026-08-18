# Network Optimization

A scheduled, **read-only** audit of your network gear that produces a best-practice
report with a score and prioritized findings. It's strictly informational — it never
changes anything, opens tickets, or sends alerts. It exists to answer: *"how healthy and
well-configured is my network, really?"*

> Admin-only: the tab appears on the Dashboard only for admin users.

## What it scans

**Network gear only** — gateway/router, switches, and access points, plus the features they
handle (VLANs, SSIDs, WAN). Endpoints, servers, and client devices are **not** scanned.

- **UniFi gear** — device health, port/link status, WAN, VLANs, SSIDs (from the controller)
- **SNMP devices** — interface status, errors/discards, speed/duplex (works for other
  vendors too)
- **Reachability/ports** — a light `nmap` pass (`-T3`, capped, no intrusive probes)

The appliance itself is **always excluded** (self-protection) — it can never scan itself.

## Running a scan

- **Run now** — the button on the Network Optimization tab. A scan of a typical home
  network takes 1–3 minutes; you can watch progress and cancel.
- **Scheduled** — enable a recurring schedule (day + hour, **local time**) or a one-time
  run. Default suggestion: weekly at 03:00.

## The score

The report scores your network **0–100** (baseline 100, penalties per finding):

| Severity | Impact | Example |
|---|---|---|
| **Critical** | −20 each (big drop) | A device unreachable that should be up |
| **Warning** | −5 each (stacks) | HTTP management exposed, repeated link flaps |
| **Info** | capped (small, never tanks the score) | Single uplink, unnamed ports, unused VLANs |

A healthy home network should score **90+** — a low score means *real* config concerns to
look at, not noise. Performance and security are scored separately so you can see where the
points went.

## Findings

Findings are grouped by severity and category:

- **Performance** — link speed/duplex mismatches, interface errors, congestion, high CPU/mem
- **Security** — exposed SSH/HTTP management, weak/legacy settings, stale firmware
- **Reliability** — link flaps, single WAN/uplink, persistent issues
- **Hygiene** — unnamed ports, unused VLANs, disabled SSIDs, stale records

Each finding has a stable key, a severity, and the evidence behind it — so you can act on
it (and later releases can link findings to fixes).

## Safety guarantees

- **Read-only** — nothing is changed on your gear
- **No alerts** — findings never open tickets or send emails
- **Self-protection** — the appliance excludes itself, always
- **Conservative scans** — capped hosts, light timing, no intrusive probes; you control the
  scan profile, concurrency, and schedule
