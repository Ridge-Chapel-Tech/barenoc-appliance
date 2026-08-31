#!/usr/bin/env python3
"""In-container tests for the devices list 'controlled' filter.

'controlled' means BareNOC has admin control: SSH credentials OR adopted
UniFi-managed gear (unifi_managed + claimed). Auto-adopted UniFi devices must
appear in the Onboarded view (claimed=true&controlled=true).

    docker compose exec api python3 -m unittest test_devices -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="devices-list-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import AuditLog, Device, DeviceJob
from routes.devices import list_devices, get_device_credentials

ADMIN_CTX = {"user": SimpleNamespace(role="admin"), "groups": []}


def _add(name, ip, claimed, unifi_managed=False, ssh=False, group="default"):
    db = SessionLocal()
    d = Device(name=name, ip_address=ip, device_type="switch", status="online",
               claimed=claimed, unifi_managed=unifi_managed,
               device_group=group, ssh_key_fingerprint="fp" if ssh else None)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _list(**kw):
    """Call list_devices directly with FastAPI-injected defaults materialized."""
    return list_devices(limit=100, offset=0, db=SessionLocal(), ctx=ADMIN_CTX, **kw)


def _names(devices):
    return [d["name"] for d in devices["devices"]]


class ControlledFilterTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()
        # 1: SSH-controlled only (legacy onboarded)
        _add("ssh-dev", "10.0.0.1", claimed=True, ssh=True)
        # 2: adopted UniFi gear (no SSH)
        _add("unifi-gw", "10.0.0.2", claimed=True, unifi_managed=True)
        # 3: claimed but NO control path (monitoring only)
        _add("plain-mon", "10.0.0.3", claimed=True)
        # 4: discovered UniFi gear, NOT yet adopted
        _add("unifi-new", "10.0.0.4", claimed=False, unifi_managed=True)
        # 5: unclaimed non-UniFi
        _add("disc-other", "10.0.0.5", claimed=False)

    def test_onboarded_includes_unifi_managed(self):
        r = _list(claimed=True, controlled=True)
        names = _names(r)
        self.assertIn("ssh-dev", names)    # legacy SSH path (regression)
        self.assertIn("unifi-gw", names)   # adopted UniFi gear = onboarded
        self.assertNotIn("plain-mon", names)   # no control path -> not onboarded
        self.assertNotIn("unifi-new", names)   # not adopted yet
        self.assertNotIn("disc-other", names)

    def test_monitoring_only_excludes_unifi_managed(self):
        r = _list(claimed=True, controlled=False)
        names = _names(r)
        self.assertIn("plain-mon", names)
        self.assertNotIn("unifi-gw", names)    # moved out of monitoring-only
        self.assertNotIn("ssh-dev", names)

    def test_controlled_true_without_claimed(self):
        r = _list(controlled=True)
        names = _names(r)
        self.assertIn("ssh-dev", names)
        self.assertIn("unifi-gw", names)
        self.assertNotIn("unifi-new", names)   # unclaimed UniFi gear is not controlled

    def test_unifi_managed_flag_in_response(self):
        r = _list(claimed=True)
        gw = next(d for d in r["devices"] if d["name"] == "unifi-gw")
        self.assertTrue(gw["unifi_managed"])
        plain = next(d for d in r["devices"] if d["name"] == "plain-mon")
        self.assertFalse(plain["unifi_managed"])

    def test_dashboard_counts_controlled_only(self):
        from routes.dashboard import get_stats
        # fleet = SSH-controlled OR adopted UniFi gear (unifi_managed + claimed)
        db = SessionLocal()
        r = get_stats(db=db, user=SimpleNamespace(role="admin"))
        db.close()
        self.assertEqual(r.total_devices, 2)   # ssh-dev + unifi-gw
        self.assertNotEqual(r.total_devices, 5)  # never the whole table


class CredentialsAccessTest(unittest.TestCase):
    """GET /devices/{id}/credentials is admin-or-agent (least privilege)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        did = Device(name="sw-1", ip_address="10.0.0.9", device_type="switch",
                     status="online", claimed=True, device_group="default",
                     ssh_user="root", ssh_key_fingerprint="fp", snmp_community="pub")
        db.add(did)
        db.commit()
        self.device_id = did.id
        db.close()

    def _creds(self, role):
        ctx = {"user": SimpleNamespace(role=role), "groups": []}
        db = SessionLocal()
        try:
            return get_device_credentials(self.device_id, db=db, ctx=ctx)
        finally:
            db.close()

    def test_agent_can_fetch_credentials(self):
        r = self._creds("agent")
        self.assertEqual(r["ssh_user"], "root")
        # snmp_community is Fernet-encrypted at rest; the raw column is not
        # plaintext (returns the encrypted marker), so only ssh_user is asserted

    def test_admin_can_fetch_credentials(self):
        r = self._creds("admin")
        self.assertEqual(r["ssh_user"], "root")

    def test_operator_denied_credentials(self):
        # operators (human staff) must NOT get decrypted SSH keys
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._creds("operator")

    def test_readonly_denied_credentials(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._creds("readonly")


class DeviceAdoptionTest(unittest.TestCase):
    """Phase F — adopt with a certificate (step-ca): mint token, link via mTLS
    report, revoke. The report endpoint authenticates by CERT (no user token)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        d = Device(name="Cam01", ip_address="192.0.2.55", device_type="camera",
                   claimed=False, status="unclaimed")
        db.add(d)
        db.commit()
        self.device_id = d.id
        db.close()

    def _ctx(self, role):
        return {"user": SimpleNamespace(role=role, username="tester"), "groups": []}

    def _adopt(self, role, **kw):
        from routes.devices import adopt_with_cert
        db = SessionLocal()
        try:
            return adopt_with_cert(self.device_id, body=kw.get("body"), db=db,
                                   ctx=self._ctx(role))
        finally:
            db.close()

    def test_readonly_cannot_adopt(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._adopt("readonly")

    def test_operator_mints_token_and_sets_enrolling(self):
        from unittest.mock import patch
        with patch("step_ca.mint_token", return_value="JWT-TOKEN"), \
             patch("step_ca.root_fingerprint", return_value="FP"):
            r = self._adopt("operator")
        self.assertEqual(r["status"], "enrolling")
        self.assertEqual(r["cn"], "device-Cam01")
        self.assertEqual(r["token"], "JWT-TOKEN")
        self.assertIn("step ca certificate", r["note"])
        db = SessionLocal()
        d = db.query(Device).get(self.device_id)
        self.assertEqual(d.adoption_status, "enrolling")
        self.assertEqual(d.adoption_method, "cert")
        db.close()

    def test_revoke(self):
        from routes.devices import revoke_adoption
        db = SessionLocal()
        d = db.query(Device).get(self.device_id)
        d.adoption_status = "linked"
        d.adoption_method = "cert"
        d.cert_cn = "device-Cam01"
        db.commit()
        r = revoke_adoption(self.device_id, db=db, ctx=self._ctx("admin"))
        db.close()
        self.assertEqual(r["status"], "revoked")

    def test_report_links_device(self):
        from routes.device_certs import device_report
        from types import SimpleNamespace as NS
        import asyncio
        req = NS(headers={"x-ssl-client-dn": "CN=device-Cam01,OU=bareNOC"})
        db = SessionLocal()
        r = asyncio.run(device_report(request=req, db=db))
        d = db.query(Device).get(self.device_id)
        self.assertEqual(r["adopted"], "linked")
        self.assertEqual(r["method"], "cert")
        self.assertEqual(d.adoption_status, "linked")
        self.assertTrue(d.claimed)
        db.close()

    def test_report_revoked_denied(self):
        from routes.device_certs import device_report
        from types import SimpleNamespace as NS
        from fastapi import HTTPException
        import asyncio
        db = SessionLocal()
        d = db.query(Device).get(self.device_id)
        d.adoption_status = "revoked"
        db.commit()
        req = NS(headers={"x-ssl-client-dn": "CN=device-Cam01"})
        with self.assertRaises(HTTPException):
            asyncio.run(device_report(request=req, db=db))
        db.close()

    def test_report_unknown_cn_self_registers(self):
        """A valid cert for an unknown CN self-registers the device (Phase F:
        the /onboard portal enrolls a cert and the first report links it)."""
        from routes.device_certs import device_report
        from types import SimpleNamespace as NS
        import asyncio
        req = NS(headers={"x-ssl-client-dn": "CN=device-Nope"})
        db = SessionLocal()
        r = asyncio.run(device_report(request=req, db=db))
        self.assertEqual(r["ok"], True)
        d = db.query(Device).filter(Device.cert_cn == "device-Nope").first()
        self.assertIsNotNone(d)
        self.assertEqual(d.adoption_status, "linked")
        self.assertEqual(d.adoption_method, "cert")
        self.assertIn("self-onboarded", d.tags or [])
        db.close()

    def test_snmp_sweep_results_create_and_update(self):
        from routes.devices import snmp_sweep_results
        from types import SimpleNamespace as NS
        db = SessionLocal()
        r = snmp_sweep_results({"found": [
            {"ip": "192.0.2.200", "sysname": "core-switch", "vendor": "Cisco",
             "sysdescr": "Cisco IOS switch"},
            {"ip": "192.0.2.201", "sysname": "printer1", "sysdescr": "HP LaserJet"},
        ]}, db=db, user=NS(role="agent"))
        self.assertEqual(r["added"], 2)
        db = SessionLocal()
        sw = db.query(Device).filter(Device.ip_address == "192.0.2.200").first()
        self.assertEqual(sw.name, "core-switch")
        self.assertEqual(sw.device_type, "switch")
        self.assertFalse(sw.claimed)
        # update path: re-sweep with a type refinement
        r2 = snmp_sweep_results({"found": [{"ip": "192.0.2.200", "sysname": "core-switch",
                                            "sysdescr": "Cisco IOS switch", "vendor": "Cisco"}]},
                                db=db, user=NS(role="agent"))
        self.assertEqual(r2["updated"], 1)
        db.close()


class MergeDuplicatesTest(unittest.TestCase):
    """device-dedupe admin cleanup: merge_duplicates folds a duplicate record
    back into its discovery record (adopt + delete + device_merged audit)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.query(DeviceJob).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def _pair(self):
        """Return (discovery_id, duplicate_id) for the canonical Plex-style
        dupe: an unclaimed discovery record + an agent-adopted duplicate."""
        db = SessionLocal()
        disc = Device(name="PLEX Server", ip_address="192.168.1.13",
                      mac_address="bc:24:11:bf:f4:69", device_type="server",
                      claimed=False, status="unclaimed")
        dup = Device(name="plex", ip_address="192.168.1.13",
                     mac_address="bc:24:11:bf:f4:69", device_type="workstation",
                     claimed=True, status="online", adoption_status="linked",
                     adoption_method="agent", cert_cn="device-plex",
                     agent_version="0.2.0", hostname="plex",
                     facts_json='{"hostname": "plex"}')
        db.add_all([disc, dup])
        db.commit()
        db.refresh(disc)
        db.refresh(dup)
        ids = (disc.id, dup.id)
        db.close()
        return ids

    def _merge(self, keep_id, dup_id, actor="admin"):
        from routes.devices import merge_duplicates
        db = SessionLocal()
        try:
            return merge_duplicates(db, keep_id, dup_id, actor)
        finally:
            db.close()

    def _get(self, did):
        db = SessionLocal()
        try:
            return db.query(Device).get(did)
        finally:
            db.close()

    def test_merge_adopts_discovery_and_deletes_duplicate(self):
        keep_id, dup_id = self._pair()
        r = self._merge(keep_id, dup_id)
        self.assertTrue(r["ok"])
        self.assertEqual(r["merged"], {"from_id": dup_id, "into_id": keep_id})
        keep = self._get(keep_id)
        self.assertIsNotNone(keep)
        self.assertEqual(keep.cert_cn, "device-plex")
        self.assertEqual(keep.adoption_status, "linked")
        self.assertEqual(keep.adoption_method, "agent")
        self.assertEqual(keep.agent_version, "0.2.0")
        self.assertTrue(keep.claimed)
        self.assertEqual(keep.hostname, "plex")
        # discovery metadata preserved
        self.assertEqual(keep.name, "PLEX Server")
        self.assertEqual(keep.device_type, "server")
        self.assertEqual(keep.mac_address, "bc:24:11:bf:f4:69")
        # duplicate deleted
        self.assertIsNone(self._get(dup_id))
        # audited as device_merged (from_id, into_id)
        db = SessionLocal()
        try:
            ev = (db.query(AuditLog)
                  .filter(AuditLog.event_type == "device_merged").first())
        finally:
            db.close()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.data["from_id"], dup_id)
        self.assertEqual(ev.data["into_id"], keep_id)

    def test_merge_repoints_jobs_to_survivor(self):
        keep_id, dup_id = self._pair()
        db = SessionLocal()
        job = DeviceJob(device_id=dup_id, action="collect_logs", params={},
                        nonce="n1", status="pending")
        db.add(job)
        db.commit()
        db.close()
        self._merge(keep_id, dup_id)
        db = SessionLocal()
        try:
            jobs = db.query(DeviceJob).filter(DeviceJob.device_id == keep_id).all()
        finally:
            db.close()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].action, "collect_logs")
        # duplicate gone, no dangling jobs
        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(DeviceJob).filter(DeviceJob.device_id == dup_id).count(), 0)
        finally:
            db.close()

    def test_merge_refuses_different_cert_identity(self):
        db = SessionLocal()
        keep = Device(name="A", ip_address="10.0.0.1", claimed=True,
                      status="online", adoption_status="linked",
                      cert_cn="device-A")
        dup = Device(name="B", ip_address="10.0.0.2", claimed=True,
                     status="online", adoption_status="linked",
                     cert_cn="device-B")
        db.add_all([keep, dup])
        db.commit()
        keep_id, dup_id = keep.id, dup.id
        db.close()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._merge(keep_id, dup_id)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIsNotNone(self._get(keep_id))
        self.assertIsNotNone(self._get(dup_id))

    def test_merge_refuses_self(self):
        keep_id, _ = self._pair()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._merge(keep_id, keep_id)

    def test_merge_refuses_missing_device(self):
        keep_id, _ = self._pair()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._merge(keep_id, 999999)

    def test_merge_route_requires_admin(self):
        keep_id, dup_id = self._pair()
        from routes.devices import merge_duplicates_route
        from fastapi import HTTPException
        from types import SimpleNamespace as NS
        db = SessionLocal()
        try:
            with self.assertRaises(HTTPException):
                merge_duplicates_route(
                    body={"keep_id": keep_id, "duplicate_id": dup_id},
                    db=db, ctx={"user": NS(role="technician", username="tech"),
                                "groups": []})
            r = merge_duplicates_route(
                body={"keep_id": keep_id, "duplicate_id": dup_id},
                db=db, ctx={"user": NS(role="admin", username="admin"),
                            "groups": []})
            self.assertTrue(r["ok"])
            self.assertIsNone(self._get(dup_id))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
