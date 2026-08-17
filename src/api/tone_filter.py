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
    return cleaned
