#!/usr/bin/env python3
"""Tests for the knowledge-layer L1 environment state model.

In-container / CI (scratch sqlite DB, no live UniFi/SNMP). Pins the locked
brief's invariants:
  * capability resolution catalog -> probe -> conservative floor, with a
    per-capability confidence basis (catalog-verified / probed / unknown-floor)
  * action-channel computation (channel x permission)
  * the digest is compact and contains NO secrets
  * unknown devices are flagged first-class + carry a catalog_contribution hint

    docker compose exec api python3 -m unittest test_environment_state -v
"""

import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="env-state-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device, DeviceFirmware
import environment_state as es


def _add_device(**kw) -> int:
    db = SessionLocal()
    defaults = dict(name="d", ip_address="192.0.2.1", claimed=True,
                    device_type="other", status="online", device_group="default")
    defaults.update(kw)
    d = Device(**defaults)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _device(did):
    db = SessionLocal()
    d = db.query(Device).filter(Device.id == did).first()
    db.close()
    return d


class _DB:
    """Context manager yielding a session, mirroring the discovery test style."""

    def __enter__(self):
        self.db = SessionLocal()
        return self.db

    def __exit__(self, *a):
        self.db.close()


class CapabilityResolutionTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(DeviceFirmware).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    # ── catalog (known class) ───────────────────────────────────────────────

    def test_unifi_gateway_resolves_catalog_verified(self):
        did = _add_device(name="gw", ip_address="192.0.2.2",
                          vendor="Ubiquiti", model="UCG-Max",
                          device_type="gateway", unifi_managed=True)
        res = es.resolve_capabilities(_device(did))
        self.assertEqual(res["confidence"], "catalog-verified")
        self.assertEqual(res["catalog_id"], "unifi-gateway")
        self.assertIn("vlan_8021q", res["capabilities"])
        self.assertIn("firmware_feed", res["capabilities"])
        for cap, meta in res["capabilities"].items():
            self.assertEqual(meta["basis"], "catalog-verified")
            self.assertIn("catalog:unifi-gateway", meta["source"])

    def test_unifi_ap_wins_over_switch_for_u6_enterprise(self):
        did = _add_device(name="ap", ip_address="192.0.2.3",
                          vendor="UniFi", model="U6-Enterprise",
                          device_type="ap", unifi_managed=True)
        res = es.resolve_capabilities(_device(did))
        self.assertEqual(res["catalog_id"], "unifi-ap")
        self.assertIn("wifi_wpa3", res["capabilities"])
        self.assertIn("wifi_ssids", res["capabilities"])

    def test_model_only_is_not_catalog_verified(self):
        # A model keyword with no vendor is NOT verification -> falls through.
        did = _add_device(name="x", ip_address="192.0.2.4",
                          vendor=None, model="UCG-Max", device_type="gateway")
        res = es.resolve_capabilities(_device(did))
        self.assertNotEqual(res["confidence"], "catalog-verified")
        self.assertIsNone(res["catalog_id"])

    # ── probe (live signal, no catalog entry) ───────────────────────────────

    def test_unifi_managed_probe(self):
        did = _add_device(name="cam", ip_address="192.0.2.5",
                          vendor="MysteryVendor", model="MysteryAP",
                          device_type="ap", unifi_managed=True)
        res = es.resolve_capabilities(_device(did))
        self.assertEqual(res["confidence"], "probed")
        self.assertIsNone(res["catalog_id"])
        self.assertIn("unifi_controller_api", res["capabilities"])
        self.assertIn("firmware_feed", res["capabilities"])
        self.assertIn("vlan_8021q", res["capabilities"])
        self.assertEqual(res["capabilities"]["unifi_controller_api"]["basis"], "probed")

    def test_agent_report_probe(self):
        did = _add_device(name="laptop", ip_address="192.0.2.6",
                          vendor="Fedora", model="laptop",
                          device_type="server", adoption_method="agent",
                          agent_version="0.2.0",
                          agent_capabilities='["check_updates", "apply_updates", "report_facts"]')
        res = es.resolve_capabilities(_device(did))
        self.assertEqual(res["confidence"], "probed")
        self.assertIn("agent_control", res["capabilities"])
        self.assertIn("update_check", res["capabilities"])
        self.assertIn("apply_updates", res["capabilities"])
        self.assertIn("agent:report_facts", res["capabilities"])

    def test_ssh_key_probe(self):
        did = _add_device(name="srv", ip_address="192.0.2.7",
                          vendor="Debian", model="server",
                          device_type="server", ssh_key_fingerprint="encrypted:srv")
        res = es.resolve_capabilities(_device(did))
        # vendor+model hit linux-endpoint catalog -> catalog-verified
        self.assertEqual(res["confidence"], "catalog-verified")
        self.assertIn("ssh_control", res["capabilities"])
        self.assertIn("collect_logs", res["capabilities"])

    def test_probe_can_exceed_catalog_when_vendor_missing(self):
        # No vendor string -> not catalog-verified, but SSH key still probes.
        did = _add_device(name="srv2", ip_address="192.0.2.9",
                          device_type="server", ssh_key_fingerprint="encrypted:srv2")
        res = es.resolve_capabilities(_device(did))
        self.assertEqual(res["confidence"], "probed")
        self.assertIn("ssh_control", res["capabilities"])
        self.assertEqual(res["capabilities"]["ssh_control"]["basis"], "probed")

    # ── conservative floor (unknown) ────────────────────────────────────────

    def test_unknown_device_floor(self):
        did = _add_device(name="mystery", ip_address="192.0.2.8",
                          vendor="Zzz", model="Thing-42", device_type="other")
        res = es.resolve_capabilities(_device(did))
        self.assertEqual(res["confidence"], "unknown-floor")
        self.assertIsNone(res["catalog_id"])
        self.assertIn("reachability", res["capabilities"])
        self.assertEqual(res["capabilities"]["reachability"]["basis"], "unknown-floor")


class ActionChannelTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(DeviceFirmware).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def test_unclaimed_is_manual_review(self):
        did = _add_device(name="u", ip_address="192.0.2.10", claimed=False,
                          ssh_key_fingerprint="encrypted:u")
        d = _device(did)
        ch = es.compute_action_channel(d, es.device_channels(d))
        self.assertEqual(ch["channel"], "manual_review")

    def test_claimed_with_ssh_is_bareNOC_fix(self):
        did = _add_device(name="s", ip_address="192.0.2.11", claimed=True,
                          ssh_key_fingerprint="encrypted:s", vendor="Debian",
                          model="server", device_type="server")
        d = _device(did)
        ch = es.compute_action_channel(d, es.device_channels(d))
        self.assertEqual(ch["channel"], "bareNOC_fix")
        self.assertIn("ssh", ch["via"])

    def test_claimed_with_unifi_is_bareNOC_fix(self):
        did = _add_device(name="sw", ip_address="192.0.2.12", claimed=True,
                          unifi_managed=True, vendor="Ubiquiti",
                          model="USW-Pro-24", device_type="switch")
        d = _device(did)
        ch = es.compute_action_channel(d, es.device_channels(d))
        self.assertEqual(ch["channel"], "bareNOC_fix")
        self.assertIn("unifi", ch["via"])

    def test_claimed_monitor_only_is_tech_action(self):
        did = _add_device(name="cam", ip_address="192.0.2.13", claimed=True,
                          device_type="camera", vendor="Axis", model="M10")
        d = _device(did)
        ch = es.compute_action_channel(d, es.device_channels(d))
        self.assertEqual(ch["channel"], "tech_action")

    def test_claimed_unknown_no_control_is_tech_action_not_guess(self):
        # Unknown capability floor is a separate first-class flag; a claimed
        # device without control still routes to a HUMAN (tech_action), never
        # an autonomous guess.
        did = _add_device(name="zzz", ip_address="192.0.2.14", claimed=True,
                          device_type="other", vendor="Zzz", model="Thing")
        d = _device(did)
        res = es.resolve_capabilities(d)
        self.assertEqual(res["confidence"], "unknown-floor")
        ch = es.compute_action_channel(d, es.device_channels(d))
        self.assertEqual(ch["channel"], "tech_action")


class DigestTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(DeviceFirmware).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def _seed(self):
        _add_device(name="gw", ip_address="192.0.2.20", vendor="Ubiquiti",
                    model="UCG-Max", device_type="gateway", unifi_managed=True,
                    claimed=True)
        _add_device(name="sw", ip_address="192.0.2.21", vendor="Ubiquiti",
                    model="USW-24", device_type="switch", unifi_managed=True,
                    claimed=True)
        _add_device(name="laptop", ip_address="192.0.2.22", vendor="Fedora",
                    model="laptop", device_type="server", adoption_method="agent",
                    agent_version="0.2.0", claimed=True)
        _add_device(name="mystery", ip_address="192.0.2.23", vendor="Zzz",
                    model="Thing-9", device_type="other", claimed=True,
                    mac_address="AA:BB:CC:00:00:23",
                    snmp_community="SECRET-COMMUNITY")
        # unclaimed but catalog-known AP -> manual_review (unmanaged).
        _add_device(name="stray-ap", ip_address="192.0.2.24", vendor="Ubiquiti",
                    model="U6-Lite", device_type="ap", unifi_managed=True,
                    claimed=False)

    def test_digest_counts_and_unknown_flags(self):
        self._seed()
        db = SessionLocal()
        s = es.summarize_environment(db)
        db.close()
        self.assertEqual(s["inventory"]["total"], 5)
        self.assertEqual(s["inventory"]["claimed"], 4)
        self.assertEqual(s["inventory"]["unclaimed"], 1)
        self.assertEqual(s["inventory"]["by_class"].get("router"), 1)   # gateway -> router
        self.assertEqual(s["inventory"]["by_class"].get("switch"), 1)
        self.assertEqual(s["inventory"]["by_class"].get("server"), 1)
        self.assertEqual(s["inventory"]["by_class"].get("ap"), 1)
        self.assertEqual(s["controls"]["bareNOC_fix"], 3)
        self.assertEqual(s["controls"]["tech_action"], 1)      # mystery (claimed, no control)
        self.assertEqual(s["controls"]["manual_review"], 1)    # stray-ap (unclaimed)
        self.assertEqual(s["unknown_count"], 1)
        self.assertEqual(s["capabilities"]["confidence"]["catalog-verified"], 3)
        self.assertEqual(s["capabilities"]["confidence"]["probed"], 1)
        self.assertEqual(s["capabilities"]["confidence"]["unknown-floor"], 1)

    def test_digest_text_is_compact_and_has_no_secrets(self):
        self._seed()
        db = SessionLocal()
        s = es.summarize_environment(db)
        db.close()
        text = s["text"]
        self.assertIn("ENVIRONMENT:", text)
        self.assertIn("Unknown devices (verify before acting)", text)
        self.assertIn("mystery", text)
        # No secrets may ever leak into the digest.
        self.assertNotIn("SECRET-COMMUNITY", text)
        self.assertNotIn("community", text.lower())
        self.assertNotIn("ssh_key", text.lower())
        self.assertNotIn("password", text.lower())
        # Compact: a handful of lines, bounded.
        self.assertLessEqual(len(text.splitlines()), 8)

    def test_unknown_brief_carries_contribution_hint(self):
        self._seed()
        db = SessionLocal()
        s = es.summarize_environment(db)
        db.close()
        u = s["unknown_devices"][0]
        self.assertEqual(u["name"], "mystery")
        self.assertEqual(u["hint"]["mac_oui"], "aa:bb:cc")
        # The OUI hint never guesses a vendor.
        self.assertNotIn("vendor", u["hint"])


class DeviceStateTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(DeviceFirmware).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def test_device_state_normalized_record(self):
        did = _add_device(name="gw", ip_address="192.0.2.30", vendor="Ubiquiti",
                          model="UCG-Max", device_type="gateway",
                          unifi_managed=True, claimed=True,
                          mac_address="aa:bb:cc:00:00:30")
        db = SessionLocal()
        db.add(DeviceFirmware(device_id=did, mac_address="aa:bb:cc:00:00:30",
                              current_version="4.0.6", available_version="4.1.0",
                              upgradeable=True))
        db.commit()
        rec = es.device_state(db, did)
        db.close()
        self.assertEqual(rec["id"], did)
        self.assertEqual(rec["identity"]["firmware"], "4.0.6")
        self.assertEqual(rec["identity"]["device_type"], "router")  # gateway -> router (canonical)
        self.assertIn("unifi", rec["channels"])
        self.assertEqual(rec["capability_confidence"], "catalog-verified")
        self.assertEqual(rec["controls"]["channel"], "bareNOC_fix")
        self.assertFalse(rec["unknown"])
        self.assertEqual(rec["config"]["firmware"]["available"], "4.1.0")

    def test_device_state_missing_returns_none(self):
        db = SessionLocal()
        self.assertIsNone(es.device_state(db, 999999))
        db.close()

    def test_optimizer_accessor(self):
        did = _add_device(name="sw", ip_address="192.0.2.31", vendor="Ubiquiti",
                          model="USW-24", device_type="switch", unifi_managed=True,
                          claimed=True)
        db = SessionLocal()
        self.assertTrue(es.has_capability(db, did, "vlan_8021q"))
        self.assertFalse(es.has_capability(db, did, "wifi_wpa3"))
        self.assertEqual(es.action_channel_for(db, did), "bareNOC_fix")
        self.assertIn("vlan_8021q", es.capabilities_for(db, did))
        # Unknown id -> empty + manual_review (never guess).
        self.assertEqual(es.capabilities_for(db, 999999), {})
        self.assertEqual(es.action_channel_for(db, 999999), "manual_review")
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
