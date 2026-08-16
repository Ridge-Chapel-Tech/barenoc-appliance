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
  4. directives       — pause/resume/note-to-tech conduit the worker honors.
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

from models import User, ChatMessage, Ticket
from schemas import generate_ticket_id
from worknotes import add_note
from audit import log_event
from queue_status import derive_status, is_paused, list_notes

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
    is not a directive. Kinds: pause / resume / note."""
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


# ── authorization ───────────────────────────────────────────────────────────

def can_direct(ticket, user) -> bool:
    """Only the ticket owner (tenant) or an operator/admin may direct a ticket."""
    if user.role in ("admin", "operator"):
        return True
    return ticket.submitter_id == user.id


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

    if user.role in ("admin", "operator"):
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
        "• \"summarize TKT-…\" — a short summary of a ticket\n"
        "• \"I need Doom installed on my laptop\" — open a ticket (I judge the priority)\n"
        "• \"pause TKT-… until 8 PM\" / \"resume TKT-…\" — hold or release a ticket\n"
        "• \"note to technician: …\" — pass a message to the tech on your active ticket"
    )


# ── intake ──────────────────────────────────────────────────────────────────

def _intake_title(text: str) -> str:
    return " ".join((text or "").strip().split())[:200]


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
    add_note(ticket, "intake_priority",
             f"Deterministic priority {priority}: {note}", actor="juniper")
    db.commit()
    try:
        log_event(db, "ticket_created", "juniper", {
            "ticket_id": ticket.ticket_id, "priority": priority,
            "source": "chat", "via": "juniper-intake", "note": note,
        }, ticket.ticket_id)
    except Exception:
        logger.exception("Juniper intake audit failed (non-fatal)")
    _alert_intake(ticket)
    return reply(db, bot, msg, f"Opened {ticket.ticket_id} ({priority}) — Lily will pick it up.")


# ── directive handlers ──────────────────────────────────────────────────────

def _handle_directive(db, bot, msg, sender, directive: dict) -> ChatMessage:
    kind = directive["kind"]
    if kind == "note":
        return _handle_note(db, bot, msg, sender, directive)

    tkt_id = directive["ticket_id"]
    ticket = db.query(Ticket).filter(Ticket.ticket_id == tkt_id).first()
    if not ticket:
        return reply(db, bot, msg, f"I can't find {tkt_id}.")
    if not can_direct(ticket, sender):
        return reply(db, bot, msg,
                     f"I can't change {tkt_id} — only the ticket owner or an operator can direct that ticket.")

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
        if not can_direct(ticket, sender):
            return reply(db, bot, msg,
                         f"I can't pass a note on {tkt_id} — only the ticket owner or an operator can direct that ticket.")
    else:
        ticket = _most_recent_active_ticket(db, sender)
        if not ticket:
            return reply(db, bot, msg,
                         "I don't see an active ticket to attach that note to — mention the ticket id "
                         "(e.g. \"note to technician on TKT-…: …\").")
    add_note(ticket, "user_message", directive["message"], actor=sender.username)
    db.commit()
    return reply(db, bot, msg, f"Done — I've passed your note to the technician on {ticket.ticket_id}.")


def _handle_summary(db, bot, msg, sender, tkt_id: str) -> ChatMessage:
    ticket = db.query(Ticket).filter(Ticket.ticket_id == tkt_id).first()
    if not ticket or (sender.role == "tenant" and ticket.submitter_id != sender.id):
        return reply(db, bot, msg, f"I can't find {tkt_id}.")
    return reply(db, bot, msg, summarize_ticket(db, ticket))


# ── dispatch ────────────────────────────────────────────────────────────────

def handle_message(db, bot, msg, sender) -> ChatMessage:
    text = (msg.body or "").strip()
    if not text:
        return reply(db, bot, msg, help_text())

    # 1. directives (pause/resume/note) — highest precedence
    directive = parse_directive(text)
    if directive:
        return _handle_directive(db, bot, msg, sender, directive)

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
