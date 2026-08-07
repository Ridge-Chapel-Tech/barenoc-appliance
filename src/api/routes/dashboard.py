from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import Device, Ticket, User, AuditLog
from schemas import DashboardStats, TicketResponse
from auth import get_current_user
from routes.settings import _read_env_file

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


from sqlalchemy import func, or_, and_
from datetime import datetime, timedelta
import json as _json


def _branding() -> tuple[str, bool]:
    """Return (customer_name, has_logo) from the .env file."""
    env = _read_env_file()
    customer = env.get("CUSTOMER_NAME", "").strip()
    has_logo = bool(env.get("BRANDING_LOGO", "").strip())
    return customer, has_logo


# ── reporting (dashboard Performance & Reporting section) ───────────────────


def _hours(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 1)


def _report_env_float(key: str, default: float) -> float:
    """Float from .env (or process env), falling back to default."""
    import os as _os
    env = {}
    try:
        with open("/opt/barenoc/.env") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    raw = env.get(key) or _os.getenv(key, "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _notes_events(t) -> list:
    try:
        return _json.loads(t.work_notes or "[]")
    except (ValueError, TypeError):
        return []


# Notes that are internal machinery, not a response to the customer/AI output.
_INTERNAL_NOTE_EVENTS = {"processing", "auto_execute", "agent_retry", "checkin_request"}


def _first_response_min(t) -> "float | None":
    """Minutes from creation to the first CUSTOMER-FACING response (the first
    work note that isn't internal machinery — e.g. agent_completed, agent_response,
    customer_input, escalated). Computed in minutes directly (no hour-rounding
    that would flatten sub-3-minute responses to 0)."""
    notes = [n for n in _notes_events(t)
             if isinstance(n, dict) and n.get("timestamp")
             and n.get("event") not in _INTERNAL_NOTE_EVENTS]
    if not notes or not t.created_at:
        return None
    try:
        first = min(datetime.fromisoformat(str(n["timestamp"]).replace("Z", "")) for n in notes)
    except ValueError:
        return None
    return round((first - t.created_at).total_seconds() / 60, 1)


def _report_stats(db, days: int = 30) -> dict:
    """Ticket performance stats for the dashboard + CSV/PDF export."""
    days = max(1, min(days, 365))
    now = datetime.utcnow()
    since = now - timedelta(days=days)

    created_rows = db.query(Ticket).filter(Ticket.created_at >= since).all()
    resolved_rows = db.query(Ticket).filter(
        Ticket.resolved_at.isnot(None), Ticket.resolved_at >= since).all()
    closed_rows = [t for t in created_rows if t.status in ("closed", "completed")]

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    res_times = [_hours(t.created_at, t.resolved_at) for t in resolved_rows]
    first_actions = [_first_response_min(t) for t in created_rows]
    res_by_priority = {}
    for t in resolved_rows:
        res_by_priority.setdefault(t.priority, []).append(_hours(t.created_at, t.resolved_at))
    first_by_priority = {}
    for t in created_rows:
        first_by_priority.setdefault(t.priority, []).append(_first_response_min(t))

    open_dur = [(_hours(t.created_at, t.resolved_at or t.updated_at)) for t in closed_rows]

    esc_rows = db.query(AuditLog).filter(
        AuditLog.event_type == "escalation", AuditLog.timestamp >= since).count()
    esc_audits = db.query(AuditLog).filter(
        AuditLog.event_type == "escalation", AuditLog.timestamp >= since).all()
    esc_ticket_ids = {a.ticket_id for a in esc_audits if a.ticket_id}
    escalated_tickets = len(esc_ticket_ids)
    # Rate = tickets created in the period that were escalated at least once ÷
    # tickets created in the period. Bounded 0..1 by construction. (Escalations
    # of older tickets still count in the raw events/distinct numbers.)
    created_ids = {t.ticket_id for t in created_rows}
    escalated_created = len(esc_ticket_ids & created_ids)
    autoclosed = db.query(AuditLog).filter(
        AuditLog.event_type == "ticket_autoclosed", AuditLog.timestamp >= since).count()
    checkins = db.query(AuditLog).filter(
        AuditLog.event_type == "ticket_checkin", AuditLog.timestamp >= since).count()
    llm_rows = db.query(AuditLog).filter(
        AuditLog.event_type == "llm_request", AuditLog.timestamp >= since).all()
    llm_cost = round(sum(float((a.data or {}).get("cost_usd") or 0) for a in llm_rows), 4)

    # Support cost: direct AI spend (hard number) vs estimated manned-NOC labor
    # (configurable rate/hours-per-ticket — an estimate, labeled as such).
    labor_rate = _report_env_float("SUPPORT_LABOR_RATE_USD", 45.0)
    hours_per_ticket = _report_env_float("SUPPORT_HOURS_PER_TICKET", 1.0)
    est_manned_noc_cost = round(len(resolved_rows) * hours_per_ticket * labor_rate, 2)
    savings_usd = round(max(0.0, est_manned_noc_cost - llm_cost), 2)

    # Reopens: a ticket whose notes contain ≥2 terminal (closed/completed) events
    reopens = 0
    for t in created_rows:
        terminals = [n for n in _notes_events(t)
                     if isinstance(n, dict) and n.get("event") in ("closed", "completed")]
        if len(terminals) >= 2:
            reopens += 1

    # daily trend (created vs resolved) over the period
    trend = []
    for i in range(days):
        day_start = since + timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        trend.append({
            "date": day_start.date().isoformat(),
            "created": db.query(func.count(Ticket.id)).filter(
                Ticket.created_at >= day_start, Ticket.created_at < day_end).scalar() or 0,
            "resolved": db.query(func.count(Ticket.id)).filter(
                Ticket.resolved_at >= day_start, Ticket.resolved_at < day_end).scalar() or 0,
        })

    status_funnel = {
        st: db.query(func.count(Ticket.id)).filter(Ticket.status == st).scalar() or 0
        for st in ("open", "in_progress", "awaiting_approval", "escalated",
                   "customer_action", "completed", "failed", "closed")
    }
    priority_dist = {
        p: db.query(func.count(Ticket.id)).filter(
            Ticket.priority == p, Ticket.created_at >= since).scalar() or 0
        for p in ("P1", "P2", "P3", "P4")
    }
    res_by_priority_avg = {p: _avg(v) for p, v in sorted(res_by_priority.items())}
    first_by_priority_avg = {p: _avg(v) for p, v in sorted(first_by_priority.items())}

    created = len(created_rows)
    return {
        "days": days,
        "period_start": since.date().isoformat(),
        "period_end": now.date().isoformat(),
        "created": created,
        "resolved": len(resolved_rows),
        "closed": len(closed_rows),
        "avg_resolution_hours": _avg(res_times),
        "avg_first_response_min": _avg(first_actions),
        "avg_open_hours": _avg(open_dur),
        "res_by_priority_hours": res_by_priority_avg,
        "first_by_priority_min": first_by_priority_avg,
        "escalations": esc_rows,
        "escalated_tickets": escalated_tickets,
        "escalation_rate": round(escalated_created / created, 3) if created else 0,
        "autoclosed": autoclosed,
        "checkins": checkins,
        "reopens": reopens,
        "llm_cost_usd": llm_cost,
        "llm_calls": len(llm_rows),
        "support_cost_usd": llm_cost,
        "est_manned_noc_cost_usd": est_manned_noc_cost,
        "savings_usd": savings_usd,
        "status_funnel": status_funnel,
        "priority_dist": priority_dist,
        "trend": trend,
    }


def _ticket_rows(db, since: datetime, now: datetime) -> list:
    """Per-ticket analysis rows for the CSV/PDF exports.

    Columns: created, time to first action (respond), time to escalate,
    escalations count, time to close, cost to close (LLM spend on the ticket),
    customer replies, check-ins, auto-closed flag, assigned to, source.
    """
    rows = db.query(Ticket).filter(
        Ticket.created_at < now,
        or_(Ticket.resolved_at.is_(None), Ticket.resolved_at >= since),
    ).order_by(func.datetime(Ticket.created_at).asc()).all()

    esc_first = {}   # ticket_id -> first escalation timestamp in period
    esc_count = {}   # ticket_id -> escalation events in period
    llm_cost = {}    # ticket_id -> LLM spend in period
    audits = db.query(AuditLog).filter(AuditLog.timestamp >= since).all()
    for a in audits:
        tid = a.ticket_id
        if not tid:
            continue
        if a.event_type == "escalation":
            esc_count[tid] = esc_count.get(tid, 0) + 1
            ts = a.timestamp
            if tid not in esc_first or ts < esc_first[tid]:
                esc_first[tid] = ts
        elif a.event_type == "llm_request":
            llm_cost[tid] = llm_cost.get(tid, 0) + float((a.data or {}).get("cost_usd") or 0)

    out = []
    for t in rows:
        notes = _notes_events(t)
        user_msgs = sum(1 for n in notes if isinstance(n, dict) and n.get("event") == "user_message")
        checkins = sum(1 for n in notes if isinstance(n, dict) and n.get("event") == "checkin_request")
        autoclosed = any(isinstance(n, dict) and n.get("event") == "autoclosed" for n in notes)
        close_ts = t.resolved_at or (t.updated_at if t.status == "closed" else None)
        # Authoritative per-ticket cost first (ticket.llm_cost_usd — set by the
        # worker for caged-pipeline calls); audit sum as fallback. Note: the
        # pi/Lily path doesn't meter LLM usage yet — those tickets show 0 until
        # runner-side cost reporting lands.
        ticket_cost = t.llm_cost_usd if t.llm_cost_usd is not None else llm_cost.get(t.ticket_id, 0)
        out.append({
            "ticket_id": t.ticket_id,
            "title": (t.title or "")[:80],
            "priority": t.priority,
            "status": t.status,
            "source": t.source,
            "assigned_to": t.assigned_to or "",
            "created": t.created_at.isoformat()[:19] if t.created_at else "",
            "time_to_respond_min": _first_response_min(t),
            "time_to_escalate_h": _hours(t.created_at, esc_first.get(t.ticket_id)),
            "escalations": esc_count.get(t.ticket_id, 0),
            "time_to_close_h": _hours(t.created_at, close_ts),
            "cost_to_close_usd": round(float(ticket_cost), 4),
            "customer_replies": user_msgs,
            "checkins": checkins,
            "autoclosed": "yes" if autoclosed else "",
        })
    return out


@router.get("/reports")
def reports(db: Session = Depends(get_db),
            user: User = Depends(get_current_user),
            days: int = Query(30, ge=1, le=365)):
    """Ticket performance + business reporting stats for the dashboard."""
    return _report_stats(db, days)


def _report_tables(stats: dict, tickets: list) -> list:
    """Ordered spreadsheet tables shared by CSV/XLSX/ODS/TSV exports.
    Returns [(sheet_name, [[row...]...])] with the header row first."""
    summary = [
        ["Metric", "Value"],
        ["Tickets created", stats["created"]], ["Tickets resolved", stats["resolved"]],
        ["Tickets closed", stats["closed"]],
        ["Avg resolution time (h)", stats["avg_resolution_hours"] or "—"],
        ["Avg time to first response (min)", stats["avg_first_response_min"] or "—"],
        ["Escalation events", stats["escalations"]],
        ["Tickets escalated (at least once)", stats["escalated_tickets"]],
        ["Escalation rate (% of created)", stats["escalation_rate"]],
        ["Auto-closed", stats["autoclosed"]], ["Check-ins sent", stats["checkins"]],
        ["Reopened / follow-ups", stats["reopens"]],
        ["AI support spend (USD, LLM)", stats["support_cost_usd"]],
        ["Est. manned-NOC cost (USD)", stats["est_manned_noc_cost_usd"]],
        ["Estimated savings (USD)", stats["savings_usd"]], ["LLM calls", stats["llm_calls"]],
    ]
    by_priority = [["Priority", "Avg resolution (h)", "Avg first response (min)", "Created"]] + [
        [p, stats["res_by_priority_hours"].get(p) or "—",
         stats["first_by_priority_min"].get(p) or "—", stats["priority_dist"].get(p, 0)]
        for p in ("P1", "P2", "P3", "P4")]
    funnel = [["Status", "Count"]] + [[k, v] for k, v in stats["status_funnel"].items()]
    trend = [["Date", "Created", "Resolved"]] + [
        [r["date"], r["created"], r["resolved"]] for r in stats["trend"]]
    per_ticket = [["Ticket", "Title", "Priority", "Status", "Source", "Assigned",
                   "Created", "Time to respond (min)", "Time to escalate (h)",
                   "Escalations", "Time to close (h)", "Cost to close ($)",
                   "Customer replies", "Check-ins", "Auto-closed"]] + [
        [t["ticket_id"], t["title"], t["priority"], t["status"], t["source"],
         t["assigned_to"], t["created"],
         t["time_to_respond_min"] if t["time_to_respond_min"] is not None else "",
         t["time_to_escalate_h"] if t["time_to_escalate_h"] is not None else "",
         t["escalations"],
         t["time_to_close_h"] if t["time_to_close_h"] is not None else "",
         t["cost_to_close_usd"], t["customer_replies"], t["checkins"], t["autoclosed"]]
        for t in tickets]
    return [("Summary", summary), ("ByPriority", by_priority),
            ("StatusFunnel", funnel), ("DailyTrend", trend), ("PerTicket", per_ticket)]


@router.get("/reports/export")
def export_report(db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  days: int = Query(30, ge=1, le=365),
                  format: str = Query("csv")):
    """Download the report as CSV, XLSX, ODS, TSV (Google-Sheets paste) or PDF."""
    if format not in ("csv", "pdf", "xlsx", "ods", "tsv"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="format must be csv, xlsx, ods, tsv, or pdf")
    stats = _report_stats(db, days)
    tickets = _ticket_rows(db, datetime.utcnow() - timedelta(days=days), datetime.utcnow())
    customer, _ = _branding()
    filename = f"barenoc-report-{stats['period_start']}_{stats['period_end']}"
    tables = _report_tables(stats, tickets)

    if format in ("csv", "tsv"):
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf, dialect="excel-tab" if format == "tsv" else "excel")
        w.writerow([f"{customer or 'BareNOC'} — NOC performance report", f"{stats['period_start']} .. {stats['period_end']}"])
        for name, rows in tables:
            w.writerow([])
            w.writerow([name])
            for row in rows:
                w.writerow(row)
        ext = "tsv" if format == "tsv" else "csv"
        media = "text/tab-separated-values" if format == "tsv" else "text/csv"
        return StreamingResponse(iter([buf.getvalue()]), media_type=media,
                                 headers={"Content-Disposition": f'attachment; filename="{filename}.{ext}"'})

    if format == "xlsx":
        try:
            import xlsxwriter
        except ImportError:
            return Response("XLSX support not installed on this appliance", status_code=503)
        import io as _io
        buf = _io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        bold = wb.add_format({"bold": True})
        header_fmt = wb.add_format({"bold": True, "bg_color": "#1e293b",
                                    "font_color": "#ffffff", "border": 1})
        for name, rows in tables:
            ws = wb.add_worksheet(name[:31])
            ws.write(0, 0, f"{customer or 'BareNOC'} — NOC performance report", bold)
            ws.write(1, 0, f"{stats['period_start']} .. {stats['period_end']}")
            for r, row in enumerate(rows, start=3):
                for c, val in enumerate(row):
                    ws.write(r, c, val, header_fmt if r == 3 else None)
        wb.close()
        return Response(content=buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'})

    if format == "ods":
        try:
            from odf.opendocument import OpenDocumentSpreadsheet
            from odf.table import Table as OdsTable, TableRow, TableCell
            from odf.text import P
        except ImportError:
            return Response("ODS support not installed on this appliance", status_code=503)
        import io as _io
        buf = _io.BytesIO()
        doc = OpenDocumentSpreadsheet()
        for name, rows in tables:
            tbl = OdsTable(name=name[:31])
            for row in rows:
                tr = TableRow()
                for val in row:
                    tc = TableCell()
                    tc.addElement(P(text=str(val)))
                    tr.addElement(tc)
                tbl.addElement(tr)
            doc.spreadsheet.addElement(tbl)
        doc.save(buf)
        return Response(content=buf.getvalue(), media_type="application/vnd.oasis.opendocument.spreadsheet",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.ods"'})

    # PDF (reportlab)
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError:
        return Response("PDF support not installed on this appliance", status_code=503)
    import io as _io
    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            rightMargin=0.6 * inch, leftMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"<b>{customer or 'BareNOC'} — NOC Performance Report</b>", styles["Title"]),
        Spacer(1, 6),
        Paragraph(f"Period: {stats['period_start']} .. {stats['period_end']} · generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                  styles["Normal"]),
        Spacer(1, 12),
        Paragraph("<b>Headline metrics</b>", styles["Heading2"]),
        Table([["Tickets created", stats["created"]], ["Tickets resolved", stats["resolved"]],
               ["Avg resolution time (h)", stats["avg_resolution_hours"] or "—"],
               ["Avg time to first response (min)", stats["avg_first_response_min"] or "—"],
               ["Tickets escalated (at least once)", stats["escalated_tickets"]],
               ["Escalation events", stats["escalations"]],
               ["Tickets closed", stats["closed"]],
               ["Auto-closed", stats["autoclosed"]], ["Check-ins sent", stats["checkins"]],
               ["Escalation rate (% of created)", stats["escalation_rate"]],
               ["Reopened / follow-ups", stats["reopens"]],
               ["AI support spend (USD)", stats["support_cost_usd"]],
               ["Est. manned-NOC cost (USD)", stats["est_manned_noc_cost_usd"]],
               ["Estimated savings (USD)", stats["savings_usd"]], ["LLM calls", stats["llm_calls"]]],
               colWidths=[3.2 * inch, 2.2 * inch]),
        Spacer(1, 12),
        Paragraph("<b>Daily trend (created vs resolved)</b>", styles["Heading2"]),
        Table([["Date", "Created", "Resolved"]]
              + [[r["date"], r["created"], r["resolved"]] for r in stats["trend"]],
              colWidths=[1.6 * inch, 1.1 * inch, 1.1 * inch]),
        Spacer(1, 12),
        Paragraph("<b>By priority</b>", styles["Heading2"]),
        Table([["Priority", "Avg resolution (h)", "Avg first response (min)", "Created"]]
              + [[p, stats["res_by_priority_hours"].get(p) or "—",
                  stats["first_by_priority_min"].get(p) or "—",
                  stats["priority_dist"].get(p, 0)] for p in ("P1", "P2", "P3", "P4")],
              colWidths=[1.0 * inch, 1.8 * inch, 1.8 * inch, 0.8 * inch]),
        Spacer(1, 12),
        Paragraph("<b>Status funnel</b>", styles["Heading2"]),
        Table([["Status", "Count"]] + [[k, v] for k, v in stats["status_funnel"].items()],
              colWidths=[2.4 * inch, 1.0 * inch]),
        Spacer(1, 12),
        Paragraph("<b>Per-ticket analysis</b>", styles["Heading2"]),
    ]
    PDF_TICKET_CAP = 100
    t_rows = [["Ticket", "P", "Status", "Created", "Resp (min)", "Esc (h)",
               "Close (h)", "Cost ($)", "Replies", "Auto"]]
    for t in tickets[:PDF_TICKET_CAP]:
        t_rows.append([
            t["ticket_id"], t["priority"], t["status"][:10], t["created"],
            t["time_to_respond_min"] if t["time_to_respond_min"] is not None else "—",
            t["time_to_escalate_h"] if t["time_to_escalate_h"] is not None else "—",
            t["time_to_close_h"] if t["time_to_close_h"] is not None else "—",
            t["cost_to_close_usd"], t["customer_replies"], t["autoclosed"] or "—",
        ])
    if len(tickets) > PDF_TICKET_CAP:
        story.append(Paragraph(
            f"Latest {PDF_TICKET_CAP} of {len(tickets)} tickets shown — the full list is in the CSV export.",
            styles["Normal"]))
    story.append(Table(t_rows, colWidths=[1.5 * inch, 0.35 * inch, 0.7 * inch, 1.15 * inch,
                                           0.7 * inch, 0.7 * inch, 0.7 * inch,
                                           0.6 * inch, 0.6 * inch, 0.45 * inch],
                       repeatRows=1))
    for table in story:
        if isinstance(table, Table):
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]))
    doc.build(story)
    data = buf.getvalue()
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'})
@router.get("/stats", response_model=DashboardStats)
def get_stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Only CONTROLLED devices count as the fleet: SSH admin access OR adopted
    # UniFi-managed gear (unifi_managed + claimed) — the same definition as the
    # Onboarded view. Ping/SNMP-only devices are candidates, not fleet.
    controlled = or_(
        Device.ssh_key_fingerprint.isnot(None),
        and_(Device.unifi_managed.is_(True), Device.claimed.is_(True)),
    )
    total_devices = db.query(func.count(Device.id)).filter(controlled).scalar() or 0
    online_devices = db.query(func.count(Device.id)).filter(controlled, Device.status == "online").scalar() or 0
    offline_devices = db.query(func.count(Device.id)).filter(controlled, Device.status == "offline").scalar() or 0
    warning_devices = db.query(func.count(Device.id)).filter(controlled, Device.status == "warning").scalar() or 0

    total_tickets = db.query(func.count(Ticket.id)).scalar() or 0
    open_tickets = db.query(func.count(Ticket.id)).filter(Ticket.status == "open").scalar() or 0
    in_progress = db.query(func.count(Ticket.id)).filter(
        Ticket.status.in_(["in_progress", "awaiting_approval"])
    ).scalar() or 0
    p1_tickets = db.query(func.count(Ticket.id)).filter(
        Ticket.priority == "P1", Ticket.status.notin_(["closed", "completed", "failed", "escalated"])
    ).scalar() or 0
    p2_tickets = db.query(func.count(Ticket.id)).filter(
        Ticket.priority == "P2", Ticket.status.notin_(["closed", "completed", "failed", "escalated"])
    ).scalar() or 0

    recent = (
        db.query(Ticket)
        .order_by(func.datetime(Ticket.created_at).desc())
        .limit(5)
        .all()
    )

    # System health
    if p1_tickets > 0:
        health = "critical"
    elif p2_tickets > 0:
        health = "warning"
    elif offline_devices > 0:
        health = "degraded"
    else:
        health = "healthy"

    customer_name, has_logo = _branding()

    return DashboardStats(
        total_devices=total_devices,
        online_devices=online_devices,
        offline_devices=offline_devices,
        warning_devices=warning_devices,
        total_tickets=total_tickets,
        open_tickets=open_tickets,
        in_progress_tickets=in_progress,
        p1_tickets=p1_tickets,
        p2_tickets=p2_tickets,
        recent_tickets=[TicketResponse.model_validate(t).model_dump() for t in recent],
        system_health=health,
        customer_name=customer_name,
        has_logo=has_logo,
    )
