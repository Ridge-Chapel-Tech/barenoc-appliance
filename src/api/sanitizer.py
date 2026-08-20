# Blocked prompt injection patterns
# Each is a regex pattern that will be checked against ticket text.
# If matched, the ticket is rejected before reaching the LLM.

BLOCKED_PATTERNS = [
    # ── Instruction override ──
    r"ignore (all )?(previous|above|below|prior) (instructions|directions|commands)",
    r"(forget|disregard|override|bypass) (all |everything |)instructions",
    r"you (are |are now |will now )?(a |an |) ?(free|unleashed|DAN|jail)",
    r"system prompt",
    r"new instructions? follow",
    r"do not (follow|obey|comply|listen)",
    r"act as (if|though)",

    # ── Direct command execution ──
    r"\b(rm|del|mkfs|dd)\b .*[/\\]",      # \b = whole word: 'confirm' must NOT trigger
    r"\bformat\b\s+(?:/|[a-z]:[\\/])",    # 'format /dev/sda' (a path follows), not 'format the output'
    r"chmod (777|755|ugo)",
    r"chown .*:",
    r"sudo .*passwd",
    r"base64.*decode|base64.*encode",
    r"wget.*\|.*sh|curl.*\|.*sh",
    r"python3? -c ",
    r"eval\(|exec\(",
    r"subprocess",

    # ── Social engineering / policy bypass ──
    r"output in (json|yaml|markdown) without",
    r"don.'?t (mention|include|tell|show)",
    r"for educational.purposes",
    r"pretend|roleplay",

    # ── Data exfiltration ──
    r"(send|post|upload|exfiltrate) (to|via) (http|https|ftp)",
    r"(cat|dump|export|copy) .*(config|password|secret|key|cert|token)",
    r"environment variable",
    r"env\b",

    # ── Escalation / policy ──
    r"create (admin|root|superuser) (user|account)",
    r"disable (security|firewall|auth|logging)",
    r"allow all (traffic|ports|connections)",
]


def sanitize_ticket(text: str) -> tuple[str | None, str | None]:
    """
    Check ticket text against blocked patterns.
    Returns (sanitized_text, error_message).
    If blocked, text is None and error explains why.
    """
    import re
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return None, f"Rejected: ticket contains blocked pattern"
    return text, None
