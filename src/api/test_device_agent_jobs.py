#!/usr/bin/env python3
"""In-container tests for the NOC_Agent job transport (P1b, design §5).

routes/device_agent must:
  1. jobs/pull claim each pending job exactly once (status pending→running),
     scoped to the cert CN's device;
  2. jobs/result dedupe by (job_id, nonce), store the result, refresh the
     device, audit via log_event, and NEVER 404 on an unknown job_id;
  3. a device cannot complete another device's job (RLS-equivalent scope);
  4. the route decorator binds to the intended endpoint (the 2026-08-16
     lesson: a misbound decorator 422'd UniFi saves for a whole release day).

    docker compose exec api python3 -m unittest test_device_agent_jobs -v
"""

import asyncio
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="device-agent-jobs-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device, DeviceJob
from routes.device_agent import (
    AGENT_ACTIONS,
    PullRequest,
    ResultRequest,
    enqueue_job,
    jobs_pull,
    jobs_result,
)


class _Req:
    """Minimal stand-in for a FastAPI Request (headers only)."""

    def __init__(self, headers):
        self.headers = headers or {}


def _cn(name):
    return f"device-{name}"


def _add_device(name, cn=None):
    db = SessionLocal()
    d = Device(name=name, ip_address="192.0.2.20", device_type="workstation",
               claimed=True, status="online", adoption_status="linked",
               adoption_method="agent", cert_cn=cn or _cn(name))
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _pull(cn, limit=None):
    db = SessionLocal()
    try:
        return asyncio.run(jobs_pull(
            body=PullRequest(limit=limit),
            request=_Req({"x-ssl-client-dn": f"CN={cn},OU=bareNOC"}),
            db=db))
    finally:
        db.close()


def _result(cn, job_id, nonce, ok=True, output="done", duration_ms=5, exit_code=0):
    db = SessionLocal()
    try:
        return asyncio.run(jobs_result(
            body=ResultRequest(job_id=str(job_id), nonce=nonce, ok=ok,
                               output=output, duration_ms=duration_ms,
                               exit_code=exit_code),
            request=_Req({"x-ssl-client-dn": f"CN={cn},OU=bareNOC"}),
            db=db))
    finally:
        db.close()


def _job_row(job_id):
    db = SessionLocal()
    try:
        j = db.get(DeviceJob, int(job_id))
        if j is None:
            return None
        return {"status": j.status, "nonce": j.nonce, "result_json": j.result_json,
                "action": j.action}
    finally:
        db.close()


class DeviceAgentJobsTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(DeviceJob).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def test_enqueue_and_pull_claims_once(self):
        did = _add_device("AgentA")
        db = SessionLocal()
        job = enqueue_job(db, did, "collect_logs", {"lines": 100}, ttl_seconds=600)
        job_id = str(job.id)
        nonce = job.nonce
        db.close()

        r = _pull("device-AgentA", limit=10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["claimed"], 1)
        self.assertEqual(len(r["jobs"]), 1)
        j = r["jobs"][0]
        self.assertEqual(j["job_id"], job_id)
        self.assertEqual(j["action"], "collect_logs")
        self.assertEqual(j["params"], {"lines": 100})
        self.assertEqual(j["nonce"], nonce)
        self.assertTrue(j["deadline"])  # ttl → RFC3339 deadline

        # Claimed → the next pull sees nothing.
        r2 = _pull("device-AgentA", limit=10)
        self.assertEqual(r2["claimed"], 0)
        self.assertEqual(r2["jobs"], [])
        self.assertEqual(_job_row(job_id)["status"], "running")

    def test_pull_is_scoped_to_cn(self):
        # Device B must not see Device A's jobs (RLS-equivalent scoping by CN).
        did_a = _add_device("AgentA")
        _add_device("AgentB")
        db = SessionLocal()
        enqueue_job(db, did_a, "check_updates", {}, ttl_seconds=600)
        db.close()

        r = _pull("device-AgentB", limit=10)
        self.assertEqual(r["claimed"], 0)
        self.assertEqual(r["jobs"], [])

    def test_result_stores_and_dedupes(self):
        did = _add_device("AgentA")
        db = SessionLocal()
        job = enqueue_job(db, did, "check_updates", {}, ttl_seconds=600)
        job_id = str(job.id)
        nonce = job.nonce
        db.close()

        # Claim it so the result path is the "real" flow.
        _pull("device-AgentA")
        r = _result("device-AgentA", job_id, nonce, ok=True, output="all good",
                    duration_ms=12, exit_code=0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "done")
        row = _job_row(job_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result_json"]["ok"], True)
        self.assertEqual(row["result_json"]["output"], "all good")

        # Same (job_id, nonce) again → deduplicated, no double store.
        r2 = _result("device-AgentA", job_id, nonce, ok=True, output="again")
        self.assertTrue(r2["ok"])
        self.assertTrue(r2.get("deduplicated"))
        self.assertEqual(_job_row(job_id)["result_json"]["output"], "all good")

    def test_result_unknown_job_returns_ok(self):
        _add_device("AgentA")
        r = _result("device-AgentA", "999999", "nonce-whatever", ok=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("no_such_job"))

    def test_result_non_numeric_job_id_returns_ok(self):
        _add_device("AgentA")
        r = _result("device-AgentA", "not-an-int", "nonce", ok=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("no_such_job"))

    def test_result_nonce_mismatch_ignored(self):
        did = _add_device("AgentA")
        db = SessionLocal()
        job = enqueue_job(db, did, "collect_logs", {}, ttl_seconds=600)
        job_id = str(job.id)
        db.close()
        _pull("device-AgentA")
        r = _result("device-AgentA", job_id, "wrong-nonce", ok=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("ignored"))
        self.assertEqual(_job_row(job_id)["status"], "running")  # not stored

    def test_result_cannot_complete_another_devices_job(self):
        did_a = _add_device("AgentA")
        _add_device("AgentB")
        db = SessionLocal()
        job = enqueue_job(db, did_a, "collect_logs", {}, ttl_seconds=600)
        job_id = str(job.id)
        nonce = job.nonce
        db.close()

        r = _result("device-AgentB", job_id, nonce, ok=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("ignored"))
        self.assertEqual(_job_row(job_id)["status"], "pending")  # untouched

    def test_enqueue_rejects_unknown_action(self):
        did = _add_device("AgentA")
        db = SessionLocal()
        with self.assertRaises(ValueError):
            enqueue_job(db, did, "install_chat_client", {})
        db.close()

    def test_enqueue_rejects_unconfirmed_reboot(self):
        did = _add_device("AgentA")
        db = SessionLocal()
        with self.assertRaises(ValueError):
            enqueue_job(db, did, "reboot", {})
        with self.assertRaises(ValueError):
            enqueue_job(db, did, "reboot", {"confirm": False})
        enqueue_job(db, did, "reboot", {"confirm": True}, ttl_seconds=600)
        db.close()

    def test_enqueue_apply_updates_requires_confirm(self):
        did = _add_device("AgentA")
        db = SessionLocal()
        # Apply writes to the endpoint OS — customer-requested only (the gate).
        with self.assertRaises(ValueError):
            enqueue_job(db, did, "apply_updates", {})
        with self.assertRaises(ValueError):
            enqueue_job(db, did, "apply_updates", {"confirm": False})
        job = enqueue_job(db, did, "apply_updates", {"confirm": True},
                          ttl_seconds=1800)
        self.assertEqual(job.action, "apply_updates")
        self.assertEqual(job.params, {"confirm": True})
        db.close()

    def test_agent_action_set_is_exactly_p1b(self):
        self.assertEqual(AGENT_ACTIONS,
                         {"collect_logs", "reboot", "check_updates",
                          "apply_updates", "report_facts"})


class DeviceAgentRouteBindingTest(unittest.TestCase):
    """Route-level guard (2026-08-16 lesson): the decorator must bind to the
    intended endpoint — a misbound decorator 422s for a whole release day."""

    def test_jobs_pull_route_binds(self):
        from fastapi.testclient import TestClient
        from main import app

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/device/jobs/pull"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "jobs_pull")

        # No client-cert CN → 403 from _client_cn (NOT a 422 misbinding, and
        # NOT a 404/405 from a wrong path).
        r = TestClient(app).post("/api/v1/device/jobs/pull", json={"limit": 10})
        self.assertNotEqual(r.status_code, 422, f"route misbound: {r.text}")
        self.assertEqual(r.status_code, 403)

    def test_jobs_result_route_binds(self):
        from fastapi.testclient import TestClient
        from main import app

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/device/jobs/result"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "jobs_result")

        r = TestClient(app).post("/api/v1/device/jobs/result",
                                 json={"job_id": "1", "nonce": "n", "ok": True})
        self.assertNotEqual(r.status_code, 422, f"route misbound: {r.text}")
        self.assertEqual(r.status_code, 403)


class DeviceAgentUpdateRouteTest(unittest.TestCase):
    """Devices-page agent update actions (Part B): enqueue check/apply for an
    agent-managed device + the result list. Reuses the device_jobs transport."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(DeviceJob).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def _ctx(self, role="admin"):
        from types import SimpleNamespace
        return {"user": SimpleNamespace(role=role, username="tester", id=1),
                "groups": []}

    def _agent_device(self, name="plex", ip="192.0.2.20"):
        db = SessionLocal()
        d = Device(name=name, ip_address=ip, device_type="server",
                   claimed=True, status="online", adoption_status="linked",
                   adoption_method="agent", cert_cn=f"device-{name}",
                   agent_version="0.2.0")
        db.add(d)
        db.commit()
        did = d.id
        db.close()
        return did

    def test_enqueue_check_updates(self):
        from routes.devices import enqueue_agent_update_job
        did = self._agent_device()
        db = SessionLocal()
        r = enqueue_agent_update_job(did, {"action": "check_updates"},
                                     db=db, ctx=self._ctx())
        self.assertEqual(r["job"]["action"], "check_updates")
        self.assertEqual(r["job"]["status"], "pending")
        db.close()

    def test_enqueue_apply_updates_requires_confirm(self):
        from fastapi import HTTPException
        from routes.devices import enqueue_agent_update_job
        did = self._agent_device()
        db = SessionLocal()
        with self.assertRaises(HTTPException):
            enqueue_agent_update_job(did, {"action": "apply_updates"},
                                     db=db, ctx=self._ctx())
        with self.assertRaises(HTTPException):
            enqueue_agent_update_job(did, {"action": "apply_updates",
                                           "confirm": False},
                                     db=db, ctx=self._ctx())
        r = enqueue_agent_update_job(did, {"action": "apply_updates",
                                           "confirm": True},
                                     db=db, ctx=self._ctx())
        self.assertEqual(r["job"]["action"], "apply_updates")
        self.assertEqual(r["job"]["params"], {"confirm": True})
        db.close()

    def test_rejects_non_agent_device(self):
        from fastapi import HTTPException
        from routes.devices import enqueue_agent_update_job
        db = SessionLocal()
        d = Device(name="sshbox", ip_address="192.0.2.21", device_type="server",
                   claimed=True, status="online", adoption_method="ssh",
                   ssh_key_fingerprint="fp")
        db.add(d)
        db.commit()
        did = d.id
        with self.assertRaises(HTTPException):
            enqueue_agent_update_job(did, {"action": "check_updates"},
                                     db=db, ctx=self._ctx())
        db.close()

    def test_rejects_unknown_action(self):
        from fastapi import HTTPException
        from routes.devices import enqueue_agent_update_job
        did = self._agent_device()
        db = SessionLocal()
        with self.assertRaises(HTTPException):
            enqueue_agent_update_job(did, {"action": "reboot"}, db=db,
                                     ctx=self._ctx())
        db.close()

    def test_list_agent_jobs(self):
        from routes.devices import enqueue_agent_update_job, list_agent_jobs
        did = self._agent_device()
        db = SessionLocal()
        enqueue_agent_update_job(did, {"action": "check_updates"}, db=db,
                                 ctx=self._ctx())
        r = list_agent_jobs(did, db=db, ctx=self._ctx())
        self.assertTrue(r["agent_managed"])
        self.assertEqual(len(r["jobs"]), 1)
        self.assertEqual(r["jobs"][0]["action"], "check_updates")
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
