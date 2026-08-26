"""Chat progress-note tone pool — shared category vocabulary + safety net.

Single source of truth for the friendly progress-note vocabulary and the
technical-fragment detection used by TWO consumers:

  * the host-side agent runner (`src/agent/runner.py`) — which VENDORS a copy
    of this module's pool + patterns because it deploys as a single
    self-contained file (deploy.sh scp's runner.py alone; it cannot import
    this module at runtime), and
  * the API-side `src/api/queue_status.py` — which imports this module so its
    "Working on it — {detail}" mapping stays in parity with the runner.

Keep the runner's vendored copy in sync — `src/agent/test_runner.py` asserts
pool/keyword/pattern parity. Stdlib-only: importable from src/api and
src/worker (never import anything outside the stdlib).
"""

import re
import zlib

# ── Activity categories ───────────────────────────────────────────────────
# The order doubles as the tie-break priority: when two categories score the
# same number of keyword hits, the earlier one wins.
CATEGORIES = ("investigating", "connecting", "applying", "verifying", "waiting")

# Keyword cues mapping a raw (technical) progress note's activity to a
# category. Matched as whole words (`\b…\b`) against the lower-cased text, so
# short words like "set"/"fix"/"add"/"read" can't false-positive inside
# "settings"/"prefix"/"address"/"thread". Each keyword counts once per note.
_CATEGORY_KEYWORDS = {
    "investigating": (
        "check", "checking", "read", "reading", "fetch", "fetching",
        "list", "listing", "look", "looking", "find", "finding", "search",
        "searching", "scan", "scanning", "inspect", "inspecting", "review",
        "reviewing", "examine", "examining", "diagnose", "diagnosing",
        "investigate", "investigating", "gather", "gathering", "query",
        "querying", "explore", "exploring", "trace", "tracing", "audit",
        "auditing", "browse", "browsing", "discover", "probe", "probing",
        "dig", "digging", "peek", "glance", "look into",
    ),
    "connecting": (
        "connect", "connecting", "ssh", "login", "log in", "reach",
        "reaching", "ping", "talk", "talking", "device", "laptop",
        "gateway", "switch", "access point", "unifi", "enroll",
        "enrolling", "adopt", "adopting", "handshake", "establish",
        "link", "linking", "attach", "interface", "network", "contact",
        "session", "remote", "handshaking",
    ),
    "applying": (
        "apply", "applying", "change", "changing", "set", "setting",
        "install", "installing", "update", "updating", "upgrade",
        "upgrading", "configure", "configuring", "config", "patch",
        "patching", "deploy", "deploying", "enable", "enabling", "disable",
        "disabling", "restart", "restarting", "reboot", "rebooting",
        "start", "starting", "stop", "stopping", "modify", "modifying",
        "create", "creating", "add", "adding", "remove", "removing",
        "delete", "deleting", "write", "writing", "push", "pushing",
        "roll", "rolling", "replace", "replacing", "move", "moving", "fix",
        "fixing", "adjust", "adjusting", "tune", "tuning", "edit",
        "editing", "put in",
    ),
    "verifying": (
        "verify", "verifying", "confirm", "confirming", "test", "testing",
        "validate", "validating", "ensure", "ensuring", "double-check",
        "doublecheck", "double check", "compare", "comparing", "assert",
        "finalize", "finalizing", "wrap", "wrapping", "finish", "finishing",
        "complete", "completing", "cleanup", "clean up", "tidy", "tidying",
        "almost done", "make sure", "works now", "end to end", "recheck",
        "check it",
    ),
    "waiting": (
        "wait", "waiting", "hold", "holding", "while", "long", "minute",
        "minutes", "be patient", "patience", "taking a", "takes a",
        "running", "processing", "compiling", "building", "downloading",
        "uploading", "syncing", "still", "moment", "hang tight",
        "bear with", "longer", "chug", "progress", "ongoing", "be patient",
    ),
}

# The friendly phrase pool — one list per activity category. Each phrase is one
# short, plain, customer-facing sentence (no paths, commands, packages, uids,
# IPs, or API detail). The runner picks from the category matching the raw
# note's activity; the API-side queue_status uses the same pool for parity.
_POOL = {
    "investigating": [
        "Taking a look at that now…",
        "Let me check on that for you…",
        "Looking into it — one moment…",
        "Digging into the details now…",
        "Reading through the current setup…",
        "Checking the latest state of things…",
        "Reviewing what's there before I change anything…",
        "Gathering the information I need…",
        "Scanning for the source of that…",
        "Tracing through the logs now…",
        "Seeing what's going on behind the scenes…",
        "Investigating — this won't take long…",
        "Pulling up the current details…",
        "Having a closer look at your setup…",
    ],
    "connecting": [
        "Connecting to the device now…",
        "Reaching out to the device…",
        "Getting a secure connection set up…",
        "Talking to the device — one sec…",
        "Making contact with your network…",
        "Linking up with the hardware…",
        "Establishing the connection…",
        "Opening a line to the device…",
        "Handshaking with the device…",
        "Connecting to your network gear…",
        "Reaching the device now…",
        "Touching base with the device…",
        "Getting through to the device…",
        "Bringing the device online…",
    ],
    "applying": [
        "Applying that change now…",
        "Making the change you asked for…",
        "Installing it now…",
        "Setting things up as requested…",
        "Applying the update…",
        "Rolling out the new setting…",
        "Writing the change into place…",
        "Putting the fix in now…",
        "Configuring that for you…",
        "Swapping in the new settings…",
        "Deploying the change…",
        "Updating things now…",
        "Making that adjustment…",
        "Setting it up — this part takes a moment…",
    ],
    "verifying": [
        "Verifying everything looks right…",
        "Confirming the change took effect…",
        "Double-checking my work…",
        "Testing that it works now…",
        "Making sure it's all good…",
        "Checking the result is correct…",
        "Validating the new setup…",
        "Confirming it works end to end…",
        "Running a quick check to be sure…",
        "Just confirming the details…",
        "Wrapping up and verifying…",
        "Almost done — just verifying…",
        "Giving it a final once-over…",
        "Confirming everything is in place…",
    ],
    "waiting": [
        "Still on it — one moment…",
        "Hang tight, this is taking a little longer…",
        "Still working away on this…",
        "This step takes a few minutes…",
        "Running the longer part now…",
        "Working through it — please bear with me…",
        "Still making progress…",
        "This one's a longer task…",
        "Chugging through it — won't be much longer…",
        "Still going — thanks for waiting…",
        "Processing now — this can take a bit…",
        "Moving along — a few more moments…",
        "Almost there — thank you for waiting…",
        "Keeping at it — a little more time…",
    ],
}

# A friendly progress note is one short sentence; anything this long is almost
# certainly several sentences of internal detail.
_PROGRESS_MAX_FRIENDLY_LEN = 220

# Technical-fragment detection (the leak safety net). Any note matching one of
# these — or that is jargon/length-heavy — is replaced with a friendly phrase;
# the raw text stays in the session transcript + runner log.
_TECH_NOTE_PATTERNS = [
    re.compile(r"`"),                       # backticked command / `code`
    re.compile(r"~/", re.IGNORECASE),       # home-path shorthand
    re.compile(r"/(etc|opt|usr|var|home|tmp|sbin|bin)/", re.IGNORECASE),
    re.compile(r"\\"),                    # Windows path separator
    re.compile(r"\b(uids?|gids?|pids?|nopasswd|sudoers|passwordless|passwd)\b",
               re.IGNORECASE),
    re.compile(r"\b(sudo|ssh|scp|rsync|dnf|apt|apt-get|yum|apk|zypper|curl|wget|"
               r"systemctl|journalctl|chmod|chown|usermod|nmap|ping|traceroute)\b",
               re.IGNORECASE),
    re.compile(r"\b(localhost|127\.0\.0\.1|0\.0\.0\.0)\b", re.IGNORECASE),
    re.compile(r"(api/|/api/v\d|endpoint|https?://)", re.IGNORECASE),
    re.compile(r"\.json\b", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"),   # bare IPv4 addresses
    re.compile(r"\b(TKT-\d|ticket_id|access_token|bearer)\b", re.IGNORECASE),
    # Tailnet account logins ("name.name@" — the peer owner login from
    # `tailscale status`); never reaches the customer (08-26 identity leak).
    re.compile(r"\b[a-z0-9][a-z0-9._-]*@(?![a-z0-9])", re.IGNORECASE),
]


def is_technical(text: str) -> bool:
    """True when a progress note looks like internal/technical detail."""
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > _PROGRESS_MAX_FRIENDLY_LEN:
        return True
    if any(p.search(t) for p in _TECH_NOTE_PATTERNS):
        return True
    # ">N chars of jargon": snake_case / long / digit-letter tokens.
    words = re.findall(r"[A-Za-z0-9_]+", t)
    jargon = 0
    for w in words:
        if len(w) > 18 or "_" in w or (
                re.search(r"\d", w) and re.search(r"[A-Za-z]", w) and len(w) >= 4):
            jargon += 1
    return jargon >= 2


def categorize(text: str) -> str:
    """Map a raw (technical) note to an activity category via keyword cues.

    The highest-scoring category wins; ties break by the order in CATEGORIES.
    No match falls back to "investigating" (a neutral "taking a look").
    """
    low = (text or "").lower()
    best, best_score = CATEGORIES[0], -1
    for category in CATEGORIES:
        score = sum(
            1 for kw in _CATEGORY_KEYWORDS[category]
            if re.search(r"\b" + re.escape(kw) + r"\b", low))
        if score > best_score:
            best, best_score = category, score
    return best


def pool_for(category: str) -> list:
    """The phrase list for a category (falls back to investigating)."""
    return _POOL.get(category) or _POOL[CATEGORIES[0]]


def pick_phrase(category: str, seed: int, recent=()) -> str:
    """Pick a phrase from a category's pool, avoiding `recent` when possible.

    Deterministic: the same (category, seed, recent) always yields the same
    phrase. Phrases in `recent` are skipped so consecutive notes differ; if the
    whole pool is in `recent`, fall back to the full pool (never fail).
    """
    pool = pool_for(category)
    recent_set = set(recent or ())
    avail = [p for p in pool if p not in recent_set]
    if not avail:
        avail = pool
    return avail[seed % len(avail)]


def friendly_note(text: str, seed: int = 0, recent=()) -> "tuple[str, bool]":
    """Map a progress note to a chat-safe phrase (canonical of the runner's
    `_friendly_progress_note`).

    Non-technical, user-facing notes pass through untouched. Technical notes
    are replaced with a category-matched friendly phrase. `seed` is a stable
    per-ticket integer (same note -> same phrase); `recent` is an iterable of
    recently-used phrases to avoid immediate repeats.
    """
    if not is_technical(text):
        return (text or "").strip(), False
    category = categorize(text)
    base = (seed or 0) ^ (zlib.crc32((text or "").encode("utf-8")) & 0xffffffff)
    return pick_phrase(category, base, recent), True


def all_phrases() -> list:
    """Every phrase in the pool, flattened (backward-compat with the runner's
    old single flat `_FRIENDLY_PROGRESS` list)."""
    return [p for category in CATEGORIES for p in _POOL[category]]
