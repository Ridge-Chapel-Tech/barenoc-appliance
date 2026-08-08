# Early-Access Access Management (vendor)

**Audience:** the BareNOC maintainer — this is the **"Me"** doc, not the user
guide. Users follow `docs/deployment_guide.md` (Part A2) with their own GitHub
account. This covers how paid/free access to the private early-access repo is
granted, automated, and revoked.

## Model

- The release repo `Ridge-Chapel-Tech/barenoc-appliance` is **private,
  invite-only**. Access is per **person** via GitHub **collaborators** (Read
  role) — tied to their GitHub identity, revocable per person, no key sharing.
- GitHub has **no native "sell repo access"** — payments happen outside GitHub
  (Stripe / Gumroad / PayPal / Sponsors); this tooling does the grant/revoke
  half. Wire a payment webhook to `mark-paid` to automate it.
- **Free slots:** the maintainer + up to **2 free users** (`FREE_SLOTS` in the
  script) are never auto-revoked. Everyone else is paid and checked monthly.

## The tool

`scripts/early_access.py` (run on a machine with `gh` authenticated — e.g. the
dev laptop). State lives in `scripts/.early-access-state.json` (**gitignored** —
usernames/due dates never enter the repos).

```bash
python3 scripts/early_access.py grant <user> <email> [--free|--months N]  collaborator + activation key
python3 scripts/early_access.py issue-key <user> <email> [--months N]      key only (replace)
python3 scripts/early_access.py revoke <user>                             collaborator + key
python3 scripts/early_access.py revoke-key <user>                         key only
python3 scripts/early_access.py mark-paid <user>                          payment received → extend due (webhook)
python3 scripts/early_access.py free <user> | unfree <user>
python3 scripts/early_access.py list
python3 scripts/early_access.py check                                     monthly sweep: revoke past-due (collab + key)
python3 scripts/early_access.py publish-keys [--no-push]                  regen + push the public allowlist
python3 scripts/early_access.py install-timer                             systemd user timer: check on the 1st
```

`grant` issues a **`BARC-XXXX-XXXX-XXXX` activation key bound to the purchase
email** and publishes the public allowlist to barenoc.com
(`downloads/activation-keys.json` — key + **hashed** email + active flag; raw
emails stay in the local state only). The appliance verifies its key against
that list: valid → updates allowed; revoked/missing → updates disabled (soft —
the appliance keeps running). The key is entered on the appliance via the
installer `--activation-key` or Settings → Licensing.

`check` never touches: the **owner** (the `gh` account running it) or **free**
users. It logs what it revoked; a dry view is `python3 scripts/early_access.py list`.

### Suggested payment flow

1. Customer pays (your payment processor).
2. Webhook → `python3 scripts/early_access.py grant <username>` (or `mark-paid`
   for renewals).
3. Monthly: the timer runs `check` → anyone past due and not free is revoked
   (`gh api -X DELETE .../collaborators/<username>`). Their existing clone
   still works but they can no longer pull updates or re-clone.

## Deploy keys (vendor test hosts only)

The maintainer's own install-test hosts may use a **read-only deploy key**
(repo → Settings → Deploy keys) — scoped to the repo but **not to a person**,
so it is for vendor-owned hosts only, never for customers.

## Automating later

- Stripe/other webhook → `mark-paid`/`grant` (the script is the single entry
  point — call it from the webhook).
- The monthly sweep can also run as a scheduled GitHub Action in the release
  repo (needs a PAT secret with `admin:repo`); the systemd user timer is the
  current mechanism and requires the dev box to be on around the 1st
  (Persistent=true catches up on next login if it was off).
