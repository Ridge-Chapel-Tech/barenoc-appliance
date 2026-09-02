#!/usr/bin/env python3
"""Tests for phone-mac-fold (private/randomized MAC sightings collapse).

Phones/tablets present a fresh locally-administered MAC per network join, so
MAC/IP match-before-insert can never collapse them. This lane folds a
randomized-MAC sighting into an existing UNCLAIMED record when the identity
(name/hostname) matches strictly, instead of INSERTing a duplicate row.

    docker compose exec api python3 -m unittest test_phone_mac_fold -v
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="phone-mac-fold-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import AuditLog, ChangeLogEntry, Device
import discovery


def _no_host_interfaces():
    """Deterministic self-exclusion: ignore the CI box's real interfaces."""
    return set(), set()


class RandomizedMacTest(unittest.TestCase):
    """Locally-administered-bit detection (the fold's MAC gate)."""

    def test_local_administered_bit(self):
        for mac in ("5a:b2:11:22:33:44", "3a:92:11:22:33:44",
                    "c6:ca:11:22:33:44", "d2:9d:11:22:33:44",
                    "02:00:00:00:00:01", "06:00:00:00:00:01",
                    "0a:00:00:00:00:01", "0e:00:00:00:00:01",
                    "12:00:00:00:00:01", "16:00:00:00:00:01"):
            self.assertTrue(discovery.is_randomized_mac(mac), mac)

    def test_real_assigned_mac_not_randomized(self):
        for mac in ("bc:24:11:bf:f4:69", "00:11:22:33:44:55",
                    "1c:2d:3e:4f:5a:6b", "b0:0c:d1:aa:bb:cc"):
            self.assertFalse(discovery.is_randomized_mac(mac), mac)

    def test_not_a_mac(self):
        for mac in ("", None, "wlan0", "5a:b2", "not-a-mac"):
            self.assertFalse(discovery.is_randomized_mac(mac), mac)


class FoldHitMissTest(unittest.TestCase):
    """upsert_discovered fold routing: hit vs miss, strict identity, skip
    guards, and the audit/change-log trail."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.query(ChangeLogEntry).delete()
        db.query(Device).delete()
        db.commit()
        db.close()
        self._patch = patch.object(discovery, "_host_interface_ids",
                                   side_effect=_no_host_interfaces)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _rows(self):
        db = SessionLocal()
        rows = db.query(Device).all()
        db.close()
        return rows

    def test_fold_hit_collapses_same_identity_sightings(self):
        db = SessionLocal()
        out1, d1 = discovery.upsert_discovered(
            db, mac="5a:b2:11:22:33:44", ip="192.0.2.10",
            name="YERY-s-Z-Flip5", device_type="workstation",
            status="online", source="unifi-sync")
        out2, d2 = discovery.upsert_discovered(
            db, mac="3a:92:11:22:33:55", ip="192.0.2.11",
            name="YERY-s-Z-Flip5", device_type="workstation",
            status="online", source="unifi-sync")
        db.commit()
        self.assertEqual(out1, "added")
        self.assertEqual(out2, "folded")
        self.assertEqual(d1.id, d2.id)
        rows = self._rows()
        self.assertEqual(len(rows), 1, "fold must not insert a second row")
        rec = rows[0]
        self.assertEqual(rec.mac_address, "5a:b2:11:22:33:44")  # canonical kept
        self.assertEqual(len(rec.mac_history or []), 1)
        self.assertEqual(rec.mac_history[0]["mac"], "3a:92:11:22:33:55")
        self.assertEqual(rec.ip_address, "192.0.2.11")  # refreshed to latest
        db = SessionLocal()
        try:
            ev = (db.query(AuditLog)
                  .filter(AuditLog.event_type == "device_sighting_folded").first())
            cl = (db.query(ChangeLogEntry)
                  .filter(ChangeLogEntry.event_type == "device_sighting_folded").first())
        finally:
            db.close()
        self.assertIsNotNone(ev, "fold must be audit-logged")
        self.assertEqual(ev.actor, "unifi-sync")
        self.assertEqual(ev.data["device_id"], rec.id)
        self.assertIsNotNone(cl, "fold must be change-logged")
        self.assertEqual(cl.actor, "unifi-sync")

    def test_fold_miss_real_mac_still_adds(self):
        db = SessionLocal()
        discovery.upsert_discovered(db, mac="bc:24:11:bf:f4:69",
                                    ip="192.0.2.20", name="plex",
                                    status="online", source="unifi-sync")
        out2, _ = discovery.upsert_discovered(db, mac="bc:24:11:bf:f4:70",
                                              ip="192.0.2.21", name="plex",
                                              status="online", source="unifi-sync")
        db.commit()
        self.assertEqual(out2, "added")
        self.assertEqual(len(self._rows()), 2)

    def test_same_name_strict_different_names_never_fold(self):
        db = SessionLocal()
        discovery.upsert_discovered(db, mac="5a:b2:11:22:33:44",
                                    ip="192.0.2.30", name="Phone-A",
                                    status="online", source="unifi-sync")
        out2, _ = discovery.upsert_discovered(db, mac="3a:92:11:22:33:55",
                                              ip="192.0.2.31", name="Phone-B",
                                              status="online", source="unifi-sync")
        db.commit()
        self.assertEqual(out2, "added")
        self.assertEqual(len(self._rows()), 2)

    def test_vendor_conflict_blocks_fold(self):
        db = SessionLocal()
        discovery.upsert_discovered(db, mac="5a:b2:11:22:33:44",
                                    ip="192.0.2.40", name="YERY-s-Z-Flip5",
                                    vendor="Samsung", status="online",
                                    source="unifi-sync")
        out2, _ = discovery.upsert_discovered(db, mac="3a:92:11:22:33:55",
                                              ip="192.0.2.41", name="YERY-s-Z-Flip5",
                                              vendor="Apple", status="online",
                                              source="unifi-sync")
        db.commit()
        self.assertEqual(out2, "added")
        self.assertEqual(len(self._rows()), 2)

    def test_linked_record_skip(self):
        db = SessionLocal()
        db.add(Device(name="YERY-s-Z-Flip5", ip_address="192.0.2.50",
                      mac_address="5a:b2:11:22:33:44", device_type="workstation",
                      claimed=False, status="online", adoption_status="linked",
                      adoption_method="cert", cert_cn="device-YERY-s-Z-Flip5"))
        db.commit()
        db.close()
        db = SessionLocal()
        out, _ = discovery.upsert_discovered(
            db, mac="3a:92:11:22:33:55", ip="192.0.2.51",
            name="YERY-s-Z-Flip5", status="online", source="unifi-sync")
        db.commit()
        self.assertEqual(out, "added")  # skip the linked record, insert new
        self.assertEqual(len(self._rows()), 2)
        db.close()

    def test_claimed_record_skip(self):
        db = SessionLocal()
        db.add(Device(name="YERY-s-Z-Flip5", ip_address="192.0.2.60",
                      mac_address="5a:b2:11:22:33:44", device_type="workstation",
                      claimed=True, status="online"))
        db.commit()
        db.close()
        db = SessionLocal()
        out, _ = discovery.upsert_discovered(
            db, mac="3a:92:11:22:33:55", ip="192.0.2.61",
            name="YERY-s-Z-Flip5", status="online", source="unifi-sync")
        db.commit()
        self.assertEqual(out, "added")
        self.assertEqual(len(self._rows()), 2)
        db.close()

    def test_discover_results_fold_counter(self):
        """Integration-ish: the discover_results route counts a fold."""
        from routes.devices import discover_results
        db = SessionLocal()
        r1 = discover_results({"found": [
            {"ip": "192.0.2.70", "mac": "5a:b2:11:22:33:44",
             "name": "YERY-s-Z-Flip5", "hostname": "flip5"}]},
            db=db, user=SimpleNamespace(role="agent"))
        r2 = discover_results({"found": [
            {"ip": "192.0.2.71", "mac": "3a:92:11:22:33:55",
             "name": "YERY-s-Z-Flip5", "hostname": "flip5"}]},
            db=db, user=SimpleNamespace(role="agent"))
        self.assertEqual(r1["added"], 1)
        self.assertEqual(r2["added"], 0)
        self.assertEqual(r2["folded"], 1)
        self.assertEqual(len(self._rows()), 1)
        db.close()


class AdoptionFoldTest(unittest.TestCase):
    """The cert/agent adoption fallback also folds a randomized-MAC report
    into a same-identity unclaimed record instead of self-registering."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    class _Req:
        def __init__(self, headers, body):
            self.headers = headers or {}
            self._body = body

        async def json(self):
            return self._body

    def test_fold_adoption_by_name_when_mac_randomized(self):
        db = SessionLocal()
        disc = Device(name="YERY-s-Z-Flip5", ip_address="192.0.2.80",
                      mac_address="5a:b2:11:22:33:44", device_type="workstation",
                      claimed=False, status="unclaimed")
        db.add(disc)
        db.commit()
        disc_id = disc.id
        db.close()

        from routes.device_certs import device_report
        body = {"hostname": "flip5", "macs": ["3a:92:11:22:33:55"],
                "ips": ["192.0.2.81"], "adoption_method": "cert"}
        req = self._Req(
            headers={"x-ssl-client-dn": "CN=device-YERY-s-Z-FLIP5,OU=bareNOC",
                     "x-real-ip": "192.0.2.81"}, body=body)
        db = SessionLocal()
        try:
            r = asyncio.run(device_report(request=req, db=db))
        finally:
            db.close()
        self.assertEqual(r["adopted"], "linked")
        db = SessionLocal()
        try:
            rows = db.query(Device).all()
        finally:
            db.close()
        self.assertEqual(len(rows), 1, "fold-adoption must not add a row")
        d = rows[0]
        self.assertEqual(d.id, disc_id)
        self.assertEqual(d.mac_address, "5a:b2:11:22:33:44")  # canonical kept
        self.assertEqual(len(d.mac_history or []), 1)
        self.assertEqual(d.mac_history[0]["mac"], "3a:92:11:22:33:55")
        self.assertTrue(d.claimed)
        self.assertEqual(d.adoption_status, "linked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
