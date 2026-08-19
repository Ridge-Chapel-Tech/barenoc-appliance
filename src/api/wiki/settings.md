# Settings Reference

An overview of the **Settings** page — the per-deployment configuration.
Settings is **admin-only**; other roles see their own pages but not these tabs.

| Tab | What you configure | Details |
|-----|--------------------|---------|
| **General** | Site ID, customer name, timezone, discovery subnets, parallel jobs, device groups, assistant names, desktop chat | [Getting Started](/wiki/getting-started) |
| **Users** | Accounts, roles (admin / technician / user), password resets | [Getting Started](/wiki/getting-started) |
| **API Keys** | LLM providers (chain + failover) and the **Autonomy Policy** | [Autonomy Policy](/wiki/autonomy) |
| **Email** | SMTP, alert recipients, morning digest + EOD summary, test sends | [Reports & KPIs](/wiki/reports) |
| **UniFi** | Controller URL + credentials, auto-sync, auto-adopt | [Network Discovery](/wiki/discovery) |
| **Identity** | Passkey login (Pocket ID), GitHub/Google OIDC, appliance identity & DNS | [Getting Started](/wiki/getting-started) |
| **Tickets** | Check-in + auto-close lifecycle per priority | [Tickets](/wiki/tickets) |
| **Restrictions** | Hard denies: blocked actions / devices / request phrases + self-protection | [Security](/wiki/security) |
| **Backups** | USB backup schedule, network (NAS) copy, first-time stick setup | [Backups](/wiki/backups) |

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
