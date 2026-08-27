#!/usr/bin/env python3
"""Tests for the Devices 'Uplink / ISP' card (vendor-agnostic uplink health).

Covers:
  1. UI — the card lives on Devices (id="uplink" + uplinkLoad) and the old
     System 'Starlink Link Health' card + its JS are gone (template asserts).
  2. Builder — source precedence: live Starlink dish -> starlink; gateway with
     stubbed UniFi WAN data -> unifi (ISP/WAN/link fields); no dish/gateway ->
     egress probe fallback; otherwise -> none.
  3. Helpers — wan1/wan2 normalization, link-state from WAN health, gateway
     discovery.
  4. Route gating — /api/v1/uplink/status is readonly+ (staff), not customer.

    docker compose exec api python3 -m unittest test_uplink -v
"""

import datetime
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="uplink-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device, Metric, User
import uplink
import starlink


def _read(name):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", name), encoding="utf-8") as f:
        return f.read()


def _clean():
    db = SessionLocal()
    for t in (Metric, Device, User):
        db.query(t).delete()
    db.commit()
    db.close()


def _add_device(name, ip, **kw):
    db = SessionLocal()
    d = Device(name=name, ip_address=ip, **kw)
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return did


def _add_metric(device_id, metric, value, days_ago=0.0):
    db = SessionLocal()
    ts = datetime.datetime.utcnow() - datetime.timedelta(days=days_ago)
    db.add(Metric(device_id=device_id, metric=metric, ts=ts, value=float(value)))
    db.commit()
    db.close()


class NoUnifi:
    """Stub UniFi channel: the controller has nothing for us."""
    def wan_picture(self, mac):
        return None


class FakeUnifi:
    """Stub UniFi channel returning a gateway WAN picture (no controller)."""
    def __init__(self, picture):
        self.picture = picture
        self.mac = None

    def wan_picture(self, mac):
        self.mac = mac
        return self.picture


def _probe_up(cfg):
    return {"gateway": cfg.get("gateway") or "", "host": cfg.get("host") or "8.8.8.8",
            "gateway_reachable": True, "internet_reachable": True,
            "gateway_ms": 2.0, "internet_ms": 18.0, "state": "up"}


class TemplateTest(unittest.TestCase):
    def test_devices_has_uplink_card(self):
        html = _read("devices.html")
        self.assertIn('id="uplink"', html)
        self.assertIn('id="uplink-body"', html)
        self.assertIn('function uplinkLoad', html)
        self.assertIn('/api/v1/uplink/status', html)
        self.assertIn('uplinkLoad();', html)

    def test_system_starlink_card_gone(self):
        html = _read("system.html")
        self.assertNotIn('id="starlink"', html)
        self.assertNotIn('starlinkLoad', html)
        self.assertNotIn('starlink_enabled', html)
        self.assertNotIn('Starlink Link Health', html)


class HelperTest(unittest.TestCase):
    def test_wan_from_config_primary_secondary_and_isp_hint(self):
        wan = {"wan1": {"ip": "203.0.113.10", "name": "Comcast WAN"},
               "wan2": {"ip": "198.51.100.20"}}
        out = uplink._wan_from_config(wan)
        self.assertEqual(out["primary_ip"], "203.0.113.10")
        self.assertEqual(out["secondary_ip"], "198.51.100.20")
        self.assertEqual(out["isp_hint"], "Comcast WAN")

    def test_wan_isp_hint_prefers_isp_field(self):
        wan = {"wan1": {"pppoe_service": "ACME", "isp_name": "BigISP"}}
        self.assertEqual(uplink._wan_isp_hint(wan), "BigISP")

    def test_link_state_from_wan(self):
        self.assertEqual(uplink._link_state_from_wan({"status": "ok"}), "up")
        self.assertEqual(uplink._link_state_from_wan({"status": "down"}), "down")
        self.assertEqual(uplink._link_state_from_wan({"status": ""}), "unknown")
        self.assertEqual(uplink._link_state_from_wan({"up": True}), "up")
        self.assertEqual(uplink._link_state_from_wan(None), "unknown")

    def test_gateway_prefers_unifi_managed_with_mac(self):
        init_db()
        _clean()
        plain = _add_device("plain-gw", "10.0.0.1", device_type="gateway",
                            claimed=True)
        managed = _add_device("managed-gw", "10.0.0.2", device_type="gateway",
                              claimed=True, unifi_managed=True,
                              mac_address="aa:bb:cc:00:00:01")
        db = SessionLocal()
        g = uplink.gateway_device(db)
        db.close()
        self.assertEqual(g.id, managed)
        self.assertEqual(g.mac_address, "aa:bb:cc:00:00:01")

    def test_probe_egress_parses_latency(self):
        with patch.object(uplink, "_ping", side_effect=[(True, 2.0), (True, 18.0)]):
            p = uplink.probe_egress({"gateway": "10.0.0.1", "host": "8.8.8.8"})
        self.assertEqual(p["state"], "up")
        self.assertEqual(p["gateway_ms"], 2.0)
        self.assertEqual(p["internet_ms"], 18.0)

    def test_probe_egress_gateway_up_internet_down(self):
        with patch.object(uplink, "_ping", side_effect=[(True, 2.0), (False, None)]):
            p = uplink.probe_egress({"gateway": "10.0.0.1", "host": "8.8.8.8"})
        self.assertEqual(p["state"], "isp_down")

    def test_probe_egress_no_gateway_internet_up(self):
        with patch.object(uplink, "_ping", side_effect=[(False, None), (True, 18.0)]):
            p = uplink.probe_egress({"gateway": "", "host": "8.8.8.8"})
        self.assertEqual(p["state"], "up")
        self.assertTrue(p["internet_reachable"])


class BuilderTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        uplink._PROBE_CACHE["value"] = None
        uplink._PROBE_CACHE["at"] = 0.0

    def tearDown(self):
        _clean()
        uplink._PROBE_CACHE["value"] = None
        uplink._PROBE_CACHE["at"] = 0.0

    def test_no_sources_returns_none(self):
        db = SessionLocal()
        s = uplink.uplink_status(db, unifi_channel=NoUnifi(),
                                 probe_fn=lambda cfg: None, env={})
        db.close()
        self.assertEqual(s["source"], "none")
        self.assertIsNone(s["starlink"])
        self.assertEqual(s["link"]["state"], "unknown")

    def test_live_dish_is_starlink_source(self):
        did = _add_device("Starlink Dish", "192.168.100.1", device_type="dish",
                          vendor="Starlink", claimed=True)
        _add_metric(did, "starlink.link_up", 1.0)
        _add_metric(did, "starlink.ping_ms", 30.0)
        _add_metric(did, "starlink.down_mbps", 150.0)
        db = SessionLocal()
        s = uplink.uplink_status(db, unifi_channel=NoUnifi(),
                                 probe_fn=lambda cfg: None, env={})
        db.close()
        self.assertEqual(s["source"], "starlink")
        self.assertEqual(s["isp"]["name"], "Starlink")
        self.assertEqual(s["link"]["state"], "up")
        self.assertIsNotNone(s["starlink"])
        self.assertEqual(s["starlink"]["latest"].get("starlink.ping_ms"), 30.0)

    def test_phantom_dish_record_is_not_starlink(self):
        # A dish record with no telemetry is a phantom — it must NOT drive the
        # Starlink card (the evidence rule), and with no gateway/probe it is
        # honest 'none'.
        _add_device("Starlink Dish", "192.168.100.1", device_type="dish",
                    vendor="Starlink", claimed=True)
        db = SessionLocal()
        s = uplink.uplink_status(db, unifi_channel=NoUnifi(),
                                 probe_fn=lambda cfg: None, env={})
        db.close()
        self.assertNotEqual(s["source"], "starlink")
        self.assertIsNone(s["starlink"])

    def test_unifi_gateway_wan_path(self):
        gw = _add_device("Edge Gateway", "192.0.2.1", device_type="gateway",
                         vendor="Ubiquiti", model="UCG-Max", claimed=True,
                         unifi_managed=True, mac_address="aa:bb:cc:00:00:01")
        picture = {
            "health": {"status": "ok", "wan_ip": "203.0.113.10",
                       "gateway_ip": "203.0.113.1", "latency": 12.0,
                       "uptime": 3600.0, "internet_ok": True,
                       "speedtest_download": 150.0, "speedtest_upload": 25.0},
            "wan": {"wan1": {"ip": "203.0.113.10", "name": "Comcast WAN"}},
        }
        db = SessionLocal()
        s = uplink.uplink_status(db, unifi_channel=FakeUnifi(picture),
                                 probe_fn=lambda cfg: None, env={})
        db.close()
        self.assertEqual(s["source"], "unifi")
        self.assertEqual(s["isp"]["name"], "Comcast WAN")
        self.assertEqual(s["isp"]["wan_ip"], "203.0.113.10")
        self.assertEqual(s["link"]["state"], "up")
        self.assertEqual(s["gateway"]["model"], "UCG-Max")
        self.assertEqual(s["gateway"]["ip"], "192.0.2.1")
        self.assertEqual(s["stats"]["latency_ms"], 12.0)
        self.assertEqual(s["stats"]["down_mbps"], 150.0)
        self.assertEqual(s["stats"]["uptime_seconds"], 3600.0)

    def test_probe_fallback_path(self):
        # No gateway device + no dish -> the appliance's egress probe answers.
        db = SessionLocal()
        s = uplink.uplink_status(db, unifi_channel=NoUnifi(),
                                 probe_fn=_probe_up, env={})
        db.close()
        self.assertEqual(s["source"], "probe")
        self.assertEqual(s["link"]["state"], "up")
        self.assertEqual(s["stats"]["latency_ms"], 18.0)
        self.assertIsNotNone(s["probe"])
        self.assertTrue(s["probe"]["internet_reachable"])


class RouteGatingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        from auth import hash_password
        db = SessionLocal()
        for role in ("readonly", "operator", "user"):
            db.add(User(username=f"up_{role}", role=role,
                        hashed_password=hash_password("pw"), is_active=True))
        db.commit()
        db.close()

    def tearDown(self):
        _clean()

    def _client(self, user):
        from main import app
        from fastapi.testclient import TestClient
        from auth import create_access_token
        token = create_access_token({"sub": user.username, "role": user.role,
                                     "groups": [], "auth_method": "password"})
        return TestClient(app), token

    def _get(self, user):
        client, token = self._client(user)
        return client.get("/api/v1/uplink/status",
                          headers={"Authorization": f"Bearer {token}"})

    def test_readonly_and_operator_allowed(self):
        for role in ("readonly", "operator"):
            user = type("U", (), {"username": f"up_{role}", "role": role})()
            self.assertEqual(self._get(user).status_code, 200, role)

    def test_customer_forbidden(self):
        user = type("U", (), {"username": "up_user", "role": "user"})()
        self.assertEqual(self._get(user).status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
