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
from models import Ticket, Device, User, AuditLog
from schemas import generate_ticket_id, generate_event_id
from sanitizer import sanitize_ticket
from action_validator import AllowedAction, validate_action, validate_target
from audit import log_event
from worknotes import add_note

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
    job = {
        "ticket_id": ticket.ticket_id,
        "action": llm_response.action,
        "target": llm_response.target or "",
        "params": llm_response.params,
        "reason": llm_response.reason,
        "confidence": llm_response.confidence,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "source": "barenoc-worker",
    }
    if requires_approval:
        job["requires_approval"] = True

    filename = f"{ticket.ticket_id}.json"
    filepath = os.path.join(JOBS_INCOMING, filename)

    with open(filepath, "w") as f:
        json.dump(job, f, indent=2)

    logger.info(f"Job file written: {filepath}")
    return filepath


def process_ticket(db, ticket):
    """Process a single ticket through the LLM pipeline."""
    from llm_client import call_llm

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
            device_context = f"Device: {device.name} ({device.ip_address}), type: {device.device_type}, status: {device.status}"

    ticket_text = f"{ticket.title}\n{ticket.description or ''}"

    # Autonomous + Lily mode: route open-ended tickets straight to the
    # local agent, Lily (full tool access, no gates — experimental).
    from policy import get_policy as _get_policy
    if _get_policy().profile == "autonomous" and _pi_enabled():
        _dispatch_pi(db, ticket, ticket_text)
        return

    # Step 4.5: Judge phase (opt-in) — the judge rules on lawfulness and
    # picks the action class; it never executes. Unlawful/ambiguous -> human.
    from policy import get_policy
    policy = get_policy()
    judge_enabled = policy.judge_required or _judge_enabled()
    verdict = None
    if judge_enabled:
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
                ticket.resolution = f"Waiting on customer: {verdict.reason}"
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
    # Failure-feedback loop: if an earlier agent run failed, feed the error back
    # so the AI can correct and retry instead of escalating. Also give it the
    # device inventory + the customer's latest comments (the full thread).
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
    ticket.llm_prompt_tokens = llm_response.prompt_tokens
    ticket.llm_response_tokens = llm_response.response_tokens
    ticket.llm_cost_usd = llm_response.cost_usd

    # Step 7: Log LLM audit
    log_event(db, "llm_request", "system", {
        "ticket_id": ticket_id,
        "model": llm_response.model,
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
            logger.warning(f"Ticket {ticket_id}: {msg}")
            add_note(ticket, "escalated", f"Target validation failed: {msg}")
            ticket.status = "escalated"
            ticket.resolution = f"Escalated: {msg}"
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
            ticket.resolution = f"Waiting on customer: {llm_response.reason}"
            ticket.assigned_to = "customer"
            db.commit()
            _notify_customer_action(db, ticket)
            log_event(db, "customer_action", "ai-tech", {
                "ticket_id": ticket_id,
                "reason": llm_response.reason,
            }, ticket_id)
            return
        add_note(ticket, "escalated",
                 f"{_assistant_name()}: needs human input — {llm_response.reason}")
        ticket.status = "escalated"
        ticket.resolution = f"Human tech required: {llm_response.reason}"
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
        ticket.resolution = f"Waiting on customer: {llm_response.reason}"
        ticket.assigned_to = "customer"
        db.commit()
        _notify_customer_action(db, ticket)
        log_event(db, "customer_action", "ai-tech", {
            "ticket_id": ticket_id,
            "reason": llm_response.reason,
        }, ticket_id)
        return

    READ_ONLY_ACTIONS = {"ping_test", "snmp_poll", "device_status", "network_discovery",
                          "network_info", "unifi_clients", "unifi_devices", "unifi_ports",
                          "unifi_client_port", "unifi_firewall_rules",
                          "fingerprint_device"}
    conf = llm_response.confidence

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
            ticket.resolution = f"Waiting on customer: {reason}"
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
    follow-up 'also do Office Wifi' after the original request)."""
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


def _dispatch_pi(db, ticket, ticket_text) -> bool:
    """Dispatch the ticket to the Pi Coding Agent (autonomous, no gates): write
    a pi_task job the agent runner executes headlessly. Returns True if handled."""
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
    add_note(ticket, "auto_execute", "Dispatched to Lily (autonomous)")
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

            # Fresh tickets waiting for the AI assistant
            tickets = (
                db.query(Ticket)
                .filter(
                    Ticket.status == "open",
                    Ticket.source != "auto",  # Let scheduler handle auto tickets
                )
                .order_by(Ticket.priority.asc())
                .limit(5)
                .all()
            )

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
                # agent_failed present, not superseded by a successful run,
                # and still within the attempt budget (jobs.py appends an
                # ai_tech_feedback note after agent_failed, so check presence).
                if (_count_failed_notes(ticket) > 0
                        and _last_note_event(ticket) != "agent_completed"
                        and _count_failed_notes(ticket) < budget):
                    logger.info(f"Ticket {ticket.ticket_id}: failed run {_count_failed_notes(ticket)}/{budget} — feeding error back to the AI")
                    tickets.append(ticket)

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
