# Settings Reference

An overview of the **Settings** page — the per-deployment configuration.
Settings is **admin-only**; other roles see their own pages but not these tabs.

Most installs see the **home tab set** — General · Network · Devices · Tickets ·
Updates · Backups. Everything else (LLM providers, Email, Identity, Restrictions,
Security, Support, Users) lives under **Advanced** — grouped, ≤2 clicks away,
and unchanged in function.

| Tab | What you configure | Details |
|-----|--------------------|---------|
| **General** | Site ID, customer name, timezone, discovery subnets, parallel jobs, device groups, assistant names, desktop chat | [Getting Started](/wiki/getting-started) |
| **Network** | UniFi controller URL + credentials, auto-sync, auto-adopt | [Network Discovery](/wiki/discovery) |
| **Devices** | Device inventory + onboarding (link to the Devices page) | [Devices](/wiki/devices) |
| **Tickets** | Check-in + auto-close lifecycle per priority | [Tickets](/wiki/tickets) |
| **Updates** | Check / schedule / rollback (link to System → Updates) | [Updates](/wiki/updates) |
| **Backups** | USB backup schedule, network (NAS) copy, first-time stick setup | [Backups](/wiki/backups) |

### Advanced

| Tab | What you configure | Details |
|-----|--------------------|---------|
| **Users** | Accounts, roles (admin / technician / user), password resets | [Getting Started](/wiki/getting-started) |
| **LLM Providers** | LLM providers (chain + failover) and the **Autonomy Policy** | [Autonomy Policy](/wiki/autonomy) |
| **Email** | SMTP, alert recipients, morning digest + EOD summary, test sends | [Reports & KPIs](/wiki/reports) |
| **Identity** | Passkey login (Pocket ID), GitHub/Google OIDC, appliance identity & DNS | [Getting Started](/wiki/getting-started) |
| **Restrictions** | Hard denies: blocked actions / devices / request phrases + self-protection | [Security](/wiki/security) |
| **Security** | Compliance/governance panel — LLM egress, MFA, telemetry, remote support, retention, audit, session policy, data deletion + **Compliance baseline** preset + **attestation export** | [Compliance Controls](/wiki/compliance) |
| **Support** | Forum-submit endpoint + token, and the **Remote support** (Tailscale) toggle | [Support / Bug Report](/wiki/support) |

## Where the rest lives

Some settings live on other pages:

| Setting | Where |
|---------|-------|
| Updates (check / schedule / rollback) | **System → Updates** — [Updates](/wiki/updates) |
| Support / bug-report bundle | **System → Support** — [Support / Bug Report](/wiki/support) |
| Device control channels + 🔔 monitor toggle | **Devices** page — [Devices](/wiki/devices) |
| Network Optimization schedule | **Dashboard → Network Optimization** — [Network Optimization](/wiki/network-optimization) |

## Roles at a glance

| Role | Settings access |
|------|-----------------|
| **Admin** | Full — every tab above |
| **Operator** | No Settings access (devices/tickets/approvals only) |
| **Read-only** | No Settings access (view dashboards/tickets/devices) |

> Autonomy, restrictions, and identity are the highest-impact tabs — changes
> there apply to the worker within one poll cycle (~15 s), no restart needed.
