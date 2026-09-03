#!/usr/bin/env python3
"""Blast-radius gate for UniFi port flips — unit + route regression tests.

Pins the 08-19 incident: a merge-safe port flip (the array merge preserved the
OTHER ports) still stranded the .4.x segment because nothing checked what was
BEHIND the flipped port. The gate refuses to remove a protected network (the
appliance's own subnet, or a management VLAN) from the port that carries it.

    docker compose exec api python3 -m unittest test_port_blast_radius -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="port-blast-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from fastapi import HTTPException

from database import SessionLocal, init_db
from models import Device
from routes import unifi_sync
import port_blast_radius as pbr


# ── fixtures ───────────────────────────────────────────────────────────────

NETS = {
    "n_default": {"name": "Default", "vlan": None, "subnet": "192.168.4.1/24",
                  "purpose": "corporate"},
    "n_prod": {"name": "Production", "vlan": 4, "subnet": "10.0.4.1/24",
               "purpose": "corporate"},
    "n_mgmt": {"name": "Management", "vlan": 8, "subnet": "10.0.8.1/24",
               "purpose": "corporate"},
}

APPLIANCE = ["192.168.4.207"]

# A port carrying the appliance's own segment (the .4.x Default network).
PORT_DEFAULT = {"port_idx": 6, "name": "Port 6",
                "native_network_id": "n_default", "tagged_network_ids": [],
                "up": True, "disabled": False}


class FakeClient:
    def __init__(self, ports, nets, raw_devices=None, clients=None):
        self.ports = ports
        self.nets = dict(nets)
        self.raw_devices = raw_devices or []
        self.clients = clients or []
        self.applied = None

    def login(self):
        return True

    def get_networks_map(self):
        return dict(self.nets)

    def get_switch_ports(self, mac):
        return [dict(p) for p in self.ports]

    def get_raw_devices(self):
        return list(self.raw_devices)

    def get_clients(self):
        return list(self.clients)

    def set_port_vlans(self, switch_mac, port_idx, tagged_network_ids,
                       native_network_id=None):
        self.applied = {"tagged": tagged_network_ids, "native": native_network_id}
        return {"applied": True, "port_idx": port_idx}

    def set_port_disabled(self, switch_mac, port_idx, disabled):
        self.applied = {"disabled": disabled}
        return {"applied": True, "port_idx": port_idx}


# ── pure gate unit tests ───────────────────────────────────────────────────

class ProtectedNetworksTest(unittest.TestCase):
    def test_appliance_subnet_and_mgmt_are_protected(self):
        protected = pbr.protected_network_ids(NETS, APPLIANCE)
        self.assertIn("n_default", protected)   # 192.168.4.x — the appliance's segment
        self.assertIn("n_mgmt", protected)      # management VLAN by name
        self.assertNotIn("n_prod", protected)

    def test_empty_appliance_ips_still_protects_mgmt(self):
        protected = pbr.protected_network_ids(NETS, [])
        self.assertNotIn("n_default", protected)
        self.assertIn("n_mgmt", protected)


class PortChangeGateTest(unittest.TestCase):
    def test_flip_removing_appliance_segment_is_blocked(self):
        """The 08-19 regression: flipping the native off the port that carries
        the appliance's .4.x segment is blocked even though the array write is
        merge-safe."""
        gate = pbr.check_port_change(
            NETS, PORT_DEFAULT,
            proposed_native_id="n_prod", proposed_tagged_ids=[],
            appliance_ips=APPLIANCE)
        self.assertTrue(gate["blocked"])
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["blast_radius"]["removed_protected"], ["Default"])
        self.assertIn("Default", gate["reason"])
        self.assertIn("Blast-radius gate", gate["reason"])

    def test_flip_preserving_segment_as_tagged_is_allowed(self):
        """Re-homing the appliance's segment native→tagged keeps it on the
        port, so it is not a silent nuke — allowed."""
        gate = pbr.check_port_change(
            NETS, PORT_DEFAULT,
            proposed_native_id="n_prod", proposed_tagged_ids=["n_default"],
            appliance_ips=APPLIANCE)
        self.assertFalse(gate["blocked"])

    def test_flip_removing_mgmt_vlan_is_blocked(self):
        port = dict(PORT_DEFAULT, native_network_id="n_mgmt")
        gate = pbr.check_port_change(
            NETS, port,
            proposed_native_id="n_prod", proposed_tagged_ids=[],
            appliance_ips=APPLIANCE)
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["blast_radius"]["removed_protected"], ["Management"])

    def test_flip_unrelated_port_is_allowed(self):
        port = dict(PORT_DEFAULT, native_network_id="n_prod")
        gate = pbr.check_port_change(
            NETS, port,
            proposed_native_id="n_default", proposed_tagged_ids=[],
            appliance_ips=APPLIANCE)
        self.assertFalse(gate["blocked"])

    def test_no_change_is_allowed(self):
        gate = pbr.check_port_change(
            NETS, PORT_DEFAULT,
            proposed_native_id="n_default", proposed_tagged_ids=[],
            appliance_ips=APPLIANCE)
        self.assertFalse(gate["blocked"])

    def test_appliance_attached_without_subnet_data_is_blocked(self):
        """Fallback: even when the appliance's subnet is not in the network map,
        changing the VLAN membership of the port the appliance is attached to
        is refused."""
        nets = {"n_prod": {"name": "Production", "vlan": 4,
                           "subnet": "10.0.4.1/24", "purpose": "corporate"}}
        port = dict(PORT_DEFAULT, native_network_id="n_prod")
        gate = pbr.check_port_change(
            nets, port,
            proposed_native_id="n_mgmt", proposed_tagged_ids=[],
            appliance_ips=APPLIANCE,
            connected_clients=[{"ip": "192.168.4.207", "sw_mac": "aa:bb:cc:dd:ee:01",
                                "sw_port": 6}])
        self.assertTrue(gate["blocked"])
        self.assertIn("appliance itself", gate["reason"])


class EffectivePortTest(unittest.TestCase):
    def test_override_native_wins_over_port_table(self):
        raw = {"port_overrides": [{"port_idx": 6, "native_networkconf_id": "n_default"}],
               "port_table": [{"port_idx": 6, "native_networkconf_id": ""}]}
        eff = pbr.effective_port(raw, {"port_idx": 6, "native_network_id": "",
                                        "tagged_network_ids": []})
        self.assertEqual(eff["native_network_id"], "n_default")

    def test_flip_blocked_when_appliance_segment_only_in_override(self):
        # the 'overrides trimmed' shape: port_table native is empty, but the
        # override still carries the appliance's .4.x Default network
        raw = {"port_overrides": [{"port_idx": 6, "native_networkconf_id": "n_default"}]}
        eff = pbr.effective_port(raw, {"port_idx": 6, "name": "Port 6",
                                        "native_network_id": "",
                                        "tagged_network_ids": []})
        gate = pbr.check_port_change(
            NETS, eff, proposed_native_id="n_prod", proposed_tagged_ids=[],
            appliance_ips=APPLIANCE)
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["blast_radius"]["removed_protected"], ["Default"])


class PortDisableGateTest(unittest.TestCase):
    def test_disable_appliance_port_blocked(self):
        gate = pbr.check_port_disable(
            NETS, PORT_DEFAULT, appliance_ips=APPLIANCE,
            connected_clients=[{"ip": "192.168.4.207"}])
        self.assertTrue(gate["blocked"])

    def test_disable_protected_network_with_clients_blocked(self):
        gate = pbr.check_port_disable(
            NETS, PORT_DEFAULT, appliance_ips=APPLIANCE,
            connected_clients=[{"ip": "10.0.4.50"}])
        self.assertTrue(gate["blocked"])
        self.assertIn("protected network", gate["reason"])

    def test_disable_downstream_uplink_blocked(self):
        gate = pbr.check_port_disable(
            NETS, dict(PORT_DEFAULT, native_network_id="n_prod"),
            appliance_ips=APPLIANCE, downstream_devices=["Office AP"])
        self.assertTrue(gate["blocked"])
        self.assertIn("downstream", gate["reason"])

    def test_disable_dead_end_allowed(self):
        gate = pbr.check_port_disable(
            NETS, dict(PORT_DEFAULT, native_network_id="n_prod"),
            appliance_ips=APPLIANCE)
        self.assertFalse(gate["blocked"])

    def test_disable_dead_end_on_default_allowed(self):
        # a dead-end access port whose native happens to be the appliance's
        # Default network still gets disabled (no clients/downstream behind it)
        gate = pbr.check_port_disable(NETS, PORT_DEFAULT, appliance_ips=APPLIANCE)
        self.assertFalse(gate["blocked"])


class PortLookupTest(unittest.TestCase):
    def test_downstream_and_clients_lookup(self):
        raw = [
            {"name": "Office AP", "mac": "aa:bb:cc:00:00:01",
             "uplink": {"uplink_mac": "aa:bb:cc:dd:ee:01", "uplink_remote_port": 6}},
            {"name": "Other AP", "mac": "aa:bb:cc:00:00:02",
             "uplink": {"uplink_mac": "aa:bb:cc:dd:ee:99", "uplink_remote_port": 6}},
        ]
        clients = [
            {"ip": "192.168.4.207", "sw_mac": "aa:bb:cc:dd:ee:01", "sw_port": 6},
            {"ip": "10.0.4.50", "sw_mac": "aa:bb:cc:dd:ee:01", "sw_port": 7},
        ]
        self.assertEqual(
            pbr.downstream_devices_for_port(raw, "aa:bb:cc:dd:ee:01", 6),
            ["Office AP"])
        self.assertEqual(
            [c["ip"] for c in pbr.clients_for_port(clients, "aa:bb:cc:dd:ee:01", 6)],
            ["192.168.4.207"])


# ── route-level regression tests ───────────────────────────────────────────

class RouteGateTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Device).delete()
        db.commit()
        db.close()
        self._env = patch.dict(os.environ, {"APPLIANCE_IP": "192.168.4.207"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _call_vlans(self, fake, body, role="agent"):
        with patch.object(unifi_sync, "_auth_ready", return_value=True), \
             patch.object(unifi_sync, "_get_unifi_config",
                          return_value={"url": "x", "username": "u",
                                        "password": "p", "api_key": ""}), \
             patch.object(unifi_sync, "_unifi_client", return_value=fake), \
             patch.object(unifi_sync, "log_event"), \
             patch.object(unifi_sync, "record"):
            return unifi_sync.set_port_vlans(
                "aa:bb:cc:dd:ee:01", 6, body,
                db=SessionLocal(),
                user=SimpleNamespace(username=role, role=role))

    def test_route_blocks_appliance_segment_flip_without_confirm(self):
        fake = FakeClient(ports=[PORT_DEFAULT], nets=NETS)
        with self.assertRaises(HTTPException) as ctx:
            self._call_vlans(fake, {"native": "Production"})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Blast-radius gate", ctx.exception.detail)
        self.assertIsNone(fake.applied)   # nothing was written

    def test_route_admin_confirm_allows_the_flip(self):
        fake = FakeClient(ports=[PORT_DEFAULT], nets=NETS)
        result = self._call_vlans(fake, {"native": "Production", "confirm": True},
                                  role="admin")
        self.assertTrue(result["applied"])
        self.assertIsNotNone(fake.applied)
        self.assertEqual(fake.applied["native"], "n_prod")

    def test_route_agent_confirm_is_not_an_override(self):
        fake = FakeClient(ports=[PORT_DEFAULT], nets=NETS)
        with self.assertRaises(HTTPException) as ctx:
            self._call_vlans(fake, {"native": "Production", "confirm": True},
                             role="agent")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIsNone(fake.applied)

    def test_route_dry_run_reports_block_without_writing(self):
        fake = FakeClient(ports=[PORT_DEFAULT], nets=NETS)
        result = self._call_vlans(fake, {"native": "Production", "dry_run": True})
        self.assertTrue(result["blocked"])
        self.assertIn("blast_radius", result)
        self.assertEqual(result["blast_radius"]["removed_protected"], ["Default"])
        self.assertIsNone(fake.applied)

    def test_route_disable_blocks_downstream_uplink(self):
        raw = [{"name": "Office AP", "mac": "aa:bb:cc:00:00:01",
                "uplink": {"uplink_mac": "aa:bb:cc:dd:ee:01",
                           "uplink_remote_port": 6}}]
        fake = FakeClient(ports=[PORT_DEFAULT], nets=NETS, raw_devices=raw)
        with patch.object(unifi_sync, "_auth_ready", return_value=True), \
             patch.object(unifi_sync, "_get_unifi_config",
                          return_value={"url": "x", "username": "u",
                                        "password": "p", "api_key": ""}), \
             patch.object(unifi_sync, "_unifi_client", return_value=fake), \
             patch.object(unifi_sync, "log_event"), \
             patch.object(unifi_sync, "record"):
            with self.assertRaises(HTTPException) as ctx:
                unifi_sync.set_port_disabled(
                    "aa:bb:cc:dd:ee:01", 6, {"disabled": True},
                    db=SessionLocal(),
                    user=SimpleNamespace(username="admin", role="admin"))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIsNone(fake.applied)

    def test_route_blocks_when_native_only_in_override(self):
        port = {"port_idx": 6, "name": "Port 6", "native_network_id": "",
                "tagged_network_ids": [], "up": True, "disabled": False}
        raw = [{"name": "Core Switch", "mac": "aa:bb:cc:dd:ee:01",
                "port_overrides": [{"port_idx": 6,
                                    "native_networkconf_id": "n_default"}],
                "uplink": {"uplink_mac": "", "uplink_remote_port": None}}]
        fake = FakeClient(ports=[port], nets=NETS, raw_devices=raw)
        with self.assertRaises(HTTPException) as ctx:
            self._call_vlans(fake, {"native": "Production"})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIsNone(fake.applied)

    def test_route_disable_allows_dead_end_port(self):
        port = dict(PORT_DEFAULT, native_network_id="n_prod")
        fake = FakeClient(ports=[port], nets=NETS)
        with patch.object(unifi_sync, "_auth_ready", return_value=True), \
             patch.object(unifi_sync, "_get_unifi_config",
                          return_value={"url": "x", "username": "u",
                                        "password": "p", "api_key": ""}), \
             patch.object(unifi_sync, "_unifi_client", return_value=fake), \
             patch.object(unifi_sync, "log_event"), \
             patch.object(unifi_sync, "record"):
            result = unifi_sync.set_port_disabled(
                "aa:bb:cc:dd:ee:01", 6, {"disabled": True},
                db=SessionLocal(),
                user=SimpleNamespace(username="admin", role="admin"))
        self.assertTrue(result["applied"])
        self.assertIsNotNone(fake.applied)


if __name__ == "__main__":
    unittest.main()
