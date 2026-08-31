#!/usr/bin/env python3
"""Device-revoke integrity sweep tests (catch un-audited revokes).

A revoked device with no matching ``device_adopt_revoke`` audit event is an
out-of-band state change (the only app path always audits its revoke). The
sweep flags it once (``device_revoke_integrity`` audit event + one alert
email) and is idempotent on re-run.

Run from src/api:
    python3 -m unittest test_revoke_integrity -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="revoke-integrity-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from database import SessionLocal, init_db  # noqa: E402
from models import User, Device, AuditLog  # noqa: E402
from audit import log_event  # noqa: E402
import revoke_integrity as ri  # noqa: E402

init_db()


def _clean():
    db = SessionLocal()
    db.query(AuditLog).delete()
    db.query(Device).delete()
    db.query(User).delete()
    db.commit()
    db.close()


def _add_device(name="fedora", ip="192.168.29.141", adoption_status="revoked"):
    db = SessionLocal()
    d = Device(name=name, ip_address=ip, device_type="server", status="offline",
               claimed=True, adoption_status=adoption_status)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _add_adopt_revoke_event(did, name="fedora"):
    """The legit app-path revoke audit trail (routes/devices.py::revoke_adoption)."""
    db = SessionLocal()
    log_event(db, "device_adopt_revoke", "admin", {
        "device_id": did, "device": name, "cn": None,
    })
    db.close()


def _integrity_events():
    db = SessionLocal()
    rows = db.query(AuditLog).filter(
        AuditLog.event_type == "device_revoke_integrity").all()
    db.close()
    return rows


def _run():
    with patch.object(ri, "send_email", return_value=(True, "")):
        return ri.run_sweep()


class SweepEngineTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()

    def tearDown(self):
        _clean()

    def test_revoked_without_event_flags_and_emails_once(self):
        did = _add_device()
        with patch.object(ri, "send_email", return_value=(True, "")) as send:
            summary = ri.run_sweep()

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["flagged"], 1)
        self.assertEqual(summary["emailed"], 1)
        self.assertEqual(send.call_count, 1)

        events = _integrity_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data.get("device_id"), did)
        self.assertEqual(events[0].data.get("device"), "fedora")
        self.assertEqual(events[0].actor, "system")

    def test_revoked_with_adopt_revoke_event_is_not_flagged(self):
        did = _add_device()
        _add_adopt_revoke_event(did)
        with patch.object(ri, "send_email", return_value=(True, "")) as send:
            summary = ri.run_sweep()

        self.assertEqual(summary["flagged"], 0)
        self.assertEqual(summary["emailed"], 0)
        send.assert_not_called()
        self.assertEqual(_integrity_events(), [])

    def test_rerun_is_idempotent(self):
        _add_device()
        with patch.object(ri, "send_email", return_value=(True, "")) as send:
            first = ri.run_sweep()
            second = ri.run_sweep()

        self.assertEqual(first["flagged"], 1)
        self.assertEqual(second["flagged"], 0)
        self.assertEqual(second["emailed"], 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(_integrity_events()), 1)

    def test_non_revoked_devices_not_checked_as_revoked(self):
        _add_device(name="gateway", adoption_status="linked")
        _add_device(name="nas", adoption_status="none")
        with patch.object(ri, "send_email", return_value=(True, "")) as send:
            summary = ri.run_sweep()

        self.assertEqual(summary["checked"], 0)
        self.assertEqual(summary["flagged"], 0)
        send.assert_not_called()

    def test_email_failure_still_flags(self):
        _add_device()
        with patch.object(ri, "send_email", return_value=(False, "no transport")) as send:
            summary = ri.run_sweep()

        self.assertEqual(summary["flagged"], 1)
        self.assertEqual(summary["emailed"], 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(_integrity_events()), 1)


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
        self.assertIn("/api/v1/revoke-integrity/sweep", methods)
        self.assertIn("POST", methods["/api/v1/revoke-integrity/sweep"])

    def test_poll_allows_agent(self):
        client, token = self._client(self.agent)
        r = client.post("/api/v1/revoke-integrity/sweep",
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_poll_denies_operator(self):
        client, token = self._client(self.op)
        r = client.post("/api/v1/revoke-integrity/sweep",
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 403)

    def test_unauthenticated_401(self):
        from main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        self.assertEqual(client.post("/api/v1/revoke-integrity/sweep").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
