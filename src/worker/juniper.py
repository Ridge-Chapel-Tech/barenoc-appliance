#!/usr/bin/env python3
"""Juniper — the Queue Manager responder (worker side, Phase 1).

Juniper is a real chat entity (a bot User row, is_bot=True). The worker runs
this loop alongside the ticket loop at the same cadence: it polls unread
ChatMessages addressed to the bot, answers each, and marks it read.

Capabilities (deterministic-first, LLM polish):
  1. status / queue   — queue depth + the user's own tickets with derived
                        stage + idle age (deterministic; shares queue_status).
  2. summarize TKT-…  — 2-4 sentence summary (LLM; deterministic fallback).
  3. intake           — "I need X installed" -> a real ticket with a judged
                        priority (deterministic rules + confidence note).
  4. directives       — pause/resume/close/note-to-tech conduit the worker
                        honors.
  5. anything else    — a short help reply.

Parallel-safe by construction: reads state, creates NEW tickets, and writes
directive notes — never mutates an in-flight job. Intake creates the ticket
directly in the shared DB (the worker's existing ticket machinery — the same
table POST /api/v1/tickets writes; the API path would need the agent-credentials
mount the worker container doesn't have).
"""

import os
import re
import json
import logging
import datetime
import threading

from models import User, ChatMessage, Ticket, PendingAction, is_tech, is_customer, tech_in_scope
from schemas import generate_ticket_id
from worknotes import add_note
from audit import log_event
from queue_status import derive_status, is_paused, list_notes, last_meaningful_note
from device_resolver import resolve_device_from_text, referenced_devices

logger = logging.getLogger("barenoc-worker.juniper")

TKT_RE = re.compile(r"\bTKT-\d{8}-\d{4}\b", re.I)

# ── name / bot lookup ───────────────────────────────────────────────────────

def _queue_manager_name() -> str:
    """The configured Queue Manager display name (BOT_QUEUE_MANAGER_NAME)."""
    try:
        from llm_providers import read_env_file
        name = (read_env_file().get("BOT_QUEUE_MANAGER_NAME")
                or os.getenv("BOT_QUEUE_MANAGER_NAME") or "").strip()
        return name or "Juniper"
    except Exception:
        return os.getenv("BOT_QUEUE_MANAGER_NAME") or "Juniper"


def get_bot_user(db):
    """The Juniper bot User row (seeded idempotently at API startup)."""
    return db.query(User).filter(User.is_bot == True).order_by(User.id.asc()).first()  # noqa: E712


# ── deterministic intent detection ─────────────────────────────────────────

_STATUS_PATTERNS = (
    "queue", "what's happening", "whats happening", "what is happening",
    "what's up", "whats up", "how's it going", "hows it going",
    "how are things", "any updates", "any news", "any progress",
    "my tickets", "my ticket", "snapshot", "status of my", "ticket status",
    "status of the queue", "update on my", "progress on my",
)

_INTAKE_MARKERS = (
    "i need", "i want", "i'd like", "i have a problem", "something wrong",
    "please", "can you", "could you", "would you", "will you", "help me",
    "install", "set up", "setup", "configure", "config", "fix", "broken",
    "not working", "won't work", "wont work", "doesn't work", "down",
    "offline", "unreachable", "open a ticket", "create a ticket", "report a problem",
    # Part A: "update/upgrade my plex server" must route to intake (the
    # device-binding ticket) — update/upgrade are the whole point of this lane.
    # Status intents win earlier (step 3 runs before intake), so "any updates
    # on my ticket?" still answers read-only.
    "update", "upgrade",
)


def looks_like_status(text: str) -> bool:
    t = " ".join((text or "").lower().split())
    return any(p in t for p in _STATUS_PATTERNS)


def looks_like_intake(text: str) -> bool:
    t = " ".join((text or "").lower().split())
    return any(m in t for m in _INTAKE_MARKERS)


def judge_intake_priority(text: str) -> tuple:
    """Deterministic priority at intake. Returns (priority, note).

    Rules (install/outage/security/urgent -> P1-P2; routine -> P3; the rest
    -> P4). No LLM refinement in Phase 1 — deterministic keeps the judgement
    honest, free and free of added risk; the note is stored on the ticket.
    """
    t = " ".join((text or "").lower().split())
    if not t:
        return "P3", "empty request — default P3"

    # P1 — outage / emergency / security incident / hard-down
    if any(w in t for w in (
            "outage", "emergency", "urgent", "critical", "breach", "ransomware",
            "hacked", "compromised", "no internet", "internet is down",
            "network is down", "entire network", "all down", "production down")):
        return "P1", "outage/urgent keyword matched"
    if any(w in t for w in (
            "down", "offline", "unreachable", "no connectivity", "can't connect",
            "cannot connect", "not responding", "no power")):
        return "P1", "connectivity/outage keyword matched"

    # P2 — install / fix / security (the classic "I need X installed")
    if any(w in t for w in (
            "install", "set up", "setup", "security", "virus", "malware",
            "broken", "not working", "won't work", "wont work", "doesn't work",
            "fix", "recover", "restore", "migrate")):
        return "P2", "install/fix/security keyword matched"

    # P3 — routine requests
    if any(w in t for w in (
            "update", "upgrade", "change", "configure", "config", "add",
            "create", "check", "review", "report", "please", "can you",
            "could you", "i need", "i want")):
        return "P3", "routine request"

    # P4 — informational / no urgency signal
    return "P4", "informational — no urgency keywords"


# ── directives (conduit) ────────────────────────────────────────────────────

def _parse_pause_time(rest: str, now=None):
    """Parse a pause target: '8 PM', '8pm', '20:00', 'now', an ISO datetime, or
    'YYYY-MM-DD HH:MM'. Returns (target_dt_naive_utc, human_label) or None."""
    r = (rest or "").strip().lower()
    now = now or datetime.datetime.utcnow()

    if r in ("now", "immediately", "right now", "asap"):
        # "now" = hold indefinitely (no resume time given).
        return now + datetime.timedelta(days=3650), "now"

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.datetime.strptime(r, fmt)
            return dt, dt.isoformat(timespec="minutes")
        except ValueError:
            pass
    try:
        dt = datetime.datetime.fromisoformat(r)
        return dt, dt.isoformat(timespec="minutes")
    except Exception:
        pass

    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", r)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target, target.strftime("%H:%M")
    return None


def parse_directive(text: str, now=None) -> dict:
    """Parse a conduit directive from a message. Returns None when the message
    is not a directive. Kinds: pause / resume / close / note."""
    t = (text or "").strip()

    m = re.search(r"\b(pause|hold)\s+(TKT-\d{8}-\d{4})\b\s*(?:until\s+)?(.+)?", t, re.I)
    if m:
        ticket_id = m.group(2).upper()
        rest = (m.group(3) or "").strip()
        if not rest:
            return {"kind": "pause", "ticket_id": ticket_id,
                    "error": f"Pause {ticket_id} until when? e.g. \"pause {ticket_id} until 8 PM\"."}
        parsed = _parse_pause_time(rest, now=now)
        if parsed is None:
            return {"kind": "pause", "ticket_id": ticket_id,
                    "error": f"I couldn't parse a time from \"{rest}\" — try \"until 8 PM\", \"until 20:00\", or \"until now\"."}
        target_dt, label = parsed
        return {"kind": "pause", "ticket_id": ticket_id, "target_dt": target_dt, "label": label}

    m = re.search(r"\bresume\s+(TKT-\d{8}-\d{4})\b", t, re.I)
    if m:
        return {"kind": "resume", "ticket_id": m.group(1).upper()}

    # close: "close TKT-…" (explicit) / "close the ticket" (context fallback).
    # If the no-id wording also names a ticket, the explicit id wins.
    m = re.search(r"\bclose\s+(TKT-\d{8}-\d{4})\b", t, re.I)
    if m:
        return {"kind": "close", "ticket_id": m.group(1).upper()}
    if re.search(r"\bclose\s+(?:the\s+|this\s+)?ticket\b", t, re.I):
        tkt = TKT_RE.search(t)
        return {"kind": "close", "ticket_id": (tkt.group(0).upper() if tkt else None)}

    m = re.search(
        r"\b(?:note|tell)\s+to\s+(?:the\s+)?technician(?:\s+on\s+(TKT-\d{8}-\d{4}))?\s*:\s*(.+)",
        t, re.I)
    if m:
        return {"kind": "note", "ticket_id": (m.group(1) or "").upper() or None,
                "message": m.group(2).strip()}
    m = re.search(r"\bnote\s+to\s+tech\s*:\s*(.+)", t, re.I)
    if m:
        return {"kind": "note", "ticket_id": None, "message": m.group(1).strip()}
    return None


# ── ticket-thread close intent (autonomous tickets) ─────────────────────────
# parse_directive above covers the Juniper DM conduit ("close TKT-…", "close
# the ticket"). The TICKET THREAD needs a broader detector: a customer replying
# in a completed ticket's thread ("yes, please close", "you can close it",
# "close", "done, thanks — close it") must close the ticket inline — never
# spawn a fresh re-investigating session (TKT-20260818-5615). These pure
# functions are shared with the worker pipeline (main.process_ticket).

# Objects a "close" may target that are NOT the ticket — a close request for
# these is a device/rule/UI action, never a ticket close.
_CLOSE_NEGATIVE_OBJECTS = frozenset({
    "port", "ports", "gap", "app", "apps", "application", "applications",
    "window", "windows", "tab", "tabs", "program", "programs", "case", "cases",
    "file", "files", "rule", "rules", "firewall", "vlan", "vlans", "ssid",
    "ssids", "account", "accounts", "network", "networks", "service",
    "services", "server", "servers", "device", "devices", "switch", "switches",
    "router", "routers", "connection", "connections", "session", "sessions",
    "terminal", "shell", "process", "processes", "ssh", "stream", "streams",
})

# Every word in a pure close request is close-related filler. Any other word
# (a new-work verb, an object) means the message isn't ONLY a close request.
_CLOSE_FILLER = frozenset({
    "a", "an", "all", "and", "ahead", "can", "close", "closed", "closing",
    "confirm", "confirmed", "done", "feel", "fine", "for", "free", "go",
    "good", "great", "is", "it", "me", "my", "now", "ok", "okay", "our",
    "out", "perfect", "please", "pls", "plz", "request", "sure", "thanks",
    "thank", "that", "the", "this", "thx", "ticket", "to", "ty", "works",
    "working", "yeah", "yep", "yes", "you", "your", "yup", "issue",
})

# Thanks/ack filler: a completed ticket where the customer says only one of
# these is an ack, handled inline (short note) rather than re-dispatched.
_ACK_FILLER = frozenset({
    "affirmative", "agree", "agreed", "all", "alright", "appreciate",
    "appreciated", "awesome", "cheers", "confirm", "confirmed", "cool", "correct",
    "done", "excellent", "fine", "fixed", "good", "got", "great", "indeed", "it",
    "its", "k", "looks", "much", "nice", "ok", "okay", "perfect", "perfecto",
    "please", "resolved", "right", "roger", "set", "so", "sound", "sounds",
    "sure", "sweet", "thank", "thanks", "that", "thx", "tnx", "true", "ty",
    "wonderful", "work", "working", "works", "yeah", "yep", "yes", "you", "yup",
})


# New-work imperatives that must NOT coexist with a lenient close directive
# (a past-tense narration of the user's own fix is fine; an instruction to the
# agent to do more work is not).
_NEW_WORK_IMPERATIVE_RE = re.compile(
    r"\b(?:please\s+)?(?:run|check|install|update|upgrade|change|set|reboot|"
    r"restart|fix|configure|scan|verify|apply|remove|uninstall|test|create|"
    r"add|delete|do|make|enable|disable|replace|investigate|look|find|monitor|"
    r"watch|setup|set\s+up)\b")


def _normalize_intent_text(text: str) -> str:
    """Lowercase, collapse whitespace, drop apostrophes/em-dashes for matching."""
    return re.sub(r"['’]", "", " ".join((text or "").lower().split()))


def close_intent(text: str) -> bool:
    """True when the message is an explicit request to close the TICKET (not a
    port/rule/app/etc.) — 'close', 'yes, please close', 'close the ticket',
    'you can close it', 'done, thanks — close it'."""
    t = _normalize_intent_text(text)
    if not re.search(r"\bclose\b", t):
        return False
    # "close <det> <object>" with a non-ticket object is a device/UI action.
    m = re.search(
        r"\bclose\b\s+(?:the\s+|this\s+|that\s+|a\s+|an\s+|my\s+|our\s+)?([a-z]+)", t)
    if m and m.group(1) in _CLOSE_NEGATIVE_OBJECTS:
        return False
    # A pure close request has only close-related words (no new-work verbs).
    if all(w in _CLOSE_FILLER for w in re.findall(r"[a-z]+", t)):
        return True
    # Lenient: the close directive is the FINAL clause and the message is a
    # past-tense statement of the user's OWN resolution, not a new-work
    # request — "I moved the link to port 2. close". A trailing new-work
    # request ("close and run updates") or any new-work imperative still
    # refuses the auto-close.
    if re.search(r"\bclose\b\s*(?:it|this\s+ticket|the\s+ticket|this|that)?\s*[.!]?\s*$", t) \
            and not re.search(_NEW_WORK_IMPERATIVE_RE, t):
        return True
    return False


def ack_intent(text: str) -> bool:
    """True when the message is ONLY a thanks/ack/confirmation — no new work
    ('thanks', 'ok', 'got it', 'sounds good', 'confirmed', 'that works')."""
    tokens = re.findall(r"[a-z]+", _normalize_intent_text(text))
    if not tokens:
        return False
    return all(w in _ACK_FILLER for w in tokens)


# ── authorization ───────────────────────────────────────────────────────────

def can_direct(db, ticket, user) -> bool:
    """Only the ticket owner (requester) or the technician tier/admin within
    their device-group scope may direct a ticket."""
    if is_tech(user):
        return tech_in_scope(db, ticket, user)
    return ticket.submitter_id == user.id


def _requester_name(db, ticket) -> str:
    """Human name for the ticket's requester (the close-loop owner)."""
    sub = db.query(User).filter(User.id == ticket.submitter_id).first() if ticket.submitter_id else None
    return (sub.username or "the requester") if sub else "the requester"


_ACTIVE_TICKET_STATUSES = ("open", "in_progress", "awaiting_approval", "escalated", "customer_action")


def _most_recent_active_ticket(db, user):
    return (db.query(Ticket)
            .filter(Ticket.submitter_id == user.id,
                    Ticket.status.in_(_ACTIVE_TICKET_STATUSES))
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
            .first())


# ── LLM summary (polish) ────────────────────────────────────────────────────

JUNIPER_SUMMARY_PROMPT = (
    "You are Juniper, the BareNOC queue manager, giving a customer a short "
    "plain-language summary of their support ticket. Use ONLY the ticket title, "
    "status and work notes provided — never invent details. Reply in 2-4 "
    "sentences, no preamble, no markdown, no code fences."
)


def _chat_completion(system: str, user: str, max_tokens: int = 250):
    """Free-text completion through the SAME provider chain/cost path the
    worker uses (llm_client.provider_chain + llm_providers.ADAPTERS). Returns
    the trimmed text or None (caller degrades to a deterministic line)."""
    from llm_client import provider_chain
    from llm_providers import ADAPTERS
    chain = provider_chain()
    if not chain:
        return None
    if all(not p.get("api_key") and p.get("deployment") != "on_prem" for p in chain):
        return None  # dev/mock: no real provider — deterministic fallback
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    for provider in chain:
        adapter = ADAPTERS.get((provider.get("type") or "").lower())
        if not adapter or (not provider.get("api_key") and provider.get("deployment") != "on_prem"):
            continue
        model = provider.get("chat_model") or provider.get("reasoner_model") or ""
        if not model:
            continue
        try:
            timeout = int(os.getenv("LLM_TIMEOUT_S", "30") or 30)
            text, pt, rt = adapter(provider, model, messages, temperature=0.2,
                                   max_tokens=max_tokens, timeout=timeout)
            if text and text.strip():
                logger.info("Juniper LLM via %s/%s (%s tokens)", provider["name"], model, rt)
                return text.strip()
        except Exception as e:
            logger.warning("Juniper LLM provider %s failed: %s", provider.get("name"), e)
            continue
    return None


def summarize_ticket(db, ticket) -> str:
    """2-4 sentence summary; deterministic stage line if the LLM fails."""
    summary = _chat_completion(
        JUNIPER_SUMMARY_PROMPT,
        _summary_prompt(ticket),
        max_tokens=250,
    )
    if summary:
        return summary
    return _deterministic_summary(ticket)


def _summary_prompt(ticket) -> str:
    notes = list_notes(ticket)
    note_lines = [f"- {n.get('event')}: {str(n.get('detail') or '')[:200]}" for n in notes[-10:]]
    st = derive_status(ticket)
    return (
        f"Ticket: {ticket.ticket_id} [{ticket.priority}] {ticket.title}\n"
        f"Status: {ticket.status} ({st.get('label')})\n"
        f"Description: {(ticket.description or '')[:400]}\n"
        f"Work notes:\n" + ("\n".join(note_lines) if note_lines else "(none)")
    )


def _deterministic_summary(ticket) -> str:
    st = derive_status(ticket)
    notes = list_notes(ticket)
    tail = ""
    if notes:
        last = notes[-1]
        tail = f" Last note: {str(last.get('detail') or '')[:160]}"
    return f"{ticket.ticket_id} [{ticket.priority}] {ticket.title} — {st['label']}.{tail}"


# ── status / queue (deterministic) ──────────────────────────────────────────

def _fmt_idle(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def queue_snapshot(db, user) -> str:
    open_n = db.query(Ticket).filter(Ticket.status == "open").count()
    inprog = db.query(Ticket).filter(Ticket.status == "in_progress").count()
    esc = db.query(Ticket).filter(Ticket.status == "escalated").count()
    lines = [f"Here's the queue: {open_n} open, {inprog} in progress, {esc} escalated."]

    own = (db.query(Ticket)
           .filter(Ticket.submitter_id == user.id,
                   Ticket.status.in_(_ACTIVE_TICKET_STATUSES))
           .order_by(Ticket.created_at.desc(), Ticket.id.desc())
           .limit(10).all())
    if own:
        lines.append("Your tickets:")
        for t in own:
            st = derive_status(t)
            idle = f" · idle {_fmt_idle(st['idle_seconds'])}" if st["idle_seconds"] is not None else ""
            paused = " · ⏸ paused" if is_paused(t) else ""
            lines.append(f"  {t.ticket_id} [{t.priority}] {t.title[:60]} — {st['label']}{idle}{paused}")
    else:
        lines.append("You have no active tickets.")

    if is_tech(user):
        active = (db.query(Ticket)
                  .filter(Ticket.status.in_(("open", "in_progress", "awaiting_approval", "escalated")))
                  .order_by(Ticket.priority.asc(), Ticket.created_at.desc())
                  .limit(10).all())
        if active:
            lines.append("Active work:")
            for t in active:
                st = derive_status(t)
                paused = " · ⏸ paused" if is_paused(t) else ""
                lines.append(f"  {t.ticket_id} [{t.priority}] {t.title[:60]} — {st['label']}{paused}")
    return "\n".join(lines)


# ── pending-items context (role-aware, per-user) ────────────────────────────
# The Juniper front desk surfaces each user's OWN pending items. Customers see
# only their own tickets awaiting verification; the technician tier/admin
# additionally see escalations + firmware pending actions (gateway approvals
# admin-only; technician visibility gated by FIRMWARE_TECH_VISIBILITY).

_PENDING_MARKERS = (
    "pending", "my pending", "pending items", "needs my attention",
    "need my attention", "what do i need to do", "what needs to be done",
    "my approvals", "awaiting my", "awaiting verification",
    "what's waiting", "whats waiting", "escalations", "firmware approvals",
    "what should i do",
)
_GREETING_RE = re.compile(
    r"^(hi|hello|hey|yo|hiya|howdy|hola|sup|good\s+(morning|afternoon|evening))"
    r"([,.!?\s]*)$", re.I)
_APPROVE_RE = re.compile(r"\b(approve|confirm)\s+(?:firmware\s+|approval\s+|#)?(\d+)\b", re.I)
_RESOLVE_RE = re.compile(r"\b(resolve|clear|dismiss)\s+(?:escalation\s+|#)?(\d+)\b", re.I)


def _tech_visibility() -> bool:
    """FIRMWARE_TECH_VISIBILITY (default off) — mirrors firmware.py."""
    try:
        from llm_providers import read_env_file
        raw = (read_env_file().get("FIRMWARE_TECH_VISIBILITY") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:
        return False


def _pending_visible(user, a) -> bool:
    """Role visibility for a PendingAction (mirrors routes/firmware._can_see)."""
    if user.role == "admin":
        return True
    if (a.required_role or "") == "admin":
        return False  # gateway approval admin-only regardless
    if user.role in ("technician", "operator") and _tech_visibility():
        return True
    return False


def pending_items(db, user) -> dict:
    """Role-aware pending items for the user. Never leaks another user's items."""
    out = {"tickets_awaiting_verification": [], "escalations": [], "firmware_approvals": []}

    # 1. The requester's own tickets awaiting THEIR verification (the ball is
    #    in the customer's court).
    own = (db.query(Ticket)
           .filter(Ticket.submitter_id == user.id,
                   Ticket.status.in_(("customer_action", "awaiting_approval")))
           .order_by(Ticket.priority.asc(), Ticket.created_at.desc()).all())
    own_ids = {t.id for t in own}
    # Also catch answered tickets whose last meaningful note is an
    # ai_tech_feedback ("Answered — awaiting your confirmation").
    for t in (db.query(Ticket)
              .filter(Ticket.submitter_id == user.id,
                      Ticket.status.in_(("open", "in_progress", "completed")))
              .all()):
        if t.id in own_ids:
            continue
        if (last_meaningful_note(t) or {}).get("event") == "ai_tech_feedback":
            own.append(t)
    out["tickets_awaiting_verification"] = own

    if is_tech(user):
        # 2. Escalations requiring review (ticket escalations + firmware
        #    escalation pending-actions in scope).
        esc = (db.query(Ticket)
               .filter(Ticket.status == "escalated")
               .order_by(Ticket.priority.asc(), Ticket.created_at.desc()).all())
        out["escalations"] = list(esc)

        # 3. Firmware pending approvals in scope.
        rows = (db.query(PendingAction)
                .filter(PendingAction.status.in_(("pending", "deferred")))
                .order_by(PendingAction.created_at.desc()).all())
        for a in rows:
            if not _pending_visible(user, a):
                continue
            if a.kind == "approval":
                out["firmware_approvals"].append(a)
            else:
                out["escalations"].append(a)
    return out


def pending_context(db, user) -> str:
    """Detailed pending-items listing for the front-desk discussion."""
    p = pending_items(db, user)
    lines = []
    tv = p["tickets_awaiting_verification"]
    if tv:
        lines.append(f"Tickets awaiting your verification ({len(tv)}):")
        for t in tv[:10]:
            st = derive_status(t)
            lines.append(f"  {t.ticket_id} [{t.priority}] {t.title[:60]} — {st['label']}")
    if is_tech(user):
        esc = p["escalations"]
        if esc:
            lines.append(f"Escalations requiring review ({len(esc)}):")
            for e in esc[:10]:
                if isinstance(e, Ticket):
                    lines.append(f"  {e.ticket_id} [{e.priority}] {e.title[:60]}")
                else:
                    lines.append(f"  pending #{e.id} {e.device_name or e.mac_address or 'device'} — {e.title[:60]}")
        fw = p["firmware_approvals"]
        if fw:
            lines.append(f"Firmware approvals in your scope ({len(fw)}):")
            for a in fw[:10]:
                ver = f" {a.firmware_from}→{a.firmware_to}" if a.firmware_from else ""
                lines.append(f"  #{a.id} {a.device_name or a.mac_address or 'device'}{ver} — {a.title[:60]}")
    has_any = bool(tv) or (is_tech(user) and bool(p["escalations"] or p["firmware_approvals"]))
    if not has_any:
        lines.append("Nothing is waiting on you right now.")
    lines.append(
        "Reply \"approve #<id>\" to approve a firmware action, \"resolve #<id>\" "
        "to clear an escalation, or \"close TKT-…\" to close a ticket.")
    return "\n".join(lines)


def front_desk_greeting(db, user) -> str:
    """Role-aware greeting with the pending-items summary line(s)."""
    p = pending_items(db, user)
    lines = ["Hi! I'm Juniper, your front desk. 👋"]
    tv = p["tickets_awaiting_verification"]
    if tv:
        lines.append(f"You have {len(tv)} ticket(s) awaiting your verification.")
    if is_tech(user):
        esc_n = len(p["escalations"])
        if esc_n:
            lines.append(f"{esc_n} escalation(s) requiring review.")
        fw_n = len(p["firmware_approvals"])
        if fw_n:
            lines.append(f"{fw_n} pending action approval(s) in your scope.")
    lines.append("What can I help with? (say \"pending\" for the details)")
    return "\n".join(lines)


def looks_like_pending(text: str) -> bool:
    t = " ".join((text or "").lower().split())
    return any(m in t for m in _PENDING_MARKERS)


def _handle_pending_action(db, bot, msg, sender, text: str):
    """Approve/resolve a firmware pending item via chat, per role. Returns a
    reply ChatMessage, or None when the message isn't a pending-action command."""
    m = _APPROVE_RE.search(text)
    if m:
        item_id = int(m.group(2))
        a = db.query(PendingAction).get(item_id)
        if not a:
            return reply(db, bot, msg, f"I can't find pending item #{item_id}.")
        if not _pending_visible(sender, a):
            return reply(db, bot, msg, f"I can't see pending item #{item_id} — it's outside your scope.")
        if a.kind != "approval":
            return reply(db, bot, msg, f"#{item_id} is an escalation, not an approval — use \"resolve #{item_id}\".")
        if a.status not in ("pending", "deferred"):
            return reply(db, bot, msg, f"#{item_id} is already {a.status}.")
        a.status = "approved"
        a.resolved_by = sender.username
        a.resolved_at = datetime.datetime.utcnow()
        a.resolved_note = "approved via Juniper chat"
        db.commit()
        try:
            log_event(db, "firmware_approval_approved", sender.username,
                      {"item_id": a.id, "device": a.device_name, "via": "juniper"})
        except Exception:
            logger.exception("Juniper approval audit failed (non-fatal)")
        return reply(db, bot, msg,
                     f"Approved #{item_id} — {a.device_name or a.mac_address or 'device'}. "
                     f"The upgrade engine will pick it up.")

    m = _RESOLVE_RE.search(text)
    if m:
        item_id = int(m.group(2))
        a = db.query(PendingAction).get(item_id)
        if not a:
            return reply(db, bot, msg, f"I can't find pending item #{item_id}.")
        if not _pending_visible(sender, a):
            return reply(db, bot, msg, f"I can't see pending item #{item_id} — it's outside your scope.")
        if a.status == "resolved":
            return reply(db, bot, msg, f"#{item_id} is already resolved.")
        a.status = "resolved"
        a.resolved_by = sender.username
        a.resolved_at = datetime.datetime.utcnow()
        a.resolved_note = "resolved via Juniper chat"
        db.commit()
        try:
            log_event(db, "firmware_pending_resolved", sender.username,
                      {"item_id": a.id, "device": a.device_name, "kind": a.kind, "via": "juniper"})
        except Exception:
            logger.exception("Juniper resolve audit failed (non-fatal)")
        return reply(db, bot, msg, f"Resolved #{item_id} — {a.device_name or a.mac_address or 'device'}.")
    return None


# ── replies ─────────────────────────────────────────────────────────────────

def reply(db, bot, msg, body: str) -> ChatMessage:
    m = ChatMessage(from_user_id=bot.id, to_user_id=msg.from_user_id, body=body)
    db.add(m)
    msg.read_at = datetime.datetime.utcnow()
    db.commit()
    return m


def help_text() -> str:
    return (
        "I'm Juniper, your queue manager. Here's what I can do:\n"
        "• \"what's happening?\" — queue depth + your tickets\n"
        "• \"pending\" — list what's waiting on you\n"
        "• \"summarize TKT-…\" — a short summary of a ticket\n"
        "• \"I need Doom installed on my laptop\" — open a ticket (I judge the priority)\n"
        "• \"pause TKT-… until 8 PM\" / \"resume TKT-…\" — hold or release a ticket\n"
        "• \"close TKT-…\" / \"close the ticket\" — close a ticket\n"
        "• \"approve #<id>\" / \"resolve #<id>\" — act on a pending firmware item\n"
        "• \"note to technician: …\" — pass a message to the tech on your active ticket"
    )


# ── intake ──────────────────────────────────────────────────────────────────

def _heuristic_title(text: str, max_chars: int = 80) -> str:
    """First-sentence fallback title (LLM down / timeout).

    Takes the first sentence, trims to ~`max_chars` on a word boundary, and
    title-cases the alphabetic words while preserving tokens that already
    carry casing/meaning (URLs, IPs, acronyms, model names). Always returns a
    non-empty string so the ticket is never created without a title.
    """
    text = " ".join((text or "").strip().split())
    if not text:
        return "Support request"
    # First sentence = up to the first sentence-ending punctuation followed by
    # whitespace. A lookahead (not a plain character class) so dots INSIDE
    # URLs/IPs ("http://192.168.4.13/") are not treated as sentence breaks.
    m = re.match(r"^(.*?)(?:[.!?]+(?=\s)|$)", text)
    first = (m.group(1) if m else text).strip()
    if not first:
        first = text
    if len(first) > max_chars:
        cut = first[:max_chars]
        boundary = max(cut.rfind(" "), cut.rfind("\n"))
        if boundary > max_chars // 2:
            cut = cut[:boundary]
        first = cut.rstrip(" ,;:")
    words = []
    for w in first.split():
        if w.isupper() or not w.isalpha():
            words.append(w)
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words) or "Support request"


def _intake_title(text: str) -> str:
    """Interpreted title for a chat-spawned ticket.

    Preferred: a short LLM summary (cheap one-shot call through the provider
    chain). Fallback: the first-sentence heuristic. Never blocks the ticket —
    any LLM failure/timeout falls through to the heuristic, which always
    returns a non-empty title.
    """
    title = None
    try:
        from llm_client import generate_title
        title = generate_title(text)
    except Exception:
        logger.exception("Title generation failed (non-fatal)")
        title = None
    return title or _heuristic_title(text)


def _alert_intake(ticket):
    """Best-effort email for P1/P2 intake tickets (mirrors the API's alert so a
    worker-created ticket still pings people). Never raises."""
    if ticket.priority not in ("P1", "P2"):
        return

    def _send():
        try:
            import html as _html
            from emailer import send_email, get_recipients, alert_html
            recipients = get_recipients("alerts")
            if not recipients:
                return
            rows = [
                ("Ticket", f"{ticket.ticket_id} <b>[{ticket.priority}]</b>"),
                ("Title", _html.escape(ticket.title or "")),
                ("Priority", f"<b style='color:#e03131'>{ticket.priority}</b>"),
                ("Description", _html.escape((ticket.description or "")[:500])),
                ("Status", "open"),
            ]
            send_email(recipients, f"[{ticket.priority}] BareNOC: {ticket.title}",
                       body_html=alert_html("New ticket (Juniper intake)", rows),
                       body_text=f"New {ticket.priority} ticket {ticket.ticket_id}: {ticket.title}")
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _handle_intake(db, bot, msg, sender, text: str) -> ChatMessage:
    priority, note = judge_intake_priority(text)
    ticket = Ticket(
        ticket_id=generate_ticket_id(),
        title=_intake_title(text),
        description=text.strip(),
        priority=priority,
        status="open",
        source="chat",
        submitter_id=sender.id,
    )
    db.add(ticket)
    db.flush()
    # Part A: bind the referenced device (name / hostname / IP substring) so
    # the ticket acts on the right box. Ambiguity -> target_device_id stays
    # None (today's behavior) — never guess wrong. Agent devices are preferred.
    try:
        bound = resolve_device_from_text(db, text)
    except Exception:
        logger.exception("Device resolution failed (non-fatal)")
        bound = None
    if bound is not None:
        ticket.target_device_id = bound.id
    add_note(ticket, "intake_priority",
             f"Deterministic priority {priority}: {note}", actor="juniper")
    if bound is not None:
        add_note(ticket, "intake_device",
                 f"Bound to device: {bound.name} ({bound.ip_address})",
                 actor="juniper")
    else:
        refs = referenced_devices(db, text)
        if len(refs) > 1:
            add_note(ticket, "intake_device",
                     "Device reference is ambiguous (multiple devices matched) "
                     "— not bound to a specific device", actor="juniper")
    db.commit()
    try:
        log_event(db, "ticket_created", "juniper", {
            "ticket_id": ticket.ticket_id, "priority": priority,
            "source": "chat", "via": "juniper-intake", "note": note,
        }, ticket.ticket_id)
    except Exception:
        logger.exception("Juniper intake audit failed (non-fatal)")
    _alert_intake(ticket)
    return reply(db, bot, msg,
                 f"Opened {ticket.ticket_id} ({priority}) — Lily will pick it up.\n"
                 f"View {ticket.ticket_id} →")


# ── directive handlers ──────────────────────────────────────────────────────

def _handle_directive(db, bot, msg, sender, directive: dict) -> ChatMessage:
    kind = directive["kind"]
    if kind == "note":
        return _handle_note(db, bot, msg, sender, directive)
    if kind == "close":
        return _handle_close(db, bot, msg, sender, directive)

    tkt_id = directive["ticket_id"]
    ticket = db.query(Ticket).filter(Ticket.ticket_id == tkt_id).first()
    if not ticket:
        return reply(db, bot, msg, f"I can't find {tkt_id}.")
    if not can_direct(db, ticket, sender):
        if is_tech(sender):
            return reply(db, bot, msg,
                         f"I can't change {tkt_id} — it's bound to a device group outside your scope.")
        return reply(db, bot, msg,
                     f"I can't change {tkt_id} — only the ticket owner or a technician can direct that ticket.")

    if kind == "pause":
        if "error" in directive:
            return reply(db, bot, msg, directive["error"])
        add_note(ticket, "pause_until", directive["target_dt"].isoformat(), actor="juniper")
        db.commit()
        if directive.get("label") == "now":
            return reply(db, bot, msg, f"Done — Lily will hold {tkt_id} until you resume it.")
        return reply(db, bot, msg, f"Done — Lily will hold {tkt_id} until {directive['label']}.")

    if kind == "resume":
        add_note(ticket, "pause_cleared", "Resumed by customer", actor="juniper")
        db.commit()
        return reply(db, bot, msg, f"Done — {tkt_id} is back in the queue.")
    return reply(db, bot, msg, help_text())


def _handle_note(db, bot, msg, sender, directive: dict) -> ChatMessage:
    tkt_id = directive.get("ticket_id")
    ticket = None
    if tkt_id:
        ticket = db.query(Ticket).filter(Ticket.ticket_id == tkt_id).first()
        if not ticket:
            return reply(db, bot, msg, f"I can't find {tkt_id}.")
        if not can_direct(db, ticket, sender):
            if is_tech(sender):
                return reply(db, bot, msg,
                             f"I can't pass a note on {tkt_id} — it's bound to a device group outside your scope.")
            return reply(db, bot, msg,
                         f"I can't pass a note on {tkt_id} — only the ticket owner or a technician can direct that ticket.")
    else:
        ticket = _most_recent_active_ticket(db, sender)
        if not ticket:
            return reply(db, bot, msg,
                         "I don't see an active ticket to attach that note to — mention the ticket id "
                         "(e.g. \"note to technician on TKT-…: …\").")
    add_note(ticket, "user_message", directive["message"], actor=sender.username)
    db.commit()
    return reply(db, bot, msg, f"Done — I've passed your note to the technician on {ticket.ticket_id}.")


def _handle_close(db, bot, msg, sender, directive: dict) -> ChatMessage:
    """Close a ticket on the customer's word — same authorization as
    pause/resume (owner/operator/admin only; a tenant can close their own).
    "close the ticket" without an id resolves to the most recent active
    ticket. Mirrors the API PATCH close (status + resolved_at + who closed
    it) and writes an audit event."""
    tkt_id = directive.get("ticket_id")
    if tkt_id:
        ticket = db.query(Ticket).filter(Ticket.ticket_id == tkt_id).first()
        if not ticket:
            return reply(db, bot, msg, f"I can't find {tkt_id}.")
    else:
        ticket = _most_recent_active_ticket(db, sender)
        if not ticket:
            return reply(db, bot, msg,
                         "I don't see an active ticket to close — mention the ticket id "
                         "(e.g. \"close TKT-…\").")
        tkt_id = ticket.ticket_id

    if not can_direct(db, ticket, sender):
        # A non-requester customer confirm is routed to "waiting on <requester>"
        # — it NEVER closes the ticket (requester-owned close-loop).
        if is_customer(sender):
            req = _requester_name(db, ticket)
            return reply(db, bot, msg,
                         f"I can't close {tkt_id} — that ticket belongs to {req}. "
                         f"Waiting on {req} to verify.")
        if is_tech(sender):
            return reply(db, bot, msg,
                         f"I can't close {tkt_id} — it's bound to a device group outside your scope.")
        return reply(db, bot, msg,
                     f"I can't close {tkt_id} — only the ticket owner or a technician can close that ticket.")
    if ticket.status == "closed":
        return reply(db, bot, msg, f"{tkt_id} is already closed.")

    ticket.status = "closed"
    ticket.resolved_at = datetime.datetime.utcnow()
    ticket.assigned_to = sender.username  # record who closed it (mirrors the API PATCH)
    add_note(ticket, "closed", f"Closed by {sender.username}", actor="juniper")
    db.commit()
    try:
        log_event(db, "ticket_closed", "juniper", {
            "ticket_id": tkt_id, "by": sender.username, "via": "juniper-close",
        }, tkt_id)
    except Exception:
        logger.exception("Juniper close audit failed (non-fatal)")
    return reply(db, bot, msg, f"Done — {tkt_id} is closed.")


def _handle_summary(db, bot, msg, sender, tkt_id: str) -> ChatMessage:
    ticket = db.query(Ticket).filter(Ticket.ticket_id == tkt_id).first()
    if not ticket or (is_customer(sender) and ticket.submitter_id != sender.id):
        return reply(db, bot, msg, f"I can't find {tkt_id}.")
    return reply(db, bot, msg, summarize_ticket(db, ticket))


# ── dispatch ────────────────────────────────────────────────────────────────

def handle_message(db, bot, msg, sender) -> ChatMessage:
    text = (msg.body or "").strip()
    if not text:
        return reply(db, bot, msg, help_text())

    # 0. bare greeting -> role-aware front-desk greeting + pending summary
    if _GREETING_RE.match(text):
        return reply(db, bot, msg, front_desk_greeting(db, sender))

    # 0.5 pending-items listing
    if looks_like_pending(text):
        return reply(db, bot, msg, pending_context(db, sender))

    # 1. directives (pause/resume/note/close) — highest precedence
    directive = parse_directive(text)
    if directive:
        return _handle_directive(db, bot, msg, sender, directive)

    # 1.5 firmware pending-item actions (approve/resolve)
    acted = _handle_pending_action(db, bot, msg, sender, text)
    if acted is not None:
        return acted

    # 2. explicit ticket reference -> summary
    m = TKT_RE.search(text)
    if m:
        return _handle_summary(db, bot, msg, sender, m.group(0).upper())

    # 3. status / queue (deterministic)
    if looks_like_status(text):
        return reply(db, bot, msg, queue_snapshot(db, sender))

    # 4. intake (casual request -> a real ticket)
    if looks_like_intake(text):
        return _handle_intake(db, bot, msg, sender, text)

    # 5. anything else -> help
    return reply(db, bot, msg, help_text())


def respond_once(db) -> int:
    """Answer all unread ChatMessages addressed to the Juniper bot. Returns the
    number of messages answered. Parallel-safe + at-most-once: each message is
    marked read BEFORE handling so a crash can never re-run intake twice."""
    bot = get_bot_user(db)
    if not bot:
        return 0
    msgs = (db.query(ChatMessage)
            .filter(ChatMessage.to_user_id == bot.id,
                    ChatMessage.read_at.is_(None))
            .order_by(ChatMessage.created_at.asc())
            .limit(20).all())
    answered = 0
    for m in msgs:
        sender = db.query(User).get(m.from_user_id)
        if sender is None or sender.is_bot:
            m.read_at = datetime.datetime.utcnow()
            db.commit()
            continue
        # At-most-once: consume before answering (prevents duplicate intake).
        m.read_at = datetime.datetime.utcnow()
        db.commit()
        try:
            handle_message(db, bot, m, sender)
            answered += 1
        except Exception as e:
            logger.exception("Juniper responder error for message %s: %s", m.id, e)
            try:
                reply(db, bot, m, "Sorry — I hit an error handling that. Please try again.")
            except Exception:
                pass
    return answered
