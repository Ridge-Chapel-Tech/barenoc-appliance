# PC-MINI session findings (dads-pc, 2026-09-03)

Reference notes from the dads-pc / PC-MINI session that the F8 Windows battery
was proven against and derived from. Kept as the *why* behind the defaults in
`docs/runbook/windows_pc_optimization.md`.

## What the session found

1. **Disk pressure, not age.** The "slow PC" was a nearly-full C: volume
   (under 10% free) plus two autostart offenders (Adobe CollabSync, Copilot)
   re-launching at every login — not failing hardware. This is why the battery
   leads with a read-only health report (`windows_diag`) before anything is
   touched.

2. **The DNS-through-router weak spot.** The PC's only configured DNS server
   was the router/gateway IP. Every lookup depended on the router's forwarder:
   one misbehaving/misconfigured forwarder = "the internet keeps stalling"
   with no way to tell from the PC side. The fix was to override the resolver
   to a non-router resolver (`1.1.1.1` / `1.0.0.1`) on the active adapter.

3. **A non-gigabit wired link.** The NIC reported a 100 Mbps link where a
   1 Gbps link was expected — worth surfacing as a `link_warning` (bad cable /
   port / negotiation), not silently assuming "the internet is slow".

4. **Latency split local vs. public.** Gateway pings were ~1 ms (healthy LAN);
   public-resolver pings were higher with intermittent loss (WAN/ISP-side or
   the router forwarder). Probing **both** the gateway and a public resolver is
   what separates "my Wi-Fi is bad" from "my ISP is bad".

## Decisions that became defaults

- **Diagnose, then harden, then clean** — never clean first.
- **The DNS override is opt-in + elevated-only.** It is a *change*, so it is
  owner-approved and gated on the session context (admin-vs-standard). A
  standard session reports + recommends instead of changing.
- **Never destructive.** Partition ops and uninstalls stay out of the actions
  entirely — they are manual, per-device, owner-confirmed steps.
- **Honest measurement.** Cleanup measures bytes BEFORE removal so "recovered"
  is real, and the DNS fix only fires when the weak spot is actually present.

## Traceability

| Session artifact | Shipped as |
|---|---|
| `dadpc-diagnostics.ps1` | `src/scripts/windows_diag.sh` (embedded PowerShell) |
| `fix1.ps1` (cleanup half) | `src/scripts/windows_cleanup.sh` |
| `fix1.ps1` (DNS half) | `src/scripts/windows_netdiag.sh` |
| these findings | `docs/runbook/windows_pc_optimization.md` |
