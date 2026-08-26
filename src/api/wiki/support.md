# Support / Bug Report

**System → Support** exports a **redacted diagnostic bundle** — a single
markdown file you attach to a bug report so a support engineer can diagnose
without ever seeing your secrets.

> Admin-only: only admins can generate the bundle.

## Remote support (Settings → Support)

**Settings → Support** has a **Remote support** toggle — **off by default**.
When you turn it on, the appliance joins the BareNOC **support tailnet** over
When **Remote support** is on, the BareNOC support team can also **SSH
into the appliance itself** (key-only, over the Tailscale tunnel, same
consent) to validate and troubleshoot — the access is removed the moment
you turn Remote support off. Your own SSH keys are never touched.

**Tailscale** (identity-verified, outbound-only, works through CGNAT
— no open ports).

- The appliance appears as `bareNOC-<appliance-id>` on the support tailnet.
- **Support key:** paste the key your provider sent you into the **Support
  key** field (password-style) in Settings → Support, then save. The key is
  stored securely (0600) and the appliance joins within a minute. The field
  appears only when remote support is available for your box.
- The support team can reach **appliance nodes only** — never the rest of your
  LAN (Tailscale ACLs enforce this; see the deployment guide).
- Turning it **off** immediately runs `tailscale down` and removes the
  appliance from the support tailnet. The change applies within a minute.
- **Beta note:** remote support is available during the beta via an expiring
  support grant. At general availability it becomes a paid **Support**
  subscription feature — the same gate that protects report submission.

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
