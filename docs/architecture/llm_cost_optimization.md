# BareNOC — LLM Cost Optimization (F6)

**Version:** 1.0
**Status:** Implemented (2026-09-03; on `feat/barenoc-cost-optimization`)
**Last Updated:** 2026-09-03

---

## 1. Overview & Goals

BareNOC inherits the Command Center's rate-aware + tier-routed LLM policy and
applies it to the appliance's ticket pipeline. The policy composes two rails:

| Rail | Owned by | Question |
|------|----------|----------|
| **WHEN** | `src/api/ratewindows.py` | Is this peak or off-peak? Should non-urgent work wait? |
| **WHERE** | `src/api/tierrouter.py` | Which tier runs this task class — local (M7 Ollama) or cloud (DeepSeek)? |

The two compose into one decision: `WHEN (rate window) × WHERE (tier map)`.

**Goals:**
1. Bias non-urgent (P3/P4) LLM work into off-peak windows (the provider's half-price hours).
2. Route bulk/cheap work to the local tier (an on-LAN Ollama/LM Studio box); keep judgment + customer-visible copy on cloud.
3. Meter local-vs-cloud calls and surface the savings in the reports cost KPI.

**Non-goals (this phase):**
- Routing the autonomous Pi Coding Agent lane (it is host-side, `src/agent/runner.py`, with its own model + the existing `jobs.py` cost metering).
- Changing the provider registry itself — the local tier is just the existing `LLM_PROVIDER_<NAME>_DEPLOYMENT=on_prem` provider.

---

## 2. The shared schema (CC parity)

Both rails read the SAME JSON schema the Command Center uses, seeded per box
(multi-tenant) under `/opt/barenoc/volumes/db/` (the shared api/worker/scheduler
volume). The files are auto-seeded on first admin access
(`GET /api/v1/admin/cost-optimization`) so the owner has a real file to edit;
until then the built-in defaults below apply. Edit the file to change policy —
no code change, no restart.

| File | Schema | Notes |
|------|--------|-------|
| `rate_windows.json` | `{provider, updated, off_peak_factor, peak_windows:[{days,start,end}]}` | DeepSeek's current structure: peak = Mon–Fri 01:00–04:00 + 06:00–10:00 UTC; off-peak = everything else at factor 0.5 |
| `tier_map.json` | `{version, unknown_class_tier, local{}, cloud{}, est_cloud_cost_per_call_usd, classes{}}` | Each class: `default_tier` (cloud/local/auto), optional `peak_override`, `local_model`, `context_budget`, `customer_visible` |
| `llm_cost_stats.json` | `{started_at, window_state, calls:{local,cloud}, by_class{}, local_down_fallbacks, est_savings_usd}` | Reset when the rate window flips; `est_savings_usd = local calls × est_cloud_cost_per_call_usd` |

Precedence (first hit wins): the JSON file → env keys (`RATE_PEAK_WINDOWS`,
`RATE_OFF_PEAK_FACTOR`, `TIER_MAP_FILE`, `LOCAL_LLM_URL`, `LOCAL_LLM_MODEL`, …)
→ the built-in defaults.

---

## 3. The built-in tier map (safe defaults)

| Task class | Call site | Tier | Why |
|------------|-----------|------|-----|
| `ticket_judge` | `judge.py` | **cloud** | Judgment (lawfulness ruling) — never downgraded |
| `ticket_technician` | `main.py` single-phase `call_llm` | **cloud** | The judgment path when the judge is off |
| `ticket_title` | `llm_client.generate_title` | **cloud** (customer_visible) | Ticket titles are customer-visible copy |
| `ticket_executor` | `executor.py` | **auto** | Bulk/cheap structured job-fill: cloud off-peak (half price + quality), local at peak (relief valve) |

`auto` is window-inverted, not a static split. Operators can flip any class to
`local` in `tier_map.json`; a `customer_visible: true` class routed local is
returned `draft_flagged: true` (the caller must label it "draft — needs review").

---

## 4. Guardrails

- **Customer-visible:** cloud by default; local routing flags a draft.
- **Local-down fallback:** `route()` probes the local box (`/api/tags`, then
  `/v1/models`) with a 30 s cache; if unreachable the decision downgrades to
  cloud and flags `local_fallback`.
- **Critical work never defers:** `plan_start(critical=True)` for P1/P2.
- **Every routed call is metered** by the provider that actually answered
  (local = `deployment == on_prem`), so a local→cloud fallback is counted as
  cloud and flagged.

---

## 5. Off-peak scheduling flow (worker)

1. `process_ticket` reaches the LLM phase (judge/technician). P1/P2 and the
   autonomous pi lane run immediately.
2. `_maybe_defer_offpeak` calls `ratewindows.plan_start(critical=False)`.
   - OFF-PEAK → run now.
   - PEAK → park the ticket: status back to `open`, an `offpeak_deferred`
     note, and a queue file under `…/rate-windows/queue/<ticket_id>.json`.
3. The poll loop excludes parked tickets from the open fetch and re-adds them
   (dequeuing) once `ratewindows.due()` says their window arrived.

Toggles (both default **on**): `LLM_COST_OPTIMIZATION`, `LLM_OFFPEAK_DEFER`.
`LLM_COST_STATE_DIR` overrides the state directory (default
`/opt/barenoc/volumes/db`).

---

## 6. Cost KPI

`routes/dashboard.py::_report_stats` adds a `cost_optimization` block:
`enabled`, `rate_state`, `rate_factor`, `off_peak_factor`, `llm_local_calls`,
`llm_cloud_calls`, `llm_local_fallbacks`, `llm_local_savings_usd`,
`local_configured`. A read-only admin endpoint
`GET /api/v1/admin/cost-optimization` returns the full rate state + tier map +
cost counter.

---

## 7. Deployment notes

`ratewindows.py` + `tierrouter.py` are shared modules (live in `src/api/` and
are copied into the worker build context). The set is **derived from
`src/worker/Dockerfile`'s `COPY` lines** — the single source of truth — so a
module added to `src/api/` + `COPY`ed in the Dockerfile is picked up with no
hand-maintained list to drift (the `.30.b`/`.03.b` self-update lessons):

- `scripts/build_release_manifest.py` → injects them into the release tarball
- `deploy.sh` → derives `SHARED_MODULES` from the Dockerfile
- `src/scripts/barenoc-self-update.sh` → backfills from the Dockerfile
- `src/scripts/bootstrap_appliance.sh` → backfills from the Dockerfile
