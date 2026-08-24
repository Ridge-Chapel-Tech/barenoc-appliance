#!/usr/bin/env python3
"""In-container tests for NOC_Agent self-report (P1a).

The device report endpoint (routes/device_certs.device_report) must:
  1. link a device with method="agent" when the body carries agent fields
     (agent_version / adoption_method=="agent") and store the agent metadata
     + facts;
  2. keep the plain cert heartbeat path untouched (method="cert" regression
     guard).

    docker compose exec api python3 -m unittest test_device_agent -v
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="device-agent-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device


class _Req:
    """Minimal stand-in for a FastAPI Request: headers + an async json()."""

    def __init__(self, headers, body=None):
        self.headers = headers or {}
        self._body = body

    async def json(self):
        return self._body


AGENT_BODY = {
    "hostname": "agentbox",
    "os": "ubuntu-24.04",
    "kernel": "6.8.0-136",
    "macs": ["aa:bb:cc:dd:ee:ff"],
    "ips": ["192.0.2.55"],
    "uptime_s": 12345,
    "disk_free_gb": 812.5,
    "agent_version": "0.1.0-p1a",
    "agent_capabilities": ["report_facts"],
    "adoption_method": "agent",
}


def _report(cn, body=None):
    from routes.device_certs import device_report
    req = _Req(headers={"x-ssl-client-dn": f"CN={cn},OU=bareNOC"}, body=body)
    db = SessionLocal()
    try:
        return asyncio.run(device_report(request=req, db=db))
    finally:
        db.close()


def _add(name):
    db = SessionLocal()
    d = Device(name=name, ip_address="192.0.2.10", device_type="workstation",
               claimed=False, status="unclaimed")
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _device(did):
    """Read the device's agent-relevant fields inside a fresh session."""
    db = SessionLocal()
    try:
        d = db.query(Device).get(did)
        if d is None:
            return None
        return {
            "adoption_status": d.adoption_status,
            "adoption_method": d.adoption_method,
            "claimed": d.claimed,
            "cert_cn": d.cert_cn,
            "agent_version": d.agent_version,
            "agent_capabilities": d.agent_capabilities,
            "facts_json": d.facts_json,
            "ssh_key_fingerprint": d.ssh_key_fingerprint,
        }
    finally:
        db.close()


def _device_by_cn(cn):
    db = SessionLocal()
    try:
        d = db.query(Device).filter(Device.cert_cn == cn).first()
        if d is None:
            return None
        return _device(d.id)
    finally:
        db.close()


class AgentReportTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()

    def test_agent_report_links_device_with_method_agent(self):
        did = _add("AgentBox")
        r = _report("device-AgentBox", AGENT_BODY)
        self.assertEqual(r["adopted"], "linked")
        self.assertEqual(r["method"], "agent")
        d = _device(did)
        self.assertEqual(d["adoption_status"], "linked")
        self.assertEqual(d["adoption_method"], "agent")
        self.assertTrue(d["claimed"])
        self.assertEqual(d["agent_version"], "0.1.0-p1a")
        self.assertEqual(json.loads(d["agent_capabilities"]), ["report_facts"])
        facts = json.loads(d["facts_json"])
        self.assertEqual(facts["os"], "ubuntu-24.04")
        self.assertEqual(facts["kernel"], "6.8.0-136")
        self.assertEqual(facts["macs"], ["aa:bb:cc:dd:ee:ff"])
        self.assertEqual(facts["ips"], ["192.0.2.55"])
        self.assertEqual(facts["uptime_s"], 12345)
        self.assertEqual(facts["disk_free_gb"], 812.5)
        # Agent adoption must NOT provision SSH credentials (the agent IS the
        # control path; no stored SSH secrets on the appliance).
        self.assertIsNone(d["ssh_key_fingerprint"])

    def test_agent_report_self_registers_unknown_cn(self):
        r = _report("device-NewBox", AGENT_BODY)
        self.assertEqual(r["ok"], True)
        d = _device_by_cn("device-NewBox")
        self.assertIsNotNone(d)
        self.assertEqual(d["adoption_method"], "agent")
        self.assertEqual(d["agent_version"], "0.1.0-p1a")

    def test_agent_report_flips_existing_cert_device_to_agent(self):
        # A cert-adopted device installs the agent; its first agent report
        # flips adoption_method to "agent" (design §12).
        did = _add("Upgraded")
        db = SessionLocal()
        d = db.query(Device).get(did)
        d.adoption_status = "linked"
        d.adoption_method = "cert"
        d.cert_cn = "device-Upgraded"
        db.commit()
        db.close()
        r = _report("device-Upgraded", AGENT_BODY)
        self.assertEqual(r["method"], "agent")
        d = _device(did)
        self.assertEqual(d["adoption_method"], "agent")
        self.assertEqual(d["agent_version"], "0.1.0-p1a")

    def test_plain_cert_report_still_links_with_method_cert(self):
        # Regression guard: a heartbeat without agent fields keeps the exact
        # legacy cert-adoption path.
        did = _add("CertBox")
        r = _report("device-CertBox", {"hostname": "certbox"})
        self.assertEqual(r["adopted"], "linked")
        self.assertEqual(r["method"], "cert")
        d = _device(did)
        self.assertEqual(d["adoption_status"], "linked")
        self.assertEqual(d["adoption_method"], "cert")
        self.assertEqual(d["cert_cn"], "device-CertBox")
        self.assertTrue(d["claimed"])
        self.assertIsNone(d["agent_version"])
        self.assertIsNone(d["facts_json"])

    def test_plain_cert_report_no_body_links_with_method_cert(self):
        # Even with NO body at all (the original heartbeat), path is unchanged.
        did = _add("BareCert")
        r = _report("device-BareCert", None)
        self.assertEqual(r["method"], "cert")
        d = _device(did)
        self.assertEqual(d["adoption_method"], "cert")


if __name__ == "__main__":
    unittest.main(verbosity=2)
