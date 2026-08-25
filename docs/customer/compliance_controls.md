# Compliance Controls — Operator & Auditor Guide

**Version:** 1.0
**Last Updated:** 2026-08-25
**Applies to:** v2026.08.25.b+

BareNOC's compliance panel (Settings → **Security**) turns every compliance
control into an individual **toggle**, provides a one-click **Compliance
baseline** preset for regulated workspaces, and exports an **attestation
snapshot** that proves the posture to an auditor. This guide is the operator /
auditor companion to the wiki page
[Compliance Controls](https://bareNOC.com/wiki/compliance/).

---

## 1. The posture at a glance

| Control | Home default | Baseline | Meaning |
|---|---|---|---|
| LLM egress | cloud | **local** | Cloud = hosted LLM may process tickets/chat/summaries. **Local = on-prem endpoint only — no data leaves the network** |
| MFA enforcement | off | **on** | Password-only admin/operator login is refused without a second factor (passkey or TOTP) |
| Telemetry | on | **off** | Local metrics collection (never egresses) — off = no collection |
| Remote support | off | off | Vendor Tailscale path; enabling records explicit consent in the audit log |
| Retention | sane | **strict** | Per-category max-age pruning (strict prunes sooner — relaxing back does **not** restore pruned data) |
| Audit log | on | on | Hash-chained audit trail with viewer + export + verify |
| Session policy | relaxed | **strict** | 30-min idle timeout + lockout after 5 failed logins |
| Data deletion | available | available | Per-user purge + factory reset — always available |

**Never toggleable (the floor, both tracks):** self-protection invariants
(restrictions), mTLS device identity, encryption at rest, GPG-signed update
integrity, and the 3-layer backups. These are stated explicitly in the
attestation export.

## 2. Enabling the baseline

Settings → Security → **Apply Compliance baseline**. A confirmation dialog
lists every change (notably: **LLM egress flips to local**, which changes chat
quality if no on-prem endpoint is configured). Every control stays
individually adjustable afterward.

## 3. LLM egress = local (the only real data-flow control)

Before flipping to local, configure an on-prem endpoint:

1. On a **separate LAN box** (not the appliance), run an OpenAI-compatible
   endpoint (e.g. Ollama; ≥7B Q4 for full function, 3B = degraded chat-only).
2. Settings → API Keys → add the provider with `deployment = on_prem`
   (base URL pointing at the LAN endpoint).
3. Settings → Security → set **LLM egress = local** (or apply the baseline).

While local-only is on:

- the worker chain never attempts a hosted provider (unit-tested: cloud URL
  dead + local endpoint live → zero cloud calls);
- the on-appliance coding agent is pinned to the on-prem endpoint;
- saving a cloud provider key is refused with HTTP 400 and a policy message.

## 4. MFA enforcement

Passkey-first (Pocket ID) with TOTP fallback:

1. Settings → Identity: register Pocket ID (existing passkey flow).
2. Settings → Security → MFA: scan the TOTP QR in an authenticator app and
   verify the first code (the secret is shown once).
3. Set **MFA enforcement = on**. Password-only admin/operator logins now
   return 401 until a valid TOTP code (or a passkey) is presented.

## 5. The attestation snapshot (what an auditor sees)

Settings → Security → **Export posture** downloads
`barenoc-attestation-<timestamp>.json`:

```json
{
  "schema_version": 1,
  "generated_at": "…",
  "appliance_version": "2026.08.25.b",
  "controls": {
    "llm_egress":  { "state": "local", "enabled_since": "…", "baseline": "local" },
    "mfa_enforcement": { "state": "on", "enabled_since": "…", "baseline": "on" }
  },
  "settings_hash": "<sha256 of states + enabled_since>",
  "settings_hash_algorithm": "sha256",
  "audit_log_export": "/api/v1/audit-log/export",
  "non_negotiable": [ "…floor…" ],
  "local_endpoint_missing": false
}
```

- `enabled_since` is per-control provenance — a control can't be shown one way
  in the UI and another in the export.
- `settings_hash` changes if any control state or timestamp is tampered with.
- The **audit-log export** (`/api/v1/audit-log/export`, or sidebar → Audit Log)
  is the companion evidence; the viewer can **verify the hash chain** to prove
  the log wasn't edited.

## 6. Auditing day-to-day

- Sidebar → **Audit Log** — viewer with JSON export and chain-verify.
- Retention `strict` prunes metrics at 14 days, audit log at 90 days, tickets
  at 365 days, chat at 180 days (per-category; see the pruner).
- The `Audit log` toggle off = nothing recorded (the viewer shows empty) —
  keep it on for regulated workspaces.

## 7. FAQ (auditor Q&A)

**Q: What data leaves the network?** With `LLM egress = local`: none of the
LLM data flows. The only remaining egresses are the version-manifest check
(metadata) and the opt-in notify relay (alert emails via a vendor transport)
when configured. Telemetry never egresses.

**Q: Can the config be changed silently?** Every change is auditable; the
attestation's per-control `enabled_since` + settings hash make retroactive
claims detectable.

**Q: What about SOC 2 / pentest evidence?** The panel proves the *config*.
Vendor-level evidence (SOC 2 report, pentest, DPA) is a separate engagement —
the panel's export is the artifact to hand that vendor so the configuration
posture is stated in one file.

---

See also: [Audit Logging](./audit_logging.md) · [Access Control](./access_control.md) ·
[Secret Management](./secret_management.md) · [Guardrails](./guardrails.md)
