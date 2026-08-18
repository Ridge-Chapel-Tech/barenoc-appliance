#!/usr/bin/env python3
"""forum-confirm glue — parse a closed `customer-bug` issue and call the
Supabase `forum-confirm` edge function so the forum thread gets the
"Bug confirmed. Patched in vX" note.

CONVENTION (single source of truth — see the PR + BUILD_LIST handoff):
  When the gate closes a `customer-bug` issue, it comments a line
      Fixed in v<version>
  (e.g. `Fixed in v2026.08.17.a`). This script parses the MOST RECENT such
  comment for the version, extracts the forum thread id from the issue body's
  `https://forum.barenoc.com/thread/<id>` link, and POSTs `{thread_id, version}`
  to the edge function. The edge function is the idempotency gate (it checks
  for an existing confirm reply before inserting).

Exit codes:
  0  — posted successfully, already confirmed (idempotent), or nothing to do
       (no forum thread link / no "Fixed in" comment → skip, non-fatal)
  1  — the forum-confirm call failed (the important signal to surface)

Usage:
  python3 forum_confirm.py --issue /tmp/issue.json --comments /tmp/comments.json
  python3 forum_confirm.py --issue issue.json --comments comments.json --parse   # print parsed fields only
  python3 forum_confirm.py --issue issue.json --comments comments.json --dry-run # print, don't call
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

FORUM_THREAD_RE = re.compile(r"https?://forum\.barenoc\.com/thread/([A-Za-z0-9-]+)")
FIXED_IN_RE = re.compile(r"Fixed in\s+v?([0-9A-Za-z][0-9A-Za-z._-]*)", re.IGNORECASE)


def extract_thread_id(body: str | None) -> str | None:
    """Return the forum thread id from an issue body, or None."""
    if not body:
        return None
    match = FORUM_THREAD_RE.search(body)
    return match.group(1) if match else None


def extract_version(comments) -> str | None:
    """Return the version from the most recent `Fixed in vX` comment.

    `comments` is a list of dicts with a `body` key, in GitHub's ascending
    created_at order. The LAST match wins (i.e. the most recent comment).
    """
    version: str | None = None
    for comment in comments or []:
        body = comment.get("body", "") if isinstance(comment, dict) else str(comment)
        match = FIXED_IN_RE.search(body)
        if match:
            version = match.group(1).rstrip(".,;:)")
    return version


def build_message(version: str) -> str:
    """The exact wording the gate wants — `v` is normalised in, not duplicated."""
    normalized = version.strip().lstrip("v").lstrip("V")
    return f"✅ Bug confirmed. Patched in v{normalized} — please verify."


def _post(url: str, token: str, thread_id: str, version: str) -> tuple[int, dict]:
    payload = json.dumps({"thread_id": thread_id, "version": version}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "barenoc-forum-confirm",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"raw": raw[:500]}
    return status, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True, help="path to the issue JSON (gh api output)")
    parser.add_argument("--comments", required=True, help="path to the comments JSON (gh api output)")
    parser.add_argument("--parse", action="store_true", help="print parsed fields and exit")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent, don't call")
    args = parser.parse_args(argv)

    with open(args.issue, encoding="utf-8") as fh:
        issue = json.load(fh)
    with open(args.comments, encoding="utf-8") as fh:
        comments = json.load(fh)

    thread_id = extract_thread_id(issue.get("body"))
    version = extract_version(comments)

    if args.parse:
        print(json.dumps({"thread_id": thread_id, "version": version}))
        return 0

    if not thread_id:
        print("forum-confirm: no forum thread link in the issue body — nothing to post")
        return 0

    if not version:
        print(
            "forum-confirm: no 'Fixed in vX' comment found — nothing to post "
            "(comment `Fixed in v<version>` on close to trigger the note)"
        )
        return 0

    message = build_message(version)
    if args.dry_run:
        print(json.dumps({"thread_id": thread_id, "version": version, "message": message}))
        return 0

    url = os.environ.get("FORUM_CONFIRM_URL", "").strip()
    token = os.environ.get("FORUM_CONFIRM_TOKEN", "").strip()
    if not url or not token:
        print("forum-confirm: FORUM_CONFIRM_URL / FORUM_CONFIRM_TOKEN env vars are required", file=sys.stderr)
        return 1

    status, body = _post(url, token, thread_id, version)
    if status in (200, 201) and (body.get("created") or body.get("note") == "already confirmed"):
        print(f"forum-confirm: ok (status {status}) -> {json.dumps(body)}")
        return 0

    print(f"forum-confirm: FAILED (status {status}) -> {json.dumps(body)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
