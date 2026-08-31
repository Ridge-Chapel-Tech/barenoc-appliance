#!/usr/bin/env python3
"""Tests for discovery-side dedupe + appliance self-exclusion.

In-container / CI (scratch sqlite DB, no live UniFi/SNMP). Pins the two
locked-brief invariants:
  1. repeated scans of the same host yield ONE record (by MAC and by IP);
     claimed records are never duplicated or overwritten;
  2. the appliance's own IP/MAC is never recorded (self-exclusion).

    docker compose exec api python3 -m unittest test_discovery_dedupe -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="discovery-dedupe-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device
import discovery
from routes.devices import snmp_sweep_results, discover_results
from routes import unifi_sync


def _no_host_interfaces():
    """Deterministic self-exclusion: ignore the CI box's real interfaces."""
    return set(), set()


def _set_env(**kw):
    """Set process-env keys for discovery._read_env fallback; return a restore fn."""
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    return lambda: [os.environ.__setitem__(k, saved[k]) if saved[k] is not None
                    else os.environ.pop(k, None) for k in kw]


def _rows(ip=None, mac=None):
    db = SessionLocal()
    q = db.query(Device)
    if ip:
        q = q.filter(Device.ip_address == ip)
    if mac:
        q = q.filter(Device.mac_address == mac)
    rows = q.all()
    db.close()
    return rows


class DiscoveryDedupeTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()
        self._patch = patch.object(discovery, "_host_interface_ids",
                                   side_effect=_no_host_interfaces)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    # ── match-before-insert: repeated scans → ONE record ───────────────────

    def test_repeated_scan_by_mac_yields_one_record(self):
        db = SessionLocal()
        out1, d1 = discovery.upsert_discovered(
            db, mac="AA:BB:CC:00:00:01", ip="192.0.2.10", name="core-sw",
            device_type="switch", source="ping-sweep")
        out2, d2 = discovery.upsert_discovered(
            db, mac="aa-bb-cc-00-00-01", ip="192.0.2.10", name="core-sw",
            device_type="switch", source="ping-sweep")
        db.commit()
        self.assertEqual(out1, "added")
        self.assertEqual(out2, "updated")
        self.assertEqual(d1.id, d2.id)
        rows = _rows(mac="AA:BB:CC:00:00:01")
        self.assertEqual(len(rows), 1, "repeated MAC scan must not duplicate")
        db.close()

    def test_repeated_scan_by_ip_yields_one_record(self):
        db = SessionLocal()
        discovery.upsert_discovered(db, ip="192.0.2.20", name="discovered-192-0-2-20",
                                    device_type="unknown", source="ping-sweep")
        out2, d2 = discovery.upsert_discovered(db, ip="192.0.2.20",
                                               name="discovered-192-0-2-20",
                                               device_type="unknown", source="ping-sweep")
        db.commit()
        self.assertEqual(out2, "updated")
        rows = _rows(ip="192.0.2.20")
        self.assertEqual(len(rows), 1, "repeated IP scan must not duplicate")
        db.close()

    def test_case_and_separator_insensitive_mac_match(self):
        db = SessionLocal()
        discovery.upsert_discovered(db, mac="aa:bb:cc:00:00:99", ip="192.0.2.30",
                                    name="x", source="t")
        db.commit()
        found = discovery.find_existing(db, mac="AA-BB-CC-00-00-99")
        self.assertIsNotNone(found)
        self.assertEqual(found.ip_address, "192.0.2.30")
        db.close()

    def test_discover_results_repeated_scan(self):
        db = SessionLocal()
        r1 = discover_results({"found": [{"ip": "192.0.2.40"}]}, db=db,
                              user=SimpleNamespace(role="agent"))
        r2 = discover_results({"found": [{"ip": "192.0.2.40"}]}, db=db,
                              user=SimpleNamespace(role="agent"))
        self.assertEqual(r1["added"], 1)
        self.assertEqual(r2["added"], 0)
        self.assertEqual(r2["updated"], 1)
        self.assertEqual(len(_rows(ip="192.0.2.40")), 1)
        db.close()

    def test_snmp_sweep_repeated_scan(self):
        db = SessionLocal()
        r1 = snmp_sweep_results({"found": [
            {"ip": "192.0.2.50", "sysname": "core-switch",
             "sysdescr": "Cisco IOS switch", "vendor": "Cisco"}]},
            db=db, user=SimpleNamespace(role="agent"))
        r2 = snmp_sweep_results({"found": [
            {"ip": "192.0.2.50", "sysname": "core-switch",
             "sysdescr": "Cisco IOS switch", "vendor": "Cisco"}]},
            db=db, user=SimpleNamespace(role="agent"))
        self.assertEqual(r1["added"], 1)
        self.assertEqual(r2["added"], 0)
        self.assertEqual(r2["updated"], 1)
        self.assertEqual(len(_rows(ip="192.0.2.50")), 1)
        db.close()

    # ── claimed records: never duplicated / overwritten ────────────────────

    def test_claimed_record_identity_never_overwritten(self):
        db = SessionLocal()
        db.add(Device(name="My Server", ip_address="192.0.2.60",
                      mac_address="aa:bb:cc:00:00:60", device_type="server",
                      claimed=True, device_group="default", status="online"))
        db.commit()
        # A discovery with a DIFFERENT identity lands on the same IP.
        outcome, dev = discovery.upsert_discovered(
            db, ip="192.0.2.60", mac="11:22:33:00:00:60",
            name="Other Box", device_type="workstation", source="ping-sweep")
        db.commit()
        self.assertEqual(outcome, "skipped_claimed")
        rec = _rows(ip="192.0.2.60")[0]
        self.assertEqual(rec.name, "My Server")
        self.assertEqual(rec.mac_address, "aa:bb:cc:00:00:60")
        self.assertTrue(rec.claimed)
        self.assertEqual(len(_rows(ip="192.0.2.60")), 1)
        db.close()

    def test_claimed_record_refreshed_without_stealing(self):
        db = SessionLocal()
        db.add(Device(name="Router", ip_address="192.0.2.70",
                      mac_address="aa:bb:cc:00:00:70", device_type="gateway",
                      claimed=True, device_group="default", status="unreachable"))
        db.commit()
        # Same physical box (same MAC) re-discovered: safe refresh only.
        outcome, dev = discovery.upsert_discovered(
            db, mac="AA:BB:CC:00:00:70", ip="192.0.2.70",
            name="Router", device_type="gateway", status="online",
            source="unifi")
        db.commit()
        self.assertEqual(outcome, "updated")
        rec = _rows(mac="aa:bb:cc:00:00:70")[0]
        self.assertEqual(rec.status, "online")
        self.assertEqual(rec.name, "Router")
        self.assertTrue(rec.claimed)
        db.close()

    # ── self-exclusion ─────────────────────────────────────────────────────

    def test_upsert_skips_appliance_ip(self):
        restore = _set_env(APPLIANCE_IP="192.0.2.207")
        try:
            db = SessionLocal()
            outcome, dev = discovery.upsert_discovered(
                db, ip="192.0.2.207", name="ubuntu-server",
                source="ping-sweep")
            db.commit()
            self.assertEqual(outcome, "skipped_self")
            self.assertEqual(len(_rows(ip="192.0.2.207")), 0)
            db.close()
        finally:
            restore()

    def test_upsert_skips_appliance_mac(self):
        restore = _set_env(SELF_EXCLUDE_MACS="aa:bb:cc:00:00:07")
        try:
            db = SessionLocal()
            outcome, dev = discovery.upsert_discovered(
                db, ip="192.0.2.77", mac="AA:BB:CC:00:00:07",
                name="appliance-nic", source="snmp-sweep")
            db.commit()
            self.assertEqual(outcome, "skipped_self")
            self.assertEqual(len(_rows(mac="aa:bb:cc:00:00:07")), 0)
            db.close()
        finally:
            restore()

    def test_snmp_sweep_self_excluded(self):
        restore = _set_env(APPLIANCE_IP="192.0.2.207")
        try:
            db = SessionLocal()
            r = snmp_sweep_results({"found": [
                {"ip": "192.0.2.207", "sysname": "ubuntu-server",
                 "sysdescr": "Linux ubuntu-server", "vendor": "Net-SNMP"}]},
                db=db, user=SimpleNamespace(role="agent"))
            self.assertEqual(r["added"], 0)
            self.assertEqual(r["skipped_self"], 1)
            self.assertEqual(len(_rows(ip="192.0.2.207")), 0)
            db.close()
        finally:
            restore()

    def test_discover_results_self_excluded(self):
        restore = _set_env(APPLIANCE_IP="192.0.2.207")
        try:
            db = SessionLocal()
            r = discover_results({"found": [{"ip": "192.0.2.207"}]},
                                 db=db, user=SimpleNamespace(role="agent"))
            self.assertEqual(r["added"], 0)
            self.assertEqual(r["skipped_self"], 1)
            self.assertEqual(len(_rows(ip="192.0.2.207")), 0)
            db.close()
        finally:
            restore()

    def test_unifi_sync_self_excluded(self):
        """The appliance's own IP coming back from the controller is skipped."""
        restore = _set_env(APPLIANCE_IP="192.0.2.207")
        try:
            init_db()
            db = SessionLocal()
            db.query(Device).delete()
            db.commit()
            devs = [
                {"name": "Main Gateway", "ip": "192.0.2.1", "mac": "aa:bb:cc:00:00:01",
                 "type": "gateway", "model": "UCG-Max", "status": "online"},
                {"name": "ubuntu-server", "ip": "192.0.2.207", "mac": "aa:bb:cc:00:00:07",
                 "type": "server", "model": "VM", "status": "online"},
            ]

            class FakeClient:
                def login(self):
                    return True

                def get_devices(self):
                    return list(devs)

                def get_clients(self):
                    return []

            with patch.object(unifi_sync, "_auth_ready", return_value=True), \
                 patch.object(unifi_sync, "_get_unifi_config",
                              return_value={"url": "x", "username": "u",
                                            "password": "p", "api_key": ""}), \
                 patch.object(unifi_sync, "_read_unifi_env",
                              return_value={"UNIFI_AUTO_ADOPT": "true"}), \
                 patch.object(unifi_sync, "_unifi_client",
                              return_value=FakeClient()):
                result = unifi_sync.sync_from_unifi(
                    db=db, user=SimpleNamespace(username="tester"))
            self.assertEqual(result["added"], 1)      # only the gateway
            self.assertEqual(result["skipped"], 1)    # the appliance
            self.assertEqual(len(_rows(ip="192.0.2.207")), 0)
            self.assertEqual(len(_rows(ip="192.0.2.1")), 1)
            db.close()
        finally:
            restore()


if __name__ == "__main__":
    unittest.main(verbosity=2)
