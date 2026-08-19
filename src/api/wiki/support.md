# Support / Bug Report

**System → Support** exports a **redacted diagnostic bundle** — a single
markdown file you attach to a bug report so a support engineer can diagnose
without ever seeing your secrets.

> Admin-only: only admins can generate the bundle.

## What's in the bundle

| Section | Contents |
|---------|----------|
| Header | Product, version, generated timestamp, timezone, your bug description |
| System snapshot | Version, host resources, container states, backup status |
| App config | Key **names** only — most values show as `<set>` / `<empty>` (a few safe values like timezone are shown) |
| Inventory | Devices (name, type, model, status, adoption) — **no credentials** |
| Tickets | Last 15 tickets (ID, status, priority, source) |
| Audit trail | Recent events, payloads redacted |
| Logs | Last 150 lines per container + the agent-runner log, error lines surfaced |

## What's scrubbed (always)

Secrets never leave the appliance. Before anything is written to the file:

- API keys (`sk-…`), bearer tokens, JWTs
- `password=` / `secret=` / `token=` assignments
- Authorization headers and cookies
- Private keys and certificates

…are replaced with `***`. The file contains **no `.env` values, no
credentials, no keys, no certs** — presence-only where config is shown.

## How to send it

**Easiest — Submit Report (recommended):**

1. Open **System → Support**, describe the bug in the comment box (required),
   and click **Submit Report**.
2. The comment is checked once by the AI — if it doesn't look like a bug, or
   needs more detail, you'll be prompted before anything is posted.
3. BareNOC creates a bug thread on the **support forum** (forum.barenoc.com)
   in your name and attaches the bundle automatically. You'll get a link to
   the thread.

**Manual:**

1. Open **System → Support**, describe the bug, and **Download diagnostic
   bundle**.
2. Attach the `.md` file to a bug report on the support forum, or to a GitHub
   issue if your support contact asked for one.

If a support engineer needs a value that was scrubbed, they'll tell you a safe
way to provide it separately — the bundle itself never carries it.
