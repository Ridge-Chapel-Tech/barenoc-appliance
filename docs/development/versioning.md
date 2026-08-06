# BareNOC Versioning & Release Process

**Last Updated:** 2026-08-05

## Versioning scheme (CalVer — date-based)

```
vYYYY.MM             — major (monthly feature) release
vYYYY.MM.DD          — minor (first release of a day)
vYYYY.MM.DD.<a,b,…>  — second+ release the same day (hotfixes & follow-ups)
```

- **Major `2026.08`** — the once-per-month feature release (the month names it).
- **Minor `2026.08.05`** — the first release on a given day.
- **Hotfix `2026.08.05.a`** — any subsequent release the same day, cut when
  urgency requires a branch off a stable tag (hot branch / zero-day patch).
  The letter is the **same-day ordinal**: a second *minor* that day also takes
  a letter (`.b`, `.c`, …) — this is what keeps same-day releases unambiguous.
  "Hotfix" is a release **category** (CHANGELOG **Fixed**/**Security**, urgent)
  that uses the same naming.

**Ordering** is plain string sort: `2026.08` < `2026.08.05` < `2026.08.05.a`
< `2026.08.06`. Months/days are zero-padded.

**Why date-based?** This is an appliance — "is this the May build?" is the
natural question, ISO/download filenames get chronological sense, and there's
no pre-GA "minor vs major" debate. We control the whole pipeline
(version.py → tag → versions.json → ISO), so nothing external needs SemVer.

## Single source of truth

`src/api/version.py` (`APP_VERSION`) drives everything:

| Surface | Reads from |
|---------|-----------|
| Web UI nav footer (`vX.Y.Z`) | `GET /api/v1/health` → `version` |
| System page / health checks | `main.py` → `APP_VERSION` |
| Chat-client download filenames | `main.py` downloads route |
| `versions.json` (installer + update checks) | release workflow reads `version.py` |
| ISO filename (`barenoc-<ver>.iso`) | `proxmox/build_barenoc_iso.sh` reads `version.py` |

**Rule:** bump `version.py` and update `CHANGELOG.md` in the *same commit*
that the release is cut from — the tag is the only other thing to create.

## Release process (one command)

```bash
# 1. on main, after the work is merged + tested
scripts/bump_version.sh minor        # → 2026.08.05 (today); or major → 2026.08; hotfix → 2026.08.05.a
                                     # bumps version.py, adds CHANGELOG entry, commits
git push origin main
git tag v2026.08.05 && git push origin v2026.08.05
```

The `release.yml` workflow then:

1. Runs the full test suite (api + worker + runner).
2. Creates a GitHub Release tagged `vX.Y.Z` with release notes = that
   version's `CHANGELOG.md` section.
3. Attaches release assets: `proxmox/barenoc-appliance.sh`,
   `proxmox/build_barenoc_iso.sh`, `install.sh`, `setup-usb-backup.sh` +
   `SHA256SUMS`.
4. Publishes `versions.json` (see `marketing/download_distribution.md`) so
   `install.sh` and future `barenoc-update` can find the release.

> The ISO (~3 GB) is built manually per release (`build_barenoc_iso.sh`) and
> uploaded to R2/B2 — not built in CI (disk/time). Its sha256 lands in
> `versions.json` too.

## Where bugs & features get logged

- **Bug fixes** — tracked as GitHub issues (label `bug`) and recorded in
  `CHANGELOG.md` under the version's **Fixed** section. The internal
  `SESSION_LOG.md` keeps the raw incident detail (diagnosis, root cause,
  verification); the CHANGELOG is the customer-facing summary.
- **New features** — issues (label `feature`) → implemented → **Added**
  section in the CHANGELOG + wiki update in the same PR.
- **Milestones** — `docs/MILESTONES.md` is the source of truth for phases;
  GitHub milestones mirror the current phase (M1…M5, installer, etc.).

## Release hygiene

- Tag only from `main`; never move a tag.
- `versions.json` is immutable per version — always publish a new file, never
  overwrite an existing version's checksum.
- Security fixes get their own PATCH release + a **Security** CHANGELOG
  entry even if tiny.
