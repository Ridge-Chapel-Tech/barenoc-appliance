#!/usr/bin/env python3
"""Tests for the unified device-add model scaffold (device_adoption_model.md).

Covers:
  1. migration idempotency — init_db() twice + channels column usable;
  2. capability validator — actions declare required channels; enforce on a
     device with known channels; channel-less actions pass;
  3. fingerprint suggestion — type + ranked channels + security-first
     recommendation + warnings;
  4. effective_channels — derived ∪ explicit;
  5. validate_job channel gate — rejects a channel-mismatched job targeting a
     managed device, passes through unknown channels;
  6. UI smoke — the Devices page carries the 08-17 relabeled actions.

    docker compose exec api python3 -m unittest test_device_channels -v
"""

import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="device-channels-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device
from action_validator import (
    CHANNEL_AGENT, CHANNEL_SSH, CHANNEL_SNMP, CHANNEL_MONITOR, CHANNEL_VENDOR_API,
    effective_channels, validate_channels, suggest_from_fingerprint,
    validate_job, MANAGED_DEVICES, canonical_device_type,
)


class MigrationTest(unittest.TestCase):
    def test_init_db_idempotent_and_channels_column(self):
        init_db()
        init_db()  # second run must be a no-op
        db = SessionLocal()
        try:
            d = Device(name="sw", ip_address="10.0.0.1", device_type="switch",
                       channels=["vendor_api"])
            db.add(d)
            db.commit()
            self.assertEqual(d.channels, ["vendor_api"])
            # the column persists a JSON list (SQLite stores it as text)
            d2 = db.query(Device).filter(Device.id == d.id).first()
            self.assertEqual(d2.channels, ["vendor_api"])
        finally:
            db.close()


class EffectiveChannelsTest(unittest.TestCase):
    def test_monitor_always_present(self):
        self.assertEqual(effective_channels(), [CHANNEL_MONITOR])

    def test_derived_plus_explicit(self):
        ch = effective_channels(ssh_configured=True, agent_connected=True,
                                explicit=[CHANNEL_VENDOR_API])
        self.assertIn(CHANNEL_SSH, ch)
        self.assertIn(CHANNEL_AGENT, ch)
        self.assertIn(CHANNEL_VENDOR_API, ch)
        self.assertIn(CHANNEL_MONITOR, ch)

    def test_explicit_ignores_unknown(self):
        ch = effective_channels(explicit=["bogus", CHANNEL_SNMP])
        self.assertNotIn("bogus", ch)
        self.assertIn(CHANNEL_SNMP, ch)


class ChannelValidatorTest(unittest.TestCase):
    def test_reboot_accepts_ssh(self):
        ok, _ = validate_channels("reboot_device", "switch", [CHANNEL_SSH, CHANNEL_MONITOR])
        self.assertTrue(ok)

    def test_reboot_rejects_monitor_only(self):
        ok, msg = validate_channels("reboot_device", "switch", [CHANNEL_MONITOR])
        self.assertFalse(ok)
        self.assertIn("reboot_device", msg)

    def test_collect_logs_needs_ssh_or_agent(self):
        self.assertTrue(validate_channels("collect_logs", "server",
                                          [CHANNEL_AGENT])[0])
        self.assertFalse(validate_channels("collect_logs", "server",
                                           [CHANNEL_SNMP, CHANNEL_MONITOR])[0])

    def test_monitor_only_camera_gets_ping_status(self):
        # ping/status/fingerprint are channel-agnostic → always allowed
        self.assertTrue(validate_channels("ping_test", "camera", [CHANNEL_MONITOR])[0])
        self.assertTrue(validate_channels("device_status", "camera", [CHANNEL_MONITOR])[0])
        self.assertTrue(validate_channels("fingerprint_device", "camera", [CHANNEL_MONITOR])[0])

    def test_snmp_poll_needs_snmp(self):
        self.assertTrue(validate_channels("snmp_poll", "switch", [CHANNEL_SNMP])[0])
        self.assertFalse(validate_channels("snmp_poll", "switch", [CHANNEL_MONITOR])[0])

    def test_unknown_channels_pass(self):
        self.assertTrue(validate_channels("reboot_device", "switch", None)[0])

    def test_channel_less_action_passes(self):
        # unifi_* / network_* are controller/appliance-side → no requirement
        self.assertTrue(validate_channels("unifi_restart", "switch", [])[0])
        self.assertTrue(validate_channels("network_discovery", "other", [])[0])


class FingerprintSuggestionTest(unittest.TestCase):
    def test_switch_ranks_vendor_api_over_insecure_snmp(self):
        r = suggest_from_fingerprint({
            "open_ports": [{"port": 161, "service": "snmp"},
                           {"port": 22, "service": "ssh"}],
            "vendor": "Cisco", "sysdescr": "Cisco IOS switch", "os": "Cisco IOS"})
        self.assertEqual(r["device_type"], "switch")
        self.assertEqual(r["recommendation"], CHANNEL_VENDOR_API)
        self.assertTrue(any("v2c" in w for w in r["warnings"]))

    def test_camera_plaintext_recommends_monitor(self):
        r = suggest_from_fingerprint({
            "open_ports": [{"port": 80, "service": "http"},
                           {"port": 554, "service": "rtsp"}],
            "vendor": "Hikvision", "os": ""})
        self.assertEqual(r["device_type"], "camera")
        self.assertEqual(r["recommendation"], CHANNEL_MONITOR)
        self.assertTrue(r["warnings"])

    def test_server_recommends_agent(self):
        r = suggest_from_fingerprint({
            "open_ports": [{"port": 22, "service": "ssh"}],
            "ssh_banner": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5",
            "os": "Ubuntu Linux"})
        self.assertEqual(r["device_type"], "server")
        self.assertEqual(r["recommendation"], CHANNEL_AGENT)

    def test_iot_http_recommends_monitor(self):
        r = suggest_from_fingerprint({
            "open_ports": [{"port": 80, "service": "http"}], "os": ""})
        self.assertEqual(r["device_type"], "iot")
        self.assertEqual(r["recommendation"], CHANNEL_MONITOR)

    def test_unknown_falls_to_monitor(self):
        r = suggest_from_fingerprint({"open_ports": []})
        self.assertEqual(r["device_type"], "other")
        self.assertEqual(r["recommendation"], CHANNEL_MONITOR)


class CanonicalTypeTest(unittest.TestCase):
    def test_legacy_mapping(self):
        self.assertEqual(canonical_device_type("gateway"), "router")
        self.assertEqual(canonical_device_type("workstation"), "server")
        self.assertEqual(canonical_device_type("printer"), "iot")
        self.assertEqual(canonical_device_type("switch"), "switch")


class ValidateJobChannelGateTest(unittest.TestCase):
    def tearDown(self):
        MANAGED_DEVICES.clear()

    def test_rejects_channel_mismatch_for_known_device(self):
        MANAGED_DEVICES["10.0.0.9"] = {"id": 1, "ip": "10.0.0.9",
                                       "type": "camera",
                                       "hostname": None,
                                       "channels": [CHANNEL_MONITOR]}
        ok, msg = validate_job({"action": "reboot_device", "target": "10.0.0.9",
                                "params": {}})
        self.assertFalse(ok)
        self.assertIn("reboot_device", msg)

    def test_passes_unknown_device_channels(self):
        ok, _ = validate_job({"action": "reboot_device", "target": "10.9.9.9",
                              "params": {}})
        self.assertTrue(ok)

    def test_passes_monitor_only_ping(self):
        MANAGED_DEVICES["10.0.0.9"] = {"id": 1, "ip": "10.0.0.9",
                                       "type": "camera",
                                       "hostname": None,
                                       "channels": [CHANNEL_MONITOR]}
        ok, _ = validate_job({"action": "ping_test", "target": "10.0.0.9",
                              "params": {}})
        self.assertTrue(ok)


class DevicesUiSmokeTest(unittest.TestCase):
    def test_relabeled_actions_present(self):
        path = os.path.join(os.path.dirname(__file__), "templates", "devices.html")
        with open(path) as f:
            html = f.read()
        for label in ("➕ Adopt a device",
                      "Adopt",
                      "Add to inventory (no credentials)",
                      "SSH credentials",
                      "Desktop agent",
                      "Register via API",
                      "Enable control",
                      "Identify",
                      "Identify All",
                      "Onboarded devices",
                      "Unclaimed devices"):
            self.assertIn(label, html, f"missing UI label: {label}")

    def test_no_monitoring_limbo_section(self):
        path = os.path.join(os.path.dirname(__file__), "templates", "devices.html")
        with open(path) as f:
            html = f.read()
        self.assertNotIn('id="monitoring-section"', html)
        self.assertNotIn("Monitoring Only", html)
        self.assertNotIn("owned, no control channel", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DeviceResponseNullChannelsTest(unittest.TestCase):
    """Regression (08-17 gate): a device with channels=NULL in the DB (the
    migration added the column; pre-existing rows are NULL) must serialize
    through DeviceResponse — pydantic v2 rejects `None` for `list` fields even
    with a default, which 500'd GET /api/v1/devices for ALL devices after #24."""

    def test_channels_none_serializes(self):
        from schemas import DeviceResponse
        import datetime
        base = dict(
            id=1, name="Old Device", hostname="old", ip_address="192.0.2.9",
            device_type="other", status="online", claimed=True,
            tags=[], created_at=datetime.datetime.utcnow(),
        )
        # channels absent → default (None is fine) — the #24 regression was
        # channels present-but-None (ORM column NULL on pre-existing rows).
        for channels in (None, [], ["monitor"]):
            d = dict(base, channels=channels)
            resp = DeviceResponse(**d)
            self.assertIsNone(resp.channels) if channels is None else self.assertEqual(resp.channels, channels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
