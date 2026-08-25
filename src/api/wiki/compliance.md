# Compliance Controls

BareNOC ships with a **compliance/security-governance panel** (Settings →
**Security**) for workspaces that must show an auditor what the appliance is
configured to do (PCI / HIPAA / SOC 2-driven shops). It is a **panel of
toggles, not a mode** — home installs keep the streamlined defaults, and every
control is individually adjustable. A one-click **Compliance baseline** preset
flips the recommended set for regulated workspaces, and an **attestation
snapshot** export proves the posture to an auditor.

> Applies to v2026.08.25.b and later.

## The control inventory

| Control | Home default | Compliance baseline | What it does |
|---|---|---|---|
| **LLM egress** | cloud | **local** | Where ticket/chat/network-summary LLM calls may go. `local` = on-prem endpoint only — no data leaves your network |
| **MFA enforcement** | off | **on** | Require a second factor (passkey or TOTP) for admin/operator sign-in |
| **Telemetry** | on | **off** | Local-only time-series metrics collection (never egresses). Off = no collection |
| **Remote support** | off | off | Vendor Tailscale support path. Enabling records explicit consent in the audit log |
| **Retention policy** | sane | **strict** | Per-category max-age pruning. `strict` prunes sooner |
| **Audit log** | on | on | Immutable hash-chained audit trail (viewer + export + verify) |
| **Session policy** | relaxed | **strict** | Idle timeout + login lockout. `strict` = 30-min idle window + lockout after 5 failed logins |
| **Data deletion** | available | available | Per-user purge + factory reset — always available |

**The Compliance baseline preset** (one click, with a confirmation dialog
listing what changes) turns ON: LLM egress → local, MFA enforced, telemetry
off, remote support off (with consent), retention → strict, audit on, session
→ strict. Everything remains individually adjustable afterward.

## LLM egress — the one real data-flow decision

The honest egress map has exactly one real data-flow blocker: **cloud LLM
calls** (tickets, chat, network summaries transit a hosted LLM by default).
`LLM egress = local` enforces the fix across every code path:

- the worker chain is filtered to on-prem providers only — a hosted endpoint
  is never attempted;
- the on-appliance coding agent (pi) is pinned to the on-prem endpoint;
- saving a cloud provider key is **refused (400)** with a clear message while
  local-only is on.

`local` requires an **OpenAI-compatible endpoint on your LAN** (the existing
Ollama path — a 7B+ Q4 model for full function; 3B is degraded chat-only and
the UI says so). The appliance itself (2 vCPU / 4 GB) is not sized to host
inference — the endpoint is a separate LAN box. The wizard offers the same
choice: "Cloud (recommended — best answers)" vs "Local only (no data leaves
your network)".

## MFA enforcement

Passkey-first (Pocket ID) with **TOTP fallback** (authenticator app). When
enforced:

- password-only admin/operator sign-in returns **401** with an MFA prompt;
- sign in with a passkey (already strong auth) is unaffected;
- TOTP is enrolled in Settings → Security → MFA (secret + QR shown once,
  verified before it becomes active).

## Audit log

Every audited event is stored in an **append-only, hash-chained** table. The
new viewer (sidebar → **Audit Log**) lists events, exports JSON, and can
**verify the chain** — recomputing each row's hash against its recorded
predecessor to prove nothing was tampered with. The `Audit log` toggle gates
recording (off = nothing written; the viewer shows empty).

## Session policy & lockout

- **Idle timeout** (strict): the refresh session dies after 30 minutes of
  inactivity — the stateless access token lives out its own ≤60-minute window.
- **Login lockout** (strict): after 5 failed password attempts the account is
  locked for 15 minutes (HTTP 423). Lockout is checked **before** password
  verification, so there is no timing oracle.

## Attestation snapshot (the auditor artifact)

Settings → Security → **Export posture** downloads a JSON snapshot:

- every control's `{state, enabled_since}` — the provenance timestamp so the
  config can't be changed retroactively;
- a **SHA-256 hash** of the settings config (tamper with any control and the
  hash changes);
- the appliance version;
- a link to the audit-log export;
- the **non-negotiable floor** (below) — stated explicitly.

## The non-negotiable floor

These are **never toggleable** — the always-on baseline for every install,
home or regulated:

- self-protection invariants (SELF_PATTERNS / SELF_DEVICES / SELF_ACTIONS)
- mTLS device identity (step-ca issued certificates)
- encryption at rest (app-level secrets + LUKS2 USB backups)
- update integrity (GPG-signed releases, verified before apply)
- 3-layer backups (VM snapshot + encrypted USB + NAS copy)

SOC 2 / penetration-testing / DPA evidence remains a separate vendor-level
stack — the panel proves the *config*, not the *audit*.

## Related

- [Security](/wiki/security) — accounts, sessions, management lockdown
- [Settings](/wiki/settings) — every Settings tab at a glance
- [Autonomy Policy](/wiki/autonomy) — how much the AI may do on its own
