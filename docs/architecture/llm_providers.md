# BareNOC — LLM Provider Abstraction (P2)

**Version:** 1.0
**Status:** Implemented & live (2025-08-01; legacy built-in `deepseek` block retired 2026-08-04)
**Last Updated:** 2026-08-05

---

## 1. Overview & Goals

BareNOC routes tickets through a **configurable provider registry** so customers
can use the LLM provider they already pay for — DeepSeek, OpenAI, Anthropic
(Claude), Google (Gemini), OpenRouter, Groq, or a local Ollama instance.

**Goals:**
1. Let the customer configure any supported provider and switch with one setting (global active provider).
2. Keep cost tracking, audit, and escalation policy provider-agnostic.
3. Lay the foundation for the **on-LAN Ollama fallback** (ISP-offline resilience) without re-architecting later.
4. Zero change to the ticket → action pipeline, sanitizer, validation, or audit trail.

**Non-goals (this phase):**
- Per-ticket provider selection (global active is sufficient — approved 2025-07-31).
- Automatic provider failover (that's the fallback workstream; this doc defines the seam it plugs into).

---

## 2. Current State

`worker/llm_client.py` calls the provider registry (`llm_providers.load_providers`
/ `active_provider_name`), which is env-backed and hot-reloaded (mtime check):

| Aspect | Today |
|--------|-------|
| Providers | N `LLM_PROVIDER_<NAME>_*` blocks; `LLM_ACTIVE_PROVIDER` selects the active one |
| Adapters | OpenAI-compatible, Anthropic, Google Gemini (`ADAPTERS` in `llm_providers.py`) |
| Tiers | `chat` / `reasoner` / `judge` model per provider (`LLM_PROVIDER_<NAME>_CHAT/REASONER/JUDGE_MODEL`); `THINKING=disabled` stops reasoning models burning tokens on chain-of-thought |
| Auth | Per-provider API key from Settings (written to `.env`, hot-read) |
| Cost | `resolve_prices` — live OpenRouter/Together fetch, static table, or `$0` for local |
| Mock mode | Yes — when no API key is set |

**Key insight:** every adapter is a thin generalization of the OpenAI-compatible
format; the production path has processed hundreds of tickets.
LLM_PROVIDER_OLLAMA_BASE_URL=http://10.0.10.20:11434
LLM_PROVIDER_OLLAMA_API_KEY=
LLM_PROVIDER_OLLAMA_CHAT_MODEL=llama3.1:8b
LLM_PROVIDER_OLLAMA_REASONER_MODEL=llama3.1:8b
LLM_PROVIDER_OLLAMA_INPUT_PRICE=0
LLM_PROVIDER_OLLAMA_OUTPUT_PRICE=0
```

**Rules:**
- `LLM_ACTIVE_PROVIDER` must match a configured provider name; default `deepseek`.
- If `LLM_PROVIDER_DEEPSEEK_API_KEY` is unset AND no other provider is active → mock mode (existing behavior).
- Provider names are lowercase alphanumeric (used in audit/model labels).

### 3.2 Adapter layer

`llm_client.py` gains an internal adapter interface. Each adapter implements:

```
build_request(model, messages, temperature, max_tokens) -> (url, headers, body)
parse_response(http_json) -> (text, prompt_tokens, response_tokens)
```

| Adapter | Providers | Endpoint | Auth | Response shape mapped from |
|---------|-----------|----------|------|----------------------------|
| `openai` | DeepSeek, Ollama, LM Studio, llama.cpp, Groq, OpenRouter, Together | `{base}/v1/chat/completions` | `Authorization: Bearer` | `choices[0].message.content`, `usage.prompt_tokens` / `usage.completion_tokens` |
| `anthropic` | Claude | `{base}/v1/messages` | `x-api-key` + `anthropic-version` | `content[0].text`, `usage.input_tokens` / `usage.output_tokens` |
| `gemini` | Gemini | `{base}/v1beta/models/{model}:generateContent` | `x-goog-api-key` | `candidates[0].content.parts[0].text`, `usageMetadata.promptTokenCount` / `candidatesTokenCount` |

- System prompt, JSON extraction (`_parse_llm_response`), `LLMResponse` construction, and the action/confidence validation stay **unchanged and shared** across adapters.
- `call_llm()` resolves the active provider, selects chat vs reasoner model via the existing `use_reasoner` flag (escalation policy unchanged: P1/P2 + retries → reasoner).

### 3.3 Cost model — live pricing resolver

Pricing is resolved per provider through a small resolver with three sources, selected by `LLM_PRICE_MODE`:

| Mode | Source | Providers it applies to |
|------|--------|--------------------------|
| `live` | Public pricing-bearing model list, cached 24h | **OpenRouter** (`GET https://openrouter.ai/api/v1/models`), **Together** (`GET /v1/models`) — both return USD-per-token per model |
| `static` | Maintained price table in code (updated on releases) + env overrides (`LLM_PROVIDER_X_INPUT_PRICE` / `_OUTPUT_PRICE`) | DeepSeek, Anthropic, Gemini, Groq, and any provider without a pricing API |
| `zero` | $0.00 | Local (Ollama, LM Studio, llama.cpp) |

```ini
LLM_PRICE_MODE=auto                  # auto | live | static | zero
LLM_PRICE_CACHE_TTL_H=24
```

**Behavior:**
- `auto` (default): try `live` if the provider exposes a pricing API → fall back to `static` on failure/timeout. `live` fails loudly if unavailable; `zero` forces $0.
- Cache: `llm_prices_cache.json` beside the DB, TTL `LLM_PRICE_CACHE_TTL_H`; a failed refresh keeps serving the last good cache.
- **Any price not confirmed live** (static default, stale cache, or fetch failure) is recorded with `cost_estimate: true` in the audit row — cost dashboard shows estimates as such.
- Live responses are USD-per-token floats → converted to per-1M-token to match the existing audit/cost dashboard.

> **Honest note:** true live pricing only exists where the provider exposes it (OpenRouter, Together, some aggregators). DeepSeek/Anthropic/Gemini have no pricing API — they use the maintained table (updated each release, overridable in env). The resolver automates the choice and marks estimates transparently.

### 3.4 Settings UI (Settings → API Keys tab)

Becomes a **provider list**:

- Each configured provider: name, type badge, base URL, chat model, reasoner model, API key (redacted `••••` + configured status), price fields.
- **Active** radio per provider (global) + **Save**.
- **Test connection** button per provider → calls a lightweight endpoint that fires a 1-token probe and reports latency + provider/model echo. Requires admin.
- Secrets stay redacted via the existing `SECTIONS`/env-redaction pattern; keys never returned to the browser.

New/changed endpoints (all admin):
- `GET /api/v1/settings/llm` — now returns provider list + active (extended).
- `POST /api/v1/settings/llm/test` — probe a provider (name + optional key override for first-time setup).
- `PUT /api/v1/settings/llm` — save provider configs + active selection (existing redaction rules extended).

### 3.5 Failover seam (for the on-LAN fallback workstream)

The fallback plugs in here without redesign:
- Provider registry becomes an **ordered list** (primary, fallback, ...).
- Worker on provider error/timeout → try next in order; record `provider_fallback: true` in the audit row.
- Manual override = temporarily pin `LLM_ACTIVE_PROVIDER` (or a `force_provider` setting surfaced in UI).
- While active provider is local (Ollama), the routing layer escalates P1/P2 to humans instead of auto-execute (approved scope rule).

---

## 4. Security Considerations

- API keys remain in `.env` (600 perms, gitignored, redacted in all API responses) — no change to storage model.
- Provider base URLs are **user-supplied** → validate scheme (`http://` or `https://` only) and reject embedded credentials in URL strings.
- SSRF consideration: admins can point at arbitrary URLs (they already own the box). Note in docs; no extra restriction for admin-only config.
- Test-connection endpoint must be admin-only and rate-limited to avoid key-probing loops.

---

## 5. Migration & Backward Compatibility

- **Backward compatible by construction:** `DEEPSEEK_API_KEY/CHAT_MODEL/REASONER_MODEL` map onto the `deepseek` provider block when `LLM_PROVIDER_DEEPSEEK_*` is absent. Existing installs keep working with zero config changes.
- `.env.example` updated with the full registry + comments.
- Worker image needs a rebuild on deploy (new `llm_client.py`); no DB schema changes.
- Existing audit rows untouched; new rows may carry provider-prefixed model names.

---

## 6. Testing Plan

| Test | Method | Pass criteria |
|------|--------|---------------|
| DeepSeek regression | Keep DeepSeek active, submit P3 ping ticket | Same flow as today: job file, audit row with tokens/cost, confidence |
| Adapter unit parity | Run OpenAI-compatible adapter against a local mock server (httpx MockTransport) | Same `LLMResponse` shape for identical payloads |
| Provider switch | Set active=claude (mock), submit ticket | Audit shows `claude/…`, cost uses Claude prices |
| Gemini adapter — **LIVE** | User provides Gemini API key; set active=gemini, submit P3 ticket | Real Gemini call parses correctly; audit records Gemini model/tokens; cost from price table (estimate marker if static) |
| Live price fetch | Configure OpenRouter (or Together), call price resolver | Per-model USD prices cached; stale-cache fallback works; `cost_estimate` flag correct |
| No-key mock mode | Unset all keys | Mock mode returns sensible action (existing behavior) |
| Test connection | UI button per provider | Latency + model echo, key redacted |
| Fallback seam (later) | Ollama server on LAN (future project) | Auto-failover, manual override, P1 → human while local |

---

## 7. Files Touched (expected)

- `src/worker/llm_client.py` — adapter layer + registry + cost refactor
- `src/api/routes/settings.py` — llm section → provider list; test endpoint
- `src/api/templates/settings.html` — API Keys tab UI
- `src/worker/main.py` — pass provider context through if needed (minor)
- `src/.env.example` — registry template
- `docs/operations/update_pipeline.md` — provider config SOP note (future, with Ollama)

---

## 8. Resolved & Open Questions

**Resolved 2025-07-31:**
1. Adapters: OpenAI-compatible + Anthropic + Gemini.
2. Switch granularity: global active provider only.
3. Gemini reasoner model: same-model fallback is acceptable (no separate reasoner tier at Gemini today) — `chat` and `reasoner` fields both default to the same model.
4. Pricing: **live prices** preferred — via the resolver in §3.3 (`live` for OpenRouter/Together, `static` maintained table otherwise, `zero` for local; `auto` default).
5. Gemini adapter will be validated **live** with a user-provided Gemini API key.

**Still open:**
- None blocking implementation. Price table review cadence (who updates DeepSeek/Anthropic/Gemini prices on releases) is an ops question, not a design one.
