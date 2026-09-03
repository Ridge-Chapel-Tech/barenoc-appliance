"""Submit-Report client — calls the forum `forum-submit` edge function.

The forum URL + shared token live in Settings → Support. The token is stored
0600 (same pattern as the device-control key / NAS credentials) in
/opt/barenoc/volumes/secrets/forum_submit.json; the URL is also persisted there
(falls back to the FORUM_SUBMIT_URL env var). Nothing secret touches .env.

Payload shape (what test_report_submit.py asserts):
    { comment, version, reporter, display_name, bundle, bundle_filename,
      appliance, flagged }

Response contract (the other half of the fix): the forum-submit edge function
must store the bundle under the thread id in the `session-logs` bucket and
confirm it in the 201 response (`bundle_path`, `bundle_url`, or
`attachment_id`). When a bundle was sent but the response carries no such
confirmation, the edge's storage step silently failed — and this client
refuses to report success (raises RuntimeError so the route surfaces a 502).
"""

import json
import os
from typing import Optional

import httpx

from llm_providers import read_env_file
from version import APP_VERSION

FORUM_SUBMIT_SECRET_FILE = "/opt/barenoc/volumes/secrets/forum_submit.json"
FORUM_SUBMIT_URL_DEFAULT = "https://eqivajpnvansfpxkegpr.supabase.co/functions/v1/forum-submit"
FORUM_SUBMIT_TIMEOUT = 30


def _read_secret_file() -> dict:
    try:
        with open(FORUM_SUBMIT_SECRET_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_forum_submit_config() -> dict:
    """Return {url, token} from the 0600 secret file (env fallback for the URL)."""
    cfg = _read_secret_file()
    env = read_env_file()
    url = (cfg.get("url") or "").strip() or env.get("FORUM_SUBMIT_URL", "").strip() \
        or FORUM_SUBMIT_URL_DEFAULT
    return {"url": url, "token": (cfg.get("token") or "").strip()}


def _bundle_confirmed(result) -> bool:
    """True when a forum-submit response confirms the bundle was persisted.

    The edge must return a `bundle_path` (the session-logs object path, e.g.
    `session-logs/<thread_id>/barenoc-support.md`) or a `bundle_url`/
    `attachment_id` once the object is safely stored. A response that only
    carries the thread id is the silent-loss signature this client refuses to
    accept.
    """
    if not isinstance(result, dict):
        return False
    for key in ("bundle_path", "bundle_url", "attachment_id"):
        if str(result.get(key) or "").strip():
            return True
    return bool(result.get("bundle_stored"))


def build_payload(comment: str, user, bundle: str = "", bundle_filename: str = "",
                  flagged: bool = False) -> dict:
    """The exact JSON body sent to the forum-submit edge function."""
    env = read_env_file()
    appliance = (env.get("APPLIANCE_HOST") or env.get("SITE_ID") or "").strip()
    return {
        "comment": comment,
        "version": APP_VERSION,
        "reporter": user.username,
        "display_name": (getattr(user, "display_name", "") or user.username),
        "bundle": bundle,
        "bundle_filename": bundle_filename or "barenoc-support.md",
        "appliance": appliance,
        "flagged": bool(flagged),
    }


def submit_report(comment: str, user, bundle: str = "", bundle_filename: str = "",
                  flagged: bool = False, config: Optional[dict] = None) -> dict:
    """POST the report to forum-submit. Raises RuntimeError on transport/token
    problems so the route can surface a 502. Returns the function's JSON dict."""
    cfg = config if config is not None else load_forum_submit_config()
    if not cfg.get("token"):
        raise RuntimeError(
            "Forum submit token is not configured — set it in Settings → Support")
    payload = build_payload(comment, user, bundle=bundle,
                            bundle_filename=bundle_filename, flagged=flagged)
    try:
        resp = httpx.post(
            cfg["url"],
            json=payload,
            headers={"Authorization": f"Bearer {cfg['token']}"},
            timeout=FORUM_SUBMIT_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"forum-submit call failed: {e}") from e
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {}
        note = body.get("note") or f"HTTP {resp.status_code}"
        raise RuntimeError(f"forum-submit rejected the report: {note}")
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"forum-submit returned a non-JSON response: {e}") from e
    if bundle and not _bundle_confirmed(data):
        raise RuntimeError(
            "forum-submit created the thread but did not confirm the support "
            "bundle was stored (missing bundle_path/bundle_url/attachment_id). "
            "The diagnostic bundle may be lost — not reporting success.")
    return data
