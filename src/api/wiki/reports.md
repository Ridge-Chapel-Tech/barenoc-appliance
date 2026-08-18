# Reports & KPIs

The Dashboard's **Performance & Reporting** section tracks how the NOC is
doing — ticket volume, response/resolution times, and cost — and exports
business-ready spreadsheets. Two scheduled emails (a morning digest and an
end-of-day summary) round it out.

Where: **Dashboard → Performance & Reporting**.

## Headline KPIs

| KPI | What it measures |
|-----|------------------|
| Tickets created / resolved / closed | Volume over the selected window (1–365 days) |
| Support tickets created / resolved | The subset a human/AI team actually works (see note) |
| Avg resolution time (h) | Creation → resolved, for support tickets |
| Avg time to first response (min) | Creation → first customer-facing reply |
| Escalations | Events, distinct tickets, and the rate (% of support tickets created) |
| Auto-closed / check-ins / reopens | Lifecycle-hygiene counters |
| AI support spend (USD) | Tracked LLM cost (see honesty note) |
| Est. manned-NOC cost + savings | What the same load would cost a human team, minus AI spend |

Charts show **created vs resolved per day**, the **priority mix**, resolution
time by priority, and the current status funnel.

### What counts as a "support ticket"

System-generated tickets (internet-outage monitor, LLM-outage ticket, seeded
demo rows) open and close on their own — they're **excluded** from the support
KPIs so a long idle auto-ticket can't skew the averages.

### Honest AI-spend note

"AI support spend" only counts **catalog-path** LLM usage. pi/Lily sessions
aren't metered yet, so tickets worked by the autonomous agent show 0 until
metering lands — the KPI is labeled accordingly.

## Exports

Download the report as **CSV**, **TSV** (paste straight into Google Sheets),
**XLSX**, **ODS**, or **PDF** — all share the same tables: summary, by-priority,
status funnel, daily trend, and a per-ticket analysis.

## Morning digest & end-of-day emails

Configured in **Settings → Email**:

| Email | Default hour (local) | Contents |
|-------|----------------------|----------|
| **Morning digest** | 07:00 | New tickets (24 h) + device health breakdown |
| **End-of-day summary** | 18:00 | Day recap + system settings changes |

Each has its own on/off, hour, and recipient list (blank = falls back to the
alert recipients). Both need SMTP configured; use the **send test** buttons to
verify.

## Who can see/do this

- **All signed-in roles** can view the dashboard report (it lives on the
  Dashboard).
- **Settings → Email** (recipients, hours, SMTP) is admin-only.
