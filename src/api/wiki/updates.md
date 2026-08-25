# Updates

BareNOC updates itself in place — check for a new release, apply it in the
background, and roll back if anything goes wrong. Updates are **free and open
(beta)**: no activation key, no license gate.

Where: **System → Updates**. The dashboard shows a slim **📦 release banner**
only when a new release is available (it links to System → Updates); otherwise
it stays out of the way.

## Versioning

Releases use **CalVer**: `YYYY.MM.DD` with a letter suffix for same-day batches
(e.g. `2026.08.18.c`). "Newer" means a later date, or a later letter on the
same date — a downgrade is never offered as an update.

## Check & apply

| Action | What it does |
|--------|--------------|
| **Check now** | Re-reads the public release manifest (barenoc.com) and reports the latest version + changelog |
| **Update now** | Queues the update in the background — the appliance pulls the release, verifies the checksum, swaps in the new build, and restarts services |
| **Rollback** | Reverts to the previous release in the background |

The appliance auto-checks on page load when its last check is stale. An
in-progress update shows live **progress** (stage + percentage); when it
finishes, the running version has already flipped and the card settles back to
"up to date".

## Auto-update (on by default)

BareNOC auto-updates **on by default**. A fresh install, or a box that
updates to this release without ever touching the schedule, gets a weekly
maintenance window — **Sunday at 03:00 local time**. When a new release is
available it is applied automatically in that window. This is safe because
every release since v2026.08.25.a is **GPG-signed and verified before it is
applied** (fail-closed — an unsigned or tampered release is never applied).

**To opt out:** System → Updates → uncheck **Auto-update** (one click). That
persists `enabled=false` and is never flipped back by a later update — once
you opt out, you stay opted out until you turn it back on.

## Schedule

Schedule updates instead of clicking **Update now**:

- **Recurring** — daily, or a specific weekday, at a local hour (e.g. Monday
  02:00). The out-of-the-box default is **Sunday 03:00**.
- **One-time** — apply the next available release at a specific local date/time.
  It fires once, then clears itself.

Times are **local** (the appliance's timezone from Settings → General →
Timezone), not UTC. A scheduled update only fires when a release is actually
available.

## What happens during an update

1. The appliance verifies the release manifest + checksum.
2. It stages the new build and applies it in the background.
3. Services restart and health is re-checked.
4. The result (done / failed) is recorded, and an email goes to the alert
   recipients (best-effort — only if SMTP is configured).

> If an update fails, the appliance keeps running the previous build; use
> **Rollback** if you ever need to step back manually.

## Who can do this

- **Admin / operator** — check, update, roll back, and set the schedule from
  System → Updates.
- **Scheduled updates** run automatically (no user needed) once enabled.
