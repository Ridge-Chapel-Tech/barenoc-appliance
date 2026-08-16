#!/usr/bin/env python3
"""In-container integration tests for UniFi sync auto-adopt.

Runs inside the barenoc-api container (needs SQLAlchemy/FastAPI + shared
modules). Uses a scratch sqlite DB and a fake UniFi client — no live DB, no
controller calls.

    docker compose exec api python3 -m unittest test_unifi_sync -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="unifi-sync-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import Device
from routes import unifi_sync


FAKE_DEVICES = [
    {"name": "Main Gateway", "ip": "192.0.2.1", "mac": "aa:bb:cc:00:00:01",
     "type": "gateway", "model": "UCG-Max", "status": "online"},
    {"name": "Core Switch", "ip": "192.0.2.10", "mac": "aa:bb:cc:00:00:02",
     "type": "switch", "model": "USW-Lite", "status": "online"},
]
FAKE_CLIENTS = [
    {"name": "Phone", "hostname": "phone", "ip": "192.0.2.50", "mac": "aa:bb:cc:00:00:99",
     "vendor": "Apple", "wired": False, "online": True, "last_seen": None},
]


class FakeClient:
    def __init__(self, devices=None, clients=None):
        self.devices = devices or []
        self.clients = clients or []

    def login(self):
        return True

    def get_devices(self):
        return list(self.devices)

    def get_clients(self):
        return list(self.clients)


def _run_sync(auto_adopt: str):
    init_db()
    db = SessionLocal()
    fake = FakeClient(FAKE_DEVICES, FAKE_CLIENTS)
    with patch.object(unifi_sync, "_auth_ready", return_value=True), \
         patch.object(unifi_sync, "_get_unifi_config",
                      return_value={"url": "x", "username": "u", "password": "p", "api_key": ""}), \
         patch.object(unifi_sync, "_read_unifi_env",
                      return_value={"UNIFI_AUTO_ADOPT": auto_adopt}), \
         patch.object(unifi_sync, "_unifi_client", return_value=fake):
        result = unifi_sync.sync_from_unifi(
            db=db, user=SimpleNamespace(username="tester"))
    db.close()
    return result


def _all_devices():
    db = SessionLocal()
    devs = db.query(Device).all()
    db.close()
    return devs


class AutoAdoptTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()

    def test_auto_adopt_on_claims_new_infra_devices(self):
        result = _run_sync("true")
        self.assertEqual(result["adopted"], 2)
        self.assertEqual(result["added"], 2)
        for d in _all_devices():
            if d.mac_address in ("aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"):
                self.assertTrue(d.claimed, f"{d.name} should be claimed")
                self.assertEqual(d.device_group, "default")
                self.assertTrue(d.unifi_managed)
                self.assertFalse(d.name.startswith("unifi-"), d.name)

    def test_auto_adopt_off_leaves_unclaimed(self):
        result = _run_sync("false")
        self.assertEqual(result["adopted"], 0)
        for d in _all_devices():
            if d.mac_address in ("aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"):
                self.assertFalse(d.claimed)
                self.assertTrue(d.name.startswith("unifi-"), d.name)

    def test_existing_unclaimed_flips_to_claimed(self):
        _run_sync("false")          # first pass: unclaimed
        result = _run_sync("true")  # adopt on: flips them
        self.assertEqual(result["adopted"], 2)
        for d in _all_devices():
            if d.mac_address in ("aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"):
                self.assertTrue(d.claimed)
                self.assertEqual(d.name, "Main Gateway" if d.mac_address.endswith("01") else "Core Switch")

    def test_clients_never_auto_adopted(self):
        _run_sync("true")
        phone = next(d for d in _all_devices() if d.mac_address == "aa:bb:cc:00:00:99")
        self.assertFalse(phone.claimed, "endpoint clients must stay unclaimed")

    def test_config_defaults(self):
        env = {}
        self.assertTrue(unifi_sync._env_bool(env.get("UNIFI_AUTO_ADOPT") or "true"))

    def test_clear_api_key_removes_stored_secret(self):
        """An explicit clear writes an EMPTY value — every consumer then treats
        the secret as unset (auth falls back to password / none)."""
        with tempfile.TemporaryDirectory() as td:
            env = os.path.join(td, ".env")
            with open(env, "w") as f:
                f.write("UNIFI_URL=https://192.0.2.1:443\n"
                        "UNIFI_API_KEY=sekret\n"
                        "UNIFI_PASSWORD=passw0rd\n")
            unifi_sync._write_env(env, {"UNIFI_API_KEY": "", "UNIFI_PASSWORD": ""})
            with open(env) as f:
                content = f.read()
        self.assertNotIn("sekret", content, "stored API key must be removed")
        self.assertNotIn("passw0rd", content, "stored password must be removed")
        self.assertIn("UNIFI_API_KEY=\n", content)
        self.assertIn("UNIFI_PASSWORD=\n", content)

    def test_set_config_clear_api_key_flag(self):
        """POST with clear_api_key maps to an empty UNIFI_API_KEY (audit-safe)."""
        calls = {}

        def fake_write(path, updates):
            calls["updates"] = dict(updates)

        with patch.object(unifi_sync, "_write_env", side_effect=fake_write), \
             patch.object(unifi_sync, "log_event"), \
             patch.object(unifi_sync, "_read_unifi_env", return_value={}):
            db = SimpleNamespace()
            user = SimpleNamespace(username="admin")
            unifi_sync.set_config({"url": "https://x:443", "username": "admin",
                                   "clear_api_key": True}, db, user)
        self.assertEqual(calls["updates"].get("UNIFI_API_KEY"), "")
        self.assertEqual(calls["updates"].get("UNIFI_URL"), "https://x:443")
        self.assertNotIn("UNIFI_API_KEY", calls["updates"].get("UNIFI_PASSWORD", ""))

    def test_topology_only_adopted(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        for name, mac, up, typ, claimed in (
            ("GW", "aa:bb:cc:00:00:01", "", "gateway", True),
            ("SW", "aa:bb:cc:00:00:02", "aa:bb:cc:00:00:01", "switch", True),
            ("AP1", "aa:bb:cc:00:00:03", "aa:bb:cc:00:00:02", "ap", True),
            ("AP-NEW", "aa:bb:cc:00:00:04", "aa:bb:cc:00:00:02", "ap", False),
        ):
            db.add(Device(name=name, ip_address=f"10.0.0.{len(mac)}", device_type=typ,
                          status="online", claimed=claimed, unifi_managed=True,
                          device_group="default", mac_address=mac))
        db.commit()
        devs = [
            {"name": "GW", "ip": "10.0.0.1", "mac": "aa:bb:cc:00:00:01", "type": "gateway",
             "model": "x", "status": "online", "uplink_mac": ""},
            {"name": "SW", "ip": "10.0.0.2", "mac": "aa:bb:cc:00:00:02", "type": "switch",
             "model": "x", "status": "online", "uplink_mac": "aa:bb:cc:00:00:01"},
            {"name": "AP1", "ip": "10.0.0.3", "mac": "aa:bb:cc:00:00:03", "type": "ap",
             "model": "x", "status": "online", "uplink_mac": "aa:bb:cc:00:00:02"},
            {"name": "AP-NEW", "ip": "10.0.0.4", "mac": "aa:bb:cc:00:00:04", "type": "ap",
             "model": "x", "status": "online", "uplink_mac": "aa:bb:cc:00:00:02"},
        ]
        fake = FakeClient(devs, [])
        with patch.object(unifi_sync, "_auth_ready", return_value=True), \
             patch.object(unifi_sync, "_get_unifi_config",
                          return_value={"url": "x", "username": "u", "password": "p", "api_key": ""}), \
             patch.object(unifi_sync, "_unifi_client", return_value=fake):
            result = unifi_sync.topology(db=db, user=SimpleNamespace(username="tester"))
        db.close()
        macs = [d["mac"] for d in result["devices"]]
        self.assertEqual(macs, ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02", "aa:bb:cc:00:00:03"])
        self.assertNotIn("aa:bb:cc:00:00:04", macs)   # unadopted AP excluded
        self.assertEqual(result["clients"], [])
        self.assertEqual(len(result["links"]), 2)      # 01->02, 02->03 only
        self.assertFalse(any(l["to"] == "aa:bb:cc:00:00:04" for l in result["links"]))

    def test_ensure_wireless_uplinks_compute(self):
        """dry_run computes the right tagged VLANs without writing; preserves
        other ports + exclusions."""
        c = unifi_sync._unifi_client({"url": "x", "username": "u", "password": "p", "api_key": ""})
        c.site = "default"
        nets = {
            "n_wifi": {"name": "WiFi", "vlan": 5},
            "n_kids": {"name": "Kids", "vlan": 9},
            "n_rctf": {"name": "RCTF", "vlan": 10},
            "n_prod": {"name": "Production", "vlan": 4},
        }
        device = {
            "name": "Mini Rack Switch", "mac": "aa:bb:cc:dd:ee:01", "_id": "sw1",
            "type": "usw",
            "uplink": {"uplink_mac": "", "uplink_remote_port": None},
            "port_overrides": [{"port_idx": 1, "native_networkconf_id": "n_wifi",
                                 "excluded_networkconf_ids": ["n_prod", "n_kids"],
                                 "tagged_networkconf_id": "", "forward": "customize",
                                 "name": "Port 1"}],
            "port_table": [{"port_idx": 1, "native_networkconf_id": "n_wifi"}],
        }
        ap = {"name": "Office Wifi", "mac": "aa:bb:cc:00:00:01", "type": "uap",
              "uplink": {"uplink_mac": "aa:bb:cc:dd:ee:01", "uplink_remote_port": 1,
                          "uplink_device_name": "Mini Rack Switch"}}
        written = {}

        def fake_request(method, path, data=None, headers=None):
            if method == "GET" and "stat/device" in path:
                return {"data": [device, ap]}
            if method == "GET" and "networkconf" in path:
                return {"data": [{"_id": k, "name": v["name"], "vlan": v["vlan"]} for k, v in nets.items()]}
            if method == "GET" and "wlanconf" in path:
                return {"data": [{"name": "Kids", "enabled": True, "networkconf_id": "n_kids"},
                                 {"name": "RCTF", "enabled": True, "networkconf_id": "n_rctf"}]}
            if method == "PUT":
                written[path] = data
                return {"meta": {"rc": "ok"}}
            return {"meta": {"rc": "ok"}}

        c._request = fake_request
        c.get_networks_map = lambda: dict(nets)

        # dry_run: computes, does NOT write
        out = c.ensure_wireless_uplinks(dry_run=True)
        self.assertEqual(len(out["changed"]), 2)  # Kids + RCTF on port 1
        self.assertTrue(all(x["tagged_vlan"] in ("Kids", "RCTF") for x in out["changed"]))
        self.assertNotIn("rest/device/sw1", written)

        # real: writes the full array, preserving exclusions minus the tagged
        out2 = c.ensure_wireless_uplinks(dry_run=False)
        self.assertEqual(out2["status"], "ok")
        self.assertIsNotNone(written.get("/proxy/network/api/s/default/rest/device/sw1"))
        ov = written["/proxy/network/api/s/default/rest/device/sw1"]["port_overrides"]
        p1 = next(o for o in ov if o["port_idx"] == 1)
        tagged = [x for x in p1["tagged_networkconf_id"].split(",") if x]
        self.assertIn("n_kids", tagged)
        self.assertIn("n_rctf", tagged)
        self.assertNotIn("n_kids", p1["excluded_networkconf_ids"])   # removed from excluded
        self.assertIn("n_prod", p1["excluded_networkconf_ids"])       # other exclusions kept

    def test_devices_status_filter_and_clients_filters(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        devs = [
            {"name": "AP1", "ip": "10.0.0.3", "mac": "aa:bb:cc:00:00:03", "type": "ap",
             "model": "x", "status": "online"},
            {"name": "AP2", "ip": "10.0.0.4", "mac": "aa:bb:cc:00:00:04", "type": "ap",
             "model": "x", "status": "offline"},
        ]
        clients = [
            {"name": "Phone", "hostname": "p", "ip": "10.0.0.50", "mac": "aa:bb:cc:00:00:50",
             "vendor": "", "wired": False, "online": True, "last_seen": None,
             "sw_mac": None, "sw_port": None, "ap_mac": "aa:bb:cc:00:00:03"},
            {"name": "Server", "hostname": "s", "ip": "10.0.0.51", "mac": "aa:bb:cc:00:00:51",
             "vendor": "", "wired": True, "online": True, "last_seen": None,
             "sw_mac": "aa:bb:cc:00:00:02", "sw_port": 3, "ap_mac": None},
            {"name": "Old", "hostname": "o", "ip": "10.0.0.52", "mac": "aa:bb:cc:00:00:52",
             "vendor": "", "wired": True, "online": False, "last_seen": None,
             "sw_mac": None, "sw_port": None, "ap_mac": None},
        ]
        fake = FakeClient(devs, clients)
        with patch.object(unifi_sync, "_auth_ready", return_value=True), \
             patch.object(unifi_sync, "_get_unifi_config",
                          return_value={"url": "x", "username": "u", "password": "p", "api_key": ""}), \
             patch.object(unifi_sync, "_unifi_client", return_value=fake):
            offline_aps = unifi_sync.list_devices(device_type="ap", status="offline",
                                                  user=SimpleNamespace(username="t"))
            online_clients = unifi_sync.list_clients(online=True, wired=None,
                                                     user=SimpleNamespace(username="t"))
            wired_clients = unifi_sync.list_clients(online=None, wired=True,
                                                    user=SimpleNamespace(username="t"))
            try:
                unifi_sync.list_devices(status="nope", device_type=None, user=SimpleNamespace(username="t"))
                bad = None
            except Exception as e:
                bad = e
        db.close()
        self.assertEqual([d["name"] for d in offline_aps["devices"]], ["AP2"])
        self.assertEqual([c["name"] for c in online_clients["clients"]], ["Phone", "Server"])
        self.assertEqual([c["name"] for c in wired_clients["clients"]], ["Server", "Old"])
        self.assertIsNotNone(bad)

    def test_devices_type_filter(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        devs = [
            {"name": "GW", "ip": "10.0.0.1", "mac": "aa:bb:cc:00:00:01", "type": "gateway",
             "model": "x", "status": "online"},
            {"name": "SW", "ip": "10.0.0.2", "mac": "aa:bb:cc:00:00:02", "type": "switch",
             "model": "x", "status": "online"},
            {"name": "AP1", "ip": "10.0.0.3", "mac": "aa:bb:cc:00:00:03", "type": "ap",
             "model": "x", "status": "online"},
            {"name": "AP2", "ip": "10.0.0.4", "mac": "aa:bb:cc:00:00:04", "type": "ap",
             "model": "x", "status": "offline"},
        ]
        fake = FakeClient(devs, [])
        with patch.object(unifi_sync, "_auth_ready", return_value=True), \
             patch.object(unifi_sync, "_get_unifi_config",
                          return_value={"url": "x", "username": "u", "password": "p", "api_key": ""}), \
             patch.object(unifi_sync, "_unifi_client", return_value=fake):
            r = unifi_sync.list_devices(device_type="ap", status=None,
                                        user=SimpleNamespace(username="tester"))
            r_bad = None
            try:
                unifi_sync.list_devices(device_type="nope", status=None,
                                        user=SimpleNamespace(username="tester"))
            except Exception as e:
                r_bad = e
        db.close()
        self.assertEqual([d["name"] for d in r["devices"]], ["AP1", "AP2"])
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["online"], 1)
        self.assertIsNotNone(r_bad)  # invalid type rejected (HTTPException)

    def test_scan_find_merges_with_unifi_client(self):
        """A ping-scan 'discovered-*' record (no MAC) at the same IP as a UniFi
        client gets the client's identity instead of lingering as a duplicate."""
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        scan = Device(name="discovered-192-168-4-13", ip_address="192.0.2.13",
                      device_type="unknown", status="online", claimed=False,
                      unifi_managed=False, mac_address=None, device_group="default")
        db.add(scan)
        db.commit()
        client = {"name": "Media Server", "hostname": "Media Server", "ip": "192.0.2.13",
                  "mac": "aa:bb:cc:00:00:99", "vendor": "Intel", "wired": True,
                  "online": True, "last_seen": None}
        fake = FakeClient([], [client])
        with patch.object(unifi_sync, "_auth_ready", return_value=True), \
             patch.object(unifi_sync, "_get_unifi_config",
                          return_value={"url": "x", "username": "u", "password": "p", "api_key": ""}), \
             patch.object(unifi_sync, "_read_unifi_env", return_value={"UNIFI_AUTO_ADOPT": "true"}), \
             patch.object(unifi_sync, "_unifi_client", return_value=fake):
            unifi_sync.sync_from_unifi(db=db, user=SimpleNamespace(username="tester"))
        db.expire_all()
        scan = db.query(Device).get(scan.id)
        self.assertEqual(scan.mac_address, "aa:bb:cc:00:00:99")
        self.assertEqual(scan.name, "Media Server")
        self.assertFalse(scan.claimed)
        self.assertIn("unifi-client", scan.tags or [])
        self.assertIn("wired", scan.tags or [])
        db.close()


class RouteBindingRegressionTest(unittest.TestCase):
    """Route-level guard: a decorator must bind to the intended endpoint.
    (2026-08-16: the /config decorator bound to the extracted _write_env
    helper → every POST /api/v1/unifi/config 422'd 'env_path required'. Unit
    tests call functions directly, so only a route-level check catches it.)"""

    def _app(self):
        from main import app
        return app

    def test_unifi_config_route_binds_to_set_config(self):
        from fastapi.testclient import TestClient
        app = self._app()
        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/unifi/config"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "set_config")
        r = TestClient(app).post("/api/v1/unifi/config", json={"url": "https://x:443"})
        self.assertNotEqual(r.status_code, 422, f"route misbound: {r.text}")
        self.assertEqual(r.status_code, 401, "unauthenticated POST should 401")

    def test_support_bundle_route_binds_to_export(self):
        app = self._app()
        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/support/bundle")
        self.assertEqual(route.endpoint.__name__, "export_bundle")


if __name__ == "__main__":
    unittest.main(verbosity=2)
