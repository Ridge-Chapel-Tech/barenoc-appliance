---
name: Bug report
about: Something is broken or behaves incorrectly
title: "[bug] "
labels: bug
assignees: ""
---

**Describe the bug**
A clear, concise description.

**Reproduction**
1. Steps to reproduce…

**Expected vs actual**
- Expected: …
- Actual: …

**Environment**
- [ ] Reproduces on the **live appliance** (192.0.2.207) — describe how
- [ ] Only in tests / dev
- Version: `vX.Y.Z` (footer of the web UI) · affected area: `devices / worker / agent / chat / unifi / api / ui / install / backup`

**Impact & urgency**
Who does this hurt, how badly? (P1 = down/blocked, P2 = degraded, P3 = annoyance)

**Logs / artifacts**
Relevant work-notes, `journalctl -u pi-agent-runner -n 200`, `docker logs barenoc-api`, or `SESSION_LOG.md` context.

**Fix verification plan**
What proves this is fixed (test name, live check, md5s)?
