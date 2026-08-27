"""Final-answer tone cleanup (chat-facing).

One small, stdlib-only helper used by the jobs-result formatter to strip the
agent's self-narration from a FINAL answer before it is posted to the ticket,
so the customer reads the answer directly:

    "Here's my final answer to the customer", "Lily finished:", "Here's what
    I found:", trailing ``---`` fences → removed.

The LIVE progress-note safety net is separate and lives in the host-side agent
runner (`src/agent/runner.py::_post_progress`), which must be self-contained
(deployed as a single file). Length/truncation of the answer is owned by the
pi-output-truncation fix — this module only strips narration, never truncates.
"""

import re

from tone_pool import redact_identities

# 08-26 identity-leak lesson: never expose tailnet account logins
# ("name.name@" with nothing after the @ — the peer owner login shown by
# `tailscale status`) in a customer answer. Emails (a@b.com) are untouched.
_TAILNET_LOGIN_RE = re.compile(r"\b[a-z0-9][a-z0-9._-]*@(?![a-z0-9])", re.IGNORECASE)

# Leading self-narration the agent prepends to its final answer. Compared
# case-insensitively; matched longest-first so "here's my final answer to the
# customer" wins over "here's my final answer".
_META_NARRATION_PHRASES = [
    "i have completed the installation and verified everything. here's my final answer to the customer",
    "i've completed the installation and verified everything. here's my final answer to the customer",
    "here's my final answer to the customer",
    "here is my final answer to the customer",
    "my final answer to the customer",
    "here's my final answer",
    "here is my final answer",
    "here's what i found",
    "here is what i found",
    "i have completed the installation and verified everything",
    "i've completed the installation and verified everything",
    "i have completed the installation",
    "i've completed the installation",
    "i have completed the task",
    "i've completed the task",
    "the installation is complete",
    "task complete",
    "lily finished",
    "final answer",
]


def strip_meta_narration(text: str) -> str:
    """Strip the agent's self-narration from a final answer (never truncates).

    Removes leading/trailing ``---``-style fences and leading meta-narration
    ("Lily finished:", "Here's my final answer to the customer", "Here's what
    I found:", "I have completed the installation…") so the customer reads the
    answer directly. The answer text itself is left untouched and whole.
    """
    if not text:
        return ""
    cleaned = text.strip()

    def _is_fence(line: str) -> bool:
        s = line.strip()
        return bool(s) and all(ch in "-=_*~" for ch in s)

    _phrases = sorted(_META_NARRATION_PHRASES, key=len, reverse=True)
    while True:
        lines = cleaned.splitlines()
        while lines and _is_fence(lines[0]):
            lines.pop(0)
        while lines and _is_fence(lines[-1]):
            lines.pop()
        cleaned = "\n".join(lines).strip()

        stripped = cleaned.lstrip()
        if not stripped:
            break
        low = stripped.lower()
        matched = None
        for phrase in _phrases:
            if low.startswith(phrase):
                rest = stripped[len(phrase):]
                if not rest or rest[0] in ":.\n\r\t —–":
                    matched = rest
                    break
        if matched is None:
            break
        nxt = matched.lstrip(":. \n\r\t")
        if not nxt.strip():
            # Never reduce a non-empty answer to nothing.
            break
        cleaned = nxt
    # 08-26: never expose tailnet account logins ("name.name@" — the
    # peer owner login from `tailscale status`) in a customer answer.
    cleaned = _TAILNET_LOGIN_RE.sub("", cleaned).strip()

    # Info-sec (TKT-20260823-4534 + 08-26): never expose the known personal
    # identifiers of the developer/owner (names, handles, emails) in an answer.
    # Work notes and customer-facing text share this scrub.
    cleaned = redact_identities(cleaned).strip()

    return cleaned


# ── readable answer structure + truncation (ticket-formatting) ───────────────
# The chat is plain-text-ish (no HTML in the customer thread), so the answer
# path emits `- ` / `1. ` list markers that the UI markdown-lite renderer turns
# into real lists, plus the same word-boundary ellipsis rule the progress cap
# uses (routes/tickets._ellipsize / agent runner) — a truncation is never silent.

# Bullet markers accepted at the start of a line (leading whitespace ignored).
_BULLET_LINE_RE = re.compile(r"^\s*[-*•·‣◦▪–—]\s+")
# Numbered list items: `1. `, `1) `, up to 3 digits.
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+")
# A short "section header" line: capitalized, no trailing period. Used only to
# guarantee blank-line separation so a multi-part answer reads as sections.
_HEADER_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,&/()'’\-]{0,47}:?\s*$")


def ellipsize(text: str, limit: int = 2000) -> str:
    """Trim `text` to at most `limit` chars, appending a Unicode ellipsis (…)
    when content was removed. Cuts on a word boundary so a list item is never
    sliced mid-word. Mirrors routes/tickets._ellipsize + the agent runner — the
    answer path uses the SAME rule so a truncation is never silent."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def structure_answer(text: str) -> str:
    """Make an agent's final answer read as a structured plain-text message.

    - List items are normalized to `- ` (bullets) / `N. ` (numbered) and
      grouped into one block per run (blank line before, blank line after).
    - Short capitalized header lines get blank-line separation so a multi-part
      answer reads as sections, not one wall of text.
    - Runs of blank lines collapse to a single blank line.

    Text-safe: only plain text is produced — no HTML is ever added (the chat
    renderer escapes the text again on the way out).
    """
    if not text:
        return ""
    out = []
    in_list = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            in_list = False
            if out and out[-1] != "":
                out.append("")
            continue
        bullet = _BULLET_LINE_RE.match(line)
        numbered = _NUMBERED_LINE_RE.match(line)
        if bullet:
            item = "- " + line[bullet.end():].strip()
            if not in_list:
                if out and out[-1] != "":
                    out.append("")
                in_list = True
            out.append(item)
        elif numbered:
            item = numbered.group(1) + ". " + line[numbered.end():].strip()
            if not in_list:
                if out and out[-1] != "":
                    out.append("")
                in_list = True
            out.append(item)
        else:
            in_list = False
            if _HEADER_RE.match(line.strip()) and out and out[-1] != "":
                out.append("")
            out.append(line)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
