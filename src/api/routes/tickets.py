from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from pydantic import BaseModel, Field
import datetime
import os
from database import get_db
from models import Ticket, Device, User
from schemas import TicketCreate, TicketUpdate, TicketResponse, generate_ticket_id
from auth import get_current_user, get_access_context, require_any_role
from worknotes import add_note
from queue_status import derive_status

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


def _assistant_name() -> str:
    """The configured AI assistant display name (Settings, BOT_ASSISTANT_NAME)."""
    try:
        with open("/opt/barenoc/.env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BOT_ASSISTANT_NAME="):
                    return line.partition("=")[2].strip() or "Lily"
    except Exception:
        pass
    return os.getenv("BOT_ASSISTANT_NAME") or "Lily"


class ProgressNote(BaseModel):
    detail: str


def _tenant_scope(q, user):
    """Tenants see only their own tickets (submitter == self)."""
    if user.role == "tenant":
        return q.filter(Ticket.submitter_id == user.id)
    return q


def _tenant_owns(ticket, user) -> bool:
    return user.role != "tenant" or (ticket.submitter_id == user.id)


@router.get("")
def list_tickets(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Ticket)
    if status:
        q = q.filter(Ticket.status == status)
    if priority:
        q = q.filter(Ticket.priority == priority)
    q = _tenant_scope(q, user)

    total = q.count()
    tickets = q.order_by(func.datetime(Ticket.created_at).desc(), Ticket.id.desc()).offset(offset).limit(limit).all()

    return {
        "tickets": [TicketResponse.model_validate(t).model_dump() for t in tickets],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/prefs")
def get_ticket_prefs(user: User = Depends(get_current_user)):
    """The current user's default tickets-page filters (per-user)."""
    return {"status": user.default_ticket_status or "",
            "priority": user.default_ticket_priority or ""}


@router.put("/prefs")
def set_ticket_prefs(body: dict, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Save the current user's default tickets-page filters."""
    status = str(body.get("status", "")).strip()
    priority = str(body.get("priority", "")).strip().upper()
    if status not in ("", "open", "in_progress", "escalated", "customer_action", "closed"):
        raise HTTPException(status_code=400, detail=f"invalid status filter: {status}")
    if priority not in ("", "P1", "P2", "P3", "P4"):
        raise HTTPException(status_code=400, detail=f"invalid priority filter: {priority}")
    u = db.query(User).filter(User.id == user.id).first()
    u.default_ticket_status = status or None
    u.default_ticket_priority = priority or None
    db.commit()
    return {"status": "ok", "status": status, "priority": priority}


@router.get("/{ticket_id}", response_model=TicketResponse)
def get_ticket(ticket_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not _tenant_owns(ticket, user):
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


# ── live status (read-only; "where are you at?" without creating new work) ──
# Stage derivation lives in queue_status.py (shared with the Juniper responder
# in the worker) — one source, byte-identical output.


@router.get("/{ticket_id}/status")
def ticket_status(ticket_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Live status of a ticket for the chat thread: derived stage from the
    last work_note event + last activity age. Read-only, parallel-safe —
    querying never creates work or interrupts the technician."""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not _tenant_owns(ticket, user):
        raise HTTPException(status_code=404, detail="Ticket not found")

    return derive_status(ticket)


@router.post("", response_model=TicketResponse, status_code=201)
def create_ticket(
    ticket_data: TicketCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = Ticket(
        ticket_id=generate_ticket_id(),
        title=ticket_data.title,
        description=ticket_data.description,
        priority=ticket_data.priority if ticket_data.priority in ("P1", "P2", "P3", "P4") else "P3",
        status="open",
        source="manual",
        submitter_id=user.id,
        target_device_id=ticket_data.target_device_id,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    # Immediate alerts: P1/P2 tickets get emailed right away (best-effort,
    # in a background thread so ticket creation is never slowed by SMTP).
    if ticket.priority in ("P1", "P2"):
        _alert_new_ticket(ticket)
    return ticket


def _alert_new_ticket(ticket):
    """Email the alert recipients about a new high-priority ticket."""
    import html as _html
    import threading

    def _send():
        try:
            from emailer import send_email, get_recipients, alert_html
            recipients = get_recipients("alerts")
            if not recipients:
                import logging as _lg
                _lg.getLogger("barenoc").warning("Ticket alert: no recipients configured")
                return
            rows = [
                ("Ticket", f"{ticket.ticket_id} <b>[{ticket.priority}]</b>"),
                ("Title", _html.escape(ticket.title or "")),
                ("Priority", f"<b style='color:#e03131'>{ticket.priority}</b>"),
                ("Description", _html.escape((ticket.description or "")[:500])),
                ("Status", "open"),
            ]
            if ticket.target_device_id:
                rows.append(("Target device", str(ticket.target_device_id)))
            ok, err = send_email(
                recipients,
                f"[{ticket.priority}] BareNOC: {ticket.title}",
                body_html=alert_html("New high-priority ticket", rows),
                body_text=f"New {ticket.priority} ticket {ticket.ticket_id}: {ticket.title}",
            )
            if not ok and err:
                import logging
                logging.getLogger("barenoc").warning(f"Ticket alert email failed: {err}")
        except Exception as e:
            import logging as _lg
            _lg.getLogger("barenoc").exception(f"Ticket alert thread error: {e}")

    threading.Thread(target=_send, daemon=True).start()


@router.patch("/{ticket_id}", response_model=TicketResponse)
def update_ticket(
    ticket_id: str,
    update: TicketUpdate,
    db: Session = Depends(get_db),
    ctx: dict = Depends(get_access_context),
):
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Tenants: own tickets only, and only close them (no re-prioritizing,
    # no assignment, no editing the resolution).
    if ctx["user"].role == "tenant":
        if ticket.submitter_id != ctx["user"].id:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if update.assigned_to is not None or update.priority is not None \
                or update.resolution is not None:
            raise HTTPException(status_code=403,
                                detail="Tenants can only close their own tickets")
        if update.status is not None and update.status != "closed":
            raise HTTPException(status_code=403,
                                detail="Tenants can only close their own tickets")

    # Approving a control action on a grouped device requires a passkey-
    # authenticated (Pocket ID) operator with access to that device group.
    if update.status == "in_progress" and ticket.target_device_id:
        device = db.query(Device).filter(Device.id == ticket.target_device_id).first()
        if device and (device.device_group or "default") != "default":
            if ctx["auth_method"] != "oidc":
                raise HTTPException(
                    status_code=403,
                    detail="Control actions on grouped devices require passkey "
                           "(Pocket ID) authentication.",
                )
            g = device.device_group
            if ctx["user"].role != "admin" and g not in (ctx.get("groups") or []):
                raise HTTPException(
                    status_code=403,
                    detail=f"You don't have access to device group '{g}'.",
                )

    if update.status is not None:
        ticket.status = update.status
        if update.status == "closed":
            ticket.resolved_at = datetime.datetime.utcnow()
            ticket.assigned_to = ctx["user"].username  # Record who closed it
    if update.resolution is not None:
        ticket.resolution = update.resolution
    if update.assigned_to is not None:
        ticket.assigned_to = update.assigned_to
    if update.priority is not None:
        new_prio = update.priority.upper()
        if new_prio not in ("P1", "P2", "P3", "P4"):
            raise HTTPException(status_code=400, detail="Priority must be P1–P4")
        if new_prio != ticket.priority:
            add_note(ticket, "priority_change",
                     f"Priority changed {ticket.priority} → {new_prio} by {ctx['user'].username}")
            ticket.priority = new_prio

    ticket.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(ticket)
    return ticket


class NoteCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


@router.post("/{ticket_id}/progress")
def add_progress_note(
    ticket_id: str,
    note: "ProgressNote",
    db: Session = Depends(get_db),
    user: User = Depends(require_any_role("operator", "admin", "agent")),
):
    """Live 1-3 line status from the AI tech mid-task (agent_progress note).
    The runner relays the agent's progress while a long-running pi task works,
    so the operator sees the work happen instead of a silent wait."""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    detail = note.detail.strip()[:300]
    if not detail:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    add_note(ticket, "agent_progress", detail, actor=_assistant_name())
    ticket.updated_at = datetime.datetime.utcnow()
    db.commit()
    return {"status": "ok", "note": detail}


@router.post("/{ticket_id}/notes", response_model=TicketResponse)
def add_ticket_note(
    ticket_id: str,
    note: NoteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Append a user comment to a ticket's work_notes (chat-client endpoint)."""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not _tenant_owns(ticket, user):
        raise HTTPException(status_code=404, detail="Ticket not found")

    message = note.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    add_note(ticket, "user_message", message, actor=user.username)
    ticket.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(ticket)
    return ticket


@router.post("/{ticket_id}/retry")
def retry_ticket(ticket_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Reset a failed/escalated ticket back to open for reprocessing."""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not _tenant_owns(ticket, user):
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket.status not in ("failed", "escalated"):
        raise HTTPException(status_code=400, detail="Can only retry failed or escalated tickets")

    ticket.status = "open"
    ticket.resolution = None
    ticket.updated_at = datetime.datetime.utcnow()
    db.commit()
    return {"status": "retrying", "ticket_id": ticket_id}
