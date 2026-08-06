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
from models import Device
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
