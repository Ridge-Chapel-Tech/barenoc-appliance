#!/usr/bin/env python3
"""Tests for the two-state Devices layout (devices-layout-v2).

Covers:
  1. UI — the page renders exactly two sections: an unclaimed LIST +
     an onboarded GRID; the "Monitoring Only" limbo section is gone;
     the 🔔 monitor toggle (toggleNotify) + Monitored filter chip are present.
  2. API — the monitor toggle flips Device.notify_state_changes via PATCH
     (DeviceUpdate), which is what the alerting engine reads.
  3. API — two-state: list_devices(claimed=True) returns EVERY claimed device
     including channel-less monitor-only devices (a camera with no control
     channel is Onboarded, not a third bucket).

    docker compose exec api python3 -m unittest test_device_layout -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="device-layout-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device
from schemas import DeviceUpdate
from routes.devices import list_devices, update_device

ADMIN_CTX = {"user": SimpleNamespace(role="admin"), "groups": []}


def _add(name, ip, claimed=True, **kw):
    db = SessionLocal()
    d = Device(name=name, ip_address=ip,
               device_type=kw.pop("device_type", "switch"),
               status="online", claimed=claimed, **kw)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _list(**kw):
    return list_devices(limit=100, offset=0, db=SessionLocal(), ctx=ADMIN_CTX, **kw)


class DevicesPageLayoutTest(unittest.TestCase):
    def test_two_sections_and_no_monitoring_limbo(self):
        path = os.path.join(os.path.dirname(__file__), "templates", "devices.html")
        with open(path) as f:
            html = f.read()
        # Two states only.
        self.assertIn('id="unclaimed-grid"', html)   # unclaimed LIST
        self.assertIn('id="devices-grid"', html)     # onboarded GRID
        # The monitor toggle that flips notify_state_changes is present.
        self.assertIn("toggleNotify", html)
        self.assertIn("toggleMonitored", html)
        # No "Monitoring Only" section / limbo remains.
        self.assertNotIn('id="monitoring-section"', html)
        self.assertNotIn("Monitoring Only", html)
        self.assertNotIn("owned, no control channel", html)

    def test_onboarded_heading_and_monitored_chip(self):
        path = os.path.join(os.path.dirname(__file__), "templates", "devices.html")
        with open(path) as f:
            html = f.read()
        self.assertIn("Onboarded devices", html)
        self.assertIn("monitored-chip", html)


class MonitorToggleTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()
        self.device_id = _add("cam-1", "10.0.0.20", device_type="camera",
                              channels=["monitor"])

    def test_toggle_flips_notify_state_changes(self):
        r = update_device(self.device_id, DeviceUpdate(notify_state_changes=True),
                          db=SessionLocal(), ctx=ADMIN_CTX)
        self.assertTrue(r["notify_state_changes"])
        db = SessionLocal()
        d = db.query(Device).get(self.device_id)
        self.assertTrue(d.notify_state_changes)
        db.close()
        # and back off
        r2 = update_device(self.device_id, DeviceUpdate(notify_state_changes=False),
                           db=SessionLocal(), ctx=ADMIN_CTX)
        self.assertFalse(r2["notify_state_changes"])


class TwoStateListTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()
        # monitor-only camera (no control channel) — must still be onboarded
        _add("mon-cam", "10.0.0.30", device_type="camera", channels=["monitor"])
        # ssh-controlled switch
        _add("sw-ssh", "10.0.0.31", device_type="switch", ssh_key_fingerprint="fp")
        # unclaimed
        _add("disc-1", "10.0.0.32", claimed=False)

    def test_onboarded_includes_channel_less_monitor_device(self):
        r = _list(claimed=True)
        names = [d["name"] for d in r["devices"]]
        self.assertIn("mon-cam", names)   # monitor-only camera is Onboarded
        self.assertIn("sw-ssh", names)
        self.assertNotIn("disc-1", names)

    def test_channels_present_in_response(self):
        r = _list(claimed=True)
        cam = next(d for d in r["devices"] if d["name"] == "mon-cam")
        self.assertIn("monitor", cam["channels"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
