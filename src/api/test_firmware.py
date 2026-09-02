#!/usr/bin/env python3
"""In-container tests for the firmware-management engine (UniFi v1).

Runs inside the barenoc-api container (needs SQLAlchemy/FastAPI + shared
modules). Uses a scratch sqlite DB and a fake UniFi controller — no live DB, no
controller calls. Matches the test_unifi_sync / test_alerting patterns.

    docker compose exec api python3 -m unittest test_firmware -v
"""

import os
import tempfile
import unittest
import datetime
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="firmware-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import (DeviceFirmware, FirmwareUpgrade, MaintenanceWindow,
                    PendingAction, Ticket)

import firmware
from routes import firmware as firmware_routes


class FakeController:
    """A fake UniFi controller that models the stat/device + cmd/devmgr surface
    the engine uses. ``upgrade_device`` bumps the version and drops the device
    to state 0 (rebooting); the test flips state back to 1 to model "device
    returned + informing"."""

    def __init__(self, devices):
        self.devices = {d["mac"]: dict(d) for d in devices}
        self.cache_calls = []
        self.upgrade_calls = []
        self.rollback_calls = []

    def login(self):
        return True

    def get_devices(self):
        out = []
        for mac, d in self.devices.items():
            out.append({
                "mac": mac, "name": d.get("name", mac),
                "type": d.get("type", "unknown"),
                "model": d.get("model", ""), "ip": d.get("ip", ""),
                "version": d.get("version", ""),
                "previous_version": d.get("previous_version", ""),
                "available_version": d.get("available_version", ""),
                "upgradeable": d.get("upgradeable", False),
                "status": "online" if d.get("state", 0) == 1 else "offline",
                "state": d.get("state", 0),
            })
        return out

    def get_device(self, mac):
        d = self.devices.get(mac)
        if not d:
            return None
        return {"mac": mac, "name": d.get("name", mac), "type": d.get("type"),
                "version": d.get("version", ""), "state": d.get("state", 0)}

    def cache_firmware(self, mac):
        self.cache_calls.append(mac)
        return mac in self.devices

    def upgrade_device(self, mac, version=None):
        d = self.devices.get(mac)
        if not d:
            return False
        self.upgrade_calls.append((mac, version))
        d["version"] = version or d.get("available_version", d.get("version"))
        d["state"] = 0  # rebooting
        return True

    def rollback_device(self, mac, previous_version):
        self.rollback_calls.append((mac, previous_version))
        return self.upgrade_device(mac, previous_version)


AUTONOMOUS = {"FIRMWARE_AUTONOMY": "autonomous", "LLM_POLICY_PROFILE": "",
              "TZ": "UTC", "FIRMWARE_TECH_VISIBILITY": ""}
BALANCED = {"FIRMWARE_AUTONOMY": "balanced", "LLM_POLICY_PROFILE": "",
            "TZ": "UTC", "FIRMWARE_TECH_VISIBILITY": ""}
STRICT = {"FIRMWARE_AUTONOMY": "strict", "LLM_POLICY_PROFILE": "",
          "TZ": "UTC", "FIRMWARE_TECH_VISIBILITY": ""}
OFF = {"FIRMWARE_AUTONOMY": "off", "LLM_POLICY_PROFILE": "",
       "TZ": "UTC", "FIRMWARE_TECH_VISIBILITY": ""}

T0 = datetime.datetime(2026, 8, 19, 3, 0, 0)


def _clean(db, *models):
    for m in models:
        db.query(m).delete()
    db.commit()


def _mk_window(db, hour=3, duration=60, mode="recurring", day="daily",
               when="", enabled=True, name="night"):
    w = MaintenanceWindow(name=name, mode=mode, day=day, hour=hour,
                          duration_minutes=duration, when=when,
                          enabled=enabled, timezone="UTC")
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


class WindowGatingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        _clean(db, DeviceFirmware, FirmwareUpgrade, PendingAction,
               MaintenanceWindow, Ticket)
        db.close()

    def test_window_active_recurring(self):
        w = _mk_window(SessionLocal(), hour=3, duration=60)
        self.assertTrue(firmware.window_active(w, T0))
        self.assertTrue(firmware.window_active(w, T0 + datetime.timedelta(minutes=59)))
        self.assertFalse(firmware.window_active(w, T0 - datetime.timedelta(minutes=1)))
        self.assertFalse(firmware.window_active(w, T0 + datetime.timedelta(minutes=60)))
        w2 = _mk_window(SessionLocal(), hour=3, duration=60, enabled=False)
        self.assertFalse(firmware.window_active(w2, T0))

    def test_window_active_onetime(self):
        w = _mk_window(SessionLocal(), mode="onetime", when="2026-08-19T03:00",
                       duration=60)
        self.assertTrue(firmware.window_active(w, T0))
        self.assertTrue(firmware.window_active(w, T0 + datetime.timedelta(minutes=30)))
        self.assertFalse(firmware.window_active(w, T0 + datetime.timedelta(minutes=61)))

    def test_no_upgrade_outside_window(self):
        db = SessionLocal()
        fake = FakeController([
            {"mac": "aa:00:01", "name": "AP", "type": "ap", "version": "6.6.50",
             "available_version": "6.6.55", "upgradeable": True, "state": 1},
        ])
        _mk_window(db, hour=3, duration=60)
        # 10:00 local — far from the 03:00 window (and outside the pre-stage lead).
        r = firmware.engine_tick(db, fake, datetime.datetime(2026, 8, 19, 10, 0, 0),
                                 AUTONOMOUS)
        self.assertEqual(r["status"], "no_window")
        self.assertEqual(r["prestaged"], 0)
        self.assertEqual(db.query(FirmwareUpgrade).count(), 0)
        self.assertEqual(fake.upgrade_calls, [])
        self.assertEqual(fake.cache_calls, [])
        db.close()


class SequencingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        _clean(db, DeviceFirmware, FirmwareUpgrade, PendingAction,
               MaintenanceWindow, Ticket)
        db.close()

    def test_one_at_a_time_and_verify_sequencing(self):
        db = SessionLocal()
        fake = FakeController([
            {"mac": "aa:00:01", "name": "AP-Living", "type": "ap", "model": "U6",
             "version": "6.6.50", "available_version": "6.6.55",
             "upgradeable": True, "state": 1},
            {"mac": "aa:00:02", "name": "SW-Core", "type": "switch", "model": "USW",
             "version": "6.6.50", "available_version": "6.6.55",
             "upgradeable": True, "state": 1},
        ])
        _mk_window(db, hour=3, duration=60)

        r = firmware.engine_tick(db, fake, T0, AUTONOMOUS)
        self.assertEqual(r["status"], "started")
        self.assertEqual(r["device"], "aa:00:01")   # AP first (risk order)

        # Only one in-flight upgrade exists at a time.
        inflight = db.query(FirmwareUpgrade).filter(
            FirmwareUpgrade.status.in_(firmware.INFLIGHT_STATUSES)).all()
        self.assertEqual(len(inflight), 1)
        u = inflight[0]
        self.assertEqual(u.device_type, "ap")
        self.assertEqual(u.status, "staging")

        # Pre-stage (cache) happens on the first advance.
        firmware.engine_tick(db, fake, T0, AUTONOMOUS)
        self.assertEqual(fake.cache_calls, ["aa:00:01"])

        # After the staging settle, the apply command fires and the device reboots.
        firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=61), AUTONOMOUS)
        self.assertEqual(fake.upgrade_calls, [("aa:00:01", "6.6.55")])
        db.expire_all()
        u = db.query(FirmwareUpgrade).get(u.id)
        self.assertEqual(u.status, "verifying")

        # The device has NOT come back yet — verify keeps waiting (no next device).
        r = firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=62), AUTONOMOUS)
        self.assertEqual(r["status"], "advancing")
        self.assertEqual(fake.upgrade_calls, [("aa:00:01", "6.6.55")])

        # Device returns + informing (state 1) with the new version → verified.
        fake.devices["aa:00:01"]["state"] = 1
        firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=63), AUTONOMOUS)
        db.expire_all()
        u = db.query(FirmwareUpgrade).get(u.id)
        self.assertEqual(u.status, "success")
        self.assertIn("total", u.durations)

        # Next tick picks the switch (still strictly one-at-a-time).
        r = firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=64), AUTONOMOUS)
        self.assertEqual(r["status"], "started")
        self.assertEqual(r["device"], "aa:00:02")
        db.close()

    def test_verify_timeout_triggers_rollback_and_recovers(self):
        db = SessionLocal()
        fake = FakeController([
            {"mac": "aa:00:01", "name": "AP", "type": "ap", "version": "6.6.50",
             "previous_version": "6.6.44", "available_version": "6.6.55",
             "upgradeable": True, "state": 1},
        ])
        _mk_window(db, hour=3, duration=60)

        firmware.engine_tick(db, fake, T0, AUTONOMOUS)
        firmware.engine_tick(db, fake, T0, AUTONOMOUS)  # cache
        firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=61), AUTONOMOUS)  # apply
        u = db.query(FirmwareUpgrade).first()
        self.assertEqual(u.status, "verifying")

        # Verify times out (device never recovers) → rollback attempted.
        firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=61 + firmware.VERIFY_TIMEOUT_S + 1),
                             AUTONOMOUS)
        db.expire_all()
        u = db.query(FirmwareUpgrade).get(u.id)
        self.assertEqual(u.status, "rolling_back")
        self.assertTrue(u.rollback_attempted)
        self.assertEqual(fake.rollback_calls, [("aa:00:01", "6.6.50")])

        # Rollback recovers the device → rolled_back (not a physical escalation).
        fake.devices["aa:00:01"]["state"] = 1
        firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=62 + firmware.VERIFY_TIMEOUT_S),
                             AUTONOMOUS)
        db.expire_all()
        u = db.query(FirmwareUpgrade).get(u.id)
        self.assertEqual(u.status, "rolled_back")
        self.assertEqual(db.query(Ticket).count(), 0)  # no ticket on clean rollback
        db.close()

    def test_double_failure_escalates_physical(self):
        db = SessionLocal()
        fake = FakeController([
            {"mac": "aa:00:01", "name": "AP", "type": "ap", "version": "6.6.50",
             "previous_version": "6.6.44", "available_version": "6.6.55",
             "upgradeable": True, "state": 1},
        ])
        _mk_window(db, hour=3, duration=60)

        firmware.engine_tick(db, fake, T0, AUTONOMOUS)
        firmware.engine_tick(db, fake, T0, AUTONOMOUS)  # cache
        firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=61), AUTONOMOUS)  # apply
        # verify timeout → rollback
        firmware.engine_tick(db, fake, T0 + datetime.timedelta(seconds=61 + firmware.VERIFY_TIMEOUT_S + 1),
                             AUTONOMOUS)
        # rollback verify timeout (device stays down) → physical escalation
        firmware.engine_tick(db, fake,
                             T0 + datetime.timedelta(seconds=61 + firmware.VERIFY_TIMEOUT_S + firmware.ROLLBACK_VERIFY_TIMEOUT_S + 2),
                             AUTONOMOUS)
        db.expire_all()
        u = db.query(FirmwareUpgrade).first()
        self.assertEqual(u.status, "failed")

        esc = db.query(PendingAction).filter(PendingAction.kind == "escalation",
                                             PendingAction.status == "pending").all()
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0].required_role, "admin")
        self.assertEqual(esc[0].extra.get("severity"), "P1")
        self.assertIn("runbook", (esc[0].extra or {}))
        tickets = db.query(Ticket).all()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].priority, "P1")
        # The escalation halts the run: no next device starts.
        fake.devices["aa:00:02"] = {"mac": "aa:00:02", "name": "SW", "type": "switch",
                                    "version": "6.6.50", "available_version": "6.6.55",
                                    "upgradeable": True, "state": 1}
        r = firmware.engine_tick(db, fake, T0, AUTONOMOUS)
        self.assertEqual(r["status"], "nothing_due")  # halted, not "started"
        db.close()


class AutonomyMatrixTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        _clean(db, DeviceFirmware, FirmwareUpgrade, PendingAction,
               MaintenanceWindow, Ticket)
        db.close()

    def test_approval_decision_matrix(self):
        cases = [
            ("autonomous", "ap", "auto"), ("autonomous", "switch", "auto"),
            ("autonomous", "gateway", "auto"),
            ("balanced", "ap", "auto"), ("balanced", "switch", "auto"),
            ("strict", "ap", "approval"), ("strict", "switch", "approval"),
            ("off", "ap", "disabled"), ("off", "gateway", "disabled"),
        ]
        for profile, dtype, decision in cases:
            got, role = firmware.approval_decision(profile, dtype)
            self.assertEqual(got, decision, f"{profile}/{dtype}")

        # balanced gateway → explicit admin approval
        got, role = firmware.approval_decision("balanced", "gateway")
        self.assertEqual((got, role), ("approval", "admin"))
        # strict gateway → admin; strict non-gateway → technician
        self.assertEqual(firmware.approval_decision("strict", "gateway"),
                         ("approval", "admin"))
        self.assertEqual(firmware.approval_decision("strict", "ap"),
                         ("approval", "technician"))

    def test_effective_autonomy(self):
        self.assertEqual(firmware.effective_autonomy({"FIRMWARE_AUTONOMY": "off"}), "off")
        self.assertEqual(firmware.effective_autonomy(
            {"FIRMWARE_AUTONOMY": "", "LLM_POLICY_PROFILE": "autonomous"}), "autonomous")
        self.assertEqual(firmware.effective_autonomy(
            {"FIRMWARE_AUTONOMY": "strict", "LLM_POLICY_PROFILE": "autonomous"}), "strict")
        self.assertEqual(firmware.effective_autonomy({}), "balanced")

    def test_balanced_gateway_requires_approval_then_upgrades(self):
        db = SessionLocal()
        fake = FakeController([
            {"mac": "aa:00:03", "name": "GW", "type": "gateway", "version": "1.0.0",
             "available_version": "1.0.1", "upgradeable": True, "state": 1},
        ])
        _mk_window(db, hour=3, duration=60)

        r = firmware.engine_tick(db, fake, T0, BALANCED)
        self.assertEqual(r["status"], "awaiting_approval")
        pa = db.query(PendingAction).filter(PendingAction.kind == "approval").first()
        self.assertIsNotNone(pa)
        self.assertEqual(pa.required_role, "admin")
        self.assertEqual(pa.status, "pending")
        self.assertEqual(db.query(FirmwareUpgrade).count(), 0)

        # Second tick dedups — still one approval, no upgrade.
        firmware.engine_tick(db, fake, T0, BALANCED)
        self.assertEqual(db.query(PendingAction).filter(
            PendingAction.kind == "approval").count(), 1)

        # An admin approves → next tick starts the gateway upgrade.
        firmware_routes.approve_pending(pa.id, body=None, db=db,
                                        user=SimpleNamespace(role="admin", username="admin"))
        r = firmware.engine_tick(db, fake, T0, BALANCED)
        self.assertEqual(r["status"], "started")
        self.assertEqual(r["device"], "aa:00:03")
        db.close()

    def test_strict_requires_approval_for_every_device(self):
        db = SessionLocal()
        fake = FakeController([
            {"mac": "aa:00:01", "name": "AP", "type": "ap", "version": "6.6.50",
             "available_version": "6.6.55", "upgradeable": True, "state": 1},
        ])
        _mk_window(db, hour=3, duration=60)
        r = firmware.engine_tick(db, fake, T0, STRICT)
        self.assertEqual(r["status"], "awaiting_approval")
        pa = db.query(PendingAction).filter(PendingAction.kind == "approval").first()
        self.assertEqual(pa.required_role, "technician")
        self.assertEqual(db.query(FirmwareUpgrade).count(), 0)
        db.close()

    def test_off_disables_engine(self):
        db = SessionLocal()
        fake = FakeController([
            {"mac": "aa:00:01", "name": "AP", "type": "ap", "version": "6.6.50",
             "available_version": "6.6.55", "upgradeable": True, "state": 1},
        ])
        _mk_window(db, hour=3, duration=60)
        r = firmware.engine_tick(db, fake, T0, OFF)
        self.assertEqual(r["status"], "off")
        self.assertEqual(db.query(FirmwareUpgrade).count(), 0)
        self.assertEqual(db.query(PendingAction).count(), 0)
        db.close()


class PendingQueueTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        _clean(db, DeviceFirmware, FirmwareUpgrade, PendingAction,
               MaintenanceWindow, Ticket)
        db.close()

    def test_persistence(self):
        db = SessionLocal()
        pa = PendingAction(kind="approval", title="t", device_name="GW",
                           device_type="gateway", firmware_from="1",
                           firmware_to="2", status="pending",
                           required_role="admin", auto=False,
                           extra={"runbook": "walk to the AP"})
        db.add(pa)
        db.commit()
        db.refresh(pa)
        pid = pa.id
        db.close()

        db2 = SessionLocal()
        got = db2.query(PendingAction).get(pid)
        self.assertIsNotNone(got)
        self.assertEqual(got.required_role, "admin")
        self.assertEqual(got.extra.get("runbook"), "walk to the AP")
        db2.close()

    def test_role_visibility(self):
        db = SessionLocal()
        gw = PendingAction(kind="approval", title="gateway approval",
                           device_type="gateway", required_role="admin",
                           status="pending")
        ap = PendingAction(kind="approval", title="AP approval", device_type="ap",
                           required_role="technician", status="pending")
        db.add_all([gw, ap])
        db.commit()

        admin = SimpleNamespace(role="admin")
        op = SimpleNamespace(role="operator")
        self.assertTrue(firmware_routes._can_see(admin, gw))
        self.assertTrue(firmware_routes._can_see(admin, ap))
        self.assertTrue(firmware_routes._can_act(admin, gw))

        with patch.object(firmware, "technician_visibility_enabled", return_value=False):
            self.assertFalse(firmware_routes._can_see(op, gw))
            self.assertFalse(firmware_routes._can_see(op, ap))
        with patch.object(firmware, "technician_visibility_enabled", return_value=True):
            self.assertFalse(firmware_routes._can_see(op, gw))  # gateway admin-only
            self.assertTrue(firmware_routes._can_see(op, ap))
            self.assertTrue(firmware_routes._can_act(op, ap))
            self.assertFalse(firmware_routes._can_act(op, gw))
        db.close()

    def test_escalate_promotes_to_admin_escalation(self):
        db = SessionLocal()
        ap = PendingAction(kind="approval", title="AP approval", device_type="ap",
                           required_role="technician", status="pending")
        db.add(ap)
        db.commit()
        db.refresh(ap)
        firmware_routes.escalate_pending(ap.id, body=None, db=db,
                                         user=SimpleNamespace(role="admin", username="admin"))
        db.expire_all()
        got = db.query(PendingAction).get(ap.id)
        self.assertEqual(got.kind, "escalation")
        self.assertEqual(got.required_role, "admin")
        self.assertEqual(got.status, "pending")
        db.close()


class ServiceSummaryTest(unittest.TestCase):
    """The managed-service snapshot (pricing tie-in: firmware as a service)."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        _clean(db, DeviceFirmware, FirmwareUpgrade, PendingAction,
               MaintenanceWindow, Ticket)
        db.close()

    def _seed(self):
        db = SessionLocal()
        db.add(DeviceFirmware(mac_address="aa:00:01", name="AP", device_type="ap",
                              current_version="6.6.50",
                              available_version="6.6.55", upgradeable=True,
                              online=True))
        db.add(DeviceFirmware(mac_address="aa:00:02", name="SW", device_type="switch",
                              current_version="6.6.55", upgradeable=False,
                              online=True))
        db.commit()
        return db

    def test_plan_positioning(self):
        db = SessionLocal()
        s = firmware.service_summary(db, now=T0, env=AUTONOMOUS,
                                     unifi_configured=False)
        self.assertEqual(s["plan"]["tier"], "managed")
        self.assertEqual(s["plan"]["name"], "Managed network service")
        self.assertTrue(s["plan"]["beta"])
        self.assertEqual(s["state"], "not_configured")
        db.close()

    def test_off(self):
        db = self._seed()
        s = firmware.service_summary(db, now=T0, env=OFF, unifi_configured=True)
        self.assertEqual(s["state"], "off")
        db.close()

    def test_needs_window(self):
        db = self._seed()
        s = firmware.service_summary(db, now=T0, env=AUTONOMOUS,
                                     unifi_configured=True)
        self.assertEqual(s["state"], "needs_window")
        self.assertEqual(s["coverage"]["total"], 2)
        self.assertEqual(s["coverage"]["upgradeable"], 1)
        self.assertEqual(s["coverage"]["up_to_date"], 1)
        self.assertEqual(s["coverage"]["by_type"], {"ap": 1, "switch": 1})
        db.close()

    def test_active_with_window(self):
        db = self._seed()
        _mk_window(db, hour=3, duration=60)
        s = firmware.service_summary(db, now=T0, env=AUTONOMOUS,
                                     unifi_configured=True)
        self.assertEqual(s["state"], "active")
        self.assertEqual(s["windows"]["enabled"], 1)
        self.assertTrue(s["windows"]["active_now"])
        self.assertIsNotNone(s["windows"]["next"])
        db.close()

    def test_awaiting_approval(self):
        db = self._seed()
        _mk_window(db, hour=3, duration=60)
        db.add(PendingAction(kind="approval", title="t", mac_address="aa:00:01",
                             status="pending", required_role="admin"))
        db.commit()
        s = firmware.service_summary(db, now=T0, env=BALANCED,
                                     unifi_configured=True)
        self.assertEqual(s["state"], "awaiting_approval")
        self.assertEqual(s["pending"]["approvals_pending"], 1)
        db.close()

    def test_escalated(self):
        db = self._seed()
        _mk_window(db, hour=3, duration=60)
        db.add(PendingAction(kind="escalation", title="t", mac_address="aa:00:01",
                             status="pending", required_role="admin",
                             extra={"severity": "P1"}))
        db.commit()
        s = firmware.service_summary(db, now=T0, env=AUTONOMOUS,
                                     unifi_configured=True)
        self.assertEqual(s["state"], "escalated")
        self.assertEqual(s["pending"]["escalations_pending"], 1)
        db.close()

    def test_healthy(self):
        db = SessionLocal()
        db.add(DeviceFirmware(mac_address="aa:00:02", name="SW", device_type="switch",
                              current_version="6.6.55", upgradeable=False,
                              online=True))
        db.commit()
        _mk_window(db, hour=3, duration=60)
        s = firmware.service_summary(db, now=T0, env=AUTONOMOUS,
                                     unifi_configured=True)
        self.assertEqual(s["state"], "healthy")
        self.assertEqual(s["coverage"]["upgradeable"], 0)
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
