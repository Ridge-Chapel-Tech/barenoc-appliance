"""Alert engine — background thread inside the api container.

* Device status monitor: watches for online -> offline/unreachable transitions
  and emails an alert (and a recovery notice when the device comes back).
  A device must be observed healthy at least once before it can alert, so the
  first scan never fires false alarms. Flap protection: no repeat alert for the
  same device within 30 minutes.
* Daily digest: every morning at DIGEST_HOUR (local) emails a summary of new
  tickets and device health.

All sending goes through emailer.send_email (best-effort, never raises) and
silently no-ops when SMTP isn't configured yet.
"""

import datetime
import html
import logging
import os
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

from database import SessionLocal
from models import Device, Ticket, AuditLog
from emailer import send_email, get_recipients, smtp_configured, alert_html
from audit import log_event
from link_monitor import LinkMonitor, find_open_wan_episode, promote_wan_ticket

logger = logging.getLogger("barenoc-alerts")

CHECK_INTERVAL = 60        # seconds between device status scans
FLAP_MIN_INTERVAL = 1800   # 30 min: minimum gap between alerts for one device

DOWN_STATES = {"offline", "unreachable", "warning"}


# ── internet / ISP link monitor ────────────────────────────────────────────
# The UniFi gateway reports itself ONLINE during an ISP outage (its LAN side
# still works — only WAN is dead), so device-status monitoring never catches
# it. This probe checks the LAN gateway + a public host from the appliance:
#   gateway OK + internet OK   -> online
#   gateway OK + internet fail -> isp_down   (ISP/service-side outage)
#   gateway fail               -> link_down  (physical/LAN-side problem)
# A confirmed outage opens a P1 ticket + alert email (deduped); recovery
# auto-closes the ticket. INTERNET_PROBE_CONFIRM consecutive probes must agree
# before the state flips (flap protection).
INTERNET_OUTAGE_TITLE = "Internet connectivity down"

# ── ticket lifecycle (check-ins + auto-close) ───────────────────────────────
# Settings → Tickets. Per-priority knobs; 0 = never.
TICKET_CHECKIN_DEFAULT_HOURS = {"P1": 1, "P2": 4, "P3": 24, "P4": 24}
TICKET_CLOSE_DEFAULT_DAYS = {"P1": 3, "P2": 3, "P3": 3, "P4": 3}


def _ticket_lifecycle_config() -> dict:
    """Hot-read ticket-lifecycle settings from .env (os.environ fallback)."""
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

    def s(key: str, default: str) -> str:
        return (env.get(key) or os.getenv(key, default) or default).strip()

    def i(key: str, default: int) -> int:
        try:
            return int(env.get(key) or os.getenv(key, str(default)) or default)
        except ValueError:
            return default

    return {
        "checkin_enabled": s("TICKET_CHECKIN_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        "checkin_email": s("TICKET_CHECKIN_EMAIL", "true").lower() in ("1", "true", "yes", "on"),
        "autoclose_enabled": s("TICKET_AUTOCLOSE_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        "checkin_hours": {p: max(0, i(f"TICKET_CHECKIN_HOURS_{p}", TICKET_CHECKIN_DEFAULT_HOURS[p]))
                           for p in ("P1", "P2", "P3", "P4")},
        "close_after_days": {p: max(0, i(f"TICKET_CLOSE_AFTER_DAYS_{p}", TICKET_CLOSE_DEFAULT_DAYS[p]))
                              for p in ("P1", "P2", "P3", "P4")},
    }


def _last_checkin_ts(t) -> "datetime.datetime | None":
    """Timestamp of the most recent checkin_request work note (or None)."""
    import json
    try:
        notes = json.loads(t.work_notes or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    latest = None
    for n in notes:
        if isinstance(n, dict) and n.get("event") == "checkin_request" and n.get("timestamp"):
            latest = n["timestamp"]
    if not latest:
        return None
    try:
        return datetime.datetime.fromisoformat(str(latest).replace("Z", ""))
    except ValueError:
        return None


def _send_checkin_email(t, interval_h: int) -> None:
    """Email the alert recipients that a ticket is waiting on a human/customer."""
    import html as _html
    rows = [
        ("Ticket", f"{t.ticket_id} <b>[{t.priority}]</b>"),
        ("Title", _html.escape(t.title or "")),
        ("Status", f"<b>{_html.escape(t.status or '').upper()}</b>"),
        ("Waiting", f"{interval_h}h without an update"),
    ]
    send_email(get_recipients("alerts"),
               f"[{t.priority}] BareNOC: {t.ticket_id} awaiting an update ({interval_h}h)",
               body_html=alert_html("Ticket needs an update", rows),
               body_text=f"{t.ticket_id} ({t.priority}, {t.status}) has awaited an update for {interval_h}h.")


def _probe_config() -> dict:
    """Hot-read internet-probe settings from .env (os.environ fallback)."""
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

    def s(key: str, default: str) -> str:
        return (env.get(key) or os.getenv(key, default) or default).strip()

    def i(key: str, default: int) -> int:
        try:
            return int(env.get(key) or os.getenv(key, str(default)) or default)
        except ValueError:
            return default

    return {
        "enabled": s("INTERNET_PROBE_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        "gateway": s("INTERNET_PROBE_GATEWAY", "192.0.2.1"),
        "host": s("INTERNET_PROBE_HOST", "1.1.1.1"),
        "interval": max(15, i("INTERNET_PROBE_INTERVAL_S", 60)),
        "confirm": max(1, i("INTERNET_PROBE_CONFIRM", 3)),
    }


class InternetMonitor:
    """Probe LAN gateway + internet host; open/close a P1 ticket on outages."""

    def __init__(self):
        self._state = None     # None | online | isp_down | link_down
        self._streak = 0       # consecutive probes agreeing with the pending state
        self._last_check = 0.0
        self._last_email = 0.0

    def _ping(self, host: str) -> bool:
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "2", host],
                               capture_output=True, timeout=4)
            return r.returncode == 0
        except Exception:
            return False

    def check(self) -> None:
        cfg = _probe_config()
        if not cfg["enabled"]:
            return
        # Unconfigured probe (empty or the RFC-5737 doc-IP default): don't
        # open false P1s — an unset gateway used to probe 192.0.2.1 forever
        # (permanent "link down" + open ticket, e.g. TKT-20260808-1165).
        gw = (cfg.get("gateway") or "").strip().lower()
        if not gw or gw.startswith(("192.0.2.", "203.0.113.", "198.51.100.")):
            return
        now = time.time()
        if now - self._last_check < cfg["interval"]:
            return
        self._last_check = now

        gw_ok = self._ping(cfg["gateway"])
        inet_ok = self._ping(cfg["host"])
        status = "online" if (gw_ok and inet_ok) else ("isp_down" if gw_ok else "link_down")

        if status == self._state:
            self._streak = 0
            return
        self._streak += 1
        if self._streak < cfg["confirm"]:
            return
        self._streak = 0
        self._state = status
        if status == "online":
            self._recovered(cfg)
        else:
            self._outage(status, cfg)

    def _outage(self, status: str, cfg: dict) -> None:
        label = ("ISP/service outage — gateway up, internet unreachable"
                 if status == "isp_down"
                 else "link/physical — LAN gateway unreachable from the appliance")
        now = time.time()
        if now - self._last_email > FLAP_MIN_INTERVAL:
            self._last_email = now
            rows = [("Status", f"<b style='color:#e03131'>{status.upper()}</b>"),
                    ("Detail", label),
                    ("Gateway probe", cfg["gateway"]),
                    ("Internet probe", cfg["host"])]
            send_email(get_recipients("alerts"),
                       "[P1] BareNOC: Internet connectivity down",
                       body_html=alert_html("Internet connectivity down", rows),
                       body_text=f"P1: Internet down ({label}).")
        s = SessionLocal()
        try:
            from schemas import generate_ticket_id
            from audit import log_event
            # WAN single-ticket lifecycle: the WAN flap ticket IS the WAN
            # outage ticket. If an open WAN link-flap ticket exists on the
            # gateway, promote IT to P1 instead of opening a duplicate
            # 'Internet connectivity down' ticket.
            ep = find_open_wan_episode(s)
            if ep and promote_wan_ticket(s, ep):
                s.commit()
                log_event(s, "internet_outage", "system", {
                    "ticket_id": ep.ticket_id, "status": status,
                    "gateway": cfg["gateway"], "host": cfg["host"],
                    "promoted_wan_flap": True,
                }, ep.ticket_id)
                logger.error(f"Internet outage: promoted WAN flap ticket {ep.ticket_id} to P1 ({status})")
                return
            existing = s.query(Ticket).filter(
                Ticket.title == INTERNET_OUTAGE_TITLE,
                Ticket.status.in_(("open", "in_progress"))).first()
            if existing:
                return
            t = Ticket(ticket_id=generate_ticket_id(), title=INTERNET_OUTAGE_TITLE,
                       description=f"Internet connectivity down — {label}.",
                       priority="P1", status="open", source="auto", assigned_to="system")
            s.add(t)
            s.commit()
            log_event(s, "internet_outage", "system", {
                "ticket_id": t.ticket_id, "status": status,
                "gateway": cfg["gateway"], "host": cfg["host"],
            }, t.ticket_id)
            logger.error(f"Internet outage: opened {t.ticket_id} (P1, {status})")
        finally:
            s.close()

    def _recovered(self, cfg: dict) -> None:
        now = time.time()
        if now - self._last_email > FLAP_MIN_INTERVAL:
            self._last_email = now
            rows = [("Status", "<b style='color:#2f9e44'>ONLINE</b>"),
                    ("Gateway probe", cfg["gateway"]),
                    ("Internet probe", cfg["host"])]
            send_email(get_recipients("alerts"),
                       "[RECOVERED] BareNOC: Internet connectivity restored",
                       body_html=alert_html("Internet connectivity restored", rows),
                       body_text="Internet connectivity restored.")
        s = SessionLocal()
        try:
            from audit import log_event
            from worknotes import add_note
            t = s.query(Ticket).filter(
                Ticket.title == INTERNET_OUTAGE_TITLE,
                Ticket.status.in_(("open", "in_progress"))).first()
            if t:
                t.status = "closed"
                t.resolution = "Internet connectivity restored"
                t.resolved_at = datetime.datetime.utcnow()
                log_event(s, "internet_recovered", "system", {
                    "ticket_id": t.ticket_id, "gateway": cfg["gateway"], "host": cfg["host"],
                }, t.ticket_id)
            # WAN single-ticket lifecycle: if the outage was tracked as a
            # probe-promoted WAN flap ticket, close THAT ticket + episode on
            # confirmed recovery (the link monitor skips wan_probe episodes —
            # the probe owns their lifecycle end-to-end).
            ep = find_open_wan_episode(s)
            if ep and ep.escalation_reason == "wan_probe" and ep.ticket_id:
                wt = s.query(Ticket).filter(Ticket.ticket_id == ep.ticket_id).first()
                if wt and wt.status in ("open", "in_progress"):
                    add_note(wt, "wan_recovered", "Internet probe confirmed recovery.")
                    wt.status = "closed"
                    wt.resolution = "Internet connectivity restored (probe confirmed)"
                    wt.resolved_at = datetime.datetime.utcnow()
                    log_event(s, "internet_recovered", "system", {
                        "ticket_id": wt.ticket_id, "gateway": cfg["gateway"], "host": cfg["host"],
                        "wan_probe_episode": True,
                    }, wt.ticket_id)
                s.delete(ep)
            s.commit()
        finally:
            s.close()


class AlertEngine(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="alert-engine")
        self._last_status = {}   # device_id -> status (first observation seeds, no alert)
        self._last_alert_at = {} # device_id -> timestamp of last down-alert
        self._last_digest_day = None
        self._last_eod_day = None
        self._seen_ids = set()
        self._internet = InternetMonitor()
        self._link = LinkMonitor()

    def _check_ticket_lifecycle(self) -> None:
        """Ticket auto-close + human-handoff check-ins (Settings → Tickets).

        - escalated / customer_action tickets: post a check-in note (+ email)
          every per-priority interval (TICKET_CHECKIN_HOURS_<P1..P4>).
        - completed tickets: auto-close after per-priority days of the resolved
          state (TICKET_CLOSE_AFTER_DAYS_<P1..P4>; 'never close unless the AI
          resolved it more than N days ago').
        """
        from worknotes import add_note
        cfg = _ticket_lifecycle_config()
        s = SessionLocal()
        try:
            now = datetime.datetime.utcnow()
            if cfg["checkin_enabled"]:
                rows = s.query(Ticket).filter(
                    Ticket.status.in_(("escalated", "customer_action"))).all()
                for t in rows:
                    interval_h = cfg["checkin_hours"].get(t.priority, 24)
                    if interval_h <= 0:
                        continue
                    last = _last_checkin_ts(t)
                    baseline = last or t.updated_at or t.created_at
                    if not baseline or (now - baseline).total_seconds() < interval_h * 3600:
                        continue
                    add_note(t, "checkin_request",
                             f"⏰ Checking in — this {t.priority} ticket has been awaiting a "
                             f"human/customer update for {interval_h}h. Reply with an update "
                             f"or close it.")
                    s.commit()
                    log_event(s, "ticket_checkin", "system", {
                        "ticket_id": t.ticket_id, "priority": t.priority,
                        "interval_hours": interval_h,
                    }, t.ticket_id)
                    if cfg["checkin_email"]:
                        _send_checkin_email(t, interval_h)
            if cfg["autoclose_enabled"]:
                rows = s.query(Ticket).filter(Ticket.status == "completed").all()
                for t in rows:
                    close_days = cfg["close_after_days"].get(t.priority, 3)
                    if close_days <= 0:
                        continue
                    base = t.resolved_at or t.updated_at or t.created_at
                    if not base or (now - base).total_seconds() < close_days * 86400:
                        continue
                    add_note(t, "autoclosed",
                             f"Auto-closed: the AI resolved this {t.priority} ticket more than "
                             f"{close_days} days ago and no follow-up arrived.")
                    t.status = "closed"
                    t.resolution = f"Auto-closed after {close_days} days in resolved state"
                    s.commit()
                    log_event(s, "ticket_autoclosed", "system", {
                        "ticket_id": t.ticket_id, "priority": t.priority,
                        "close_after_days": close_days,
                    }, t.ticket_id)
                    logger.info(f"Auto-closed {t.ticket_id} (resolved {close_days}d)")
        finally:
            s.close()

    def run(self):
        logger.info("Alert engine started (SMTP configured: %s)", smtp_configured())
        while True:
            try:
                now = _local_now()
                # Internet link probe runs regardless of SMTP — the outage
                # TICKET opens even when no email transport is configured.
                self._internet.check()
                # Ticket lifecycle (check-ins + auto-close) also runs without
                # SMTP — notes + status changes are the record; email is extra.
                self._check_ticket_lifecycle()
                # Link-stability monitor (flap/outage tickets) — the TICKET is
                # the record, so this runs regardless of SMTP too.
                self._link.check()
                if smtp_configured():
                    self._check_devices()
                    self._maybe_digest(now)
                    self._maybe_eod(now)
            except Exception:
                logger.exception("Alert engine cycle error")
            time.sleep(CHECK_INTERVAL)

    # ── device status monitor ────────────────────────────────────

    def _check_devices(self):
        s = SessionLocal()
        try:
            # Only devices the operator opted in to state-change alerts
            # (notify_state_changes) — otherwise every phone leaving/rejoining
            # the wifi would page the whole fleet.
            devices = s.query(Device).filter(Device.notify_state_changes.is_(True)).all()
        finally:
            s.close()
        for d in devices:
            status = (d.status or "pending").lower()
            device_id = d.id
            prev = self._last_status.get(device_id)

            # Seed on first observation — never alert on day one
            if device_id not in self._last_status:
                self._last_status[device_id] = status
                continue

            if prev == status:
                continue

            # Transition happened
            self._last_status[device_id] = status
            name = d.name or d.ip_address or f"device {device_id}"
            if status in DOWN_STATES and prev == "online":
                self._alert_down(d, name, prev, status)
            elif prev in DOWN_STATES and status == "online":
                self._alert_recovery(d, name)

    def _alert_down(self, d, name, prev, status):
        now = time.time()
        if now - self._last_alert_at.get(d.id, 0) < FLAP_MIN_INTERVAL:
            return  # flap protection
        self._last_alert_at[d.id] = now
        rows = [
            ("Device", name),
            ("IP", d.ip_address),
            ("Type", d.device_type),
            ("Status", f"<b style='color:#e03131'>{status.upper()}</b> (was {prev})"),
            ("MAC", d.mac_address or "—"),
        ]
        if d.fingerprint and d.fingerprint.get("os_guess"):
            rows.append(("Identity", d.fingerprint["os_guess"]))
        ok, err = send_email(
            get_recipients("alerts"),
            f"[DOWN] BareNOC: {name} is {status}",
            body_html=alert_html("Device went down", rows),
            body_text=f"Device {name} ({d.ip_address}) is {status} (was {prev}).",
        )
        if ok:
            logger.info(f"DOWN alert sent for {name}")

    def _alert_recovery(self, d, name):
        rows = [
            ("Device", name),
            ("IP", d.ip_address),
            ("Status", "<b style='color:#2f9e44'>ONLINE</b>"),
        ]
        ok, err = send_email(
            get_recipients("alerts"),
            f"[RECOVERED] BareNOC: {name} is back online",
            body_html=alert_html("Device recovered", rows),
            body_text=f"Device {name} ({d.ip_address}) is back online.",
        )
        if ok:
            logger.info(f"Recovery notice sent for {name}")

    # ── daily digest ─────────────────────────────────────────────

    # ── daily reports (digest + EOD summary) ────────────────────

    def _maybe_digest(self, now: datetime.datetime):
        """Morning digest — hour, on/off, and recipients come from .env (Settings → Email)."""
        cfg = _report_config()
        if not cfg.get("morning_enabled") or now.hour != cfg.get("digest_hour"):
            return
        if self._last_digest_day == now.date():
            return
        self._last_digest_day = now.date()

        recipients = get_recipients("digest")
        if not recipients:
            logger.warning("Morning digest skipped — no digest recipients configured")
            return
        report = build_report("morning_digest", now)
        ok, err = send_email(
            recipients, report["subject"],
            body_html=report["body_html"], body_text=report["body_text"],
        )
        if ok:
            logger.info("Morning digest sent to %d recipients", len(recipients))

    def _maybe_eod(self, now: datetime.datetime):
        """End-of-day summary — settings changes (audit) + day recap.
        Hour, on/off, and recipients come from .env (Settings → Email)."""
        cfg = _report_config()
        if not cfg.get("eod_enabled") or now.hour != cfg.get("eod_hour"):
            return
        if self._last_eod_day == now.date():
            return
        self._last_eod_day = now.date()

        recipients = get_recipients("eod")
        if not recipients:
            logger.warning("EOD summary skipped — no EOD recipients configured")
            return
        report = build_report("eod_summary", now)
        ok, err = send_email(
            recipients, report["subject"],
            body_html=report["body_html"], body_text=report["body_text"],
        )
        if ok:
            logger.info("EOD summary sent to %d recipients (%d settings changes)",
                        len(recipients), len(report.get("settings_changes", [])))


def _report_config() -> dict:
    """Hot-reload report settings from .env (Settings → Email)."""
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

    def enabled(key, default):
        return env.get(key, default).lower() in ("1", "true", "yes", "on")

    def hour(key, default):
        try:
            h = int(env.get(key, default))
        except (TypeError, ValueError):
            h = default
        return h if 0 <= h <= 23 else default

    return {
        "morning_enabled": enabled("REPORT_MORNING_DIGEST", "true"),
        "eod_enabled": enabled("REPORT_EOD_SUMMARY", "true"),
        "digest_hour": hour("DIGEST_HOUR", 7),
        "eod_hour": hour("EOD_HOUR", 18),
        "timezone": env.get("TZ", ""),
    }


def _local_now() -> datetime.datetime:
    """Current wall-clock time in the configured timezone (TZ from .env).
    Returns a NAIVE datetime of local time — safe for hour comparisons and
    DB queries, and strftime renders the local wall-clock (the email 'Date'
    row shows the actual local send time, not UTC). Falls back to container
    local time (UTC) when TZ is unset or invalid."""
    tz_name = _report_config().get("timezone")
    if tz_name:
        try:
            return datetime.datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.datetime.now()


def build_report(report_type: str, now: datetime.datetime = None) -> dict:
    """Shared report builder — used by the scheduled sends AND the
    Settings → Email "send test" buttons. Returns subject/rows/html/text."""
    now = now or datetime.datetime.now()
    if report_type == "morning_digest":
        title, subject = "BareNOC daily summary", "BareNOC Morning Digest"
    elif report_type == "eod_summary":
        title, subject = "BareNOC end-of-day summary", "BareNOC End-of-Day Summary"
    else:
        raise ValueError(f"Unknown report type: {report_type}")

    s = SessionLocal()
    try:
        since = now - datetime.timedelta(hours=24)
        tickets = s.query(Ticket).filter(Ticket.created_at >= since).all()
        devices = s.query(Device).all()
        events = []
        if report_type == "eod_summary":
            events = (
                s.query(AuditLog)
                .filter(AuditLog.event_type == "settings_change",
                        AuditLog.timestamp >= since)
                .order_by(AuditLog.timestamp.desc())
                .all()
            )
    finally:
        s.close()

    by_status = {}
    for t in tickets:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    dev_counts = {}
    for d in devices:
        dev_counts[d.status] = dev_counts.get(d.status, 0) + 1

    rows = [
        ("Date", now.strftime("%Y-%m-%d %H:%M")),
        ("New tickets (24h)", len(tickets)),
        ("Ticket breakdown", ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())) or "—"),
        ("Devices tracked", len(devices)),
        ("Device breakdown", ", ".join(f"{k}: {v}" for k, v in sorted(dev_counts.items())) or "—"),
    ]
    if events:
        lines = []
        for e in events[:20]:
            d = e.data or {}
            fields = d.get("fields") or []
            when = e.timestamp.strftime("%H:%M") if e.timestamp else "?"
            lines.append(
                f"{when} — <b>{html.escape(str(e.actor))}</b>: "
                f"{html.escape(str(d.get('section', '?')))} "
                f"({html.escape(', '.join(str(f) for f in fields))})"
            )
        rows.append((f"System settings changes ({len(events)})", "<br>".join(lines)))
    if tickets:
        rows.append(("Recent tickets", "<br>".join(
            f"{t.ticket_id} <b>[{t.priority}]</b> {html.escape(t.title)}" for t in tickets[:8])))

    return {
        "subject": subject,
        "rows": rows,
        "settings_changes": events,
        "body_html": alert_html(title, rows),
        "body_text": f"Settings changes: {len(events)}. New tickets (24h): {len(tickets)}. Devices: {len(devices)}.",
    }


_engine = None


def start_alert_engine():
    """Idempotently start the background alert engine."""
    global _engine
    if _engine is None:
        _engine = AlertEngine()
        _engine.start()
    return _engine
