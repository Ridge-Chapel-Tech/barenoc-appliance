# Contributing

Small, focused, trunk-based. One repo, one dev (you) plus the AI assistant —
rules exist so history stays readable and releases stay mechanical.

## Commit format (Conventional Commits)

```
<type>(<scope>): <imperative summary>

<optional body — why, not what>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `ops`, `chore`, `security`,
`build`. Scopes: `chat`, `worker`, `api`, `devices`, `unifi`, `agent`,
`wiki`, `docs`, `install`, `backup`, `ci`.

Examples:
```
feat(devices): persist SSH key on claim so devices become SSH-controlled
fix(worker): reboot tickets no longer require a scheduled_at param
docs(install): rewrite iac doc to match the deployed system
ops(backup): make weekly USB backup LUKS-aware
```

- One logical change per commit; the session log gets one entry per session.
- **Never commit `.env`**, `client_secret_*.json`, `__pycache__/`,
  `client/build`, `client/dist`, or `volumes/` (see `.gitignore`).

## Branching

- `main` is always deployable. Work on short branches
  (`feat/<topic>`, `fix/<topic>`), open a PR, squash-merge.
- Tags `vX.Y.Z` are cut from `main` only; the release workflow builds from
  the tag.

## Code style & gates

- Python: 4-space indent, stdlib-first where practical (the dev box has no
  pip; tests must run with stdlib only where noted).
- **Tests gate everything.** Every change keeps these green:
  - api: `docker exec barenoc-api python3 -m unittest test_devices test_admin
    test_settings test_alerting test_unifi_sync`
  - worker: `docker exec barenoc-worker python3 -m unittest test_judge
    test_integration`
  - runner: `cd src/agent && python3 -m unittest test_runner`
  - JS: `node --check <extracted script>` for template JS changes
- Hermeticity rule (learned the hard way): tests that read env/config must
  mock `read_env_file` — the live `.env`'s explicit values beat profile
  presets by design and will leak into tests otherwise.
- Bash scripts: `bash -n` before commit; agent scripts must **not** end in
  `exit 0` (masks Python failures as success).

## Releases

See `docs/development/versioning.md`. Short version: bump `src/api/version.py`
+ update `CHANGELOG.md` → tag `v<ver>` on main (`v2026.08.05`, `v2026.08.05.a`,
…) → the release workflow runs tests, creates the GitHub Release, and
publishes `versions.json`.

## Issue / PR conventions

- Bugs: use the bug template — steps, expected, actual, impact, plus whether
  it reproduces on the live appliance (192.0.2.207) or only in tests.
- Features: user story + acceptance criteria + any SAT it maps to
  (`docs/system_acceptance_test.md`).
- PR template asks for: what/why, tests run, deploy+verify notes, and whether
  docs (local + wiki) were updated — they must be, in the same PR.
