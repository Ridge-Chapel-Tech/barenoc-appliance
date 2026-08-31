#!/usr/bin/env python3
"""In-container tests for device-dedupe at adoption (report path).

routes/device_certs.device_report must, when the cert CN and its name don't
match any record but an UNCLAIMED discovery record for the same box exists
(same IP or MAC), ADOPT that record in place instead of self-registering a
duplicate. Guardrails: never adopt a claimed/linked record with a different
cert identity, and never adopt a revoked record.

    docker compose exec api python3 -m unittest test_device_certs -v
"""

import asyncio
import datetime
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="device-certs-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import AuditLog, Device


class _Req:
    """Minimal stand-in for a FastAPI Request: headers + an async json()."""

    def __init__(self, headers, body=None):
        self.headers = headers or {}
        self._body = body

    async def json(self):
        return self._body


AGENT_BODY = {
    "hostname": "plex",
    "os": "linux",
    "macs": ["bc:24:11:bf:f4:69"],
    "ips": ["192.168.1.13"],
    "agent_version": "0.2.0",
    "agent_capabilities": ["report_facts"],
    "adoption_method": "agent",
}


def _report(cn, body=None, real_ip=None):
    from routes.device_certs import device_report
    headers = {"x-ssl-client-dn": f"CN={cn},OU=bareNOC"}
    if real_ip:
        headers["x-real-ip"] = real_ip
    req = _Req(headers=headers, body=body)
    db = SessionLocal()
    try:
        return asyncio.run(device_report(request=req, db=db))
    finally:
        db.close()


def _count_devices():
    db = SessionLocal()
    try:
        return db.query(Device).count()
    finally:
        db.close()


def _device(did):
    db = SessionLocal()
    try:
        return db.query(Device).get(did)
    finally:
        db.close()


def _by_name(name):
    db = SessionLocal()
    try:
        return db.query(Device).filter(Device.name == name).first()
    finally:
        db.close()


class ReportDedupeTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def _add_discovery(self, ip="192.168.1.13", mac="bc:24:11:bf:f4:69",
                       name="PLEX Server"):
        db = SessionLocal()
        d = Device(name=name, ip_address=ip, mac_address=mac, device_type="server",
                   claimed=False, status="unclaimed")
        db.add(d)
        db.commit()
        did = d.id
        db.close()
        return did

    def test_adopts_unclaimed_discovery_by_ip_and_mac(self):
        did = self._add_discovery()
        r = _report("device-plex", AGENT_BODY, real_ip="192.168.1.13")
        self.assertEqual(r["adopted"], "linked")
        self.assertEqual(r["method"], "agent")
        d = _device(did)
        self.assertEqual(d.cert_cn, "device-plex")
        self.assertEqual(d.adoption_status, "linked")
        self.assertTrue(d.claimed)
        self.assertEqual(d.adoption_method, "agent")
        self.assertEqual(d.agent_version, "0.2.0")
        # discovery metadata preserved
        self.assertEqual(d.name, "PLEX Server")
        self.assertEqual(d.device_type, "server")
        self.assertEqual(d.mac_address, "bc:24:11:bf:f4:69")
        self.assertEqual(d.hostname, "plex")
        # no new record was created (the SAME id was adopted)
        self.assertEqual(_count_devices(), 1)
        # in-place adoption is NOT a merge — no device_merged audit event
        db = SessionLocal()
        try:
            merged = (db.query(AuditLog)
                      .filter(AuditLog.event_type == "device_merged").count())
        finally:
            db.close()
        self.assertEqual(merged, 0)

    def test_adopts_unclaimed_discovery_by_mac_only(self):
        # The discovery record's IP differs from what the agent reports; the
        # MAC is the only matching key — and the discovery IP is preserved.
        did = self._add_discovery(ip="192.168.1.99", mac="bc:24:11:bf:f4:69")
        r = _report("device-plex", AGENT_BODY, real_ip="10.0.0.9")
        self.assertEqual(r["adopted"], "linked")
        d = _device(did)
        self.assertEqual(d.cert_cn, "device-plex")
        self.assertEqual(d.mac_address, "bc:24:11:bf:f4:69")
        self.assertEqual(d.ip_address, "192.168.1.99")  # discovery IP not clobbered
        self.assertEqual(_count_devices(), 1)

    def test_self_registers_when_no_ip_mac_match(self):
        r = _report("device-NewBox", AGENT_BODY)
        self.assertEqual(r["ok"], True)
        d = _by_name("NewBox")
        self.assertIsNotNone(d)
        self.assertEqual(d.cert_cn, "device-NewBox")
        self.assertEqual(d.adoption_method, "agent")
        self.assertEqual(_count_devices(), 1)

    def test_never_adopts_claimed_record_with_different_identity(self):
        # A claimed + linked record (different cert identity AND name) shares
        # the IP/MAC — the fallback must skip it and self-register instead.
        db = SessionLocal()
        d = Device(name="different-box", ip_address="192.168.1.13",
                   mac_address="bc:24:11:bf:f4:69", device_type="server",
                   claimed=True, status="online", adoption_status="linked",
                   adoption_method="agent", cert_cn="device-other")
        db.add(d)
        db.commit()
        db.close()
        r = _report("device-plex", AGENT_BODY, real_ip="192.168.1.13")
        self.assertEqual(r["ok"], True)
        # the claimed record is untouched
        claimed = _by_name("different-box")
        self.assertEqual(claimed.cert_cn, "device-other")
        self.assertEqual(claimed.adoption_status, "linked")
        # a NEW record was self-registered for the unknown CN
        new = _by_name("plex")
        self.assertIsNotNone(new)
        self.assertEqual(new.cert_cn, "device-plex")
        self.assertEqual(_count_devices(), 2)

    def test_never_adopts_revoked_record(self):
        db = SessionLocal()
        d = Device(name="revoked-box", ip_address="192.168.1.13",
                   mac_address="bc:24:11:bf:f4:69", device_type="server",
                   claimed=True, status="offline", adoption_status="revoked",
                   cert_cn="device-revoked-box")
        db.add(d)
        db.commit()
        db.close()
        r = _report("device-plex", AGENT_BODY, real_ip="192.168.1.13")
        self.assertEqual(r["ok"], True)
        revoked = _by_name("revoked-box")
        self.assertEqual(revoked.adoption_status, "revoked")
        self.assertEqual(revoked.cert_cn, "device-revoked-box")
        new = _by_name("plex")
        self.assertIsNotNone(new)
        self.assertEqual(_count_devices(), 2)

    def test_plain_cert_report_adopts_in_place_with_method_cert(self):
        did = self._add_discovery()
        r = _report("device-plex", {"hostname": "plex"}, real_ip="192.168.1.13")
        self.assertEqual(r["adopted"], "linked")
        self.assertEqual(r["method"], "cert")
        d = _device(did)
        self.assertEqual(d.cert_cn, "device-plex")
        self.assertEqual(d.adoption_method, "cert")
        self.assertTrue(d.claimed)

    def test_multiple_unclaimed_matches_picks_most_recently_seen(self):
        db = SessionLocal()
        older = Device(name="old-box", ip_address="192.168.1.13", claimed=False,
                       status="unclaimed",
                       last_seen=datetime.datetime.utcnow() - datetime.timedelta(days=2))
        newer = Device(name="new-box", ip_address="192.168.1.13", claimed=False,
                       status="unclaimed", last_seen=datetime.datetime.utcnow())
        db.add_all([older, newer])
        db.commit()
        newer_id = newer.id
        db.close()
        r = _report("device-plex", AGENT_BODY, real_ip="192.168.1.13")
        self.assertEqual(r["adopted"], "linked")
        d = _device(newer_id)
        self.assertEqual(d.cert_cn, "device-plex")
        self.assertEqual(d.adoption_status, "linked")
        self.assertEqual(_count_devices(), 2)  # only one was adopted


if __name__ == "__main__":
    unittest.main(verbosity=2)
