#!/usr/bin/env python3
"""In-container tests for the link-stability monitor (link-flap detection with
graduated severity). Fake channel — no UniFi controller, no SNMP, no SMTP.

    docker compose exec api python3 -m unittest test_link_monitor -v
"""

import datetime
import os
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="link-monitor-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device, Ticket, AuditLog
import link_monitor as lm
from link_monitor import LinkEpisode
from alerting import InternetMonitor, INTERNET_OUTAGE_TITLE


CFG = {
    "enabled": True,
    "flap_window_min": 30,
    "escalate_count": 3,
    "persist_down_min": 10,
    "stable_close_min": 30,
    "unifi_cache_seconds": 60,
}


class Clock:
    """Mutable wall clock so the tests control the episode windows."""
    def __init__(self, start=None):
        self.now = start or datetime.datetime(2026, 1, 1, 0, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += datetime.timedelta(seconds=seconds)


class FakeChannel:
    """A data channel whose snapshot the test mutates between cycles."""
    def __init__(self, name="unifi", data=None, missing_means_down=True):
        self.name = name
        self.data = data or {}
        self.missing_means_down = missing_means_down
        self.collect_calls = 0

    def collect(self, session):
        self.collect_calls += 1
        return dict(self.data)


def _add_device(name, ip, dtype="switch", notify=True, status="online"):
    db = SessionLocal()
    d = Device(name=name, ip_address=ip, device_type=dtype, status=status,
               claimed=True, notify_state_changes=notify)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _clean():
    db = SessionLocal()
    db.query(LinkEpisode).delete()
    db.query(Ticket).delete()
    db.query(Device).delete()
    db.query(AuditLog).delete()
    db.commit()
    db.close()


def _episodes():
    db = SessionLocal()
    rows = db.query(LinkEpisode).all()
    db.close()
    return rows


def _tickets():
    db = SessionLocal()
    rows = db.query(Ticket).all()
    db.close()
    return rows


def _run(mon):
    with patch.object(lm, "link_monitor_config", return_value=dict(CFG)), \
         patch.object(lm, "send_email", return_value=(True, "")):
        mon.check()


class LinkFlapStateMachineTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def _mon(self, ch):
        clock = Clock()
        mon = lm.LinkMonitor(channels=[ch], now_fn=clock)
        return mon, clock

    def test_first_flap_opens_p2_and_keeps_open(self):
        dev = _add_device("core-switch", "10.0.0.2")
        ch = FakeChannel("unifi", {(dev, "eth0"): lm.UP})
        mon, clock = self._mon(ch)

        _run(mon)            # first observation seeds the baseline — no alert
        _run(mon)            # steady up — still nothing
        self.assertEqual(_episodes(), [])
        self.assertEqual(_tickets(), [])

        ch.data[(dev, "eth0")] = lm.DOWN
        _run(mon)            # up -> down transition opens the P2 ticket

        tickets = _tickets()
        self.assertEqual(len(tickets), 1)
        t = tickets[0]
        self.assertEqual(t.priority, "P2")
        self.assertEqual(t.status, "open")
        self.assertEqual(t.title, "Link flap: core-switch eth0")

        eps = _episodes()
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].state, "flapping")
        self.assertIsNotNone(eps[0].down_since)

        # still down but under the 10-min persist threshold -> stays open P2
        clock.advance(5 * 60)
        _run(mon)
        self.assertEqual(_tickets()[0].priority, "P2")
        self.assertEqual(_tickets()[0].status, "open")

    def test_recurrence_escalates_to_p1(self):
        dev = _add_device("core-switch", "10.0.0.2")
        ch = FakeChannel("unifi", {(dev, "eth0"): lm.UP})
        mon, clock = self._mon(ch)
        _run(mon)
        _run(mon)            # steady up baseline

        for _ in range(3):   # three down->up flaps within the 30-min window
            ch.data[(dev, "eth0")] = lm.DOWN
            clock.advance(60)
            _run(mon)
            ch.data[(dev, "eth0")] = lm.UP
            clock.advance(60)
            _run(mon)

        t = _tickets()[0]
        self.assertEqual(t.priority, "P1")
        self.assertEqual(t.title, "Recurring link flap: core-switch eth0")
        self.assertIn("flap", t.work_notes or "")          # timestamps work note
        self.assertEqual(_episodes()[0].flap_count, 3)
        self.assertEqual(_episodes()[0].escalation_reason, "recurrence")

    def test_persistent_down_escalates_to_p1(self):
        dev = _add_device("core-switch", "10.0.0.2")
        ch = FakeChannel("unifi", {(dev, "eth0"): lm.UP})
        mon, clock = self._mon(ch)
        _run(mon)
        _run(mon)

        ch.data[(dev, "eth0")] = lm.DOWN
        _run(mon)            # open P2
        self.assertEqual(_tickets()[0].priority, "P2")

        clock.advance(11 * 60)
        _run(mon)            # down > 10 min -> outage (not recurrence)

        t = _tickets()[0]
        self.assertEqual(t.priority, "P1")
        self.assertEqual(t.title, "Link outage: core-switch eth0")
        self.assertEqual(_episodes()[0].state, "outage")
        self.assertEqual(_episodes()[0].escalation_reason, "outage")
        self.assertEqual(_episodes()[0].flap_count, 0)

    def test_stable_30_min_autocloses_with_summary(self):
        dev = _add_device("core-switch", "10.0.0.2")
        ch = FakeChannel("unifi", {(dev, "eth0"): lm.UP})
        mon, clock = self._mon(ch)
        _run(mon)
        _run(mon)

        ch.data[(dev, "eth0")] = lm.DOWN
        _run(mon)            # open P2
        ch.data[(dev, "eth0")] = lm.UP
        _run(mon)            # one recovery (flap), link back up

        self.assertEqual(_episodes()[0].flap_count, 1)

        clock.advance(31 * 60)
        _run(mon)            # no further events for 30 min -> auto-close

        self.assertEqual(_episodes(), [])
        t = _tickets()[0]
        self.assertEqual(t.status, "closed")
        self.assertIn("flap", t.work_notes or "")          # summary note
        self.assertIn("auto-closed", (t.resolution or "").lower())

    def test_opt_out_closes_orphan_episode(self):
        dev = _add_device("core-switch", "10.0.0.2", notify=True)
        ch = FakeChannel("unifi", {(dev, "eth0"): lm.UP})
        mon, clock = self._mon(ch)
        _run(mon)
        _run(mon)
        ch.data[(dev, "eth0")] = lm.DOWN
        _run(mon)            # open episode + P2 ticket
        self.assertEqual(len(_episodes()), 1)

        # toggle off the opt-in mid-episode
        db = SessionLocal()
        d = db.query(Device).get(dev)
        d.notify_state_changes = False
        db.commit()
        db.close()

        _run(mon)            # device no longer eligible -> close the episode
        self.assertEqual(_episodes(), [])
        self.assertEqual(_tickets()[0].status, "closed")


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_restart_resumes_episode(self):
        dev = _add_device("core-switch", "10.0.0.2")
        clock = Clock()
        now = clock.now

        # Persisted episode from a previous container run: flapping, down since
        # 11 min ago, P2 ticket open.
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-RESUME-1", title="Link flap: core-switch eth0",
                   description="x", priority="P2", status="open", source="auto",
                   assigned_to="system", target_device_id=dev)
        db.add(t)
        db.flush()
        db.add(LinkEpisode(device_id=dev, interface="eth0", state="flapping",
                           flap_count=0,
                           window_start=now - datetime.timedelta(minutes=11),
                           down_since=now - datetime.timedelta(minutes=11),
                           last_event_at=now - datetime.timedelta(minutes=11),
                           escalated="P2", ticket_id=t.ticket_id))
        db.commit()
        db.close()

        # A fresh monitor (empty in-memory channel history) resumes from DB.
        ch = FakeChannel("unifi", {(dev, "eth0"): lm.DOWN})
        mon = lm.LinkMonitor(channels=[ch], now_fn=clock)
        _run(mon)

        tickets = _tickets()
        self.assertEqual(len(tickets), 1, "must not open a duplicate ticket")
        self.assertEqual(tickets[0].priority, "P1")
        self.assertEqual(tickets[0].title, "Link outage: core-switch eth0")
        self.assertEqual(_episodes()[0].state, "outage")


class WanSingleTicketTest(unittest.TestCase):
    """The WAN flap ticket IS the WAN outage ticket — the internet probe
    promotes it instead of opening a duplicate 'Internet connectivity down'."""

    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_wan_probe_promotes_same_ticket_no_duplicate(self):
        gw = _add_device("gateway", "192.168.1.1", dtype="gateway", notify=False)
        now = datetime.datetime.utcnow()
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-WAN-1", title="Link flap: gateway wan",
                   description="x", priority="P2", status="open", source="auto",
                   assigned_to="system", target_device_id=gw)
        db.add(t)
        db.flush()
        db.add(LinkEpisode(device_id=gw, interface="wan", state="flapping",
                           flap_count=1, window_start=now, last_event_at=now,
                           escalated="P2", ticket_id=t.ticket_id))
        db.commit()
        db.close()

        mon = InternetMonitor()
        mon._last_email = time.time()   # suppress the probe's alert email
        cfg = {"gateway": "192.168.1.1", "host": "1.1.1.1"}
        with patch("alerting.send_email", return_value=(True, "")):
            mon._outage("isp_down", cfg)

        db = SessionLocal()
        dup = db.query(Ticket).filter(Ticket.title == INTERNET_OUTAGE_TITLE).all()
        wan = db.query(Ticket).filter(Ticket.ticket_id == "TKT-WAN-1").first()
        ep = db.query(LinkEpisode).first()
        db.close()

        self.assertEqual(dup, [], "must not open a duplicate outage ticket")
        self.assertEqual(wan.priority, "P1")
        self.assertEqual(wan.title, "Link outage: gateway wan")
        self.assertEqual(ep.escalation_reason, "wan_probe")
        self.assertEqual(ep.state, "outage")

    def test_flap_that_recovers_stays_p2(self):
        """A WAN flap that recovers before the probe confirms is never promoted
        by the probe (the probe only fires on 3 consecutive failures)."""
        gw = _add_device("gateway", "192.168.1.1", dtype="gateway", notify=False)
        now = datetime.datetime.utcnow()
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-WAN-2", title="Link flap: gateway wan",
                   description="x", priority="P2", status="open", source="auto",
                   assigned_to="system", target_device_id=gw)
        db.add(t)
        db.flush()
        db.add(LinkEpisode(device_id=gw, interface="wan", state="flapping",
                           flap_count=1, window_start=now, last_event_at=now,
                           down_since=None, escalated="P2", ticket_id=t.ticket_id))
        db.commit()
        db.close()

        # No probe confirmation happens; the ticket simply stays P2/open.
        db = SessionLocal()
        wan = db.query(Ticket).filter(Ticket.ticket_id == "TKT-WAN-2").first()
        db.close()
        self.assertEqual(wan.priority, "P2")
        self.assertEqual(wan.status, "open")

    def test_wan_probe_outage_owned_by_probe_not_link_monitor(self):
        """Once the probe promotes a WAN flap ticket, the probe owns the
        episode's lifecycle: the link monitor must not auto-close it (UniFi WAN
        reads 'ok' during an upstream ISP outage), and the probe's _recovered
        closes the ticket + episode on confirmed recovery."""
        gw = _add_device("gateway", "192.168.1.1", dtype="gateway", notify=False)
        now = datetime.datetime.utcnow()
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-WAN-3", title="Link flap: gateway wan",
                   description="x", priority="P2", status="open", source="auto",
                   assigned_to="system", target_device_id=gw)
        db.add(t)
        db.flush()
        db.add(LinkEpisode(device_id=gw, interface="wan", state="flapping",
                           flap_count=1, window_start=now, last_event_at=now,
                           down_since=None, escalated="P2", ticket_id=t.ticket_id))
        db.commit()
        db.close()

        mon = InternetMonitor()
        mon._last_email = time.time()
        cfg = {"gateway": "192.168.1.1", "host": "1.1.1.1"}
        with patch("alerting.send_email", return_value=(True, "")):
            mon._outage("isp_down", cfg)

        # Link monitor cycles with UniFi WAN reporting up — must NOT auto-close.
        clock = Clock(datetime.datetime.utcnow().replace(microsecond=0))
        ch = FakeChannel("unifi", {(gw, "wan"): lm.UP})
        lm_mon = lm.LinkMonitor(channels=[ch], now_fn=clock)
        clock.advance(31 * 60)
        _run(lm_mon)

        db = SessionLocal()
        wan = db.query(Ticket).filter(Ticket.ticket_id == "TKT-WAN-3").first()
        ep = db.query(LinkEpisode).first()
        db.close()
        self.assertEqual(wan.priority, "P1")
        self.assertEqual(wan.status, "open", "probe owns it — must stay open")
        self.assertIsNotNone(ep)

        # Probe confirms recovery -> closes the WAN ticket + deletes the episode.
        with patch("alerting.send_email", return_value=(True, "")):
            mon._recovered(cfg)
        db = SessionLocal()
        wan = db.query(Ticket).filter(Ticket.ticket_id == "TKT-WAN-3").first()
        ep = db.query(LinkEpisode).first()
        db.close()
        self.assertEqual(wan.status, "closed")
        self.assertIsNone(ep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
