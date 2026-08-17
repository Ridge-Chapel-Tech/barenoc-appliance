import os
import json
import logging
import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Ticket, User
from auth import require_any_role, require_role
from schemas import TicketResponse
from worknotes import add_note
from tone_filter import strip_meta_narration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


# ── readable info answers (agent read-only actions) ──────────────────────────
# These turn the agent's raw UniFi/API output into a human answer posted into
# the ticket thread (mirrors the network_info flow). Return None for actions
# that are NOT info answers (handled generically).

def _assistant_name() -> str:
    """Hot-read the configured AI assistant name (Settings, BOT_ASSISTANT_NAME)."""
    from llm_providers import read_env_file
    name = (read_env_file().get("BOT_ASSISTANT_NAME") or os.getenv("BOT_ASSISTANT_NAME") or "").strip()
    return name or "Lily"


def _result_detail_text(result) -> str:
    """Human-readable text for a successful job's output (raw_output → pi
    reports / script stdout; JSON dicts get pretty-printed)."""
    out = getattr(result, "output", None) or {}
    if isinstance(out, dict):
        raw = out.get("raw_output")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:800]
        try:
            return json.dumps(out, indent=2, default=str)[:800]
        except Exception:
            pass
    return str(out)[:800]



def _format_info_answer(action: str, out: dict) -> "str | None":
    lines = []
    if action == "network_info":
        for n in (out.get("networks") or []):
            vlan = f"vlan {n['vlan']}" if n.get("vlan") else "no vlan tag"
            dhcp = (f"dhcp {n['dhcp_start']}–{n['dhcp_stop']}" if n.get("dhcp")
                    else "static only")
            state = "in use" if n.get("enabled", True) else "disabled"
            lines.append(f"  • {n.get('name','?')} — {n.get('subnet','')} ({vlan}, {dhcp}, {state})")
        for w in (out.get("wlans") or []):
            lines.append(f"  wifi: {w.get('ssid','?')} — {'enabled' if w.get('enabled') else 'disabled'}, "
                         f"{w.get('security') or 'open'}")
    elif action == "unifi_clients":
        total, online = out.get("total", 0), out.get("online", 0)
        lines.append(f"{online} of {total} known clients online now:")
        for c in (out.get("clients") or []):
            if not c.get("online"):
                continue
            mode = "wired" if c.get("wired") else "wireless"
            vendor = f" ({c['vendor']})" if c.get("vendor") else ""
            lines.append(f"  • {c.get('name','?')} — {c.get('ip','no ip')}{vendor} [{mode}]")
        offline = total - online
        if offline > 0:
            lines.append(f"  … {offline} offline (not listed)")
    elif action == "unifi_devices":
        total, online = out.get("total", 0), out.get("online", 0)
        lines.append(f"{online} of {total} UniFi devices online:")
        for d in (out.get("devices") or []):
            up = int(d.get("uptime") or 0)
            uptime = f"up {up // 86400}d {(up % 86400) // 3600}h" if up else "n/a"
            lines.append(f"  • {d.get('name','?')} — {d.get('ip','')} ({d.get('type','?')}, "
                         f"{d.get('status','?')}, {d.get('model','')}, {uptime}, "
                         f"fw {d.get('version','?')})")
    elif action == "unifi_ports":
        switch = out.get("switch_mac", "switch")
        lines.append(f"Port table for {switch}:")
        for pt in (out.get("ports") or []):
            state = "UP" if pt.get("up") else "down"
            tagged = ", ".join(pt.get("tagged_names") or [])
            native = pt.get("native_name") or ""
            vlan_desc = native + (f" + tagged [{tagged}]" if tagged else "")
            lines.append(f"  • {pt.get('name','?')} — {state}, {vlan_desc or 'no vlans'}")
    elif action == "unifi_client_port":
        lines.append(f"Client is on {out.get('switch_name') or out.get('switch_mac') or '?'} "
                     f"port {out.get('port_idx', '?')}"
                     + (f" — vlan {out['vlan']}" if out.get("vlan") else "")
                     + (f", network {out['network_name']}" if out.get("network_name") else ""))
    elif action == "unifi_firewall_rules":
        rules = out.get("rules") or []
        lines.append(f"{len(rules)} custom firewall rule(s):")
        for r in rules:
            name = r.get("name") or r.get("_id") or "?"
            action = str(r.get("action", "?")).lower()
            state = "enabled" if r.get("enabled", True) else "DISABLED"
            lines.append(f"  • {name} — {action} ({state})")
    elif action == "unifi_network_create":
        if out.get("created"):
            lines.append(f"Created network {out.get('name','?')} — vlan {out.get('vlan','?')}, "
                         f"subnet {out.get('subnet','192.168.<vlan>.1/24')}.")
            lines.append("  • It appears in the UniFi controller now; adopt/assign devices to it as needed.")
        else:
            lines.append(f"Network creation did not complete: {out.get('error') or 'unknown error'}")
    elif action == "system_time":
        lines.append(f"Appliance clock says {out.get('local') or 'n/a'}.")
        tz = out.get("tz_setting")
        if tz and tz != "unset":
            lines.append(f"  • timezone: set to {tz} (Settings → General)")
        else:
            lines.append("  • timezone: unset — the appliance runs UTC")
        if out.get("utc"):
            lines.append(f"  • UTC: {out['utc']}")
        if out.get("uptime"):
            lines.append(f"  • uptime: {out['uptime']}")
    elif action == "ticket_status":
        tid = out.get("ticket_id") or "?"
        lines.append(f"Ticket {tid}: {out.get('label') or out.get('status') or 'no activity yet'}.")
        idle = out.get("idle_seconds")
        if idle is not None:
            try:
                mins = max(0, int(idle) // 60)
                lines.append(f"  • last activity {'<1m' if mins < 1 else str(mins) + 'm'} ago")
            except (TypeError, ValueError):
                pass
        if out.get("resolution"):
            lines.append(f"  • resolution: {out['resolution']}")
    elif action == "enroll_device":
        if out.get("enrolled"):
            lines.append(f"✅ Adopted {out.get('device','the device')} with a certificate.")
            lines.append("  • step-cli + a short-lived cert were installed; the device renews it and reports "
                         "over mTLS every 10 minutes (it shows as 🔐 cert on the Devices page).")
        else:
            lines.append(f"Enrollment did not complete: {out.get('error') or 'unknown error'}")
    elif action == "apply_patch":
        pm = out.get("package_manager") or "?"
        avail = bool(out.get("updates_available"))
        lines.append(f"Update check on {out.get('target','?')} ({pm}): "
                     + ("updates available" if avail else "up to date"))
        b64 = out.get("updates_b64") or ""
        if b64:
            import base64 as _b
            text = _b.b64decode(b64).decode(errors="replace")
            for ln in text.splitlines()[:25]:
                if ln.strip():
                    lines.append(f"  {ln[:160]}")
    elif action == "batch":
        res = out.get("results") or []
        total = out.get("total", len(res))
        ok_n = out.get("succeeded", 0)
        failed_n = out.get("failed", 0)
        head = f"Batch: {ok_n}/{total} sub-actions succeeded"
        if failed_n:
            head += f", {failed_n} failed"
        lines.append(head + ":")
        for r in res:
            mark = "✓" if r.get("success") else "✗"
            label = f"{r.get('action')} on {r.get('target') or '(no target)'}"
            if r.get("success"):
                lines.append(f"  {mark} {label}")
            else:
                err = r.get("error") or (r.get("output") or {}).get("error") or "failed"
                lines.append(f"  {mark} {label}: {str(err)[:120]}")
    elif action == "unifi_ensure_wireless_uplinks":
        changed = out.get("changed") or []
        if not changed:
            lines.append(out.get("message") or "No changes needed — wireless VLANs already available on all AP uplinks.")
        else:
            lines.append(f"Wireless VLANs ensured on {len(changed)} AP uplink port(s):")
            for c in changed:
                lines.append(f"  ✓ {c.get('ap')} -> {c.get('switch')} port {c.get('port')}: now tagged "
                             f"{c.get('tagged_vlan')} (vlan {c.get('vlan')})")
    elif action == "pi_task":
        response = strip_meta_narration(out.get("response") or "")
        lines.append(response or "Lily finished (no output).")
    elif action == "unifi_set_ssid_password":
        lines.append(f"SSID '{out.get('ssid')}' passphrase updated (security: {out.get('security', 'wpapsk')}).")
    if not lines:
        return None
    return "\n".join(lines)


def _result_error_text(result) -> str:
    """Clean, human-readable error from a failed job result."""
    if getattr(result, "error", None):
        return str(result.error)
    out = getattr(result, "output", None)
    if isinstance(out, dict):
        if out.get("error"):
            return str(out["error"])
        if out.get("raw_output"):
            return str(out["raw_output"])[:400]
    return str(out or "Unknown error")[:400]


class JobResult(BaseModel):
    ticket_id: str
    action: str
    target: Optional[str] = None
    success: bool
    output: Optional[dict] = None
    error: Optional[str] = None


@router.post("/result")
def report_job_result(result: JobResult, db: Session = Depends(get_db),
                      user: User = Depends(require_any_role("operator", "admin", "agent"))):
    """Called by the Pi Agent Runner to report job execution results.
    Requires an authenticated operator+, admin, or the agent service account
    (the runner's identity)."""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == result.ticket_id).first()
    if not ticket:
        # Synthetic jobs (e.g. discovery ping sweeps: disc-<ip>-<ts>) have no
        # Ticket row. The runner already wrote the result file; the
        # discover_add callback (runner-side) handles inventory additions.
        # Don't 404 — it just polluted every discovery run's runner log.
        return {"status": "ok", "ticket_id": result.ticket_id, "no_ticket": True}

    # Dedup guard: the agent may post its own result (legacy behavior) or the
    # runner may retry — if the last note is already an agent_completed posted
    # within the last 60s, ignore the duplicate instead of double-posting.
    try:
        notes = json.loads(ticket.work_notes) if ticket.work_notes else []
    except Exception:
        notes = []
    if result.success and notes and notes[-1].get("event") == "agent_completed":
        last_ts = notes[-1].get("timestamp") or ""
        try:
            posted = datetime.datetime.fromisoformat(str(last_ts).replace("Z", ""))
            if posted.tzinfo is not None:
                posted = posted.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            if (datetime.datetime.utcnow() - posted).total_seconds() <= 60:
                logger.warning(
                    f"Ticket {result.ticket_id}: duplicate agent_completed result ignored "
                    f"(posted {last_ts})")
                return {"status": "ok", "ticket_id": result.ticket_id,
                        "new_status": ticket.status, "deduplicated": True}
        except Exception:
            pass

    info_answer = None

    if result.success:
        # Job succeeded → the AI asks the customer to verify/close out: the
        # ticket moves to CUSTOMER ACTION (waiting on the customer), not
        # auto-closed. The customer confirms in-thread (or a human closes).
        ticket.status = "customer_action"
        ticket.assigned_to = ticket.assigned_to or "customer"
        formatted = _format_info_answer(result.action, result.output or {})
        if formatted is not None:
            # assistant answered an info request — put the answer in the thread
            info_answer = formatted
            add_note(ticket, "agent_completed",
                     f"{info_answer}\n\n"
                     f"Does this answer your question? Reply to confirm, or tell me what's still missing.")
            ticket.resolution = "Answered by " + _assistant_name() + " — awaiting customer confirmation"
        else:
            detail = _result_detail_text(result)
            add_note(ticket, "agent_completed",
                     f"{result.action} on {result.target or '(no target)'} succeeded."
                     + (f"\n{detail}" if detail else ""))
            # assistant: request customer feedback to verify the outcome
            add_note(ticket, "ai_tech_feedback",
                     f"Task completed — reply here to confirm it's fixed, or tell me what's still wrong.")
    else:
        ticket.status = "escalated"
        ticket.assigned_to = "human-tech"
        error_msg = _result_error_text(result)
        add_note(ticket, "agent_failed", f"{result.action} on {result.target or '(no target)'} failed: {error_msg}")
        # assistant: request escalation path — inform the customer, route to human tech
        add_note(ticket, "ai_tech_feedback",
                 f"The action failed and has been routed to a human technician for manual "
                 f"intervention. Error: {error_msg[:300]}")

    # resolution: for info answers use the readable text, else the raw output
    if not info_answer:
        ticket.resolution = _result_error_text(result) if not result.success else str(result.output or "")
    else:
        ticket.resolution = info_answer
    ticket.updated_at = datetime.datetime.utcnow()
    db.commit()
    # A successful agent run moves the ticket to Customer Action ("please
    # confirm this answers your question") — the submitter must be emailed.
    if ticket.status == "customer_action":
        from emailer import notify_customer_action
        notify_customer_action(db, ticket)
    return {"status": "ok", "ticket_id": result.ticket_id, "new_status": ticket.status}
