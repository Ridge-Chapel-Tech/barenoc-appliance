# Windows PC Optimization Playbook (F8 expansion)

> **Status:** runbook for the FULL PC diagnostic/optimization battery, built on
> top of the F8 v1 device actions (`windows_diag` + `windows_cleanup`).
> Reference material for the original dads-pc / PC-MINI session lives in
> `reference/dads-pc/` (see §6). The **canonical, executed** logic lives in
> `src/scripts/windows_diag.sh`, `windows_cleanup.sh`, and `windows_netdiag.sh`
> — the `.ps1` files under `reference/` are annotations of that session and are
> **never executed by the appliance**.

This is the reusable runbook an operator follows to take a "my PC is slow /
my internet is flaky" ticket from report to resolution on an adopted Windows
PC. Every step is safe-by-default; the only destructive classes (partition
ops, uninstalls) have **no code path** and always require a separate,
per-device owner confirmation outside the actions.

---

## 1. The battery, in order

| # | Action | What it does | Write? | Default |
|---|---|---|---|---|
| 1 | `windows_diag` | Health report: volumes/disk-full, top CPU+RAM, startup items, Defender status + signature age, 7-day critical/error events, boot times, SMART | read-only | always safe |
| 2 | `windows_netdiag` | Network/DNS report: NIC link rate, latency probes (gateway + public resolvers), DNS-through-router detection | read-only by default | report-only |
| 3 | `windows_netdiag` + `apply_dns_fix` | The 1.1.1.1 fix — override the router-as-resolver with a non-router resolver | write (gated) | opt-in + elevated |
| 4 | `windows_cleanup` | Stop + remove autostart for known offenders (Adobe CollabSync, Copilot), clear TEMP + recycle, report bytes recovered | write (safe) | low-usage window |

**Order matters:** diagnose first (`windows_diag`, `windows_netdiag`), then
harden (`windows_netdiag` + `apply_dns_fix`), then clean (`windows_cleanup`).
Never start with cleanup — the diagnostics tell you *why* it was slow.

---

## 2. Safe defaults (what the operator does NOT change)

1. **Read-only first.** `windows_diag` and a bare `windows_netdiag` never
   write. They always run first and their output is what justifies the next
   steps.
2. **The DNS fix is opt-in.** `windows_netdiag` with `apply_dns_fix=true` is
   the only way the resolver is touched; a bare run only reports + recommends.
3. **The DNS fix never rewrites a healthy config.** It only fires when the
   PC's DNS is pointed at the router/gateway (the weak spot). A PC already on
   `1.1.1.1` / `8.8.8.8` is left alone.
4. **Cleanup is allow-listed + measured.** `windows_cleanup` only touches the
   offender list (configurable) + TEMP + recycle bin, and measures BEFORE
   removal so "recovered" is honest. It never uninstalls software.
5. **Never destructive.** Partition operations and software uninstalls have no
   code path in any of these actions. If the battery shows a failing disk or a
   program that should go, that is a **separate, owner-confirmed manual step**
   (see §5).

---

## 3. Admin-vs-standard session pattern

The Windows onboard flow creates a `barenoc` control user in the local
`Administrators` group (key in `C:\ProgramData\ssh\administrators_authorized_keys`),
so an SSH session is **elevated by default**. But a device can be re-configured
by its owner to a standard account, so the actions never assume elevation:

- **Every report captures the session context** (`elevated: true|false`) from
  `WindowsPrincipal.IsInRole(Administrator)`.
- **Elevated writes are gated twice.** `Set-DnsClientServerAddress` (the only
  elevated operation in the battery) runs only when BOTH
  `apply_dns_fix=true` AND the session is elevated.
- **Standard sessions degrade gracefully.** A non-admin session reports the
  weak spot + the exact recommended fix, and sets `dns_fix.applied=false` with
  a plain-language reason ("re-run elevated, or apply manually"). The result
  is posted back to the owner either way.

The rule to remember: **detect the session, gate the elevated command, report
which gate held.** That is the whole pattern, and it lives in
`windows_netdiag.sh` (the `$report.elevated` capture + the gated
`Set-DnsClientServerAddress` block).

---

## 4. The DNS-through-router weak spot + the 1.1.1.1 fix

**Weak spot:** the PC is configured to use the router/gateway IP as its DNS
server. The router's forwarder is then a single point of failure and a hidden
dependency — when it misbehaves, every lookup stalls, and it leaks/answers
queries the operator can't see. `windows_netdiag` flags this as
`dns.via_router: true` (and `router_is_only_resolver` when the router is the
*only* configured server).

**Fix:** override the router-as-resolver with a non-router resolver —
`1.1.1.1` (primary) and `1.0.0.1` (secondary) by default, configurable via the
`resolvers` param or the `WINDOWS_NETDIAG_RESOLVERS` env (comma-separated, max
4). The override is applied to every **Up** physical adapter via
`Set-DnsClientServerAddress`.

**Reversibility:** the change is a normal NIC DNS setting. Undo it by resetting
the adapter's DNS in Windows, or by re-running with your own `resolvers` list.
It is not destructive and does not touch DHCP/static-IP, firewalls, or registry
keys beyond the per-adapter DNS server list.

**Owner confirmation:** because hardening *changes* a setting, the playbook
treats `apply_dns_fix` as an owner-approved step — run it only when the owner
has said yes (or it is part of an approved low-usage maintenance window).

---

## 5. The destructive line (never crossed by these actions)

If diagnostics turn up any of the following, the operator **stops** and opens a
separate, owner-confirmed change — none of it is automatable here:

- **Partition ops** — shrink/resize/format/migrate a volume.
- **Uninstalls** — removing software that isn't on the cleanup offender list.
- **Startup surgery beyond the allow-list** — disabling arbitrary services or
  drivers.
- **Any registry change beyond the cleanup Run-key offender removal.**

The `windows_cleanup` offender list is deliberately small and configurable so
"stop X from autostarting" stays a *config* change, not a code change. If the
owner wants a program gone, that is a manual uninstall with their confirmation
on-record.

---

## 6. Reference material (dads-pc / PC-MINI session, 2026-09-03)

The battery was proven on the dads-pc / PC-MINI session. The original
artifacts are folded in under `reference/dads-pc/` as **read-only reference**
(never executed — the appliance runs the `src/scripts/` shell wrappers):

| File | What it documents |
|---|---|
| `dadpc-diagnostics.ps1` | The full read-only diagnostic battery (volumes, CPU/RAM, startup, Defender, events, boot, SMART) |
| `fix1.ps1` | The cleanup fix (offender autostart removal + TEMP/recycle) and the DNS-through-router → 1.1.1.1 override |
| `PC-MINI-session-findings.md` | What the session found and decided, kept as the why behind the defaults above |

**If the reference `.ps1` and the `src/scripts/*.sh` ever disagree, the shell
scripts win** — they are the shipped, tested, executed form.

---

## 7. Example run (chat → action → result)

```
owner:  "why is dad's PC slow and the internet keeps stalling?"
Lily:   windows_diag on dads-pc      → disk 8% free (LOW), 2 offenders in startup
        windows_netdiag on dads-pc   → NIC at 100 Mbps (non-gigabit), DNS via router,
                                        gateway ping 1ms, 1.1.1.1 ping 35ms + loss
owner:  "yes, harden the DNS and clean it up"
Lily:   windows_netdiag {apply_dns_fix: true} on dads-pc
          → elevated, via_router=true → override applied on "Ethernet" → 1.1.1.1/1.0.0.1
        windows_cleanup {offenders: [...]} on dads-pc → recovered 5.2 GB
```

Result reports post back to the owner as ticket notes; the ticket waits on
customer confirmation (the standard `customer_action` close-out).

---

## 8. Scheduling

`windows_diag` (daily/weekly) and `windows_cleanup` (low-usage window) run on a
per-device schedule (`windows_health_schedule`) via the scheduler.
`windows_netdiag` is **on-demand** today (chat/operator); wiring it into the
daily health run is a follow-up and is deliberately not implied by this runbook.
