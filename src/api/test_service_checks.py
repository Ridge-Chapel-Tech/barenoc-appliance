#!/usr/bin/env python3
"""In-container tests for Service Checks (ping/TCP/HTTP monitors → tickets).

Fake probe — no real network I/O in the state-machine tests (the injected
clock drives the graduated P2 → P1 → auto-close lifecycle, like link-flap).
The TCP/HTTP probe helpers get deterministic local socket/HTTP-server tests.

    docker compose exec api python3 -m unittest test_service_checks -v
"""

import datetime
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="service-checks-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import User, Device, Ticket, AuditLog, ServiceMonitor, ServiceCheckEpisode
import service_checks as sc


CFG = {
    "enabled": True,
    "default_interval_min": 5,
    "default_fail_threshold": 3,
    "default_recovery_ok": 3,
    "p1_after_min": 10,
    "p2_priority": "P2",
    "p1_priority": "P1",
    "timeout_s": 5,
    "http_expected_status": 200,
}


class Clock:
    def __init__(self, start=None):
        self.now = start or datetime.datetime(2026, 1, 1, 0, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += datetime.timedelta(seconds=seconds)


class FakeProbe:
    """Probe whose per-monitor result the test mutates between cycles."""
    def __init__(self, data=None):
        self.data = data or {}
        self.calls = 0

    def __call__(self, monitor, target, cfg):
        self.calls += 1
        v = self.data.get(monitor.id, "down")
        if isinstance(v, tuple):
            return v
        return (v == "up", "ok" if v == "up" else "down")


def _add_device(name, ip, dtype="server"):
    db = SessionLocal()
    d = Device(name=name, ip_address=ip, device_type=dtype, status="online",
               claimed=True)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _add_monitor(name, ctype="ping", target="10.0.0.9", fail_threshold=3,
                 recovery_ok=3, notify=False, enabled=True, target_device_id=None,
                 params=None):
    db = SessionLocal()
    m = ServiceMonitor(name=name, check_type=ctype, target=target,
                       target_device_id=target_device_id, params=params or {},
                       fail_threshold=fail_threshold, recovery_ok=recovery_ok,
                       notify=notify, enabled=enabled)
    db.add(m)
    db.commit()
    mid = m.id
    db.close()
    return mid


def _clean():
    db = SessionLocal()
    db.query(ServiceCheckEpisode).delete()
    db.query(ServiceMonitor).delete()
    db.query(Ticket).delete()
    db.query(Device).delete()
    db.query(AuditLog).delete()
    db.query(User).delete()
    db.commit()
    db.close()


def _tickets():
    db = SessionLocal()
    rows = db.query(Ticket).all()
    db.close()
    return rows


def _episodes():
    db = SessionLocal()
    rows = db.query(ServiceCheckEpisode).all()
    db.close()
    return rows


def _monitors():
    db = SessionLocal()
    rows = db.query(ServiceMonitor).order_by(ServiceMonitor.id).all()
    db.close()
    return rows


def _raw_delete_device(dev_id):
    """Delete a device out-of-band (FK off) — simulates a device row that
    vanished without the devices route detaching its service monitors first.
    Uses the SAME engine as the models (not a hard-coded file path) so it
    works when the full suite shares one process with several temp DBs."""
    from database import engine
    conn = engine.raw_connection()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM devices WHERE id=?", (dev_id,))
        conn.commit()
    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        finally:
            conn.close()


def _run(eng):
    with patch.object(sc, "service_check_config", return_value=dict(CFG)), \
         patch.object(sc, "send_email", return_value=(True, "")):
        return eng.check()


class ServiceCheckStateMachineTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_ping_dead_host_lifecycle(self):
        m = _add_monitor("wan-ip", ctype="ping", target="10.9.9.9")
        clock = Clock()
        probe = FakeProbe({m: "down"})
        eng = sc.ServiceCheckEngine(probe_fn=probe, now_fn=clock)

        # two failures — under the threshold, no ticket, streak persists
        _run(eng)
        clock.advance(5 * 60)
        _run(eng)
        self.assertEqual(_tickets(), [])
        self.assertEqual(_episodes(), [])
        self.assertEqual(_monitors()[0].fail_streak, 2)

        # third consecutive failure → P2 (kept open)
        clock.advance(5 * 60)
        _run(eng)
        t = _tickets()[0]
        self.assertEqual(t.priority, "P2")
        self.assertEqual(t.status, "open")
        self.assertEqual(t.title, "Service check failed: wan-ip")

        # still down but under 10 min sustained → stays P2
        clock.advance(9 * 60)
        _run(eng)
        self.assertEqual(_tickets()[0].priority, "P2")

        # sustained > 10 min → SAME ticket escalates to P1
        clock.advance(2 * 60)
        _run(eng)
        t = _tickets()[0]
        self.assertEqual(t.priority, "P1")
        self.assertEqual(t.title, "Service outage: wan-ip")
        self.assertEqual(_episodes()[0].state, "outage")
        self.assertEqual(_episodes()[0].escalation_reason, "outage")

        # recovery streak: 1st + 2nd success → still open
        probe.data[m] = "up"
        clock.advance(5 * 60)
        _run(eng)
        self.assertEqual(_tickets()[0].status, "open")
        clock.advance(5 * 60)
        _run(eng)
        self.assertEqual(_tickets()[0].status, "open")

        # 3rd consecutive success → auto-close with a summary note
        clock.advance(5 * 60)
        _run(eng)
        t = _tickets()[0]
        self.assertEqual(t.status, "closed")
        self.assertIn("consecutive", (t.work_notes or ""))
        self.assertEqual(_episodes(), [])

    def test_under_threshold_never_opens_but_resets_on_recovery(self):
        m = _add_monitor("wan-ip", ctype="ping", target="10.9.9.9")
        clock = Clock()
        probe = FakeProbe({m: "down"})
        eng = sc.ServiceCheckEngine(probe_fn=probe, now_fn=clock)

        _run(eng)
        _run(eng)                          # 2 failures — still no ticket
        self.assertEqual(_tickets(), [])

        probe.data[m] = "up"
        _run(eng)                          # one success resets the failure streak
        self.assertEqual(_monitors()[0].fail_streak, 0)
        self.assertEqual(_monitors()[0].ok_streak, 1)

        probe.data[m] = "down"
        _run(eng)
        _run(eng)                          # fresh streak: 2 again — no ticket
        self.assertEqual(_tickets(), [])

    def test_notify_off_still_creates_ticket(self):
        m = _add_monitor("silent", ctype="ping", target="10.9.9.9", notify=False)
        clock = Clock()
        eng = sc.ServiceCheckEngine(probe_fn=FakeProbe({m: "down"}), now_fn=clock)
        with patch.object(sc, "send_email") as send:
            with patch.object(sc, "service_check_config", return_value=dict(CFG)):
                for _ in range(3):
                    eng.check()
                    clock.advance(5 * 60)
        self.assertEqual(len(_tickets()), 1)
        send.assert_not_called()


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_restart_resumes_episode_no_duplicate(self):
        m = _add_monitor("wan-ip", ctype="ping", target="10.9.9.9")
        now = datetime.datetime(2026, 1, 1, 0, 0, 0)

        # persisted episode from a previous container run: P2 open, down since
        # 11 min ago.
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-SC-RESUME", title="Service check failed: wan-ip",
                   description="x", priority="P2", status="open", source="auto",
                   assigned_to="system")
        db.add(t)
        db.flush()
        db.add(ServiceCheckEpisode(
            monitor_id=m, state="down",
            down_since=now - datetime.timedelta(minutes=11),
            last_event_at=now - datetime.timedelta(minutes=11),
            escalated="P2", ticket_id=t.ticket_id))
        db.commit()
        db.close()

        # A fresh engine (empty in-memory state) resumes from the DB.
        eng = sc.ServiceCheckEngine(probe_fn=FakeProbe({m: "down"}), now_fn=Clock(now))
        _run(eng)

        tickets = _tickets()
        self.assertEqual(len(tickets), 1, "must not open a duplicate ticket")
        self.assertEqual(tickets[0].priority, "P1")
        self.assertEqual(_episodes()[0].state, "outage")
        self.assertEqual(_episodes()[0].ticket_id, "TKT-SC-RESUME")

    def test_restart_before_threshold_resumes_fail_streak(self):
        m = _add_monitor("wan-ip", ctype="ping", target="10.9.9.9")
        db = SessionLocal()
        mon = db.get(ServiceMonitor, m)
        mon.fail_streak = 2   # two failures already observed before the restart
        db.commit()
        db.close()

        eng = sc.ServiceCheckEngine(probe_fn=FakeProbe({m: "down"}),
                                    now_fn=Clock(datetime.datetime(2026, 1, 1)))
        _run(eng)
        self.assertEqual(len(_tickets()), 1)   # 3rd failure → opens


class DeviceDeletedCleanupTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_deleted_device_disables_monitor_without_error(self):
        dev = _add_device("nas", "10.0.0.20")
        m = _add_monitor("nas-http", ctype="http", target="", target_device_id=dev,
                         params={"path": "/", "expected_status": 200})
        _raw_delete_device(dev)

        eng = sc.ServiceCheckEngine(probe_fn=FakeProbe({}), now_fn=Clock())
        summary = _run(eng)   # must not raise

        mon = _monitors()[0]
        self.assertFalse(mon.enabled)
        self.assertIn("deleted", mon.last_error or "")
        self.assertEqual(summary["disabled"], 1)

    def test_deleted_device_closes_open_episode(self):
        dev = _add_device("nas", "10.0.0.20")
        m = _add_monitor("nas-http", ctype="http", target="", target_device_id=dev,
                         params={"path": "/", "expected_status": 200})
        db = SessionLocal()
        t = Ticket(ticket_id="TKT-SC-DEL", title="Service check failed: nas-http",
                   description="x", priority="P2", status="open", source="auto",
                   assigned_to="system", target_device_id=dev)
        db.add(t)
        db.flush()
        db.add(ServiceCheckEpisode(monitor_id=m, state="down",
                                   down_since=datetime.datetime.utcnow(),
                                   escalated="P2", ticket_id=t.ticket_id))
        db.commit()
        db.close()
        _raw_delete_device(dev)

        eng = sc.ServiceCheckEngine(probe_fn=FakeProbe({}), now_fn=Clock())
        _run(eng)

        self.assertEqual(_episodes(), [])
        self.assertEqual(_tickets()[0].status, "closed")
        self.assertFalse(_monitors()[0].enabled)


# ── probe helpers (deterministic local sockets / HTTP server) ─────────────

class TcpProbeTest(unittest.TestCase):
    def test_closed_port_is_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()   # now nothing is listening
        m = SimpleNamespace(check_type="tcp", params={"port": port})
        ok, detail = sc.run_probe(m, "127.0.0.1", dict(CFG))
        self.assertFalse(ok)
        self.assertIn("failed", detail)

    def test_open_port_is_up(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            m = SimpleNamespace(check_type="tcp", params={"port": port})
            ok, detail = sc.run_probe(m, "127.0.0.1", dict(CFG))
            self.assertTrue(ok, detail)
        finally:
            listener.close()

    def test_missing_port_is_down(self):
        m = SimpleNamespace(check_type="tcp", params={})
        ok, detail = sc.run_probe(m, "127.0.0.1", dict(CFG))
        self.assertFalse(ok)
        self.assertIn("port", detail)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            body = b"hello world"
            self.send_response(200)
        elif self.path == "/missing":
            body = b"not here"
            self.send_response(404)
        else:
            body = b""
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _mon(self, **params):
        return SimpleNamespace(check_type="http", params=params)

    def test_200_expected_200_is_up(self):
        m = self._mon(path="/ok", expected_status=200)
        ok, detail = sc.run_probe(m, f"127.0.0.1:{self.port}", dict(CFG))
        self.assertTrue(ok, detail)

    def test_non_200_honors_expected_status(self):
        m = self._mon(path="/missing", expected_status=200)
        ok, detail = sc.run_probe(m, f"127.0.0.1:{self.port}", dict(CFG))
        self.assertFalse(ok)
        self.assertIn("404", detail)

        m2 = self._mon(path="/missing", expected_status=404)
        ok2, _ = sc.run_probe(m2, f"127.0.0.1:{self.port}", dict(CFG))
        self.assertTrue(ok2)

    def test_body_contains(self):
        m = self._mon(path="/ok", expected_status=200, body_contains="hello")
        ok, _ = sc.run_probe(m, f"127.0.0.1:{self.port}", dict(CFG))
        self.assertTrue(ok)

        m2 = self._mon(path="/ok", expected_status=200, body_contains="absent")
        ok2, detail = sc.run_probe(m2, f"127.0.0.1:{self.port}", dict(CFG))
        self.assertFalse(ok2)
        self.assertIn("does not contain", detail)

    def test_ping_loopback_when_available(self):
        if not shutil.which("ping"):
            self.skipTest("ping not installed")
        # ping may be installed but non-functional in unprivileged/containerized
        # environments (no CAP_NET_RAW): verify it actually works before
        # asserting a successful loopback probe, else skip.
        try:
            probe = subprocess.run(
                ["ping", "-c", "1", "-W", "2", "127.0.0.1"],
                capture_output=True, timeout=6)
        except Exception:
            probe = None
        if probe is None or probe.returncode != 0:
            self.skipTest("ping not permitted (no raw socket)")
        m = SimpleNamespace(check_type="ping", params={})
        ok, _ = sc.run_probe(m, "127.0.0.1", dict(CFG))
        self.assertTrue(ok)


# ── route binding + admin gating (the 08-16 lesson) ────────────────────────

class RouteBindingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        from auth import hash_password
        db = SessionLocal()
        admin = User(username="admin", role="admin",
                     hashed_password=hash_password("pw"), is_active=True)
        op = User(username="op", role="operator",
                  hashed_password=hash_password("pw"), is_active=True)
        agent = User(username="agent", role="agent",
                     hashed_password=hash_password("pw"), is_active=True)
        db.add_all([admin, op, agent])
        db.commit()
        db.close()
        self.admin = SimpleNamespace(username="admin", role="admin")
        self.op = SimpleNamespace(username="op", role="operator")
        self.agent = SimpleNamespace(username="agent", role="agent")

    def tearDown(self):
        _clean()

    def _client(self, user):
        from main import app
        from fastapi.testclient import TestClient
        from auth import create_access_token
        token = create_access_token({"sub": user.username, "role": user.role,
                                     "groups": [], "auth_method": "password"})
        return TestClient(app), token

    def test_route_binds(self):
        from main import app
        methods = {}
        for r in app.routes:
            p = getattr(r, "path", "")
            methods.setdefault(p, set())
            methods[p].update(getattr(r, "methods", set()) or set())
        self.assertIn("/api/v1/service-checks", methods)
        self.assertIn("GET", methods["/api/v1/service-checks"])
        self.assertIn("POST", methods["/api/v1/service-checks"])
        self.assertIn("/api/v1/service-checks/poll", methods)

    def test_admin_routes_require_admin(self):
        client, token = self._client(self.op)
        self.assertEqual(client.get("/api/v1/service-checks",
                                    headers={"Authorization": f"Bearer {token}"}).status_code, 403)
        self.assertEqual(client.post("/api/v1/service-checks", json={"name": "x"},
                                     headers={"Authorization": f"Bearer {token}"}).status_code, 403)

    def test_poll_allows_agent(self):
        client, token = self._client(self.agent)
        r = client.post("/api/v1/service-checks/poll",
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_poll_denies_operator(self):
        client, token = self._client(self.op)
        self.assertEqual(client.post("/api/v1/service-checks/poll",
                                     headers={"Authorization": f"Bearer {token}"}).status_code, 403)

    def test_admin_can_create_and_list(self):
        client, token = self._client(self.admin)
        r = client.post("/api/v1/service-checks",
                        json={"name": "gw-ping", "check_type": "ping", "target": "10.0.0.1",
                              "notify": False},
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["check_type"], "ping")

        r = client.get("/api/v1/service-checks",
                       headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["monitors"]), 1)

    def test_unauthenticated_401(self):
        from main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        self.assertEqual(client.get("/api/v1/service-checks").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
