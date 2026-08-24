#!/usr/bin/env python3
"""In-container tests for alert scope: only devices with notify_state_changes
opt-in get down/recovery emails (no more phone leave/join spam).

    docker compose exec api python3 -m unittest test_alerting -v
"""

import datetime
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="alert-scope-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device, Ticket
from alerting import AlertEngine, InternetMonitor, INTERNET_OUTAGE_TITLE


def _add(name, ip, status, notify=False):
    db = SessionLocal()
    d = Device(name=name, ip_address=ip, device_type="switch", status=status,
               claimed=True, notify_state_changes=notify)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


class AlertScopeTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()
        # device 1: NOT opted in — must never alert
        self.silent_id = _add("phone-wifi", "10.0.0.10", "online", notify=False)
        # device 2: opted in — alerts on down + recovery
        self.watch_id = _add("core-switch", "10.0.0.2", "online", notify=True)

    def _transition(self, device_id, to_status):
        db = SessionLocal()
        d = db.query(Device).get(device_id)
        d.status = to_status
        db.commit()
        db.close()

    def _patched_send(self):
        return patch("alerting.send_email", return_value=(True, ""))

    def test_only_opted_in_devices_alert(self):
        eng = AlertEngine()
        eng._last_alert_at = {}
        with self._patched_send() as send:
            eng._check_devices()            # seed: online
            self._transition(self.watch_id, "unreachable")
            self._transition(self.silent_id, "unreachable")  # phone drops too
            eng._check_devices()            # transition
        sends = [c.args for c in send.call_args_list]
        self.assertEqual(len(sends), 1, f"expected 1 alert, got {len(sends)}")
        self.assertIn("core-switch", str(sends))
        self.assertNotIn("phone-wifi", str(sends))

    def test_recovery_only_for_opted_in(self):
        eng = AlertEngine()
        eng._last_alert_at = {}
        with self._patched_send() as send:
            eng._check_devices()            # seed online
            self._transition(self.watch_id, "unreachable")
            self._transition(self.silent_id, "unreachable")
            eng._check_devices()            # down alert (1)
            self._transition(self.watch_id, "online")
            self._transition(self.silent_id, "online")
            eng._check_devices()            # recovery
        send_text = str([c.args for c in send.call_args_list])
        self.assertEqual(send.call_count, 2, send_text)
        self.assertNotIn("phone-wifi", send_text)


class InternetMonitorTest(unittest.TestCase):
    """Internet link probe → P1 outage ticket (dedup) → auto-close on recovery."""

    CFG = {"enabled": True, "gateway": "10.0.0.1", "host": "1.1.1.1",
           "interval": 0, "confirm": 2}

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.query(Ticket).delete()
        db.commit()
        db.close()
        self.mon = InternetMonitor()
        self.mon._last_email = time.time()  # no alert emails during tests

    def _probe(self, gw_ok: bool, inet_ok: bool):
        with patch("alerting._probe_config", return_value=dict(self.CFG)), \
             patch.object(self.mon, "_ping",
                          side_effect=lambda h: gw_ok if h == "10.0.0.1" else inet_ok):
            for _ in range(2):
                self.mon._last_check = 0
                self.mon.check()

    def _tickets(self):
        db = SessionLocal()
        rows = db.query(Ticket).filter(Ticket.title == INTERNET_OUTAGE_TITLE).all()
        db.close()
        return rows

    def test_isp_outage_opens_deduped_p1_and_closes_on_recovery(self):
        self._probe(True, True)    # online baseline
        self.assertEqual(self._tickets(), [])

        self._probe(True, False)   # gateway ok, internet dead → isp_down
        rows = self._tickets()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].priority, "P1")
        self.assertIn("ISP/service outage", rows[0].description)

        self._probe(True, False)   # still down → dedupe
        self.assertEqual(len(self._tickets()), 1)

        self._probe(True, True)    # recovered → auto-close
        rows = self._tickets()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "closed")

    def test_link_down_physical_outage(self):
        self._probe(True, True)    # baseline
        self._probe(False, False)  # gateway unreachable → link_down
        rows = self._tickets()
        self.assertEqual(len(rows), 1)
        self.assertIn("link/physical", rows[0].description)

    def test_flap_requires_confirmation(self):
        # a single bad probe must not open a ticket (confirm=2)
        with patch("alerting._probe_config", return_value=dict(self.CFG)), \
             patch.object(self.mon, "_ping", side_effect=lambda h: False):
            self.mon._last_check = 0
            self.mon.check()       # streak 1 — below confirm
        self.assertEqual(self._tickets(), [])
        self.assertIsNone(self.mon._state)


class TicketLifecycleTest(unittest.TestCase):
    """Settings → Tickets: per-priority check-ins + auto-close of resolved tickets."""

    CFG = {
        "checkin_enabled": True, "checkin_email": True, "autoclose_enabled": True,
        "checkin_hours": {"P1": 1, "P2": 4, "P3": 24, "P4": 24},
        "close_after_days": {"P1": 3, "P2": 3, "P3": 3, "P4": 3},
    }

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.query(Ticket).delete()
        db.commit()
        db.close()

    def _add(self, **kw):
        import json
        db = SessionLocal()
        t = Ticket(ticket_id=kw.pop("ticket_id"), title=kw.pop("title"),
                   priority=kw.pop("priority", "P3"), status=kw.pop("status", "open"),
                   created_at=kw.pop("created_at", datetime.datetime.utcnow()),
                   updated_at=kw.pop("updated_at", datetime.datetime.utcnow()),
                   resolved_at=kw.pop("resolved_at", None),
                   work_notes=json.dumps(kw.pop("notes", [])))
        db.add(t)
        db.commit()
        tid = t.id
        db.close()
        return tid

    def test_autoclose_only_after_resolved_days(self):
        now = datetime.datetime.utcnow()
        # resolved 4 days ago -> closes (P3 close_after_days=3)
        self._add(ticket_id="TKT-T1", title="old resolved", priority="P3", status="completed",
                  resolved_at=now - datetime.timedelta(days=4))
        # resolved 1 day ago -> stays open
        self._add(ticket_id="TKT-T2", title="fresh resolved", priority="P3", status="completed",
                  resolved_at=now - datetime.timedelta(days=1))
        eng = AlertEngine()
        with patch("alerting._ticket_lifecycle_config", return_value=dict(self.CFG)), \
             patch("alerting.send_email", return_value=(True, "")):
            eng._check_ticket_lifecycle()
        db = SessionLocal()
        t1 = db.query(Ticket).filter(Ticket.ticket_id == "TKT-T1").first()
        t2 = db.query(Ticket).filter(Ticket.ticket_id == "TKT-T2").first()
        db.close()
        self.assertEqual(t1.status, "closed")
        self.assertEqual(t2.status, "completed")

    def test_checkin_interval_and_dedupe(self):
        import json
        cfg = dict(self.CFG)
        cfg["checkin_hours"] = {"P1": 1, "P2": 4, "P3": 1, "P4": 24}  # P3 pokes hourly
        now = datetime.datetime.utcnow()
        self._add(ticket_id="TKT-T3", title="with human", priority="P3", status="escalated",
                  updated_at=now - datetime.timedelta(hours=2))
        eng = AlertEngine()
        with patch("alerting._ticket_lifecycle_config", return_value=cfg), \
             patch("alerting.send_email", return_value=(True, "")):
            eng._check_ticket_lifecycle()
            eng._check_ticket_lifecycle()   # immediate second run — must not re-poke
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.ticket_id == "TKT-T3").first()
        notes = json.loads(t.work_notes or "[]")
        db.close()
        checkins = [n for n in notes if n.get("event") == "checkin_request"]
        self.assertEqual(len(checkins), 1, str(notes))

    def test_zero_interval_disables_checkin(self):
        cfg = dict(self.CFG)
        cfg["checkin_hours"] = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
        now = datetime.datetime.utcnow()
        self._add(ticket_id="TKT-T4", title="stale but unmanaged", priority="P4",
                  status="customer_action",
                  updated_at=now - datetime.timedelta(days=5))
        eng = AlertEngine()
        with patch("alerting._ticket_lifecycle_config", return_value=cfg):
            eng._check_ticket_lifecycle()
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.ticket_id == "TKT-T4").first()
        db.close()
        self.assertNotIn("checkin_request", t.work_notes or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
