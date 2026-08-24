#!/usr/bin/env python3
"""In-container tests for the Starlink dish gRPC collector + link-health monitor.

Covers: the grpc payload normalization (fixture, no live dish), metric
extraction, gap handling (unreachable -> no samples), the threshold classifier,
and the graduated-ticket lifecycle (degraded -> P2 -> outage -> P1 ->
recovery -> close) against a scratch sqlite DB. No grpc / starlink-grpc-core
needed — the client is faked.

    docker compose exec api python3 -m unittest test_starlink -v
"""

import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="starlink-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device, Ticket, Metric, AuditLog, StarlinkEpisode, User
import starlink as st


# ── fixtures ────────────────────────────────────────────────────────────────

def status_payload(**overrides):
    """Mimics starlink_grpc.status_data()'s status dict."""
    base = {
        "id": "ut01000000-00000000-00012345",
        "hardware_version": "rev3_proto2",
        "software_version": "2024.01.01.mr1234",
        "state": "CONNECTED",
        "uptime": 3600,
        "pop_ping_drop_rate": 0.0,
        "downlink_throughput_bps": 150_000_000.0,
        "uplink_throughput_bps": 25_000_000.0,
        "pop_ping_latency_ms": 30.0,
        "is_snr_above_noise_floor": True,
        "fraction_obstructed": 0.01,
        "currently_obstructed": False,
    }
    base.update(overrides)
    return base


HEALTHY = {"state": "CONNECTED", "link_up": True, "ping_ms": 30.0,
           "down_mbps": 150.0, "up_mbps": 25.0, "snr": 1.0,
           "obstructed": 0.0, "obstruction_fraction": 0.01,
           "uptime_seconds": 100.0, "ping_drop_rate": 0.0}
DEGRADED = {"state": "CONNECTED", "link_up": True, "ping_ms": 220.0,
            "down_mbps": 5.0, "up_mbps": 25.0, "snr": 1.0,
            "obstructed": 0.0, "obstruction_fraction": 0.01,
            "uptime_seconds": 100.0, "ping_drop_rate": 0.0}
OUTAGE = {"state": "SEARCHING", "link_up": False, "ping_ms": None,
          "down_mbps": 0.0, "up_mbps": 0.0, "snr": 0.0,
          "obstructed": 0.0, "obstruction_fraction": 0.0,
          "uptime_seconds": 100.0, "ping_drop_rate": 1.0}

CFG = {
    "enabled": True,
    "address": "192.168.100.1:9200",
    "interval": 60,
    "timeout_s": 5,
    "ping_degrade_ms": 150.0,
    "snr_min": 1.0,
    "down_min_mbps": 10.0,
    "up_min_mbps": 2.0,
    "obstruction_max": 0.2,
    "degrade_window_min": 5,
    "outage_window_min": 3,
    "recover_window_min": 10,
}


class Clock:
    def __init__(self, start=None):
        self.now = start or datetime.datetime(2026, 1, 1, 0, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += datetime.timedelta(seconds=seconds)


class FakeClient:
    """Injected gRPC client — returns whatever the test sets."""
    def __init__(self, result=None):
        self.result = result
        self.calls = 0
        self.address = st.DEFAULT_ADDRESS
        self.timeout_s = st.DEFAULT_TIMEOUT

    def fetch_status(self):
        self.calls += 1
        return self.result


class Feed:
    """Mutable get_snapshot replacement (like link_monitor's FakeChannel)."""
    def __init__(self, snapshot=None):
        self.snapshot = snapshot

    def __call__(self, cfg, client=None, force=False):
        return self.snapshot


def _clean():
    db = SessionLocal()
    for t in (Metric, StarlinkEpisode, Ticket, Device, AuditLog):
        db.query(t).delete()
    db.commit()
    db.close()


def _tickets():
    db = SessionLocal()
    rows = db.query(Ticket).all()
    db.close()
    return rows


def _episodes():
    db = SessionLocal()
    rows = db.query(StarlinkEpisode).all()
    db.close()
    return rows


# ═══════════════════════════════ normalization ══════════════════════════════

class NormalizeTest(unittest.TestCase):
    def test_connected_payload(self):
        s = st.normalize_status(status_payload())
        self.assertTrue(s["link_up"])
        self.assertEqual(s["state"], "CONNECTED")
        self.assertEqual(s["ping_ms"], 30.0)
        self.assertEqual(s["down_mbps"], 150.0)
        self.assertEqual(s["up_mbps"], 25.0)
        self.assertEqual(s["snr"], 1.0)
        self.assertEqual(s["obstructed"], 0.0)
        self.assertEqual(s["obstruction_fraction"], 0.01)
        self.assertEqual(s["uptime_seconds"], 3600)
        self.assertEqual(s["ping_drop_rate"], 0.0)

    def test_searching_payload_is_link_down(self):
        s = st.normalize_status(status_payload(state="SEARCHING"))
        self.assertFalse(s["link_up"])

    def test_missing_fields_become_none(self):
        s = st.normalize_status({})
        self.assertIsNone(s["ping_ms"])
        self.assertIsNone(s["down_mbps"])
        self.assertIsNone(s["snr"])
        self.assertEqual(s["state"], "UNKNOWN")

    def test_nonfinite_values_become_none(self):
        s = st.normalize_status(status_payload(pop_ping_latency_ms=float("nan"),
                                                downlink_throughput_bps=float("inf")))
        self.assertIsNone(s["ping_ms"])
        self.assertIsNone(s["down_mbps"])


# ═══════════════════════════════ metrics ════════════════════════════════════

class CollectTest(unittest.TestCase):
    def test_collect_metric_names_and_values(self):
        samples = st.collect_starlink_metrics(7, HEALTHY)
        by = {s["metric"]: s for s in samples}
        self.assertEqual(by["starlink.ping_ms"]["value"], 30.0)
        self.assertEqual(by["starlink.link_up"]["value"], 1.0)
        self.assertEqual(by["starlink.down_mbps"]["value"], 150.0)
        self.assertEqual(by["starlink.up_mbps"]["value"], 25.0)
        self.assertEqual(by["starlink.snr"]["value"], 1.0)
        self.assertEqual(by["starlink.obstructed"]["value"], 0.0)
        self.assertEqual(by["starlink.obstruction_fraction"]["value"], 0.01)
        self.assertEqual(by["starlink.uptime_seconds"]["value"], 100.0)
        self.assertEqual(by["starlink.ping_drop_rate"]["value"], 0.0)
        self.assertTrue(all(s["kind"] == "gauge" for s in samples))

    def test_none_fields_are_skipped(self):
        snap = dict(HEALTHY, ping_ms=None)
        samples = st.collect_starlink_metrics(7, snap)
        self.assertNotIn("starlink.ping_ms", {s["metric"] for s in samples})
        self.assertIn("starlink.down_mbps", {s["metric"] for s in samples})


# ═══════════════════════════════ classify ═══════════════════════════════════

class ClassifyTest(unittest.TestCase):
    def test_healthy(self):
        state, reasons = st.classify(HEALTHY, CFG)
        self.assertEqual(state, "healthy")
        self.assertEqual(reasons, [])

    def test_ping_degrade(self):
        state, reasons = st.classify(dict(HEALTHY, ping_ms=220.0), CFG)
        self.assertEqual(state, "degraded")
        self.assertTrue(any("ping" in r for r in reasons))

    def test_snr_degrade(self):
        state, _ = st.classify(dict(HEALTHY, snr=0.0), CFG)
        self.assertEqual(state, "degraded")

    def test_throughput_degrade(self):
        state, _ = st.classify(dict(HEALTHY, down_mbps=5.0), CFG)
        self.assertEqual(state, "degraded")
        state, _ = st.classify(dict(HEALTHY, up_mbps=1.0), CFG)
        self.assertEqual(state, "degraded")

    def test_obstruction_degrade(self):
        state, _ = st.classify(dict(HEALTHY, obstruction_fraction=0.5), CFG)
        self.assertEqual(state, "degraded")

    def test_outage_wins_over_degradation(self):
        state, reasons = st.classify(OUTAGE, CFG)
        self.assertEqual(state, "outage")
        self.assertIn("link down", reasons[0])

    def test_gap_link_up_none_is_not_outage(self):
        # A missing link_up (gap) must not read as an outage.
        snap = dict(HEALTHY, link_up=None)
        self.assertEqual(st.classify(snap, CFG)[0], "healthy")


# ═══════════════════════════════ snapshot cache / gaps ══════════════════════

class SnapshotGapTest(unittest.TestCase):
    def tearDown(self):
        st._CACHE["snapshot"] = None
        st._CACHE["fetched_at"] = 0.0

    def test_unreachable_returns_none_not_crash(self):
        fake = FakeClient(result=None)
        self.assertIsNone(st.get_snapshot(CFG, client=fake, force=True))

    def test_snapshot_cached_for_interval(self):
        fake = FakeClient(result=HEALTHY)
        self.assertEqual(st.get_snapshot(CFG, client=fake), HEALTHY)
        self.assertEqual(st.get_snapshot(CFG, client=fake), HEALTHY)
        self.assertEqual(fake.calls, 1)          # second call served from cache
        st.get_snapshot(CFG, client=fake, force=True)
        self.assertEqual(fake.calls, 2)


# ═══════════════════════════════ health monitor ═════════════════════════════

class HealthMonitorTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()
        st._CACHE["snapshot"] = None
        st._CACHE["fetched_at"] = 0.0

    def _mon(self, feed, clock):
        mon = st.StarlinkHealthMonitor(now_fn=clock)
        mon._last_email = 1_000_000_000.0  # suppress emails in tests
        return mon

    def _run(self, mon, feed, clock):
        with patch.object(st, "starlink_config", return_value=dict(CFG)), \
             patch.object(st, "get_snapshot", side_effect=feed), \
             patch.object(st, "send_email", return_value=(True, "")):
            mon.check()

    def test_first_observation_seeds_no_ticket(self):
        clock = Clock()
        feed = Feed(snapshot=OUTAGE)   # even a bad first sighting is just a baseline
        mon = self._mon(feed, clock)
        self._run(mon, feed, clock)
        self.assertEqual(_tickets(), [])
        self.assertEqual(len(_episodes()), 1)
        self.assertEqual(_episodes()[0].state, "outage")

    def test_unreachable_gap_leaves_state_untouched(self):
        clock = Clock()
        feed = Feed(snapshot=HEALTHY)
        mon = self._mon(feed, clock)
        self._run(mon, feed, clock)
        self.assertEqual(len(_episodes()), 1)
        feed.snapshot = None
        self._run(mon, feed, clock)   # gap — must not crash, no ticket
        self.assertEqual(_tickets(), [])
        self.assertEqual(len(_episodes()), 1)

    def test_degraded_opens_p2_then_outage_escalates_same_ticket_then_recovers(self):
        clock = Clock()
        feed = Feed(snapshot=HEALTHY)
        mon = self._mon(feed, clock)

        self._run(mon, feed, clock)          # seed healthy baseline

        feed.snapshot = DEGRADED
        self._run(mon, feed, clock)          # starts the degradation window
        clock.advance(6 * 60)
        self._run(mon, feed, clock)          # sustained -> P2

        tickets = _tickets()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].priority, "P2")
        self.assertEqual(tickets[0].status, "open")
        self.assertEqual(tickets[0].title, "Starlink link degraded")
        self.assertEqual(_episodes()[0].state, "degraded")
        self.assertEqual(_episodes()[0].escalated, "P2")

        feed.snapshot = OUTAGE
        self._run(mon, feed, clock)          # link down begins
        clock.advance(4 * 60)
        self._run(mon, feed, clock)          # sustained outage -> P1 (SAME ticket)

        tickets = _tickets()
        self.assertEqual(len(tickets), 1, "must escalate the same ticket, not open a new one")
        self.assertEqual(tickets[0].priority, "P1")
        self.assertEqual(tickets[0].title, "Starlink link outage")
        self.assertEqual(_episodes()[0].state, "outage")
        self.assertEqual(_episodes()[0].escalated, "P1")
        self.assertEqual(_episodes()[0].escalation_reason, "outage")

        feed.snapshot = HEALTHY
        self._run(mon, feed, clock)          # recovery begins
        clock.advance(11 * 60)
        self._run(mon, feed, clock)          # sustained recovery -> auto-close

        self.assertEqual(_episodes(), [])
        self.assertEqual(_tickets()[0].status, "closed")
        self.assertIn("auto-closed", (_tickets()[0].resolution or "").lower())

    def test_short_blip_does_not_ticket(self):
        clock = Clock()
        feed = Feed(snapshot=HEALTHY)
        mon = self._mon(feed, clock)
        self._run(mon, feed, clock)
        feed.snapshot = DEGRADED
        self._run(mon, feed, clock)
        feed.snapshot = HEALTHY
        clock.advance(60)
        self._run(mon, feed, clock)          # recovered well inside the window
        self.assertEqual(_tickets(), [])


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_restart_resumes_and_escalates(self):
        clock = Clock()
        now = clock.now
        db = SessionLocal()
        d = Device(name="Starlink Dish", ip_address="192.168.100.1",
                   device_type="dish", vendor="Starlink", claimed=True)
        db.add(d)
        db.flush()
        t = Ticket(ticket_id="TKT-SL-1", title="Starlink link degraded",
                   description="x", priority="P2", status="open", source="auto",
                   assigned_to="system", target_device_id=d.id)
        db.add(t)
        db.flush()
        db.add(StarlinkEpisode(device_id=d.id, state="degraded",
                               degraded_since=now - datetime.timedelta(minutes=6),
                               last_event_at=now - datetime.timedelta(minutes=6),
                               escalated="P2", escalation_reason="degraded",
                               ticket_id=t.ticket_id))
        db.commit()
        db.close()

        # Fresh monitor resumes from the DB; the first sighting starts the
        # outage window (the episode was degraded, not yet outage, at restart).
        feed = Feed(snapshot=OUTAGE)
        mon = st.StarlinkHealthMonitor(now_fn=clock)
        mon._last_email = 1_000_000_000.0

        def run():
            with patch.object(st, "starlink_config", return_value=dict(CFG)), \
                 patch.object(st, "get_snapshot", side_effect=feed), \
                 patch.object(st, "send_email", return_value=(True, "")):
                mon.check()

        run()
        tickets = _tickets()
        self.assertEqual(len(tickets), 1, "must not open a duplicate ticket")
        self.assertEqual(tickets[0].ticket_id, "TKT-SL-1")
        self.assertEqual(tickets[0].priority, "P2")   # outage window not elapsed yet

        clock.advance(4 * 60)
        run()

        tickets = _tickets()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].ticket_id, "TKT-SL-1")
        self.assertEqual(tickets[0].priority, "P1")
        self.assertEqual(_episodes()[0].escalation_reason, "outage")


# ═══════════════════════════════ dish device ════════════════════════════════

class DeviceTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_ensure_dish_device_find_or_create(self):
        db = SessionLocal()
        first = st.ensure_dish_device(db, "192.168.100.1:9200")
        second = st.ensure_dish_device(db, "192.168.100.1:9200")
        db.close()
        self.assertEqual(first, second)
        db = SessionLocal()
        d = db.get(Device, first)
        self.assertEqual(d.device_type, "dish")
        self.assertEqual(d.ip_address, "192.168.100.1")
        self.assertTrue(d.claimed)
        db.close()

    def test_phantom_dish_purged_when_unconfigured(self):
        """A dish record on a box with NO explicit STARLINK_ADDRESS is a phantom
        (the default 192.168.100.1 was never a real dish) — the purge removes it
        (the 08-20 fabrication bug: every appliance claimed a 'Starlink Dish')."""
        db = SessionLocal()
        pid = st.ensure_dish_device(db, "192.168.100.1:9200")
        self.assertIsNotNone(db.get(Device, pid))
        st._purge_phantom_dish(db, "192.168.100.1:9200")
        self.assertIsNone(db.get(Device, pid))
        db.close()

    def test_configured_dish_survives_unreachable_purge(self):
        """An explicitly-configured dish keeps its record even when temporarily
        unreachable — the purge only removes no-config phantoms."""
        db = SessionLocal()
        pid = st.ensure_dish_device(db, "192.168.100.1:9200")
        old = os.environ.get("STARLINK_ADDRESS")
        os.environ["STARLINK_ADDRESS"] = "192.168.100.1:9200"
        try:
            st._purge_phantom_dish(db, "192.168.100.1:9200")
        finally:
            if old is None:
                os.environ.pop("STARLINK_ADDRESS", None)
            else:
                os.environ["STARLINK_ADDRESS"] = old
        self.assertIsNotNone(db.get(Device, pid))
        db.close()

    def test_startup_purge_removes_phantom_even_when_disabled(self):
        """The startup sweep removes a phantom dish record even when the
        telemetry collector is disabled (the 08-24 fix: the collector-only
        purge never ran on boxes with STARLINK_ENABLED=false, so the
        fabricated record survived updates — forum thread 9eaa106e)."""
        db = SessionLocal()
        pid = st.ensure_dish_device(db, "192.168.100.1:9200")
        db.close()
        self.assertIsNotNone(db.get(Device, pid))
        old = os.environ.get("STARLINK_ENABLED")
        os.environ["STARLINK_ENABLED"] = "false"
        try:
            st.purge_phantom_dish_at_startup()
        finally:
            if old is None:
                os.environ.pop("STARLINK_ENABLED", None)
            else:
                os.environ["STARLINK_ENABLED"] = old
        self.assertIsNone(db.get(Device, pid))


# ═══════════════════════════════ route gating ═══════════════════════════════

class GatingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        from auth import hash_password
        db = SessionLocal()
        for name, role in (("sl_admin", "admin"), ("sl_op", "operator"), ("sl_ro", "readonly")):
            db.add(User(username=name, role=role,
                        hashed_password=hash_password("pw"), is_active=True))
        db.commit()
        db.close()
        self.op = type("U", (), {"username": "sl_op", "role": "operator"})()
        self.ro = type("U", (), {"username": "sl_ro", "role": "readonly"})()

    def tearDown(self):
        _clean()

    def _client(self, user):
        from main import app
        from fastapi.testclient import TestClient
        from auth import create_access_token
        token = create_access_token({"sub": user.username, "role": user.role,
                                     "groups": [], "auth_method": "password"})
        return TestClient(app), token

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_status_requires_operator(self):
        client, token = self._client(self.op)
        self.assertEqual(client.get("/api/v1/starlink/status",
                                    headers=self._auth(token)).status_code, 200)
        client2, token2 = self._client(self.ro)
        self.assertEqual(client2.get("/api/v1/starlink/status",
                                     headers=self._auth(token2)).status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
