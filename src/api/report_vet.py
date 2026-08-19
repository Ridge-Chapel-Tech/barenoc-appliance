"""AI vetting for the Submit Report flow — one LLM call per comment.

Classifies the bug description as:
  bug      — interpretable as a bug; proceed to submit
  not-bug  — not a bug (question, praise, config request…); explain + ask for
             specifics; the UI shows the explanation inline and does not submit
  unclear  — might be a bug but lacks specifics; the UI prompts once for detail,
             then submits flagged

Reuses the provider registry from llm_providers (same keys/config as the
worker + settings). The call is cheap + deterministic-ish: judge-tier model,
temperature 0, small token budget, strict JSON envelope. On any provider
failure the vetting degrades to `bug` (fail-open — never block a report on a
vetting outage) with an explanatory note.

Tests feed fixture responses through a patched adapter (see
test_report_submit.py).
"""

import json
import re
from typing import Optional

from llm_providers import ADAPTERS, judge_model_name, load_providers, provider_order, read_env_file

VETTING_SYSTEM_PROMPT = (
    "You are a support-report triage assistant for a network appliance. "
    "Classify the user's bug description into exactly one of: "
    "bug (an interpretable defect/malfunction), "
    "not-bug (a question, feature request, praise, or configuration request — not a defect), "
    "unclear (might be a defect but lacks enough specifics to act on). "
    "Reply with ONLY a JSON object: "
    '{"verdict": "bug"|"not-bug"|"unclear", "explanation": "one short sentence"}.'
)

_MAX_TOKENS = 120
_TIMEOUT = 30


def _extract_json(text: str) -> Optional[dict]:
    """Pull the verdict JSON out of a possibly-prosy model reply."""
    if not text:
        return None
    # Strip markdown fences if present.
    stripped = re.sub(r"```(?:json)?", "", text).strip("` \n")
    try:
        return json.loads(stripped)
    except Exception:
        pass
    m = re.search(r"\{[^{}]*\}", stripped)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _normalise(parsed: Optional[dict]) -> dict:
    verdict = str((parsed or {}).get("verdict", "")).strip().lower()
    if verdict not in ("bug", "not-bug", "unclear"):
        verdict = "bug"
    explanation = str((parsed or {}).get("explanation", "")).strip()[:300]
    return {"verdict": verdict, "explanation": explanation}


def vet_comment(comment: str, env: Optional[dict] = None) -> dict:
    """One LLM call classifying `comment`. Never raises.

    Returns {verdict, explanation, note}. On no configured provider or any
    failure, verdict is "bug" (fail-open) with a note.
    """
    comment = (comment or "").strip()
    if not comment:
        return {"verdict": "bug", "explanation": "", "note": "empty comment"}

    env = env if env is not None else read_env_file()
    providers = load_providers(env)
    chain = provider_order(env)

    # No configured provider at all -> nothing to vet with; fail open.
    usable = [n for n in chain if n in providers
              and (providers[n].get("api_key") or providers[n].get("deployment") == "on_prem")]
    if not usable:
        return {"verdict": "bug", "explanation": "",
                "note": "no LLM provider configured — submitted without vetting"}

    messages = [
        {"role": "system", "content": VETTING_SYSTEM_PROMPT},
        {"role": "user", "content": comment},
    ]

    for name in usable:
        provider = providers[name]
        adapter = ADAPTERS.get((provider.get("type") or "").lower())
        if not adapter:
            continue
        model = judge_model_name(provider)
        if not model:
            continue
        try:
            raw_text, _, _ = adapter(
                provider, model, messages,
                temperature=0.0, max_tokens=_MAX_TOKENS, timeout=_TIMEOUT,
            )
            parsed = _normalise(_extract_json(raw_text))
            parsed["note"] = ""
            return parsed
        except Exception:
            continue

    return {"verdict": "bug", "explanation": "",
            "note": "vetting provider unavailable — submitted without vetting"}
