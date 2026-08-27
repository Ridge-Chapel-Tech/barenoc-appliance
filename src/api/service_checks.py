"""Service checks — per-endpoint ping / TCP / HTTP(S) monitors → tickets.

The missing middle of the NOC story: "is my stuff up?" Each ServiceMonitor
runs a small state machine:

  unknown --N consecutive failures--> P2 ticket ("Service check failed: <name>"),
                                       kept open while the service stays down
  still down > 10 min (sustained)   --> SAME ticket P1 ("Service outage: …")
  N consecutive successes           --> auto-close with a summary note

Persistence: ``service_check_episodes`` (models.ServiceCheckEpisode) so a
scheduler/API restart resumes an in-flight outage instead of opening a
duplicate. ``fail_streak``/``ok_streak`` live on the monitor row (restart-safe
before the ticket even opens). The TICKET is the record; email/push is
best-effort and gated by the per-monitor ``notify`` flag.

The SCHEDULER is the poller (POST /api/v1/service-checks/poll) — this module
is imported by routes/service_checks.py and must not import FastAPI.
"""

import datetime
import logging
import os
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from database import SessionLocal
from models import Device, ServiceMonitor, ServiceCheckEpisode, Ticket
from schemas import generate_ticket_id
from emailer import send_email, get_recipients, alert_html
from audit import log_event
from worknotes import add_note

logger = logging.getLogger("barenoc-service-checks")

CHECK_TYPES = ("ping", "tcp", "http")

# ── config (hot-read from .env, os.environ fallback) ────────────────────────

def service_check_config() -> dict:
    """Env knobs — per-monitor fields (interval/thresholds/notify) live on the
    monitor row; these are the global defaults + the P1 escalation window."""
    env = _read_env()

    def s(key, default):
        return (env.get(key) or os.getenv(key, default) or default).strip()

    def b(key, default):
        return s(key, default).lower() in ("1", "true", "yes", "on")

    def i(key, default):
        try:
            return int(env.get(key) or os.getenv(key, str(default)) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": b("SERVICE_CHECK_ENABLED", "true"),
        "default_interval_min": max(1, i("SERVICE_CHECK_INTERVAL_MIN", 5)),
        "default_fail_threshold": max(1, i("SERVICE_CHECK_FAIL_THRESHOLD", 3)),
        "default_recovery_ok": max(1, i("SERVICE_CHECK_RECOVERY_OK", 3)),
        "p1_after_min": max(1, i("SERVICE_CHECK_P1_AFTER_MIN", 10)),
        "p2_priority": s("SERVICE_CHECK_P2_PRIORITY", "P2"),
        "p1_priority": s("SERVICE_CHECK_P1_PRIORITY", "P1"),
        "timeout_s": max(1, i("SERVICE_CHECK_TIMEOUT_S", 5)),
        "http_expected_status": max(100, i("SERVICE_CHECK_HTTP_EXPECTED_STATUS", 200)),
    }


def _read_env() -> dict:
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
    return env


# ── probe implementations ───────────────────────────────────────────────────

def resolve_target(session, monitor) -> str:
    """The host/IP a monitor actually probes. When target_device_id is set the
    device's current IP wins (a DHCP change is picked up automatically);
    otherwise the literal target string is used."""
    if monitor.target_device_id:
        d = session.get(Device, monitor.target_device_id)
        if d is None:
            return ""   # deleted device — caller disables the monitor
        return (d.ip_address or "").strip()
    return (monitor.target or "").strip()


def run_probe(monitor, target: str, cfg: dict = None) -> tuple:
    """Run one check against a RESOLVED target. Returns (ok: bool, detail: str).
    Never raises — failures are returned as (False, reason)."""
    cfg = cfg or service_check_config()
    ctype = (monitor.check_type or "ping").strip().lower()
    params = monitor.params or {}
    timeout = cfg["timeout_s"]

    if ctype == "ping":
        return _probe_ping(target, timeout)
    if ctype == "tcp":
        return _probe_tcp(target, params, timeout)
    if ctype == "http":
        return _probe_http(target, params, cfg, timeout)
    return False, f"unknown check_type: {ctype}"


def _probe_ping(target: str, timeout: int) -> tuple:
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(max(1, min(timeout, 10))), target],
            capture_output=True, timeout=timeout + 2)
        if r.returncode == 0:
            return True, "ping ok"
        return False, "no ping reply"
    except Exception as e:
        return False, f"ping error: {e}"


def _probe_tcp(target: str, params: dict, timeout: int) -> tuple:
    try:
        port = int(params.get("port"))
    except (TypeError, ValueError):
        return False, "TCP check missing a valid port"
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True, f"tcp connect ok ({target}:{port})"
    except Exception as e:
        return False, f"tcp connect failed ({target}:{port}): {e}"


def _probe_http(target: str, params: dict, cfg: dict, timeout: int) -> tuple:
    https = bool(params.get("https"))
    scheme = "https" if https else "http"
    path = str(params.get("path") or "/")
    if not path.startswith("/"):
        path = "/" + path
    url = f"{scheme}://{target}{path}"
    expected = int(params.get("expected_status") or cfg["http_expected_status"])
    body_contains = str(params.get("body_contains") or "")
    req = urllib.request.Request(url, headers={"User-Agent": "BareNOC-service-check"})
    ctx = ssl.create_default_context() if https else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = resp.status
            body = resp.read(65536).decode("utf-8", errors="replace")
            if status != expected:
                return False, f"HTTP {status} (expected {expected})"
            if body_contains and body_contains not in body:
                return False, f"body does not contain {body_contains!r}"
            return True, f"HTTP {status} ok"
    except urllib.error.HTTPError as e:
        if e.code == expected:
            return True, f"HTTP {e.code} ok"
        return False, f"HTTP {e.code} (expected {expected})"
    except Exception as e:
        return False, f"HTTP request failed: {e}"


# ── the monitor engine ──────────────────────────────────────────────────────

class ServiceCheckEngine:
    """State machine over the monitor table. See module docstring."""

    def __init__(self, probe_fn=None, now_fn=None, session_factory=SessionLocal):
        self._probe = probe_fn or run_probe
        self._now = now_fn or (lambda: datetime.datetime.utcnow())
        self._session_factory = session_factory

    def check(self) -> dict:
        """Run one pass over every ENABLED monitor. Returns a summary dict."""
        cfg = service_check_config()
        summary = {"checked": 0, "up": 0, "down": 0, "opened": 0,
                   "escalated": 0, "closed": 0, "disabled": 0}
        if not cfg["enabled"]:
            return summary
        session = self._session_factory()
        try:
            monitors = session.query(ServiceMonitor).order_by(ServiceMonitor.id).all()
            episodes = {e.monitor_id: e for e in session.query(ServiceCheckEpisode).all()}
            now = self._now()

            # Device-deleted cleanup: a monitor pointing at a removed device is
            # disabled (with a note) instead of erroring forever.
            summary["disabled"] += self._disable_deleted_device_refs(session, monitors, now)

            for m in monitors:
                if not m.enabled:
                    continue
                ep = episodes.get(m.id)
                self._advance(session, m, ep, cfg, now, summary)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("service check cycle error")
        finally:
            session.close()
        return summary

    # ── helpers ────────────────────────────────────────────────

    def _ticket(self, session, ep):
        if not ep or not ep.ticket_id:
            return None
        return session.query(Ticket).filter(Ticket.ticket_id == ep.ticket_id).first()

    def _disable_deleted_device_refs(self, session, monitors, now) -> int:
        n = 0
        for m in monitors:
            if not m.target_device_id:
                continue
            d = session.get(Device, m.target_device_id)
            if d is None:
                m.enabled = False
                m.last_error = "target device was deleted — monitor disabled"
                m.last_check_at = now
                ep = session.query(ServiceCheckEpisode).filter(
                    ServiceCheckEpisode.monitor_id == m.id).first()
                if ep:
                    self._close(session, m, ep, now,
                                reason="target device deleted — monitor disabled")
                n += 1
        return n

    def _advance(self, session, m, ep, cfg, now, summary):
        target = resolve_target(session, m)
        if not target:
            # no resolvable target (deleted device or blank target): keep the
            # monitor from accumulating a phantom streak.
            m.last_check_at = now
            m.last_error = "no target (set a host/IP or link a device)"
            return

        ok, detail = self._probe(m, target, cfg)
        m.last_check_at = now
        m.last_status = "up" if ok else "down"
        m.last_error = None if ok else detail
        if ok:
            summary["up"] += 1
        else:
            summary["down"] += 1
        summary["checked"] += 1

        if ok:
            m.fail_streak = 0
            m.ok_streak = min((m.ok_streak or 0) + 1, max(1, m.recovery_ok or cfg["default_recovery_ok"]))
            if ep is not None:
                ep.last_event_at = now
                if m.ok_streak >= (m.recovery_ok or cfg["default_recovery_ok"]):
                    self._close(session, m, ep, now)
                    summary["closed"] += 1
            return

        # failure
        m.ok_streak = 0
        m.fail_streak = (m.fail_streak or 0) + 1
        threshold = m.fail_threshold or cfg["default_fail_threshold"]
        if ep is None:
            if m.fail_streak >= threshold:
                self._open(session, m, cfg, now)
                summary["opened"] += 1
            return

        ep.last_event_at = now
        if ep.state != "outage" and ep.down_since is not None \
                and (now - ep.down_since).total_seconds() >= cfg["p1_after_min"] * 60:
            self._escalate(session, m, ep, cfg, now)
            summary["escalated"] += 1

    # ── ticket lifecycle transitions ───────────────────────────

    def _open(self, session, m, cfg, now):
        title = f"Service check failed: {m.name}"
        desc = (f"{m.name} is down — {m.check_type} check failed after "
                f"{m.fail_threshold or cfg['default_fail_threshold']} consecutive failures. "
                f"Target: {self._describe_target(session, m)}. Last error: {m.last_error or 'unknown'}")
        ticket = Ticket(
            ticket_id=generate_ticket_id(),
            title=title,
            description=desc,
            priority=cfg["p2_priority"],
            status="open", source="auto", assigned_to="system",
            target_device_id=m.target_device_id,
        )
        session.add(ticket)
        ep = ServiceCheckEpisode(
            monitor_id=m.id, state="down", down_since=now, last_event_at=now,
            escalated=cfg["p2_priority"], escalation_reason="threshold",
            ticket_id=ticket.ticket_id,
        )
        session.add(ep)
        log_event(session, "service_check_failed", "system", {
            "monitor_id": m.id, "monitor": m.name, "check_type": m.check_type,
            "target": self._describe_target(session, m),
            "ticket_id": ticket.ticket_id, "priority": cfg["p2_priority"],
        }, ticket.ticket_id)
        self._notify(m, f"[{cfg['p2_priority']}] BareNOC: {m.name} is down",
                     "Service check failed",
                     [("Monitor", m.name), ("Target", self._describe_target(session, m)),
                      ("Check", m.check_type), ("Detail", m.last_error or "down"),
                      ("Ticket", ticket.ticket_id)],
                     f"{m.name} is down ({m.check_type} check failed).")

    def _escalate(self, session, m, ep, cfg, now):
        ticket = self._ticket(session, ep)
        if ticket is not None:
            if ticket.priority != cfg["p1_priority"]:
                ticket.priority = cfg["p1_priority"]
            ticket.title = f"Service outage: {m.name}"
            add_note(ticket, "service_check_outage",
                     f"Sustained outage — still down more than {cfg['p1_after_min']} min "
                     f"after the ticket opened.")
            log_event(session, "service_check_escalate", "system", {
                "monitor_id": m.id, "monitor": m.name,
                "ticket_id": ticket.ticket_id, "priority": cfg["p1_priority"],
            }, ticket.ticket_id)
        ep.escalated = cfg["p1_priority"]
        ep.escalation_reason = "outage"
        ep.state = "outage"
        ep.updated_at = now
        self._notify(m, f"[{cfg['p1_priority']}] BareNOC: {m.name} is down (sustained)",
                     "Service outage",
                     [("Monitor", m.name), ("Target", self._describe_target(session, m)),
                      ("Check", m.check_type), ("Down for", f"> {cfg['p1_after_min']} min")],
                     f"{m.name} is down for more than {cfg['p1_after_min']} min.")

    def _close(self, session, m, ep, now, reason=None):
        ticket = self._ticket(session, ep)
        if ticket is not None:
            add_note(ticket, "service_check_recovered",
                     reason or (f"Recovered after {m.ok_streak or 0} consecutive "
                                f"successful checks — episode auto-closed."))
            ticket.status = "closed"
            ticket.resolution = reason or (f"Service recovered — {m.ok_streak or 0} "
                                           "consecutive successful checks")
            ticket.resolved_at = now
            log_event(session, "service_check_recovered", "system", {
                "monitor_id": m.id, "monitor": m.name, "ticket_id": ticket.ticket_id,
            }, ticket.ticket_id)
        session.delete(ep)
        self._notify(m, f"[CLOSED] BareNOC: {m.name} recovered",
                     "Service recovered",
                     [("Monitor", m.name), ("Target", self._describe_target(session, m)),
                      ("Check", m.check_type)],
                     f"{m.name} recovered.")

    def _describe_target(self, session, m) -> str:
        t = resolve_target(session, m)
        if m.target_device_id:
            d = session.get(Device, m.target_device_id)
            name = (d.name if d else "deleted device")
            return f"{name} ({t or '?'})"
        return t or "(unset)"

    def _notify(self, m, subject, title, rows, body_text):
        if not m.notify:
            return
        send_email(get_recipients("alerts"), subject,
                   body_html=alert_html(title, rows), body_text=body_text)
