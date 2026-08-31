#!/usr/bin/env python3
"""BareNOC Worker — processes tickets through LLM and writes job files."""

import os
import sys
import json
import time
import re
import html
import logging
import datetime
from pathlib import Path

# Add parent dir for shared modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal, init_db
from models import Ticket, Device, User, AuditLog, is_tech
from schemas import generate_ticket_id, generate_event_id
from sanitizer import sanitize_ticket
from action_validator import (
    AllowedAction, validate_action, validate_target,
    unknown_target_detail, find_subnet,
)
from audit import log_event
from worknotes import add_note
from queue_status import is_paused
import juniper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("barenoc-worker")

# Paths
JOBS_INCOMING = "/opt/barenoc/jobs/incoming"
JOBS_RUNNING = "/opt/barenoc/jobs/running"
JOBS_COMPLETED = "/opt/barenoc/jobs/completed"
POLL_INTERVAL = 5  # seconds

# LLM provider chain health: True while every configured provider is unusable.
# Drives the P1 outage ticket (opened on total failure, closed on recovery).
_LLM_OUTAGE = False


def _assistant_name() -> str:
    """Hot-read the configured AI assistant name (Settings, BOT_ASSISTANT_NAME)."""
    from llm_providers import read_env_file
    name = (read_env_file().get("BOT_ASSISTANT_NAME") or os.getenv("BOT_ASSISTANT_NAME") or "").strip()
    return name or "Lily"


# ── deterministic ticket-status routing (bug #16) ────────────────────────
# A chat message that names a ticket with a status/where-at intent must answer
# read-only from the ticket's derived status — never spawn a device-action
# ticket for a ticket id. The LLM catalog + prompt make `ticket_status` the
# obvious pick generally; this short-circuit guarantees the explicit case in
# every profile (strict/balanced/autonomous).
_TKT_RE = re.compile(r"\bTKT-\d{8}-\d{4}\b", re.I)
_TKT_STATUS_INTENT_RE = re.compile(
    r"\b(status|where|where'?s|wheres|progress|update|doing|done|complete|completed|"
    r"finished|state|happening)\b", re.I)


def _ticket_status_intent(text: str):
    """Return the TKT-… id when `text` names a ticket AND asks about its
    status/where-it-is — the read-only ticket_status answer. None otherwise."""
    t = text or ""
    m = _TKT_RE.search(t)
    if not m or not _TKT_STATUS_INTENT_RE.search(t):
        return None
    return m.group(0).upper()


# ── whole-subnet ping resilience (friend's bug #2) ────────────────────────
# A "ping sweep 192.168.1.0/24" request must not abort because the AI pinned
# an unresolvable device NAME. When the request is a subnet/IP scan, scan the
# subnet and note the name miss; a bare name-only request still fails friendly.
_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_SCAN_INTENT_RE = re.compile(
    r"\b(ping|sweep|scan|discover|find|reachable|online|hosts?|devices?)\b", re.I)


def _subnet_scan_fallback(ticket_text: str, target: str) -> "str | None":
    """Return a subnet CIDR to scan when the AI pinned an unresolvable device
    NAME but the customer actually asked for a whole-subnet/IP scan. None when
    the request isn't a scan (or the target isn't a bare name) — the bare
    name-only request then fails with the friendly validation message."""
    t = (target or "").strip()
    if not t or _MAC_RE.match(t):
        return None
    if not _SCAN_INTENT_RE.search(ticket_text or ""):
        return None
    return find_subnet(ticket_text)


def _judge_enabled() -> bool:
    """Legacy switch: LLM_JUDGE_ENABLED. Policy profiles also enable the judge
    (see policy.get_policy().judge_required)."""
    from llm_providers import read_env_file
    val = (read_env_file().get("LLM_JUDGE_ENABLED") or os.getenv("LLM_JUDGE_ENABLED") or "false").strip().lower()
    return val in ("1", "true", "yes", "on")


def load_managed_devices(db) -> dict:
    """Load managed devices from DB into action_validator's cache."""
    from action_validator import MANAGED_DEVICES
    devices = db.query(Device).all()
    MANAGED_DEVICES.clear()
    for dev in devices:
        MANAGED_DEVICES[dev.name] = {
            "id": dev.id,
            "ip": dev.ip_address,
            "type": dev.device_type,
            "hostname": dev.hostname,
            "channels": dev.channels or [],
        }
        if dev.hostname:
            MANAGED_DEVICES[dev.hostname] = MANAGED_DEVICES[dev.name]
        if dev.ip_address:
            MANAGED_DEVICES[dev.ip_address] = MANAGED_DEVICES[dev.name]
        # MAC passthrough (UniFi switch-port actions target the switch MAC)
        if dev.mac_address:
            MANAGED_DEVICES[dev.mac_address] = MANAGED_DEVICES[dev.name]
    logger.info(f"Loaded {len(devices)} managed devices")


def should_use_reasoner(ticket) -> bool:
    """Determine whether to use the expensive reasoner model."""
    if ticket.priority in ("P1", "P2"):
        return True
    # Could also check for retry count, keywords, etc.
    return False


def write_job_file(ticket, llm_response, requires_approval: bool = False) -> str:
    """Write a validated job file to the incoming directory.
    requires_approval=True: the agent holds the job until the human approves
    the ticket (status -> in_progress)."""
    # The runner runs as pi-agent, which cannot read the 0600 .env — carry
    # the timezone (and other runtime config the scripts need) in the job.
    _tz = ""
    try:
        from llm_providers import read_env_file
        _tz = (read_env_file().get("TZ") or "").strip()
    except Exception:
        pass
    job = {
        "ticket_id": ticket.ticket_id,
        "action": llm_response.action,
        "target": llm_response.target or "",
        "params": llm_response.params,
        "reason": llm_response.reason,
        "confidence": llm_response.confidence,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "source": "barenoc-worker",
        "tz": _tz,
    }
    if requires_approval:
        job["requires_approval"] = True

    filename = f"{ticket.ticket_id}.json"
    filepath = os.path.join(JOBS_INCOMING, filename)

    with open(filepath, "w") as f:
        json.dump(job, f, indent=2)

    logger.info(f"Job file written: {filepath}")
    return filepath


_NO_INTENT_MARKERS = ("no actionable", "only a greeting", "greeting",
                     "clarification needed", "no specific", "not a request")


def _customer_reply(reason: str) -> str:
    """Customer-facing reply for a no-action ticket: a friendly greeting for
    greetings / vague intents (a dry 'legality/target scope' line reads as no
    reply to a home user), otherwise the plain clarification reason."""
    friendly = ("Hi! 👋 I'm Lily, your BareNOC network assistant. I can check on your "
                "devices, run network tasks and more — what would you like me to do?")
    if reason and any(m in str(reason).lower() for m in _NO_INTENT_MARKERS):
        return friendly
    return f"Waiting on customer: {reason}"


def _plain_reason(reason: str) -> str:
    """One-line, human-facing reason for an escalation (no raw executor dump)."""
    cleaned = " ".join((reason or "").split())
    return cleaned[:240] or "the request needs manual review"


def process_ticket(db, ticket):
    """Process a single ticket through the LLM pipeline."""
    from llm_client import call_llm

    # Defense-in-depth: the poll loops already skip paused tickets, but a
    # direct caller must never start work on a ticket the customer paused.
    if is_paused(ticket):
        logger.info(f"Ticket {ticket.ticket_id}: paused — skipping")
        return

    # Close-directive + no-reinvestigate (TKT-20260818-5615): when the agent
    # already completed the work and the customer's latest reply is only a
    # close/ack/thanks, handle it inline — close or a short ack note — and
    # never dispatch a fresh re-investigating session. Non-close follow-ups on
    # a completed ticket still dispatch normally.
    if _handle_close_intent(db, ticket):
        return

    ticket_id = ticket.ticket_id
    logger.info(f"Processing ticket {ticket_id}: {ticket.title}")

    # Step 1: Sanitize input
    sanitized, error = sanitize_ticket(f"{ticket.title}\n{ticket.description or ''}")
    if error:
        logger.warning(f"Ticket {ticket_id} rejected by sanitizer: {error}")
        add_note(ticket, "escalated", f"Sanitizer blocked: {error}")
        ticket.status = "escalated"
        ticket.resolution = f"Escalated: {error}"
        db.commit()
        log_event(db, "ticket_rejected", "system", {
            "ticket_id": ticket_id, "reason": error
        }, ticket_id)
        return

    # Step 2: Mark as in progress
    add_note(ticket, "processing", "Worker picked up ticket, starting the Lily pipeline")
    ticket.status = "in_progress"
    ticket.assigned_to = "system"
    db.commit()
    # Absorb the user-comment count at processing time: the comment-reprocess
    # path only fires for comments that arrive AFTER this run, so a reopen+
    # comment (adjacent polls) doesn't dispatch the agent twice.
    _USER_COMMENT_COUNT[ticket.ticket_id] = _count_user_notes(ticket)

    # Step 3: Determine model
    use_reasoner = should_use_reasoner(ticket)

    # Step 4: Get device context if target device is set
    device_context = None
    if ticket.target_device_id:
        device = db.query(Device).filter(Device.id == ticket.target_device_id).first()
        if device:
            device_context = _bound_device_context(device)

    ticket_text = f"{ticket.title}\n{ticket.description or ''}"

    # Hard restrictions (Settings → Restrictions): pattern denies block the
    # request outright BEFORE any LLM/judge/pi work — even in autonomous mode.
    from restrictions import blocks_request
    _blocked = blocks_request(ticket_text)
    if _blocked:
        add_note(ticket, "escalated", f"🔒 Blocked by restriction: {_blocked}")
        ticket.status = "escalated"
        ticket.resolution = f"Blocked by restriction: {_blocked}"
        ticket.assigned_to = "human-tech"
        db.commit()
        log_event(db, "restriction_blocked", "system", {
            "ticket_id": ticket_id, "reason": _blocked, "stage": "request"},
            ticket_id)
        return

    # A bare greeting isn't a network task — answer conversationally without
    # spending an LLM call or a judge round. BUT only for a FRESH greeting:
    # once the customer has followed up in-thread (user_message notes exist),
    # the follow-up is the real request and the stale title must not re-trigger
    # a greeting (that's how "hi" swallowed "please run all updates" earlier).
    _GREETING_RE = re.compile(
        r"^(hi|hello|hey|yo|hiya|howdy|hola|sup|good\s+(morning|afternoon|evening))"
        r"([,.!?\s]*|\s+[a-z][a-z0-9]*[,.!?]?)?$", re.I)
    _has_followup = any(n.get("event") == "user_message" for n in _notes_list(ticket))
    if not _has_followup and _GREETING_RE.match(ticket_text.strip()):
        reply = _customer_reply("only a greeting")
        add_note(ticket, "ai_tech_feedback", reply)
        ticket.status = "customer_action"
        ticket.resolution = reply
        ticket.assigned_to = "customer"
        db.commit()
        _notify_customer_action(db, ticket)
        log_event(db, "customer_action", "greeting", {"ticket_id": ticket_id}, ticket_id)
        return

    # Deterministic ticket-status short-circuit (bug #16): an explicit TKT-…
    # reference with a status/where-at intent is answered read-only from the
    # ticket's derived status. Placed BEFORE the autonomous-pi dispatch and the
    # judge/LLM so every profile answers the same and no device-action ticket
    # is ever spawned for a ticket id.
    _tkt_status_id = _ticket_status_intent(ticket_text)
    if _tkt_status_id:
        from llm_client import LLMResponse as _LLMResponse
        llm_response = _LLMResponse(
            action="ticket_status", target="", params={"ticket_id": _tkt_status_id},
            reason=f"Look up the live status of {_tkt_status_id}",
            confidence=0.99, raw_text="", model="deterministic",
            prompt_tokens=0, response_tokens=0, cost_usd=0.0)
    else:
        llm_response = None

    # Autonomous + Lily mode: route open-ended tickets straight to the
    # local agent, Lily (full tool access, no gates — experimental).
    from policy import get_policy as _get_policy
    if llm_response is None and _get_policy().profile == "autonomous" and _pi_enabled():
        _dispatch_pi(db, ticket, ticket_text)
        return

    # Step 4.5: Judge phase (opt-in) — the judge rules on lawfulness and
    # picks the action class; it never executes. Unlawful/ambiguous -> human.
    from policy import get_policy
    policy = get_policy()
    judge_enabled = policy.judge_required or _judge_enabled()
    verdict = None
    if judge_enabled and llm_response is None:
        from judge import judge_request
        verdict = judge_request(
            ticket_text, priority=ticket.priority, device_context=device_context,
            risk_filters=policy.risk_filters)
        log_event(db, "judge_verdict", "system", {
            "ticket_id": ticket_id,
            "profile": policy.profile or "legacy",
            "lawful": verdict.lawful,
            "action_class": verdict.action_class,
            "risk": verdict.risk,
            "scope": verdict.scope,
            "checks": verdict.checks,
            "reason": verdict.reason,
            "model": verdict.model,
            "cached": verdict.cached,
            "short_circuit": verdict.short_circuit,
            "cost_usd": verdict.cost_usd,
        }, ticket_id)
        # Meter the judge's own LLM call (a real call, not the cache/short-
        # circuit/mock path) so judge spend is no longer under-counted in the
        # reports KPI. Accumulate onto the ticket like the executor call does.
        if verdict.model and not verdict.short_circuit and not verdict.cached:
            ticket.llm_prompt_tokens = (ticket.llm_prompt_tokens or 0) + verdict.prompt_tokens
            ticket.llm_response_tokens = (ticket.llm_response_tokens or 0) + verdict.response_tokens
            ticket.llm_cost_usd = round((ticket.llm_cost_usd or 0.0) + verdict.cost_usd, 6)
            ticket.llm_cost_estimate = bool(ticket.llm_cost_estimate or verdict.cost_estimate)
            log_event(db, "llm_request", "system", {
                "ticket_id": ticket_id,
                "model": verdict.model,
                "source": "judge",
                "prompt_tokens": verdict.prompt_tokens,
                "response_tokens": verdict.response_tokens,
                "cost_usd": verdict.cost_usd,
                "cost_estimate": verdict.cost_estimate,
            }, ticket_id)
        if verdict.lawful == "no":
            add_note(ticket, "escalated",
                     f"Judge: request ruled UNLAWFUL — {verdict.reason} "
                     f"(checks: {verdict.checks or 'n/a'})")
            ticket.status = "escalated"
            ticket.resolution = f"Judge ruled unlawful: {verdict.reason}"
            ticket.assigned_to = "human-tech"
            db.commit()
            log_event(db, "escalation", "judge", {
                "ticket_id": ticket_id, "confidence": 1.0,
                "reason": f"Unlawful: {verdict.reason}",
            }, ticket_id)
            return
        if verdict.lawful == "ambiguous":
            if "model call failed" in (verdict.reason or ""):
                # Judge model unreachable — retry on the schedule, don't escalate yet
                _handle_llm_failure(db, ticket, verdict.reason)
                return
            if policy.profile == "autonomous":
                # Autonomous: no escalation — ask the customer to clarify
                add_note(ticket, "customer_input",
                         f"Judge: needs clarification — {verdict.reason}")
                ticket.status = "customer_action"
                ticket.resolution = _customer_reply(verdict.reason)
                ticket.assigned_to = "customer"
                db.commit()
                _notify_customer_action(db, ticket)
                log_event(db, "customer_action", "judge", {
                    "ticket_id": ticket_id, "reason": verdict.reason,
                }, ticket_id)
                return
            add_note(ticket, "escalated",
                     f"Judge: AMBIGUOUS — {verdict.reason}")
            ticket.status = "escalated"
            ticket.resolution = f"Judge ambiguous, human review required: {verdict.reason}"
            ticket.assigned_to = "human-tech"
            db.commit()
            log_event(db, "escalation", "judge", {
                "ticket_id": ticket_id, "confidence": 0.5,
                "reason": f"Ambiguous: {verdict.reason}",
            }, ticket_id)
            return

    # Step 5: Call LLM — executor (judge-enabled) or the single-phase technician
    # (skipped when the deterministic TKT-… status short-circuit already decided).
    # Failure-feedback loop: if an earlier agent run failed, feed the error back
    # so the AI can correct and retry instead of escalating. Also give it the
    # device inventory + the customer's latest comments (the full thread).
    if llm_response is None:
        failure_ctx = _failure_context(ticket)
        inv_ctx = _device_inventory_context(db)
        user_ctx = _recent_user_context(ticket)
        prior_ctx = _prior_agent_context(ticket)
        extra_ctx = "\n\n".join(x for x in (user_ctx, failure_ctx, prior_ctx, inv_ctx) if x)
        if judge_enabled:
            from executor import call_executor
            llm_response = call_executor(
                ticket_text=ticket_text,
                priority=ticket.priority,
                device_context=device_context,
                verdict=verdict,
            )
        else:
            llm_response = call_llm(
                ticket_text=ticket_text,
                priority=ticket.priority,
                device_context=device_context,
                use_reasoner=use_reasoner,
                extra_context=extra_ctx,
            )

        if not llm_response:
            _handle_llm_failure(db, ticket, "model service returned no response")
            return

    # The LLM chain answered — if an outage ticket is open, close it.
    # (skipped for the deterministic TKT-… short-circuit: no provider answered)
    if not _tkt_status_id:
        _clear_llm_outage(db, llm_response.model or "")

    # Step 6: Store LLM metadata on ticket
    # Read-action guardrail: the ticket asked for a specific device subset but
    # the AI picked a read action without the matching filter — apply the
    # obvious one so the answer matches the question ("list my APs" must not
    # return switches; "which APs are offline" must not return everything).
    import re as _re
    blob = f"{ticket.title or ''} {ticket.description or ''}".lower()
    if llm_response.action == "unifi_devices":
        params = dict(llm_response.params or {})
        changed = False
        if not params.get("device_type"):
            if _re.search(r"\b(aps?|access points?|wireless)\b", blob):
                params["device_type"] = "ap"; changed = True
            elif _re.search(r"\bswitches?\b", blob):
                params["device_type"] = "switch"; changed = True
            elif _re.search(r"\b(gateway|router)\b", blob):
                params["device_type"] = "gateway"; changed = True
        if not params.get("status"):
            if _re.search(r"\b(online|reachable)\b", blob):
                params["status"] = "online"; changed = True
            elif _re.search(r"\b(offline|down|unreachable)\b", blob):
                params["status"] = "offline"; changed = True
        if changed:
            llm_response.params = params
    elif llm_response.action == "unifi_clients":
        params = dict(llm_response.params or {})
        changed = False
        if not params.get("online") and _re.search(r"\b(who is online|online now|currently online|which clients are online)\b", blob):
            params["online"] = True; changed = True
        if not params.get("wired"):
            if _re.search(r"\bwired clients?\b", blob):
                params["wired"] = True; changed = True
            elif _re.search(r"\bwireless clients?\b", blob):
                params["wired"] = False; changed = True
        if changed:
            llm_response.params = params

    add_note(ticket, "agent_response", f"{_assistant_name()} suggested {llm_response.action} on {llm_response.target or '(no target)'} (confidence {llm_response.confidence}, via {llm_response.model})")
    ticket.llm_confidence = llm_response.confidence
    ticket.llm_model = llm_response.model
    # Accumulate (never overwrite): a ticket re-dispatched across retries / new
    # customer replies makes a fresh LLM call each time, and each call's cost
    # must be counted exactly once — this mirrors the audit log's per-call rows.
    ticket.llm_prompt_tokens = (ticket.llm_prompt_tokens or 0) + llm_response.prompt_tokens
    ticket.llm_response_tokens = (ticket.llm_response_tokens or 0) + llm_response.response_tokens
    ticket.llm_cost_usd = round((ticket.llm_cost_usd or 0.0) + llm_response.cost_usd, 6)
    ticket.llm_cost_estimate = bool(ticket.llm_cost_estimate or llm_response.cost_estimate)

    # Step 7: Log LLM audit
    log_event(db, "llm_request", "system", {
        "ticket_id": ticket_id,
        "model": llm_response.model,
        "source": "catalog",
        "prompt_tokens": llm_response.prompt_tokens,
        "response_tokens": llm_response.response_tokens,
        "cost_usd": llm_response.cost_usd,
        "cost_estimate": llm_response.cost_estimate,
        "confidence": llm_response.confidence,
        "action": llm_response.action,
        "target": llm_response.target,
    }, ticket_id)

    # Step 8: Validate action
    valid, msg = validate_action(llm_response.action)
    if not valid:
        logger.warning(f"Ticket {ticket_id}: {msg}")
        add_note(ticket, "escalated", f"Action validation failed: {msg}")
        ticket.status = "escalated"
        ticket.resolution = f"Escalated: {msg}"
        db.commit()
        return

    # Step 9: Validate target (escalate_human carries no meaningful target)
    if llm_response.target and llm_response.action != "escalate_human":
        valid, msg = validate_target(llm_response.target)
        if not valid:
            bad_name = llm_response.target
            technical = unknown_target_detail(bad_name)
            # Whole-subnet resilience (friend's bug #2): a subnet/IP scan must
            # not abort because the AI happened to pin an unresolvable device
            # name. Scan the subnet instead and note the name miss.
            subnet = _subnet_scan_fallback(ticket_text, bad_name)
            # Tone-discipline: technical detail goes in the log (logger +
            # hidden note), friendly text goes in the customer-facing note.
            logger.warning(f"Ticket {ticket_id}: {technical}"
                           + (f" — falling back to subnet scan {subnet}" if subnet else ""))
            add_note(ticket, "target_validation_failed", technical)
            if subnet:
                add_note(ticket, "agent_progress",
                         f"{_assistant_name()}: I couldn't find a device named "
                         f"'{bad_name}', so I'll scan {subnet} instead. "
                         f"(If you meant a specific device, tell me its exact name or IP.)")
                llm_response.action = "network_discovery"
                llm_response.target = subnet
                llm_response.params = {}
                llm_response.reason = (
                    f"{llm_response.reason or 'subnet scan'} — note: could not "
                    f"resolve device name '{bad_name}'")
                valid = True
            else:
                add_note(ticket, "escalated", msg)
                ticket.status = "escalated"
                ticket.resolution = msg
                db.commit()
                return

    # Step 10: Check confidence gates
    ticket.action = llm_response.action
    db.commit()

    # Logical actions that need no job/agent:
    if llm_response.action == "complete_ticket":
        add_note(ticket, "completed",
                 f"{_assistant_name()}: closing — {llm_response.reason}")
        ticket.status = "closed"
        ticket.resolution = llm_response.reason
        ticket.resolved_at = datetime.datetime.utcnow()
        ticket.assigned_to = ticket.assigned_to or "ai-tech"
        db.commit()
        log_event(db, "ticket_completed", "ai-tech", {
            "ticket_id": ticket_id,
            "reason": llm_response.reason,
        }, ticket_id)
        return
    if llm_response.action == "escalate_human":
        if policy.profile == "autonomous":
            # Autonomous mode: no human-tech escalation queue (the owner IS the
            # operator) — route back to the customer for info/confirmation.
            add_note(ticket, "customer_input",
                     f"{_assistant_name()}: I can't complete this on my own — {llm_response.reason}"
                     + _review_context(db, llm_response.action))
            ticket.status = "customer_action"
            ticket.resolution = _customer_reply(llm_response.reason)
            ticket.assigned_to = "customer"
            db.commit()
            _notify_customer_action(db, ticket)
            log_event(db, "customer_action", "ai-tech", {
                "ticket_id": ticket_id,
                "reason": llm_response.reason,
            }, ticket_id)
            return
        add_note(ticket, "escalated",
                 f"{_assistant_name()} needs a human for this one: "
                 f"{_plain_reason(llm_response.reason)}")
        ticket.status = "escalated"
        ticket.resolution = f"Human tech required: {_plain_reason(llm_response.reason)}"
        ticket.assigned_to = "human-tech"
        db.commit()
        log_event(db, "escalation", "ai-tech", {
            "ticket_id": ticket_id,
            "confidence": llm_response.confidence,
            "reason": llm_response.reason,
        }, ticket_id)
        return
    if llm_response.action == "request_customer_input":
        add_note(ticket, "customer_input",
                 f"{_assistant_name()}: needs more from you — {llm_response.reason}")
        ticket.status = "customer_action"
        ticket.resolution = _customer_reply(llm_response.reason)
        ticket.assigned_to = "customer"
        db.commit()
        _notify_customer_action(db, ticket)
        log_event(db, "customer_action", "ai-tech", {
            "ticket_id": ticket_id,
            "reason": llm_response.reason,
        }, ticket_id)
        return

    READ_ONLY_ACTIONS = {"ping_test", "snmp_poll", "device_status", "network_discovery",
                          "network_info", "system_time", "ticket_status",
                          "unifi_clients", "unifi_devices", "unifi_ports",
                          "unifi_client_port", "unifi_firewall_rules",
                          "fingerprint_device"}
    conf = llm_response.confidence

    # Hard restrictions: action/device denies are the final gate before
    # execution (covers both the judge→executor and direct-LLM paths).
    from restrictions import check as _restrictions_check
    _blocked = _restrictions_check(ticket_text, llm_response.action, llm_response.target)
    if _blocked:
        add_note(ticket, "escalated", f"🔒 Blocked by restriction: {_blocked}")
        ticket.status = "escalated"
        ticket.resolution = (f"Blocked by restriction: {_blocked} "
                             f"(action: {llm_response.action}, target: {llm_response.target})")
        ticket.assigned_to = "human-tech"
        db.commit()
        log_event(db, "restriction_blocked", "system", {
            "ticket_id": ticket_id, "reason": _blocked,
            "action": llm_response.action, "target": llm_response.target,
            "stage": "execution"},
            ticket_id)
        return

    # Autonomy policy: profile set -> policy gates; none -> exact legacy gates.
    if policy.legacy:
        is_read_only = llm_response.action in READ_ONLY_ACTIONS
        is_high_conf = conf >= 0.95
        is_critical = ticket.priority in ("P1", "P2")
        auto = (is_read_only and conf >= 0.80) or (is_high_conf and is_critical)
        approval = (not auto) and conf >= 0.80
    else:
        auto = policy.autoexec_decision(llm_response.action, ticket.priority,
                                        conf, READ_ONLY_ACTIONS)
        # Customer confirmation in autonomous mode: the owner just said "do it"
        # — respect it and lift confidence to the write threshold.
        if (policy.profile == "autonomous" and not auto
                and _customer_confirmed(ticket)):
            conf = max(conf, policy.autoexec_threshold)
            auto = policy.autoexec_decision(llm_response.action, ticket.priority,
                                            conf, READ_ONLY_ACTIONS)
        approval = (not auto) and conf >= 0.80 and policy.approval_enabled(
            llm_response.action, ticket.priority)

    if auto:
        logger.info(f"Ticket {ticket_id}: auto-executing (action={llm_response.action}, conf={conf})")
        add_note(ticket, "auto_execute", f"Auto-executing {llm_response.action} on {llm_response.target or '(no target)'}")
        job_path = write_job_file(ticket, llm_response)
        ticket.job_file_path = job_path
        ticket.status = "in_progress"
        db.commit()
        log_event(db, "job_created", "system", {
            "ticket_id": ticket_id, "action": llm_response.action,
            "target": llm_response.target, "job_file": job_path,
            "auto_executed": True,
        }, ticket_id)
    elif approval:
        logger.info(f"Ticket {ticket_id}: held for approval (action={llm_response.action}, conf={llm_response.confidence})")
        add_note(ticket, "awaiting_approval", f"Held for approval: {llm_response.reason}")
        ticket.status = "escalated"
        ticket.resolution = f"Needs review: {llm_response.reason} (action: {llm_response.action}, target: {llm_response.target})"
        job_path = None
        if llm_response.action != "escalate_human":
            # escalate_human is a logical action — no script to run. Leave the
            # ticket in the approval queue for the operator to review/close.
            # The job file is marked requires_approval: the agent holds it
            # until the human approves (ticket status -> in_progress).
            job_path = write_job_file(ticket, llm_response, requires_approval=True)
            ticket.job_file_path = job_path
        db.commit()
        log_event(db, "job_created", "system", {
            "ticket_id": ticket_id, "action": llm_response.action,
            "target": llm_response.target, "job_file": job_path,
            "auto_executed": False,
        }, ticket_id)
    else:
        logger.info(f"Ticket {ticket_id}: escalated (conf={llm_response.confidence})")
        reason = (llm_response.reason or "no reasoning provided").strip()
        suggested = llm_response.action + (f" on {llm_response.target}" if llm_response.target else "")
        if policy.profile == "autonomous":
            # Autonomous: never escalate — ask the customer to confirm/adjust
            add_note(ticket, "customer_input",
                     f"{_assistant_name()}: I'm not confident enough to run this automatically "
                     f"(conf {llm_response.confidence:.2f}). {reason} — reply here with any "
                     f"details and I'll retry."
                     + _review_context(db, llm_response.action))
            ticket.status = "customer_action"
            ticket.resolution = _customer_reply(reason)
            ticket.assigned_to = "customer"
            db.commit()
            _notify_customer_action(db, ticket)
            log_event(db, "customer_action", "ai-tech", {
                "ticket_id": ticket_id, "reason": reason,
                "suggested": suggested,
            }, ticket_id)
            return
        add_note(ticket, "escalated",
            f"Confidence {llm_response.confidence:.2f} is below the 0.80 gate — escalated "
            f"for human review. What this means: the {_assistant_name()}'s self-assessed certainty that its "
            f"suggested action is correct (<0.80 = escalate, 0.80–0.95 = approval queue, "
            f"≥0.95 = auto-run). AI reasoning: {reason}. Suggested action: {suggested}. "
            f"Technician: verify the situation manually, then act on it or close the ticket.")
        ticket.status = "escalated"
        ticket.resolution = (f"Low confidence ({llm_response.confidence:.2f}). "
                             f"Reason: {reason}. Suggested: {suggested}. Verify manually.")
        db.commit()
        log_event(db, "escalation", "system", {
            "ticket_id": ticket_id,
            "confidence": llm_response.confidence,
            "reason": reason,
            "suggested": suggested,
        }, ticket_id)


def _count_user_notes(ticket) -> int:
    """Count customer/user comments (user_message events) on a ticket."""
    import json as _json
    notes = []
    if ticket.work_notes:
        try:
            notes = _json.loads(ticket.work_notes)
        except (json.JSONDecodeError, TypeError):
            notes = []
    return sum(1 for n in notes if isinstance(n, dict) and n.get("event") == "user_message")


def _notes_list(ticket) -> list:
    """Parse a ticket's work_notes JSON into a list of dicts."""
    try:
        return json.loads(ticket.work_notes) if ticket.work_notes else []
    except (json.JSONDecodeError, TypeError):
        return []


def _last_note_event(ticket) -> str:
    """Event name of the most recent work note ("" if none)."""
    notes = _notes_list(ticket)
    return (notes[-1].get("event") or "") if notes else ""


# ── ticket close-directive (autonomous tickets honor a customer's "close") ──
# The re-dispatch trigger: a completed ticket (last work event agent_completed)
# that gets a new customer reply re-enters process_ticket via the poll loop's
# re_process set; in autonomous mode _dispatch_pi re-spawned a fresh session
# that re-investigated from scratch (TKT-20260818-5615 — the customer asked
# twice to close). These helpers detect close/ack intent on the latest reply
# and handle it inline so no session is ever dispatched for a close/ack.

# Work events that mark where the technician actually is. Chatter notes
# (user_message, ai_tech_feedback, agent_progress, agent_retry) are skipped so
# the latest agent_completed stays visible even after the confirm-ask note.
_TERMINAL_WORK_EVENTS = (
    "processing", "auto_execute", "agent_completed", "agent_failed",
    "escalated", "closed", "customer_input", "awaiting_approval",
)


def _latest_user_message(ticket) -> "dict | None":
    """The most recent user_message note (the customer's latest reply)."""
    for n in reversed(_notes_list(ticket)):
        if n.get("event") == "user_message":
            return n
    return None


def _work_state(ticket) -> str:
    """The latest work event (skipping chatter), or "" when never worked."""
    for n in reversed(_notes_list(ticket)):
        if n.get("event") in _TERMINAL_WORK_EVENTS:
            return n.get("event") or ""
    return ""


def _close_actor(db, ticket) -> tuple:
    """(actor_username, actor_user) for the customer's latest reply."""
    note = _latest_user_message(ticket)
    username = (note.get("actor") or "").strip() if note else ""
    user = db.query(User).filter(User.username == username).first() if username else None
    return username, user


def _requester_name(db, ticket) -> str:
    sub = db.query(User).filter(User.id == ticket.submitter_id).first() if ticket.submitter_id else None
    return (sub.username or "the requester") if sub else "the requester"


def _can_close(ticket, user) -> bool:
    """Requester or the technician tier/admin may close. A non-requester
    (e.g. another customer or a readonly user) gets routed to
    'waiting on <requester> to verify'."""
    if user is None:
        return False
    if is_tech(user):
        return True
    return ticket.submitter_id == user.id


def _close_ticket_inline(db, ticket, username, user) -> None:
    """Close a completed ticket inline (no pi session) at the customer's
    explicit request. Mirrors the API PATCH close + Juniper's close."""
    if not _can_close(ticket, user):
        req = _requester_name(db, ticket)
        add_note(ticket, "customer_input",
                 f"Waiting on {req} to verify before this ticket can be closed.")
        ticket.status = "customer_action"
        ticket.resolution = f"Waiting on {req} to verify"
        ticket.assigned_to = "customer"
        db.commit()
        log_event(db, "customer_action", "system", {
            "ticket_id": ticket.ticket_id,
            "reason": f"non-requester ({username or 'unknown'}) asked to close; "
                      f"waiting on {req}",
        }, ticket.ticket_id)
        return

    who = username or "customer"
    ticket.status = "closed"
    ticket.resolved_at = datetime.datetime.utcnow()
    ticket.assigned_to = who  # record who closed it (mirrors the API PATCH)
    ticket.resolution = "Closed at the customer's request"
    add_note(ticket, "closed", f"Closed by {who} — customer confirmed.", actor=who)
    db.commit()
    log_event(db, "ticket_closed", "system", {
        "ticket_id": ticket.ticket_id, "by": who, "via": "close-directive",
    }, ticket.ticket_id)


def _ack_ticket_inline(db, ticket) -> None:
    """A thanks/ack on a completed ticket: a short friendly note, no session."""
    add_note(ticket, "ai_tech_feedback",
             f"You're welcome! Glad it's sorted. Just say 'close' whenever you'd "
             f"like me to close this ticket.")
    ticket.status = "customer_action"
    ticket.assigned_to = "customer"
    db.commit()


def _handle_close_intent(db, ticket) -> bool:
    """Detect + handle a close/ack reply inline. Returns True when the message
    was consumed (the caller must stop processing — no dispatch)."""
    note = _latest_user_message(ticket)
    if not note:
        return False
    text = (note.get("detail") or "").strip()
    if not text:
        return False

    close = juniper.close_intent(text)
    ack = juniper.ack_intent(text)
    state = _work_state(ticket)

    if state == "agent_completed":
        if close or ack:
            username, user = _close_actor(db, ticket)
            if close:
                _close_ticket_inline(db, ticket, username, user)
            else:
                _ack_ticket_inline(db, ticket)
            return True
        # Non-close follow-up on a completed ticket (a NEW request) dispatches
        # normally.
        return False

    # Mid-work ticket + close: don't close (the work isn't done) and don't
    # re-dispatch — a polite note instead.
    if close:
        add_note(ticket, "ai_tech_feedback",
                 f"Noted — this ticket is still open, so I'll close it once the "
                 f"work is done. You can also close it anytime from the ticket page.")
        db.commit()
        return True

    return False


def _count_retry_notes(ticket) -> int:
    """How many llm_retry attempts have already happened on this ticket."""
    return sum(1 for n in _notes_list(ticket) if n.get("event") == "agent_retry")


def _retry_config() -> tuple:
    """(interval_min, max_attempts) — hot-read from .env, defaults (2, 10)."""
    from llm_providers import read_env_file
    env = read_env_file()

    def _int(key: str, default: int) -> int:
        try:
            return int((env.get(key) or os.getenv(key) or "").strip() or default)
        except ValueError:
            return default

    return (_int("LLM_RETRY_INTERVAL_MIN", 2), _int("LLM_RETRY_MAX_ATTEMPTS", 10))


def _attempt_config() -> tuple:
    """(budget, cooldown_s) for the failure-feedback loop: how many times the AI
    may re-attempt a failed job (with the error fed back) before it escalates,
    and the minimum gap between attempts. Env: LLM_ATTEMPT_BUDGET (3),
    LLM_ATTEMPT_COOLDOWN_S (60)."""
    from llm_providers import read_env_file
    env = read_env_file()

    def _int(key: str, default: int) -> int:
        try:
            return int((env.get(key) or os.getenv(key) or "").strip() or default)
        except ValueError:
            return default

    return (_int("LLM_ATTEMPT_BUDGET", 3), _int("LLM_ATTEMPT_COOLDOWN_S", 60))


# Actions whose customer-facing review messages should list the adopted gear
# (so the homeowner can see the devices under discussion, not just prose).
_UNIFI_REVIEW_ACTIONS = {
    "unifi_port_config", "unifi_ports", "unifi_restart", "unifi_port_bounce",
    "unifi_port_rename", "batch", "unifi_devices", "network_info",
    "unifi_network_create",
}


def _adopted_unifi_summary(db) -> str:
    """Short list of adopted UniFi gear for customer-facing review messages."""
    devs = (
        db.query(Device)
        .filter(Device.claimed.is_(True), Device.unifi_managed.is_(True))
        .order_by(Device.device_type, Device.name)
        .all()
    )
    if not devs:
        return ""
    lines = ["Here's what I found on your network:"]
    for d in devs:
        meta = " · ".join(x for x in (d.device_type, d.ip_address) if x)
        lines.append(f"  • {d.name or d.ip_address} ({meta})")
    return "\n" + "\n".join(lines)


def _review_context(db, action) -> str:
    """Extra context appended to autonomous-mode customer review messages."""
    if action in _UNIFI_REVIEW_ACTIONS:
        return _adopted_unifi_summary(db)
    return ""


def _notify_customer_action(db, ticket):
    """Email the submitter (or alert recipients) that their input is needed.
    Delegates to the shared emailer helper; best-effort, never raises."""
    try:
        from emailer import notify_customer_action as _n
        _n(db, ticket)
    except Exception:
        pass


_CONFIRM_RE = re.compile(
    r"\b(proceed|go ahead|go|confirmed|confirm|yes|do it|please do|that'?s fine|ok[.,]? do|retry)\b", re.I)


def _customer_confirmed(ticket) -> bool:
    """The most recent customer message is an explicit go-ahead. Used in
    autonomous mode: a customer confirming an action lifts the confidence to
    the threshold (the boss said do it)."""
    for n in reversed(_notes_list(ticket)):
        if n.get("event") == "user_message":
            return bool(_CONFIRM_RE.search(n.get("detail") or ""))
        if n.get("event") in ("agent_completed", "agent_failed", "customer_input", "auto_execute"):
            break  # only the most recent customer message counts
    return False


def _count_failed_notes(ticket) -> int:
    """How many agent_failed notes this ticket has (attempts consumed)."""
    return sum(1 for n in _notes_list(ticket) if n.get("event") == "agent_failed")


def _failure_context(ticket) -> str:
    """Build a 'what went wrong' block from the last agent_failed note(s), fed
    back into the next LLM call so the AI can adjust its plan (technician loop)."""
    parts = []
    for n in _notes_list(ticket):
        if n.get("event") == "agent_failed":
            parts.append(n.get("detail") or "")
    if not parts:
        return ""
    last = parts[-1]
    return (f"Your previous attempt FAILED. The agent reported:\n{last[:600]}\n"
            f"Adjust your plan: correct the action/target/params, try a different "
            f"approach, or use escalate_human / request_customer_input only if you are "
            f"genuinely stuck.")


def _device_inventory_context(db) -> str:
    """Give the AI the managed-device list so it picks real targets. Includes
    every claimed device with its control channel(s): unifi (controller API),
    ssh (stored control key), cert (step-ca identity). Monitoring-only devices
    are listed too but marked. Empty string when nothing managed."""
    devs = (db.query(Device)
            .filter(Device.claimed.is_(True))
            .order_by(Device.device_type, Device.name)
            .all())
    if not devs:
        return ""
    lines = ["Managed devices (use these EXACT names/IPs as targets):"]
    for d in devs:
        ch = []
        if d.unifi_managed:
            ch.append("unifi")
        if d.ssh_key_fingerprint:
            ch.append("ssh")
        if d.adoption_status == "linked":
            ch.append("cert")
        suffix = f" [{','.join(ch)}]" if ch else " (monitoring only)"
        hn = f" ({d.hostname})" if d.hostname else ""
        lines.append(f"  • {d.name}{hn} — {d.device_type}, {d.ip_address}{suffix}")
    return "\n".join(lines)


def _recent_user_context(ticket, limit: int = 4) -> str:
    """The customer's latest comments — a real technician reads the whole
    thread, so the latest instructions must steer the next decision (e.g. a
    follow-up 'also do the Office AP' after the original request)."""
    msgs = [n.get("detail") or "" for n in _notes_list(ticket)
            if n.get("event") == "user_message"]
    msgs = msgs[-limit:]
    if not msgs:
        return ""
    lines = ["The customer recently said (follow these, they are the latest instructions):"]
    for m in msgs:
        lines.append(f"  • {m[:200]}")
    return "\n".join(lines)


def _pi_enabled() -> bool:
    """Run open-ended tickets through the local Pi Coding Agent instead of the
    caged pipeline (autonomous mode). Env: PI_AGENT_ENABLED (default off)."""
    from llm_providers import read_env_file
    val = (read_env_file().get("PI_AGENT_ENABLED") or os.getenv("PI_AGENT_ENABLED") or "false").strip().lower()
    return val in ("1", "true", "yes", "on")


def _prior_agent_context(ticket, limit: int = 3) -> str:
    """The AI's own prior work on this ticket (last completed/failed runs and the
    recorded resolution). A follow-up comment must see the backstory — 'how about
    now?' means nothing without the earlier answer."""
    parts = []
    for n in reversed(_notes_list(ticket)):
        if n.get("event") in ("agent_completed", "agent_failed", "customer_input"):
            parts.append(f"{n.get('event')}: {str(n.get('detail') or '')[:400]}")
            if len(parts) >= limit:
                break
    if ticket.resolution:
        parts.append(f"recorded resolution: {str(ticket.resolution)[:600]}")
    if not parts:
        return ""
    return ("Prior work on this ticket (you already did this — build on it, don't redo it):\n"
            + "\n".join(parts))


def _bound_device_context(device) -> str:
    """One-line context naming the ticket's bound target device (Part A).
    Used in both the LLM/judge device_context and the pi sysctx so the agent
    acts on the right box. Agent-managed devices are flagged (the update
    capability lives on the agent channel)."""
    agent = " (agent-managed)" if (
        getattr(device, "adoption_method", None) == "agent"
        or getattr(device, "agent_version", None)
    ) else ""
    hostname = f" ({device.hostname})" if getattr(device, "hostname", None) else ""
    return (f"Device: {device.name}{hostname} ({device.ip_address}), "
            f"type: {device.device_type}, status: {device.status}{agent}")


def _pi_task_context(db, ticket, ticket_text) -> str:
    """Context for the Pi Coding Agent: operations guide + ticket thread + the
    managed-device inventory. The guide is first so truncation never drops it."""
    parts = []
    parts.append(
        "How to operate devices:\n"
        "- A target matching a managed device's name/IP in the inventory below IS "
        "manageable — cross-check that list before ever concluding a host is unmanaged.\n"
        "- To SSH to a managed device, fetch its DECRYPTED credentials with the agent "
        "token: GET /api/v1/devices/{id}/credentials (list /api/v1/devices to find ids). "
        "Never try to read or decrypt key files under /opt/barenoc — they are "
        "Fernet-encrypted at rest; the API decrypts server-side. This is the intended path.\n"
        "- In sshd logs, 'Accepted publickey for X from <IP>' means the connection "
        "ORIGINATED from <IP> (the SSH client), not that it connected to <IP>.\n"
        "- OS flavors: Debian/Ubuntu=apt, Fedora/RHEL/Rocky=dnf, Alpine=apk, "
        "openSUSE=zypper, macOS=no journald (use log show). Pick the right tool for the "
        "device's OS; if no catalog action fits, say so honestly and offer alternatives — "
        "never report a fabricated blocker."
    )
    if ticket.target_device_id:
        device = db.query(Device).filter(Device.id == ticket.target_device_id).first()
        if device:
            parts.append(
                "Target device for this ticket: " + _bound_device_context(device)
                + ". Act on THIS device."
            )
    user_ctx = _recent_user_context(ticket)
    if user_ctx:
        parts.append(user_ctx)
    prior = _prior_agent_context(ticket)
    if prior:
        parts.append(prior)
    inv = _device_inventory_context(db)
    if inv:
        parts.append(inv)
    parts.append("Ticket: " + ticket_text[:3000])
    return "\n\n".join(parts)


# The auto_execute note _dispatch_pi writes. Its presence (with no terminal
# result after it) marks an active pi session; the re-dispatch guard keys off it.
_PI_DISPATCH_DETAIL = "Dispatched to Lily (autonomous pi session)"


def _is_pi_dispatch(note: dict) -> bool:
    """Is this work note the pi-dispatch marker? Accepts the current text and
    the pre-2026-08-17 text ('Dispatched to Lily (autonomous)') so tickets
    dispatched before this change deploys are still guarded."""
    if note.get("event") != "auto_execute":
        return False
    detail = (note.get("detail") or "").strip().lower()
    return detail in ("dispatched to lily (autonomous pi session)",
                      "dispatched to lily (autonomous)")


def _pi_run_active(ticket) -> bool:
    """True when a pi session for this ticket is already queued or running.
    Two signals, either suffices:
      1. an incoming/ or running/ job file with action == pi_task, or
      2. a pi-dispatch note with no terminal result (agent_completed /
         agent_failed / escalated) after it.
    The 60 s same-run dedup is not enough — a new user reply re-enters
    process_ticket and would otherwise re-dispatch a parallel session
    (08-13 re-queue lesson + the 08-17 double-session incident)."""
    for d in (JOBS_INCOMING, JOBS_RUNNING):
        path = os.path.join(d, f"{ticket.ticket_id}.json")
        try:
            with open(path) as f:
                if json.load(f).get("action") == "pi_task":
                    return True
        except (OSError, json.JSONDecodeError):
            continue
    notes = _notes_list(ticket)
    dispatch_idx = None
    for i, n in enumerate(notes):
        if _is_pi_dispatch(n):
            dispatch_idx = i
    if dispatch_idx is None:
        return False
    for n in notes[dispatch_idx + 1:]:
        if n.get("event") in ("agent_completed", "agent_failed", "escalated"):
            return False
    return True


def _dispatch_pi(db, ticket, ticket_text) -> bool:
    """Dispatch the ticket to the Pi Coding Agent (autonomous, no gates): write
    a pi_task job the agent runner executes headlessly. Returns True if handled.

    Re-dispatch guard: a ticket may have at most ONE active pi session. A new
    user reply during an active run must not spawn a second session (the 08-17
    incident) — post a short note and skip. The watchdog path is untouched, so
    a genuinely-stuck run still escalates.
    """
    if _pi_run_active(ticket):
        logger.info(f"Ticket {ticket.ticket_id}: pi session already active — not re-dispatching")
        add_note(ticket, "agent_progress",
                 f"{_assistant_name()} is already working on this — I'll update you when it finishes.",
                 actor=_assistant_name())
        db.commit()
        return True
    context = _pi_task_context(db, ticket, ticket_text)
    job = {
        "ticket_id": ticket.ticket_id,
        "action": "pi_task",
        "target": "",
        "params": {"task": ticket_text[:8000], "context": context[:6000]},
        "reason": "Autonomous Pi Coding Agent dispatch",
        "confidence": 1.0,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "source": "barenoc-worker",
    }
    filepath = os.path.join(JOBS_INCOMING, f"{ticket.ticket_id}.json")
    with open(filepath, "w") as f:
        json.dump(job, f, indent=2)
    ticket.action = "pi_task"
    ticket.status = "in_progress"
    ticket.assigned_to = "pi-agent"
    add_note(ticket, "auto_execute", _PI_DISPATCH_DETAIL)
    db.commit()
    log_event(db, "job_created", "system", {
        "ticket_id": ticket.ticket_id, "action": "pi_task",
        "job_file": filepath, "auto_executed": True,
    }, ticket.ticket_id)
    logger.info(f"Ticket {ticket.ticket_id}: dispatched to Pi Coding Agent")
    return True


def _handle_llm_failure(db, ticket, reason: str):
    """LLM unreachable — retry on a schedule instead of failing instantly.

    The ticket stays in_progress with an llm_retry note; the poll loop re-runs
    process_ticket once LLM_RETRY_INTERVAL_MIN has elapsed (updated_at is
    stamped by add_note). After LLM_RETRY_MAX_ATTEMPTS it escalates to a human.
    """
    _mark_llm_outage(db, reason)
    interval_min, max_attempts = _retry_config()
    attempts = _count_retry_notes(ticket) + 1
    ticket_id = ticket.ticket_id

    if attempts >= max_attempts:
        logger.error(f"Ticket {ticket_id}: model service unreachable after {attempts} attempts — {reason}")
        add_note(ticket, "escalated",
                 f"The model service was unreachable after {attempts} attempts (~{interval_min * attempts} min) — {reason}")
        ticket.status = "escalated"
        ticket.resolution = f"Escalated: model service unreachable ({reason})"
        ticket.assigned_to = "human-tech"
        db.commit()
        log_event(db, "llm_failed", "system", {
            "ticket_id": ticket_id, "error": reason, "attempts": attempts,
        }, ticket_id)
        return

    logger.error(f"Ticket {ticket_id}: model call failed (attempt {attempts}/{max_attempts}); "
                 f"retrying in ~{interval_min} min — {reason}")
    add_note(ticket, "agent_retry",
             f"Model service call failed (attempt {attempts}/{max_attempts}); will retry in ~{interval_min} min — {reason}")
    ticket.status = "in_progress"
    ticket.resolution = None
    db.commit()
    log_event(db, "llm_failed", "system", {
        "ticket_id": ticket_id, "error": reason, "attempts": attempts,
        "retry_in_min": interval_min, "max_attempts": max_attempts,
    }, ticket_id)


OUTAGE_TITLE = "LLM provider outage — all providers unreachable"


def _mark_llm_outage(db, reason: str) -> None:
    """The whole LLM chain is down: open (once) a P1 ticket + alert email.
    Dedupes: an existing open/in_progress outage ticket is left untouched."""
    global _LLM_OUTAGE
    if _LLM_OUTAGE:
        return
    _LLM_OUTAGE = True

    existing = db.query(Ticket).filter(
        Ticket.title == OUTAGE_TITLE,
        Ticket.status.in_(("open", "in_progress")),
    ).first()
    if existing:
        return

    ticket_id = generate_ticket_id()
    t = Ticket(
        ticket_id=ticket_id,
        title=OUTAGE_TITLE,
        description=(f"The LLM provider chain (primary → secondary → tertiary) is "
                     f"unusable: {reason}"),
        priority="P1",
        status="open",
        source="auto",
        assigned_to="system",
    )
    db.add(t)
    db.commit()
    log_event(db, "llm_outage", "system", {
        "ticket_id": ticket_id, "reason": reason,
    }, ticket_id)
    logger.error(f"LLM outage: opened {ticket_id} (P1) — {reason}")
    _alert_llm_outage(t)


def _clear_llm_outage(db, provider_model: str) -> None:
    """A provider answered — close the outage ticket if one is open."""
    global _LLM_OUTAGE
    if not _LLM_OUTAGE:
        return
    _LLM_OUTAGE = False
    t = db.query(Ticket).filter(
        Ticket.title == OUTAGE_TITLE,
        Ticket.status.in_(("open", "in_progress")),
    ).first()
    if not t:
        return
    add_note(t, "completed",
             f"Provider chain recovered ({provider_model}) — closing outage ticket")
    t.status = "closed"
    t.resolution = f"Provider chain recovered ({provider_model})"
    t.resolved_at = datetime.datetime.utcnow()
    db.commit()
    log_event(db, "llm_recovered", "system", {
        "ticket_id": t.ticket_id, "provider": provider_model,
    }, t.ticket_id)
    logger.info(f"LLM recovered ({provider_model}) — closed outage ticket {t.ticket_id}")


def _alert_llm_outage(t) -> None:
    """Email the alert recipients about the LLM outage (mirrors the API's
    P1 ticket alert so a worker-created ticket still pings people)."""
    import threading as _threading

    def _send():
        try:
            from emailer import send_email, get_recipients, alert_html
            recipients = get_recipients("alerts")
            if not recipients:
                logger.warning("LLM outage: no alert recipients configured")
                return
            rows = [
                ("Ticket", f"{t.ticket_id} <b>[{t.priority}]</b>"),
                ("Title", html.escape(t.title or "")),
                ("Priority", "<b style='color:#e03131'>P1</b>"),
                ("Description", html.escape((t.description or "")[:500])),
                ("Status", "open"),
            ]
            ok, err = send_email(
                recipients,
                f"[P1] BareNOC: {t.title}",
                body_html=alert_html("LLM provider outage", rows),
                body_text=f"P1: {t.title} ({t.ticket_id})",
            )
            if not ok and err:
                logger.warning(f"LLM outage alert email failed: {err}")
        except Exception as e:
            logger.exception(f"LLM outage alert thread error: {e}")

    _threading.Thread(target=_send, daemon=True).start()


def _escalate_stuck_jobs(db) -> int:
    """Watchdog: an auto-executed job that never produced a result (runner
    down / result POST lost) leaves the ticket silently in_progress forever.
    Escalate tickets whose auto-executed job has no terminal note and whose
    last update is >10 min old. Independent of the pi re-dispatch guard — a
    genuinely-stuck run is escalated, and the guard's 'already working' note
    does not block it. Returns how many tickets were escalated."""
    stuck_cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=10)
    stuck = (
        db.query(Ticket)
        .filter(Ticket.status == "in_progress",
                Ticket.updated_at <= stuck_cutoff)
        .limit(10)
        .all()
    )
    escalated = 0
    for t in stuck:
        if is_paused(t):
            logger.info(f"Ticket {t.ticket_id}: paused — watchdog exempt")
            continue
        evs = [str(n.get("event") or "") for n in _notes_list(t)]
        if ("auto_execute" in evs
                and "agent_completed" not in evs
                and "agent_failed" not in evs
                and "escalated" not in evs):
            logger.warning(
                f"Ticket {t.ticket_id}: auto-executed job never reported back "
                f"— escalating (runner may be down)")
            add_note(t, "escalated",
                     "The dispatched job never reported back — the agent runner "
                     "may be down. Routed to human review.")
            t.status = "escalated"
            t.assigned_to = "human-tech"
            escalated += 1
    if escalated:
        db.commit()
    return escalated


# ticket_id -> last seen user-comment count (drives re-processing on replies)
_USER_COMMENT_COUNT = {}


def poll_for_tickets():
    """Main loop: poll for open tickets and process them.

    Processes:
      * new open tickets (status == open, not auto-generated)
      * tickets the AI assistant is waiting on / working on (in_progress,
        awaiting_approval) that have NEW customer comments — this closes the
        feedback loop (AI asks, customer replies, AI re-engages).
    """
    logger.info("Worker started, polling for tickets...")

    # Ensure job directories exist
    os.makedirs(JOBS_INCOMING, exist_ok=True)
    os.makedirs(JOBS_RUNNING, exist_ok=True)
    os.makedirs(JOBS_COMPLETED, exist_ok=True)

    while True:
        try:
            db = SessionLocal()

            # Refresh managed devices periodically
            load_managed_devices(db)

            # Juniper (Queue Manager) — answer unread bot messages on the same
            # cadence as ticket polling. Reads state, creates new tickets, and
            # writes directive notes only; never mutates in-flight work.
            try:
                juniper.respond_once(db)
            except Exception as e:
                logger.exception(f"Juniper responder error: {e}")

            # Fresh tickets waiting for the AI assistant. Fetch a wider window
            # then drop paused tickets BEFORE capping, so a handful of held
            # tickets can't starve the rest of the queue.
            tickets = (
                db.query(Ticket)
                .filter(
                    Ticket.status == "open",
                    Ticket.source != "auto",  # Let scheduler handle auto tickets
                )
                .order_by(Ticket.priority.asc())
                .limit(20)
                .all()
            )
            tickets = [t for t in tickets if not is_paused(t)][:5]

            # Tickets with new customer replies (feedback loop)
            re_process = (
                db.query(Ticket)
                .filter(
                    Ticket.status.in_(("in_progress", "escalated", "customer_action")),
                    Ticket.source != "auto",
                )
                .all()
            )
            for ticket in re_process:
                if is_paused(ticket):
                    logger.info(f"Ticket {ticket.ticket_id}: paused — skipping reprocess")
                    continue
                count = _count_user_notes(ticket)
                seen = _USER_COMMENT_COUNT.get(ticket.ticket_id, -1)
                if seen == -1:
                    _USER_COMMENT_COUNT[ticket.ticket_id] = count
                    if _last_note_event(ticket) == "user_message":
                        # A comment arrived while we were restarting — the
                        # baseline seed must not swallow it.
                        logger.info(f"Ticket {ticket.ticket_id}: fresh comment after restart — re-processing")
                        tickets.append(ticket)
                    continue  # seed baseline on first sight — no reprocess
                if count > seen:
                    _USER_COMMENT_COUNT[ticket.ticket_id] = count
                    logger.info(f"Ticket {ticket.ticket_id}: new customer reply — re-processing")
                    tickets.append(ticket)

            # LLM-call retries: in_progress tickets whose last note is an
            # llm_retry and whose retry window (LLM_RETRY_INTERVAL_MIN) elapsed.
            # updated_at is stamped by add_note when the retry was scheduled.
            interval_min, _ = _retry_config()
            retry_cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=interval_min)
            due_retries = (
                db.query(Ticket)
                .filter(Ticket.status == "in_progress",
                        Ticket.updated_at <= retry_cutoff)
                .limit(10)
                .all()
            )
            for ticket in due_retries:
                if is_paused(ticket):
                    logger.info(f"Ticket {ticket.ticket_id}: paused — skipping LLM retry")
                    continue
                if _last_note_event(ticket) == "agent_retry":
                    logger.info(f"Ticket {ticket.ticket_id}: LLM retry due — re-processing")
                    tickets.append(ticket)

            # Failure-feedback loop: a failed agent run re-engages the AI with
            # the error (budget-bounded) so it can correct and retry instead of
            # leaving the ticket escalated. Cooldown prevents hot-looping.
            budget, cooldown_s = _attempt_config()
            fail_cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=cooldown_s)
            due_failures = (
                db.query(Ticket)
                .filter(Ticket.status == "escalated",
                        Ticket.updated_at <= fail_cutoff)
                .limit(10)
                .all()
            )
            for ticket in due_failures:
                if is_paused(ticket):
                    logger.info(f"Ticket {ticket.ticket_id}: paused — skipping failure feedback")
                    continue
                # agent_failed present, not superseded by a successful run,
                # and still within the attempt budget (jobs.py appends an
                # ai_tech_feedback note after agent_failed, so check presence).
                if (_count_failed_notes(ticket) > 0
                        and _last_note_event(ticket) != "agent_completed"
                        and _count_failed_notes(ticket) < budget):
                    logger.info(f"Ticket {ticket.ticket_id}: failed run {_count_failed_notes(ticket)}/{budget} — feeding error back to the AI")
                    tickets.append(ticket)

            # Stuck-job watchdog: escalate auto-executed jobs that never
            # reported back (runner down / result POST lost). Independent of
            # the pi re-dispatch guard — a genuinely-stuck run still escalates.
            _escalate_stuck_jobs(db)

            # The same ticket can surface from several paths in one poll (e.g.
            # reopened to 'open' AND carrying a new user comment): process it
            # ONCE, or the agent gets dispatched twice for the same follow-up.
            seen_ids = set()
            unique = []
            for ticket in tickets:
                if ticket.ticket_id not in seen_ids:
                    seen_ids.add(ticket.ticket_id)
                    unique.append(ticket)
            tickets = unique

            for ticket in tickets:
                try:
                    process_ticket(db, ticket)
                except Exception as e:
                    logger.exception(f"Error processing ticket {ticket.ticket_id}: {e}")
                    ticket.status = "escalated"
                    ticket.resolution = f"Worker error: {str(e)}"
                    ticket.updated_at = datetime.datetime.utcnow()
                    db.commit()

            db.close()

        except Exception as e:
            logger.exception(f"Polling error: {e}")

        time.sleep(POLL_INTERVAL)


def main():
    logger.info("BareNOC Worker starting...")
    init_db()
    poll_for_tickets()


if __name__ == "__main__":
    main()
