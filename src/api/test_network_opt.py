#!/usr/bin/env python3
"""In-container tests for Network Optimization (P1 read-only audit/report).

Covers: the rules engine (each check + thresholds + scoring), the orchestrator
(fake collectors), scheduling (recurring/onetime local-time), admin gating, and
the self-protection exclusion. Uses a scratch sqlite DB + fake collectors — no
live network probes, no controller calls.

    docker compose exec api python3 -m unittest test_network_opt -v
"""

import datetime
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="netopt-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import User, Device, ScanRun, Finding, Ticket
import network_opt_rules as rules
import network_opt
import netopt_tickets
from routes import network_opt as routes


# ── snapshot builders ───────────────────────────────────────────────────────

def dev_snap(**kw):
    d = {
        "device_id": None, "name": "core-sw", "ip": "10.0.0.2",
        "mac": "aa:bb:cc:00:00:02", "device_type": "switch", "vendor": "Ubiquiti",
        "model": "USW", "unifi_managed": True,
        "ping": {"reachable": True, "latency_ms": 1.0},
        "nmap": {"open_ports": [], "open_services": {}, "os": ""},
        "snmp": None, "unifi": None, "last_seen": None,
    }
    d.update(kw)
    return d


def snap(devices=None, networks=None, wlans=None, **kw):
    s = {"schema_version": 1, "scope": {}, "devices": devices or [],
         "networks": networks or [], "wlans": wlans or [],
         "meta": {"collector_errors": [], "hosts_scanned": 0, "profile": "standard"}}
    s.update(kw)
    return s


def keys(findings):
    return {f["finding_key"] for f in findings}


def by_key(findings, key):
    return [f for f in findings if f["finding_key"] == key]


# ═══════════════════════════════ rules engine ══════════════════════════════

class RuleCatalogTest(unittest.TestCase):
    def test_catalog_size(self):
        # ~35-45 deterministic checks across the four categories.
        self.assertGreaterEqual(rules.count_rules(), 35)
        self.assertLessEqual(rules.count_rules(), 45)

    def test_categories_balanced(self):
        counts = {}
        for r in rules.RULES + rules.SNAPSHOT_RULES:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        for c in ("performance", "security", "reliability", "hygiene"):
            self.assertGreaterEqual(counts.get(c, 0), 6, f"{c} too thin")

    def test_keys_stable_and_unique(self):
        seen = set()
        for r in rules.RULES + rules.SNAPSHOT_RULES:
            self.assertRegex(r["key"], r"^(perf|sec|rel|hyg)\.[a-z0-9_]+$")
            self.assertNotIn(r["key"], seen, f"duplicate key {r['key']}")
            seen.add(r["key"])
            self.assertIn(r["severity"], ("critical", "warning", "info"))


class PerformanceRulesTest(unittest.TestCase):
    def test_duplex_half_fires(self):
        d = dev_snap(snmp={"version": "2c", "community": "x", "interfaces": [
            {"ifdescr": "Gi0/1", "iftype": 6, "oper_status": "up", "admin_status": "up",
             "speed_mbps": 1000, "duplex": "half", "mtu": 1500, "in_errors": 0,
             "out_errors": 0, "in_discards": 0, "out_discards": 0,
             "in_pkts": 0, "out_pkts": 0}]})
        fs = rules.evaluate(snap([d]))
        self.assertEqual(by_key(fs, "perf.duplex_half")[0]["severity"], "warning")

    def test_duplex_full_does_not_fire(self):
        d = dev_snap(snmp={"version": "2c", "community": "x", "interfaces": [
            {"ifdescr": "Gi0/1", "iftype": 6, "oper_status": "up", "admin_status": "up",
             "speed_mbps": 1000, "duplex": "full", "mtu": 1500, "in_errors": 0,
             "out_errors": 0, "in_discards": 0, "out_discards": 0,
             "in_pkts": 0, "out_pkts": 0}]})
        self.assertNotIn("perf.duplex_half", keys(rules.evaluate(snap([d]))))

    def test_interface_errors_ratio_threshold(self):
        # errors below ratio do not fire
        d = dev_snap(snmp={"version": "2c", "community": "x", "interfaces": [
            {"ifdescr": "Gi0/1", "iftype": 6, "oper_status": "up", "admin_status": "up",
             "speed_mbps": 1000, "duplex": "full", "mtu": 1500, "in_errors": 1,
             "out_errors": 0, "in_discards": 0, "out_discards": 0,
             "in_pkts": 100000, "out_pkts": 100000}]})
        self.assertNotIn("perf.interface_errors", keys(rules.evaluate(snap([d]))))
        # errors above the absolute fallback threshold fire (no packet counts)
        d2 = dev_snap(snmp={"version": "2c", "community": "x", "interfaces": [
            {"ifdescr": "Gi0/1", "iftype": 6, "oper_status": "up", "admin_status": "up",
             "speed_mbps": 1000, "duplex": "full", "mtu": 1500, "in_errors": 9000,
             "out_errors": 0, "in_discards": 0, "out_discards": 0,
             "in_pkts": None, "out_pkts": None}]})
        self.assertIn("perf.interface_errors", keys(rules.evaluate(snap([d2]))))

    def test_mtu_mismatch(self):
        d = dev_snap(snmp={"version": "2c", "community": "x", "interfaces": [
            {"ifdescr": "Gi0/1", "iftype": 6, "oper_status": "up", "admin_status": "up",
             "speed_mbps": 1000, "duplex": "full", "mtu": 9000, "in_errors": 0,
             "out_errors": 0, "in_discards": 0, "out_discards": 0,
             "in_pkts": 0, "out_pkts": 0}]})
        self.assertIn("perf.mtu_mismatch", keys(rules.evaluate(snap([d]))))

    def test_high_cpu_and_memory(self):
        d = dev_snap(snmp={"version": "2c", "community": "x", "cpu_load": 95.0,
                           "mem_used_pct": 92.0, "interfaces": []})
        fs = rules.evaluate(snap([d]))
        self.assertIn("perf.high_cpu", keys(fs))
        self.assertIn("perf.high_memory", keys(fs))
        d2 = dev_snap(snmp={"version": "2c", "community": "x", "cpu_load": 10.0,
                            "mem_used_pct": 20.0, "interfaces": []})
        self.assertNotIn("perf.high_cpu", keys(rules.evaluate(snap([d2]))))

    def test_link_speed_mismatch(self):
        d = dev_snap(unifi={"version": "6", "upgradable": False, "uplink_mac": "",
                            "fixed_ip": None, "ports": [
            {"port_idx": 1, "name": "Port 1", "up": True, "speed_mbps": 100,
             "max_speed_mbps": 1000, "native_vlan": None, "tagged_vlans": [],
             "link_down_count": 0, "tx_errors": 0, "rx_errors": 0, "is_uplink": False}]})
        self.assertIn("perf.link_speed_100", keys(rules.evaluate(snap([d]))))


class SecurityRulesTest(unittest.TestCase):
    def test_telnet_ssh_http(self):
        # Non-Ubiquiti gear: SSH exposed is a warning.
        d = dev_snap(vendor="Cisco", unifi_managed=False,
                     nmap={"open_ports": [22, 23, 80], "os": ""})
        fs = rules.evaluate(snap([d]))
        self.assertEqual(by_key(fs, "sec.telnet_exposed")[0]["severity"], "critical")
        self.assertEqual(by_key(fs, "sec.ssh_exposed")[0]["severity"], "warning")
        self.assertIn("sec.http_mgmt_plaintext", keys(fs))

    def test_ssh_on_ubiquiti_gear_is_info(self):
        # UniFi-default SSH is the vendor's stock management channel -> info.
        d = dev_snap(nmap={"open_ports": [22], "os": ""})   # vendor=Ubiquiti, unifi_managed=True
        self.assertEqual(by_key(rules.evaluate(snap([d])),
                                "sec.ssh_exposed")[0]["severity"], "info")

    def test_default_snmp_community_critical(self):
        d = dev_snap(snmp={"version": "2c", "community": "public", "interfaces": []})
        self.assertEqual(by_key(rules.evaluate(snap([d])),
                                "sec.default_snmp_community")[0]["severity"], "critical")
        d2 = dev_snap(snmp={"version": "2c", "community": "h4rd2guess", "interfaces": []})
        self.assertNotIn("sec.default_snmp_community", keys(rules.evaluate(snap([d2]))))

    def test_snmp_v2c_warning(self):
        d = dev_snap(snmp={"version": "2c", "community": "h4rd2guess", "interfaces": []})
        self.assertIn("sec.snmp_v2c", keys(rules.evaluate(snap([d]))))

    def test_open_and_legacy_ssids(self):
        s = snap(wlans=[
            {"name": "Guest", "enabled": True, "security": "open", "wpa_mode": "", "wpa_enc": "", "vlan": None},
            {"name": "Old", "enabled": True, "security": "wpa1", "wpa_mode": "wpa1", "wpa_enc": "", "vlan": None},
            {"name": "Good", "enabled": True, "security": "wpapsk", "wpa_mode": "wpa2", "wpa_enc": "ccmp", "vlan": 5},
        ])
        fs = rules.evaluate(s)
        self.assertEqual(by_key(fs, "sec.open_ssid")[0]["severity"], "critical")
        self.assertEqual(by_key(fs, "sec.legacy_wpa")[0]["severity"], "critical")
        self.assertNotIn("sec.wpa2_tkip", keys(fs))

    def test_wpa2_tkip(self):
        s = snap(wlans=[{"name": "Tk", "enabled": True, "security": "wpapsk",
                         "wpa_mode": "wpa2", "wpa_enc": "tkip", "vlan": 5}])
        self.assertIn("sec.wpa2_tkip", keys(rules.evaluate(s)))

    def test_firmware_outdated(self):
        d = dev_snap(unifi={"version": "6.6.53", "upgradable": True, "uplink_mac": "",
                            "fixed_ip": None, "ports": []})
        self.assertIn("sec.firmware_outdated", keys(rules.evaluate(snap([d]))))


class ReliabilityRulesTest(unittest.TestCase):
    def test_offline_gear_critical(self):
        d = dev_snap(ping={"reachable": False, "latency_ms": None})
        self.assertEqual(by_key(rules.evaluate(snap([d])),
                                "rel.offline_gear")[0]["severity"], "critical")

    def test_single_wan_info(self):
        d = dev_snap(device_type="gateway",
                     unifi={"version": "x", "upgradable": False, "uplink_mac": "",
                            "fixed_ip": None, "wan": {"status": "ok", "wan_count": 1}, "ports": []})
        self.assertIn("rel.single_wan", keys(rules.evaluate(snap([d]))))

    def test_link_down_count(self):
        def port(n):
            return {"port_idx": 1, "name": "Port 1", "up": True, "speed_mbps": 1000,
                    "max_speed_mbps": 1000, "native_vlan": None, "tagged_vlans": [],
                    "link_down_count": n, "tx_errors": 0, "rx_errors": 0, "is_uplink": False}
        uni = lambda n: {"version": "x", "upgradable": False, "uplink_mac": "",
                         "fixed_ip": None, "ports": [port(n)]}
        # a single old flap (the 08-18 PoE-cycle artifact) must NOT warn forever
        self.assertNotIn("rel.link_down_count", keys(rules.evaluate(snap([dev_snap(unifi=uni(1))]))))
        self.assertNotIn("rel.link_down_count", keys(rules.evaluate(snap([dev_snap(unifi=uni(2))]))))
        # repeated flaps (>2) warn
        self.assertIn("rel.link_down_count", keys(rules.evaluate(snap([dev_snap(unifi=uni(3))]))))

    def test_uptime_anomalies(self):
        d_reboot = dev_snap(snmp={"version": "2c", "community": "x", "uptime_seconds": 60,
                                  "interfaces": []})
        self.assertIn("rel.uptime_recent_reboot", keys(rules.evaluate(snap([d_reboot]))))
        d_long = dev_snap(snmp={"version": "2c", "community": "x",
                                "uptime_seconds": 400 * 24 * 3600, "interfaces": []})
        self.assertIn("rel.uptime_extended", keys(rules.evaluate(snap([d_long]))))

    def test_oper_down_admin_up(self):
        d = dev_snap(snmp={"version": "2c", "community": "x", "interfaces": [
            {"ifdescr": "Gi0/2", "iftype": 6, "oper_status": "down", "admin_status": "up",
             "speed_mbps": 1000, "duplex": "full", "mtu": 1500, "in_errors": 0,
             "out_errors": 0, "in_discards": 0, "out_discards": 0,
             "in_pkts": 0, "out_pkts": 0}]})
        self.assertIn("rel.oper_down_admin_up", keys(rules.evaluate(snap([d]))))


class HygieneRulesTest(unittest.TestCase):
    def test_duplicate_ip_critical(self):
        s = snap([dev_snap(name="a", ip="10.0.0.5"),
                  dev_snap(name="b", ip="10.0.0.5")])
        f = by_key(rules.evaluate(s), "hyg.duplicate_ip")[0]
        self.assertEqual(f["severity"], "critical")
        self.assertEqual(len(f["evidence"]["devices"]), 2)

    def test_duplicate_mac_warning(self):
        s = snap([dev_snap(name="a", mac="aa:bb:cc:00:00:99"),
                  dev_snap(name="b", mac="AA:BB:CC:00:00:99")])
        self.assertEqual(by_key(rules.evaluate(s),
                                "hyg.duplicate_mac")[0]["severity"], "warning")

    def test_unused_vlan(self):
        s = snap(networks=[{"name": "Old", "vlan": 50, "subnet": "", "enabled": True,
                            "dhcp": False, "dhcp_start": "", "dhcp_stop": ""}])
        self.assertIn("hyg.unused_vlan", keys(rules.evaluate(s)))
        # referenced by a port -> not unused
        s2 = snap(
            devices=[dev_snap(unifi={"version": "x", "upgradable": False, "uplink_mac": "",
                                     "fixed_ip": None, "ports": [
                {"port_idx": 1, "name": "Port 1", "up": True, "speed_mbps": 1000,
                 "max_speed_mbps": 1000, "native_vlan": 50, "tagged_vlans": [],
                 "link_down_count": 0, "tx_errors": 0, "rx_errors": 0, "is_uplink": False}]})],
            networks=[{"name": "Used", "vlan": 50, "subnet": "", "enabled": True,
                       "dhcp": False, "dhcp_start": "", "dhcp_stop": ""}])
        self.assertNotIn("hyg.unused_vlan", keys(rules.evaluate(s2)))

    def test_disabled_ssid_and_network(self):
        s = snap(networks=[{"name": "Off", "vlan": 60, "subnet": "", "enabled": False,
                            "dhcp": False, "dhcp_start": "", "dhcp_stop": ""}],
                 wlans=[{"name": "OffSSID", "enabled": False, "security": "wpapsk",
                         "wpa_mode": "wpa2", "wpa_enc": "ccmp", "vlan": None}])
        fs = rules.evaluate(s)
        self.assertIn("hyg.disabled_ssid", keys(fs))
        self.assertIn("hyg.disabled_network", keys(fs))

    def test_default_vlan1(self):
        s = snap(networks=[{"name": "LAN", "vlan": 1, "subnet": "", "enabled": True,
                            "dhcp": True, "dhcp_start": "", "dhcp_stop": ""}])
        self.assertIn("hyg.default_vlan1", keys(rules.evaluate(s)))


class PortDiscoveryTest(unittest.TestCase):
    """Per-port classification (connected/dead_end/unused) + the 08-19 Mini
    Rack port 4 flood signature (must classify dead_end)."""

    def _port(self, **kw):
        p = {
            "port_idx": 4, "name": "Port 4", "up": True, "speed_mbps": 1000,
            "mac_table_count": 0, "rx_packets": 0, "tx_packets": 0,
            "tx_multicast": 0, "stp_state": "forwarding", "uplink_devices": [],
        }
        p.update(kw)
        return p

    def test_down_port(self):
        self.assertEqual(rules.classify_port(self._port(up=False)), "down")

    def test_connected_by_macs(self):
        self.assertEqual(rules.classify_port(self._port(mac_table_count=3)), "connected")

    def test_connected_by_real_traffic(self):
        self.assertEqual(rules.classify_port(self._port(rx_packets=500)), "connected")
        self.assertEqual(rules.classify_port(self._port(tx_packets=500)), "connected")

    def test_unused_zero_traffic(self):
        self.assertEqual(rules.classify_port(self._port()), "unused")
        # negligible counters below the thresholds still count as unused
        self.assertEqual(rules.classify_port(self._port(rx_packets=3, tx_packets=3,
                                                        tx_multicast=3)), "unused")

    def test_dead_end_flood_signature(self):
        # the 08-19 Mini Rack port 4: UP @1G, 0 MACs, rx≈1, tx_multicast≈1.4M
        p = self._port(mac_table_count=0, rx_packets=1, tx_multicast=1400000)
        self.assertEqual(rules.classify_port(p), "dead_end")

    def test_dead_end_requires_flood(self):
        # moderate multicast is NOT the flood signature
        self.assertNotEqual(rules.classify_port(self._port(tx_multicast=50)), "dead_end")
        self.assertNotEqual(rules.classify_port(self._port(tx_multicast=5)), "dead_end")

    def test_rule_dead_end_port(self):
        d = dev_snap(name="Mini Rack", unifi={"version": "7", "upgradable": False,
                                              "uplink_mac": "", "fixed_ip": None,
                                              "wan": None, "ports": [
            self._port(mac_table_count=0, rx_packets=1, tx_multicast=1400000)]})
        fs = rules.evaluate(snap([d]))
        f = by_key(fs, "hyg.dead_end_port")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "warning")
        self.assertIn("Mini Rack Port 4", f[0]["title"])
        self.assertIn("dead-end cable", f[0]["title"])
        fx = rules.fixability("hyg.dead_end_port")
        self.assertTrue(fx["high_risk"])
        self.assertIn("Disable the port", fx["suggested_action"])
        self.assertIn("PLAN FIRST", fx["suggested_action"])

    def test_rule_unused_port_up(self):
        d = dev_snap(name="HouseSwitch", unifi={"version": "7", "upgradable": False,
                                                "uplink_mac": "", "fixed_ip": None,
                                                "wan": None, "ports": [
            self._port(port_idx=8, name="Port 8")]})
        fs = rules.evaluate(snap([d]))
        f = by_key(fs, "hyg.unused_port_up")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "info")
        self.assertIn("HouseSwitch Port 8", f[0]["title"])
        fx = rules.fixability("hyg.unused_port_up")
        self.assertEqual(fx["suggested_action"], "Disable the unused port.")
        self.assertFalse(fx["high_risk"])

    def test_port_discovery_story(self):
        dev = {"name": "Mini Rack"}
        p = self._port(rx_packets=1, tx_multicast=1400000)
        pd = rules.port_discovery(dev, p)
        self.assertEqual(pd["classification"], "dead_end")
        self.assertEqual(pd["label"], "Mini Rack Port 4")
        self.assertEqual(pd["mac_table_count"], 0)
        self.assertEqual(pd["tx_multicast"], 1400000)

    def test_port_discovery_known_uplink(self):
        dev = {"name": "HouseSwitch"}
        p = self._port(mac_table_count=3, uplink_devices=["U6 Mesh"])
        pd = rules.port_discovery(dev, p)
        self.assertEqual(pd["classification"], "connected")
        self.assertIn("U6 Mesh", pd["what"])

    def test_build_port_discovery(self):
        s = snap([dev_snap(name="Mini Rack", unifi={"version": "7", "upgradable": False,
                                                    "uplink_mac": "", "fixed_ip": None,
                                                    "wan": None, "ports": [
            self._port(rx_packets=1, tx_multicast=1400000)]})])
        pd = rules.build_port_discovery(s)
        self.assertEqual(len(pd), 1)
        self.assertEqual(pd[0]["classification"], "dead_end")
class PortNamingTest(unittest.TestCase):
    """Port findings use the canonical '<dev.name> Port <idx> (<desc>)' naming
    (display-only — finding keys stay stable)."""

    def _uni(self, ports):
        return {"version": "7", "upgradable": False, "uplink_mac": "",
                "fixed_ip": None, "wan": None, "ports": ports}

    def test_title_with_port_description(self):
        d = dev_snap(name="HouseSwitch", unifi=self._uni([
            {"port_idx": 7, "name": "Google WAN", "up": True, "speed_mbps": 1000,
             "max_speed_mbps": 1000, "native_vlan": None, "tagged_vlans": [],
             "link_down_count": 0, "tx_errors": 0, "rx_errors": 0, "is_uplink": False}]))
        f = by_key(rules.evaluate(snap([d])), "hyg.port_no_profile")[0]
        self.assertEqual(f["title"], "HouseSwitch Port 7 (Google WAN): no assigned network")
        self.assertIn("HouseSwitch Port 7 (Google WAN) is up with no", f["detail"])
        # display-only — the key and the interface (port idx) are unchanged
        self.assertEqual(f["finding_key"], "hyg.port_no_profile")
        self.assertEqual(f["interface"], "7")

    def test_title_without_port_description(self):
        d = dev_snap(name="HouseSwitch", unifi=self._uni([
            {"port_idx": 8, "name": "Port 8", "up": True, "speed_mbps": 1000,
             "max_speed_mbps": 1000, "native_vlan": 10, "tagged_vlans": [],
             "link_down_count": 0, "tx_errors": 0, "rx_errors": 0, "is_uplink": True}]))
        f = by_key(rules.evaluate(snap([d])), "hyg.unnamed_uplink_port")[0]
        self.assertEqual(f["title"], "HouseSwitch Port 8: unnamed uplink port")
        self.assertNotIn("(", f["title"])
        self.assertEqual(f["finding_key"], "hyg.unnamed_uplink_port")

    def test_uplink_title_with_description(self):
        d = dev_snap(name="HouseSwitch", unifi=self._uni([
            {"port_idx": 8, "name": "Uplink", "up": True, "speed_mbps": 1000,
             "max_speed_mbps": 1000, "native_vlan": 1, "tagged_vlans": [],
             "link_down_count": 0, "tx_errors": 0, "rx_errors": 0, "is_uplink": True}]))
        f = by_key(rules.evaluate(snap([d])), "sec.mgmt_vlan_on_uplink")[0]
        self.assertEqual(f["title"], "HouseSwitch Port 8 (Uplink): management VLAN on uplink")
        self.assertIn("HouseSwitch Port 8 (Uplink) carries the management VLAN", f["detail"])

    def test_link_flap_and_speed_titles(self):
        # link flap + a down-negotiated speed use the same naming
        d = dev_snap(name="HouseSwitch", unifi=self._uni([
            {"port_idx": 3, "name": "Downlink", "up": True, "speed_mbps": 100,
             "max_speed_mbps": 1000, "native_vlan": 10, "tagged_vlans": [],
             "link_down_count": 4, "tx_errors": 0, "rx_errors": 0, "is_uplink": False}]))
        fs = rules.evaluate(snap([d]))
        self.assertEqual(by_key(fs, "perf.link_speed_100")[0]["title"],
                         "HouseSwitch Port 3 (Downlink): link negotiated down to 100 Mbps")
        self.assertEqual(by_key(fs, "rel.link_down_count")[0]["title"],
                         "HouseSwitch Port 3 (Downlink): link has flapped")


class TicketNamingTest(unittest.TestCase):
    """Optimize ticket descriptions + change plans echo the port label."""

    def _finding(self, **kw):
        f = {
            "finding_key": "hyg.port_no_profile",
            "category": "hygiene",
            "severity": "info",
            "title": "HouseSwitch Port 7 (Google WAN): no assigned network",
            "detail": "HouseSwitch Port 7 (Google WAN) is up with no network profile "
                      "assigned — traffic lands on the default network.",
            "evidence": {"port": 7, "name": "Google WAN",
                         "port_label": "HouseSwitch Port 7 (Google WAN)"},
        }
        f.update(kw)
        return f

    def test_per_item_description_uses_port_label(self):
        desc = netopt_tickets.finding_description(self._finding(), 42)
        self.assertIn("HouseSwitch Port 7 (Google WAN)", desc)
        self.assertIn("HouseSwitch Port 7 (Google WAN): no assigned network", desc)

    def test_batched_description_uses_port_label(self):
        desc = netopt_tickets.batched_description([self._finding()], 42)
        self.assertIn("Finding 1: HouseSwitch Port 7 (Google WAN): no assigned network",
                      desc)

    def test_change_plan_current_state_uses_port_label(self):
        p = netopt_tickets.change_plan(self._finding())
        self.assertIn("HouseSwitch Port 7 (Google WAN)", p["current_state"])

    def test_change_plan_falls_back_to_port_label_without_detail(self):
        f = self._finding(detail="")
        p = netopt_tickets.change_plan(f)
        self.assertEqual(p["current_state"], "HouseSwitch Port 7 (Google WAN)")



class ScoringTest(unittest.TestCase):
    def test_clean_snapshot_scores_100(self):
        self.assertEqual(rules.score([])["overall"], 100)

    def test_score_penalties(self):
        findings = [
            {"severity": "critical", "category": "security"},
            {"severity": "warning", "category": "performance"},
            {"severity": "info", "category": "hygiene"},
        ]
        sc = rules.score(findings)
        self.assertEqual(sc["overall"], 100 - (20 + 5 + 2))
        self.assertEqual(sc["categories"]["security"], 80)
        self.assertEqual(sc["categories"]["performance"], 95)
        self.assertEqual(sc["counts"]["critical"], 1)

    def test_info_cap_100_infos(self):
        # 100 infos => only the first 5 cost −2 each; noise never tanks the score.
        findings = [{"severity": "info", "category": "hygiene"} for _ in range(100)]
        sc = rules.score(findings)
        self.assertEqual(sc["overall"], 90)
        self.assertEqual(sc["counts"]["info"], 100)   # the count stays honest
        self.assertGreaterEqual(sc["overall"], 88)

    def test_warnings_stack_no_cap(self):
        findings = [{"severity": "warning", "category": "performance"} for _ in range(10)]
        self.assertEqual(rules.score(findings)["overall"], 50)   # 10 × 5, no cap

    def test_critical_weight_20(self):
        self.assertEqual(rules.score([{"severity": "critical", "category": "security"}])["overall"], 80)

    def test_healthy_home_scores_ge_88(self):
        # U6 up (no critical), SSH/single-WAN/single-uplink/unnamed-port all
        # cosmetic infos -> a healthy home must score 90+ (pinned).
        gw = dev_snap(name="UCG-Max", device_type="gateway", ip="192.168.1.1",
                      mac="aa:bb:cc:00:00:01",
                      unifi={"version": "8", "upgradable": False, "uplink_mac": "",
                             "fixed_ip": None,
                             "wan": {"status": "ok", "wan_count": 1}, "ports": []})
        ap = dev_snap(name="U6 Mesh", device_type="ap", ip="192.168.5.41",
                      mac="aa:bb:cc:00:00:77",
                      nmap={"open_ports": [22], "os": ""},
                      ping={"reachable": True, "latency_ms": None},
                      unifi={"version": "6", "upgradable": False, "uplink_mac": "aa:bb:cc:00:00:01",
                             "fixed_ip": None, "wan": None, "ports": []})
        sw = dev_snap(name="HouseSwitch", device_type="switch", ip="192.168.5.2",
                      mac="aa:bb:cc:00:00:02",
                      unifi={"version": "7", "upgradable": False, "uplink_mac": "aa:bb:cc:00:00:01",
                             "fixed_ip": None, "wan": None, "ports": [
                          {"port_idx": 1, "name": "Port 1", "up": True, "speed_mbps": 1000,
                           "max_speed_mbps": 1000, "native_vlan": None, "tagged_vlans": [],
                           "link_down_count": 1, "tx_errors": 0, "rx_errors": 0, "is_uplink": True}]})
        findings = rules.evaluate(snap([gw, ap, sw]))
        self.assertNotIn("rel.offline_gear", keys(findings))
        self.assertNotIn("rel.link_down_count", keys(findings))   # single old flap ignored
        self.assertGreaterEqual(rules.score(findings)["overall"], 88)

    def test_score_floor_zero(self):
        findings = [{"severity": "critical", "category": "security"} for _ in range(10)]
        self.assertEqual(rules.score(findings)["overall"], 0)


# ═══════════════════════════════ collectors (pure parse helpers) ═══════════

class CollectorParseTest(unittest.TestCase):
    def test_parse_nmap_grepable(self):
        text = (
            "# Nmap 7.94 scan initiated\n"
            "Host: 192.0.2.1 ()\tStatus: Up\n"
            "Host: 192.0.2.1 ()\tPorts: 22/open/tcp//ssh//, 80/open/tcp//http//\tIgnored State: closed (98)\n"
        )
        parsed = network_opt.parse_nmap_grepable(text)
        self.assertEqual(parsed["open_ports"], [22, 80])
        self.assertEqual(parsed["open_services"]["22"], "ssh")

    def test_parse_snmp_value(self):
        self.assertEqual(network_opt._parse_snmp_value("STRING: Cisco IOS"), "Cisco IOS")
        self.assertEqual(network_opt._parse_snmp_value("INTEGER: 42"), 42)
        self.assertEqual(network_opt._parse_snmp_value("Gauge32: 1000"), 1000)
        self.assertEqual(network_opt._parse_snmp_value("Timeticks: (123456) 0:20:34.56"), 123456)
        self.assertIsNone(network_opt._parse_snmp_value(""))

    def test_guess_os(self):
        self.assertIn("network device", network_opt.guess_os([161, 443], None).lower())
        self.assertIn("windows", network_opt.guess_os([445], None).lower())
        self.assertIn("unix", network_opt.guess_os([22], 64).lower())


class PortCollectorTest(unittest.TestCase):
    """UniFi per-port snapshot extension: MAC/table counters + the controller
    uplink mapping (port -> known AP/switch name)."""

    def test_uplinks_by_port(self):
        raw = [
            {"mac": "aa:bb:cc:00:00:02", "name": "HouseSwitch", "type": "usw"},
            {"mac": "aa:bb:cc:00:00:77", "name": "U6 Mesh", "type": "uap",
             "uplink": {"uplink_mac": "aa:bb:cc:00:00:02", "uplink_remote_port": 4}},
            {"mac": "aa:bb:cc:00:00:78", "name": "U6 Lite", "type": "uap",
             "uplink": {"uplink_mac": "AA:BB:CC:00:00:02", "uplink_remote_port": "4"}},
        ]
        up = network_opt._uplinks_by_port(raw)
        self.assertEqual(up[("aa:bb:cc:00:00:02", 4)], ["U6 Mesh", "U6 Lite"])

    def test_uplinks_ignores_missing(self):
        raw = [{"mac": "aa:bb:cc:00:00:77", "name": "U6 Mesh", "type": "uap",
                "uplink": {"uplink_mac": "", "uplink_remote_port": None}}]
        self.assertEqual(network_opt._uplinks_by_port(raw), {})

    def test_port_mac_table_count(self):
        self.assertEqual(network_opt._port_mac_table_count({"mac_table_count": "3"}), 3)
        self.assertEqual(network_opt._port_mac_table_count({"mac_table": ["a", "b"]}), 2)
        self.assertEqual(network_opt._port_mac_table_count({}), 0)
        self.assertEqual(network_opt._port_mac_table_count({"mac_table_count": "x"}), 0)


# ═══════════════════════════ SNMP OID pinning ══════════════════════════════

class SnmpOidTest(unittest.TestCase):
    """Pin the exact OIDs the collector reads (the 2026-08-18 wrong-OID bug).

    The fake ``_run`` answers ONLY the correct OIDs; a reverted/incorrect OID
    returns ``noSuchObject`` -> ``collect_snmp`` returns None -> test fails.
    """

    def _collect(self, ip="10.0.0.2", community="public"):
        calls = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            binary = cmd[0]
            if binary == "snmpget":
                oid = cmd[-1]
                vals = {
                    "1.3.6.1.2.1.1.1.0": "Cisco IOS Software, Version 17.9",
                    "1.3.6.1.2.1.1.5.0": "core-sw",
                    "1.3.6.1.2.1.1.3.0": "360000",   # Timeticks -> 3600 s
                    "1.3.6.1.4.1.2021.10.1.3.1": "42",
                    "1.3.6.1.4.1.2021.4.5.0": "8000",
                    "1.3.6.1.4.1.2021.4.6.0": "2000",
                }
                if oid not in vals:
                    return {"ok": False, "stdout": "", "stderr": "noSuchObject"}
                return {"ok": True, "stdout": vals[oid] + "\n", "stderr": ""}
            if binary == "snmpwalk":
                base = cmd[-1]
                if base == network_opt._IFTABLE:
                    return {"ok": True, "stdout": (
                        "1.3.6.1.2.1.2.2.1.1.1 = INTEGER: 1\n"
                        "1.3.6.1.2.1.2.2.1.2.1 = STRING: Gi0/1\n"
                        "1.3.6.1.2.1.2.2.1.3.1 = INTEGER: 6\n"
                        "1.3.6.1.2.1.2.2.1.4.1 = INTEGER: 1500\n"
                        "1.3.6.1.2.1.2.2.1.5.1 = Gauge32: 1000000000\n"
                        "1.3.6.1.2.1.2.2.1.7.1 = INTEGER: 1\n"
                        "1.3.6.1.2.1.2.2.1.8.1 = INTEGER: 1\n"
                        "1.3.6.1.2.1.2.2.1.11.1 = Counter32: 1000\n"
                        "1.3.6.1.2.1.2.2.1.13.1 = Counter32: 2\n"
                        "1.3.6.1.2.1.2.2.1.14.1 = Counter32: 5\n"
                        "1.3.6.1.2.1.2.2.1.17.1 = Counter32: 900\n"
                        "1.3.6.1.2.1.2.2.1.19.1 = Counter32: 1\n"
                        "1.3.6.1.2.1.2.2.1.20.1 = Counter32: 7\n"
                    ), "stderr": ""}
                if base == "1.3.6.1.2.1.31.1.1.1.15":
                    return {"ok": True, "stdout":
                            "1.3.6.1.2.1.31.1.1.1.15.1 = Gauge32: 1000\n", "stderr": ""}
                if base == network_opt._DOT3_DUPLEX:
                    return {"ok": True, "stdout":
                            "1.3.6.1.2.1.10.7.2.1.19.1 = INTEGER: 3\n", "stderr": ""}
                return {"ok": False, "stdout": "", "stderr": "unknown base"}
            return {"ok": False, "stdout": "", "stderr": "unknown binary"}

        with patch.object(network_opt, "_binary", return_value=True), \
             patch.object(network_opt, "_run", side_effect=fake_run):
            snap = network_opt.collect_snmp(ip, community)
        return snap, calls

    def test_system_oids_correct(self):
        snap, calls = self._collect()
        get_oids = [c[-1] for c in calls if c[0] == "snmpget"]
        self.assertIn("1.3.6.1.2.1.1.1.0", get_oids)   # sysDescr
        self.assertIn("1.3.6.1.2.1.1.5.0", get_oids)   # sysName
        self.assertIn("1.3.6.1.2.1.1.3.0", get_oids)   # sysUpTime
        self.assertEqual(snap["sysdescr"], "Cisco IOS Software, Version 17.9")
        self.assertEqual(snap["sysname"], "core-sw")
        self.assertEqual(snap["uptime_seconds"], 3600)

    def test_ucdsnmp_oids_correct(self):
        snap, calls = self._collect()
        get_oids = [c[-1] for c in calls if c[0] == "snmpget"]
        self.assertIn("1.3.6.1.4.1.2021.10.1.3.1", get_oids)   # laLoad 1-min
        self.assertIn("1.3.6.1.4.1.2021.4.5.0", get_oids)      # memTotalReal
        self.assertIn("1.3.6.1.4.1.2021.4.6.0", get_oids)      # memAvailReal
        self.assertEqual(snap["cpu_load"], 42.0)
        self.assertEqual(snap["mem_used_pct"], 75.0)

    def test_iftable_parse_uses_correct_columns(self):
        snap, calls = self._collect()
        walk_bases = [c[-1] for c in calls if c[0] == "snmpwalk"]
        self.assertIn("1.3.6.1.2.1.2.2.1", walk_bases)          # ifTable
        self.assertEqual(len(snap["interfaces"]), 1)
        itf = snap["interfaces"][0]
        self.assertEqual(itf["ifindex"], "1")
        self.assertEqual(itf["ifdescr"], "Gi0/1")
        self.assertEqual(itf["iftype"], 6)
        self.assertEqual(itf["mtu"], 1500)
        self.assertEqual(itf["oper_status"], "up")              # ifOperStatus .8
        self.assertEqual(itf["admin_status"], "up")
        self.assertEqual(itf["speed_mbps"], 1000)
        self.assertEqual(itf["duplex"], "full")
        self.assertEqual(itf["in_errors"], 5)                   # ifInErrors .14
        self.assertEqual(itf["out_errors"], 7)                  # ifOutErrors .20
        self.assertEqual(itf["in_discards"], 2)
        self.assertEqual(itf["out_discards"], 1)
        self.assertEqual(itf["in_pkts"], 1000)
        self.assertEqual(itf["out_pkts"], 900)

    def test_no_sysdescr_returns_none(self):
        # a device that drops the (now-correct) sysDescr get is treated as
        # not answering SNMP — same contract as before, still honored.
        def fake_run(cmd, timeout):
            return {"ok": False, "stdout": "", "stderr": "timeout"}
        with patch.object(network_opt, "_binary", return_value=True), \
             patch.object(network_opt, "_run", side_effect=fake_run):
            self.assertIsNone(network_opt.collect_snmp("10.0.0.2"))


# ═══════════════════════════ controller-live authority ═════════════════════

class ControllerAuthorityTest(unittest.TestCase):
    """For UniFi-managed gear the controller snapshot is the authority for
    reachability + the live IP — a stale DB record must never produce a false
    ``offline_gear`` critical (the 08-18 U6 Mesh incident)."""

    def _device(self, **kw):
        d = SimpleNamespace(id=1, name="U6 Mesh", ip_address="192.168.1.41",
                            mac_address="aa:bb:cc:00:00:77", device_type="ap",
                            vendor="Ubiquiti", model="U6 Mesh", unifi_managed=True,
                            snmp_community=None, last_seen=None)
        for k, v in kw.items():
            setattr(d, k, v)
        return d

    def _rec(self, status="online", ip="192.168.5.41"):
        return {"ip": ip, "status": status, "name": "U6 Mesh", "model": "U6 Mesh",
                "version": "6.6.53", "upgradable": False, "uptime_seconds": 1000,
                "uplink_mac": "", "fixed_ip": None, "wan": None, "ports": []}

    def test_stale_record_controller_live_no_false_offline(self):
        dev = self._device()
        with patch.object(network_opt, "collect_nmap",
                          return_value={"open_ports": [22], "open_services": {}, "os": ""}) as nm:
            dev_snap = network_opt.collect_device(dev, {"profile": "standard"}, self._rec())
        self.assertEqual(dev_snap["ip"], "192.168.5.41")          # live IP used, not the record
        self.assertTrue(dev_snap["ping"]["reachable"])             # controller authority
        self.assertEqual(dev_snap["ping"]["source"], "unifi")
        nm.assert_called_once_with("192.168.5.41", "standard")     # nmap hits the LIVE ip
        self.assertNotIn("rel.offline_gear", keys(rules.evaluate(snap([dev_snap]))))

    def test_controller_offline_still_bites(self):
        dev = self._device()
        with patch.object(network_opt, "collect_nmap",
                          return_value={"open_ports": [], "open_services": {}, "os": ""}) as nm:
            dev_snap = network_opt.collect_device(dev, {"profile": "standard"},
                                                  self._rec(status="offline"))
        self.assertFalse(dev_snap["ping"]["reachable"])
        nm.assert_not_called()    # offline gear isn't port-scanned
        self.assertIn("rel.offline_gear", keys(rules.evaluate(snap([dev_snap]))))

    def test_non_unifi_keeps_record_path(self):
        dev = self._device(unifi_managed=False, vendor="Cisco", model="Catalyst")
        with patch.object(network_opt, "collect_ping",
                          return_value={"reachable": True, "latency_ms": 1.0}) as ping, \
             patch.object(network_opt, "collect_nmap",
                          return_value={"open_ports": [], "open_services": {}, "os": ""}):
            dev_snap = network_opt.collect_device(dev, {"profile": "standard"}, None)
        ping.assert_called_once_with("192.168.1.41")   # non-UniFi keeps the DB record IP
        self.assertNotIn("source", dev_snap["ping"])


# ═══════════════════════════════ orchestrator (fake collectors) ════════════

class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        for table in (Finding, ScanRun, Ticket, Device, User):
            db.query(table).delete()
        db.commit()
        db.close()

    def _add_device(self, name, ip, device_type, claimed=True, unifi=False, mac=None):
        db = SessionLocal()
        d = Device(name=name, ip_address=ip, device_type=device_type, claimed=claimed,
                   unifi_managed=unifi, mac_address=mac)
        db.add(d)
        db.commit()
        db.refresh(d)
        did = d.id
        db.close()
        return did

    def _run(self):
        db = SessionLocal()
        run = ScanRun(status="queued", scope={})
        db.add(run)
        db.commit()
        db.refresh(run)
        rid = run.id
        db.close()
        return rid

    def test_execute_scan_persists_findings(self):
        self._add_device("core-sw", "10.0.0.2", "switch", mac="aa:bb:cc:00:00:02")

        def fake_collect_device(device, config, unifi_rec=None):
            return {"device_id": device.id, "name": device.name, "ip": device.ip_address,
                    "device_type": (device.device_type or "unknown").lower(),
                    "mac": device.mac_address, "unifi_managed": device.unifi_managed,
                    "ping": {"reachable": True, "latency_ms": 1.0},
                    "nmap": {"open_ports": [23], "os": ""},
                    "snmp": {"version": "2c", "community": "public", "interfaces": []},
                    "unifi": None, "last_seen": None}

        rid = self._run()
        db = SessionLocal()
        with patch.object(network_opt, "collect_unifi",
                          return_value={"devices_by_mac": {}, "networks": [], "wlans": []}), \
             patch.object(network_opt, "collect_device", side_effect=fake_collect_device), \
             patch.object(network_opt, "netopt_config",
                          return_value={"enabled": True, "max_hosts": 25, "concurrency": 2,
                                        "profile": "standard",
                                        "default_schedule": {"mode": "recurring", "day": "0",
                                                             "hour": 3, "enabled": False}}):
            result = network_opt.execute_scan(db, rid)
        db.close()

        self.assertEqual(result["status"], "completed")
        db = SessionLocal()
        run = db.query(ScanRun).get(rid)
        self.assertEqual(run.status, "completed")
        self.assertIsNotNone(run.score)
        self.assertIsNotNone(run.summary)
        findings = db.query(Finding).filter(Finding.run_id == rid).all()
        self.assertTrue(findings)
        self.assertIn("sec.telnet_exposed", {f.finding_key for f in findings})
        db.close()

    def test_execute_scan_skips_servers(self):
        # servers are NOT network gear — out of scope even if claimed
        self._add_device("nas", "10.0.0.20", "server")
        rid = self._run()
        db = SessionLocal()
        with patch.object(network_opt, "collect_unifi",
                          return_value={"devices_by_mac": {}, "networks": [], "wlans": []}), \
             patch.object(network_opt, "netopt_config",
                          return_value={"enabled": True, "max_hosts": 25, "concurrency": 2,
                                        "profile": "standard",
                                        "default_schedule": {"mode": "recurring", "day": "0",
                                                             "hour": 3, "enabled": False}}):
            result = network_opt.execute_scan(db, rid)
        db.close()
        self.assertEqual(result["status"], "completed")
        db = SessionLocal()
        self.assertEqual(db.query(Finding).filter(Finding.run_id == rid).count(), 0)
        db.close()

    def test_unifi_managed_uses_controller_live_ip(self):
        # 08-18 regression: stale DB record (.1.41) + controller live (.5.41,
        # online) -> device UP, NO offline_gear critical, score stays high.
        self._add_device("U6 Mesh", "192.168.1.41", "ap", unifi=True, mac="aa:bb:cc:00:00:77")
        rid = self._run()
        db = SessionLocal()
        unifi_data = {
            "devices_by_mac": {
                "aa:bb:cc:00:00:77": {"ip": "192.168.5.41", "status": "online",
                                      "name": "U6 Mesh", "model": "U6 Mesh",
                                      "version": "6.6.53", "upgradable": False,
                                      "uptime_seconds": 1000, "uplink_mac": "",
                                      "fixed_ip": None, "wan": None, "ports": []}},
            "networks": [], "wlans": [],
        }
        with patch.object(network_opt, "collect_unifi", return_value=unifi_data), \
             patch.object(network_opt, "collect_nmap",
                          return_value={"open_ports": [], "open_services": {}, "os": ""}), \
             patch.object(network_opt, "netopt_config",
                          return_value={"enabled": True, "max_hosts": 25, "concurrency": 2,
                                        "profile": "standard",
                                        "default_schedule": {"mode": "recurring", "day": "0",
                                                             "hour": 3, "enabled": False}}):
            result = network_opt.execute_scan(db, rid)
        db.close()
        self.assertEqual(result["status"], "completed")
        db = SessionLocal()
        run_keys = {f.finding_key for f in db.query(Finding).filter(Finding.run_id == rid)}
        self.assertNotIn("rel.offline_gear", run_keys)
        self.assertGreaterEqual(db.query(ScanRun).get(rid).score, 88)
        db.close()


class SelfProtectionTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        for table in (Finding, ScanRun, Ticket, Device, User):
            db.query(table).delete()
        db.commit()
        db.close()

    def test_self_identifiers(self):
        ids = network_opt.self_identifiers({"APPLIANCE_IP": "192.168.4.207"})
        self.assertIn("192.168.4.207", ids)
        self.assertIn("127.0.0.1", ids)

    def test_is_self(self):
        ids = network_opt.self_identifiers({"APPLIANCE_IP": "192.168.4.207"})
        d = SimpleNamespace(name="core-sw", ip_address="192.168.4.207", hostname=None)
        self.assertTrue(network_opt.is_self(d, ids))
        d2 = SimpleNamespace(name="core-sw", ip_address="192.168.4.50", hostname=None)
        self.assertFalse(network_opt.is_self(d2, ids))
        d3 = SimpleNamespace(name="bareNOC-host", ip_address="10.0.0.9", hostname=None)
        self.assertTrue(network_opt.is_self(d3, ids))

    def test_build_scope_excludes_appliance(self):
        db = SessionLocal()
        db.add(Device(name="appliance", ip_address="192.168.4.207", device_type="switch",
                      claimed=True))
        db.add(Device(name="core-sw", ip_address="192.168.4.50", device_type="switch",
                      claimed=True))
        db.add(Device(name="nas", ip_address="192.168.4.60", device_type="server",
                      claimed=True))
        db.commit()
        config = {"enabled": True, "max_hosts": 25, "concurrency": 2,
                  "profile": "standard",
                  "default_schedule": {"mode": "recurring", "day": "0", "hour": 3,
                                       "enabled": False}}
        scope = network_opt.build_scope(db, config,
                                        env={"APPLIANCE_IP": "192.168.4.207"})
        db.close()
        included_ips = {d.ip_address for d in scope["devices"]}
        self.assertEqual(included_ips, {"192.168.4.50"})   # self + server excluded
        excluded_ips = {e["ip"] for e in scope["excluded"]}
        self.assertIn("192.168.4.207", excluded_ips)

    def test_max_hosts_cap(self):
        db = SessionLocal()
        for i in range(10):
            db.add(Device(name=f"sw-{i}", ip_address=f"192.168.4.{10 + i}",
                          device_type="switch", claimed=True))
        db.commit()
        config = {"enabled": True, "max_hosts": 3, "concurrency": 2, "profile": "standard",
                  "default_schedule": {"mode": "recurring", "day": "0", "hour": 3,
                                       "enabled": False}}
        scope = network_opt.build_scope(db, config, env={})
        db.close()
        self.assertEqual(len(scope["devices"]), 3)


# ═══════════════════════════════ scheduling ════════════════════════════════

class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netopt-sched-")
        self._dir = patch.object(routes, "SCHEDULE_FILE",
                                 os.path.join(self.tmp, "netopt_schedule.conf"))
        self._dir.start()
        self._cfg = patch.object(network_opt, "netopt_config",
                                 return_value={"enabled": True, "max_hosts": 25,
                                               "concurrency": 5, "profile": "standard",
                                               "default_schedule": {"mode": "recurring",
                                                                    "day": "0", "hour": 3,
                                                                    "enabled": False}})
        self._cfg.start()

    def tearDown(self):
        self._dir.stop()
        self._cfg.stop()

    def test_default_schedule(self):
        sc = routes._read_schedule()
        self.assertEqual(sc["mode"], "recurring")
        self.assertEqual(sc["hour"], 3)
        self.assertFalse(sc["enabled"])

    def test_set_schedule_recurring_writes_canonical(self):
        r = routes.set_schedule(routes.ScheduleBody(enabled=True, mode="recurring",
                                                    day="1", hour=4),
                                SimpleNamespace(username="admin"))
        self.assertEqual(r["schedule"]["mode"], "recurring")
        with open(os.path.join(self.tmp, "netopt_schedule.conf")) as f:
            content = f.read()
        self.assertIn("mode=recurring\n", content)
        self.assertIn("day=1\n", content)
        self.assertIn("hour=4\n", content)

    def test_set_schedule_onetime_requires_future(self):
        past = (routes._local_now() - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
        with self.assertRaises(Exception):
            routes.set_schedule(routes.ScheduleBody(enabled=True, mode="onetime", when=past),
                                SimpleNamespace(username="admin"))
        future = (routes._local_now() + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        r = routes.set_schedule(routes.ScheduleBody(enabled=True, mode="onetime", when=future),
                                SimpleNamespace(username="admin"))
        self.assertEqual(r["schedule"]["when"], future)

    def test_complete_marks_fired_and_disables(self):
        routes._write_schedule({"mode": "onetime", "enabled": True, "day": "0", "hour": 3,
                                "when": "2026-08-20T03:00", "fired": ""})
        r = routes.complete_schedule(SimpleNamespace(username="agent"))
        self.assertFalse(r["schedule"]["enabled"])
        self.assertTrue(r["schedule"]["fired"])

    def test_cancel_disables(self):
        routes._write_schedule({"mode": "recurring", "enabled": True, "day": "0", "hour": 3,
                                "when": "", "fired": ""})
        r = routes.cancel_schedule(SimpleNamespace(username="admin"))
        self.assertFalse(r["schedule"]["enabled"])


# ═══════════════════════════════ admin gating ══════════════════════════════

class AdminGatingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        for table in (Finding, ScanRun, Ticket, Device, User):
            db.query(table).delete()
        db.commit()
        from auth import hash_password
        admin = User(username="admin", role="admin",
                     hashed_password=hash_password("pw"), is_active=True)
        op = User(username="op", role="operator",
                  hashed_password=hash_password("pw"), is_active=True)
        db.add(admin)
        db.add(op)
        db.commit()
        # capture plain attrs BEFORE the session closes (ORM objects expire on close)
        self.admin = SimpleNamespace(username="admin", role="admin")
        self.op = SimpleNamespace(username="op", role="operator")
        db.close()

    def _client(self, user):
        from main import app
        from fastapi.testclient import TestClient
        from auth import create_access_token
        token = create_access_token({"sub": user.username, "role": user.role,
                                     "groups": [], "auth_method": "password"})
        return TestClient(app), token

    def test_routes_require_admin(self):
        client, token = self._client(self.op)
        self.assertEqual(client.get("/api/v1/netopt/status",
                                    headers={"Authorization": f"Bearer {token}"}).status_code, 403)
        self.assertEqual(client.post("/api/v1/netopt/runs", json={},
                                     headers={"Authorization": f"Bearer {token}"}).status_code, 403)
        self.assertEqual(client.get("/api/v1/netopt/limits",
                                    headers={"Authorization": f"Bearer {token}"}).status_code, 403)

    def test_admin_passes_gate(self):
        client, token = self._client(self.admin)
        self.assertEqual(client.get("/api/v1/netopt/status",
                                    headers={"Authorization": f"Bearer {token}"}).status_code, 200)
        # empty scope -> 400 (gate passed; nothing to scan)
        self.assertEqual(client.post("/api/v1/netopt/runs", json={},
                                     headers={"Authorization": f"Bearer {token}"}).status_code, 400)

    def test_unauthenticated_401(self):
        from main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        self.assertEqual(client.get("/api/v1/netopt/status").status_code, 401)
        self.assertEqual(client.post("/api/v1/netopt/runs", json={}).status_code, 401)

    def test_route_binds_to_start_run(self):
        from main import app
        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/netopt/runs"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "start_run")


# ═══════════════════════════════ fixability mapping ═══════════════════════

class FixabilityTest(unittest.TestCase):
    NON_FIXABLE = {"rel.single_wan", "rel.single_uplink",
                   "hyg.disabled_ssid", "hyg.unused_vlan"}

    def test_non_fixable_rules_disabled(self):
        for key in self.NON_FIXABLE:
            fx = rules.fixability(key)
            self.assertFalse(fx["fixable"], key)
            self.assertEqual(fx["suggested_action"], "informational — not actionable")

    def test_fixable_rules_have_suggested_action(self):
        for r in rules.RULES + rules.SNAPSHOT_RULES:
            fx = rules.fixability(r["key"])
            if r["key"] in self.NON_FIXABLE:
                self.assertFalse(fx["fixable"], r["key"])
            else:
                self.assertTrue(fx["fixable"], r["key"])
                self.assertTrue((fx["suggested_action"] or "").strip(), r["key"])

    def test_registry_annotated_with_fixability(self):
        # every registered rule gains fixable + suggested_action
        for r in rules.RULES + rules.SNAPSHOT_RULES:
            self.assertIn("fixable", r, r["key"])
            self.assertIn("suggested_action", r, r["key"])
            self.assertEqual(r["fixable"], rules.fixability(r["key"])["fixable"])

    def test_unknown_key_defaults_fixable(self):
        fx = rules.fixability("perf.does_not_exist")
        self.assertTrue(fx["fixable"])
        self.assertTrue(fx["suggested_action"])

    # ── agent-foresight: risk-aware recommendations ─────────────────────

    def test_high_risk_flag_on_port_vlan_uplink_rules(self):
        for key in rules.HIGH_RISK_KEYS:
            self.assertTrue(rules.fixability(key)["high_risk"], key)
        # ssh/http/telnet management-plane fixes are safe (the brief's case)
        for key in ("sec.ssh_exposed", "sec.http_mgmt_plaintext", "sec.telnet_exposed"):
            self.assertFalse(rules.fixability(key)["high_risk"], key)

    def test_high_risk_rules_carry_blast_radius_and_plan_note(self):
        for key in rules.HIGH_RISK_KEYS:
            fx = rules.fixability(key)
            self.assertTrue((fx["blast_radius"] or "").strip(), key)
            self.assertTrue((fx["plan_note"] or "").strip(), key)
            # the suggested_action is risk-aware: blast radius + plan-first note
            self.assertIn("PLAN FIRST", fx["suggested_action"], key)

    def test_unnamed_uplink_never_changes_uplink(self):
        fx = rules.fixability("hyg.unnamed_uplink_port")
        self.assertIn("DO NOT CHANGE THE UPLINK", fx["suggested_action"].upper())

    def test_port_no_profile_blast_radius_mentions_connected_devices(self):
        fx = rules.fixability("hyg.port_no_profile")
        self.assertIn("move", fx["blast_radius"].lower())
        self.assertIn("verify", fx["suggested_action"].lower())

    def test_ssh_http_rules_marked_safe(self):
        for key in ("sec.ssh_exposed", "sec.http_mgmt_plaintext", "sec.telnet_exposed"):
            fx = rules.fixability(key)
            self.assertTrue(fx["blast_radius"].lower().startswith("safe"), key)

    def test_registry_annotated_with_risk(self):
        for r in rules.RULES + rules.SNAPSHOT_RULES:
            self.assertIn("high_risk", r, r["key"])
            self.assertIn("blast_radius", r, r["key"])
            self.assertIn("plan_note", r, r["key"])


# ═══════════════════════════════ optimize API ═════════════════════════════

class OptimizeApiTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        for table in (Finding, ScanRun, Ticket, Device, User):
            db.query(table).delete()
        db.commit()
        from auth import hash_password
        admin = User(username="admin", role="admin",
                     hashed_password=hash_password("pw"), is_active=True)
        op = User(username="op", role="operator",
                  hashed_password=hash_password("pw"), is_active=True)
        db.add(admin)
        db.add(op)
        db.commit()
        run = ScanRun(status="completed", score=80, summary="{}", scope={})
        db.add(run)
        db.commit()
        db.refresh(run)
        self.run_id = run.id
        self.admin_id = admin.id

        f1 = Finding(run_id=run.id, finding_key="sec.firmware_outdated",
                     category="security", severity="warning",
                     title="Firmware update available for core-sw",
                     detail="core-sw (v6) has a firmware update available.",
                     evidence={"version": "6"})
        f2 = Finding(run_id=run.id, finding_key="hyg.unnamed_uplink_port",
                     category="hygiene", severity="info",
                     title="Unnamed uplink port on core-sw",
                     detail="Uplink port has no meaningful label.",
                     evidence={"port": 1})
        f3 = Finding(run_id=run.id, finding_key="rel.single_wan",
                     category="reliability", severity="info",
                     title="Single WAN (no failover) on UCG-Max",
                     detail="Single WAN link.",
                     evidence={"wan_count": 1})
        db.add_all([f1, f2, f3])
        db.commit()
        db.refresh(f1)
        db.refresh(f2)
        db.refresh(f3)
        self.fixable_ids = [f1.id, f2.id]
        self.nonfixable_id = f3.id
        db.close()

    def _client(self, username="admin", role="admin"):
        from main import app
        from fastapi.testclient import TestClient
        from auth import create_access_token
        token = create_access_token({"sub": username, "role": role,
                                     "groups": [], "auth_method": "password"})
        return TestClient(app), token

    def _post(self, body, username="admin", role="admin"):
        client, token = self._client(username, role)
        return client.post(f"/api/v1/netopt/runs/{self.run_id}/optimize",
                           json=body, headers={"Authorization": f"Bearer {token}"})

    def test_optimize_requires_admin(self):
        r = self._post({"finding_ids": self.fixable_ids, "mode": "per_item"},
                       username="op", role="operator")
        self.assertEqual(r.status_code, 403)

    def test_per_item_creates_one_ticket_per_finding(self):
        r = self._post({"finding_ids": self.fixable_ids, "mode": "per_item"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["mode"], "per_item")
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["tickets"]), 2)
        db = SessionLocal()
        tickets = db.query(Ticket).filter(Ticket.source == "optimize").all()
        self.assertEqual(len(tickets), 2)
        self.assertEqual({t.priority for t in tickets}, {"P2", "P3"})  # warning->P2, info->P3
        db.close()


    def test_findings_linked_to_ticket_after_optimize(self):
        """Optimize must write fix_ticket_id back to the findings (the 08-19
        'still actionable after batch' bug) so the run detail stops offering
        them."""
        from models import Finding
        r = self._post({"finding_ids": self.fixable_ids, "mode": "batched"})
        self.assertEqual(r.status_code, 200, r.text)
        db = SessionLocal()
        ticket = db.query(Ticket).filter(Ticket.source == "optimize").first()
        linked = db.query(Finding).filter(Finding.id.in_(self.fixable_ids)).all()
        self.assertTrue(all(f.fix_ticket_id == ticket.ticket_id for f in linked))
        db.close()

    def test_comment_embedded_and_run_ref_and_priority(self):
        r = self._post({"finding_ids": [self.fixable_ids[0]], "mode": "per_item",
                        "comments": {str(self.fixable_ids[0]): "Do it after hours"}})
        self.assertEqual(r.status_code, 200, r.text)
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.source == "optimize").first()
        desc = t.description or ""
        self.assertIn("ADMIN CONTEXT", desc)
        self.assertIn("read them fully before any action", desc)
        self.assertIn("Do it after hours", desc)
        self.assertIn(f"#{self.run_id}", desc)
        self.assertIn("Suggested action:", desc)
        self.assertEqual(t.title, "Firmware update available for core-sw")
        self.assertEqual(t.priority, "P2")   # warning -> P2
        notes = json.loads(t.work_notes or "[]")
        self.assertTrue(any(n.get("event") == "admin_context" for n in notes))
        self.assertTrue(any("read them fully before any action" in n.get("detail", "")
                            for n in notes))
        db.close()

    def test_batched_single_ticket_with_sections(self):
        r = self._post({"finding_ids": self.fixable_ids, "mode": "batched"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["mode"], "batched")
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["tickets"]), 1)
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.source == "optimize").first()
        self.assertIn("Finding 1:", t.description)
        self.assertIn("Finding 2:", t.description)
        self.assertIn(f"#{self.run_id}", t.description)
        self.assertEqual(t.priority, "P2")   # highest selected severity wins
        db.close()

    def test_change_plan_artifact_in_ticket_description(self):
        # f2 = hyg.unnamed_uplink_port (high-risk) — the ticket carries the
        # pre-thought CHANGE PLAN: current -> proposed -> blast radius ->
        # verification -> rollback (the 08-19 incident fix).
        r = self._post({"finding_ids": [self.fixable_ids[1]], "mode": "per_item"})
        self.assertEqual(r.status_code, 200, r.text)
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.source == "optimize").first()
        desc = t.description or ""
        self.assertIn("CHANGE PLAN", desc)
        self.assertIn("Current state:", desc)
        self.assertIn("Proposed change:", desc)
        self.assertIn("Blast radius:", desc)
        self.assertIn("Verification step:", desc)
        self.assertIn("Rollback step:", desc)
        # the high-risk blast radius for an unnamed uplink is explicit
        self.assertIn("DO NOT CHANGE THE UPLINK", desc.upper())
        notes = json.loads(t.work_notes or "[]")
        self.assertTrue(any(n.get("event") == "change_plan" for n in notes))
        db.close()

    def test_safe_rule_blast_radius_still_has_plan(self):
        # f1 = sec.firmware_outdated (not high-risk) — the change plan is still
        # present, with the default "no port/VLAN/uplink" blast radius.
        r = self._post({"finding_ids": [self.fixable_ids[0]], "mode": "per_item"})
        self.assertEqual(r.status_code, 200, r.text)
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.source == "optimize").first()
        desc = t.description or ""
        self.assertIn("CHANGE PLAN", desc)
        self.assertIn("Blast radius:", desc)
        self.assertIn("does not change any port/VLAN/uplink", desc)
        db.close()

    def test_nonfixable_rejected(self):
        r = self._post({"finding_ids": [self.nonfixable_id], "mode": "per_item"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not actionable", r.json()["detail"])

    def test_mixed_fixable_and_nonfixable_rejected(self):
        r = self._post({"finding_ids": self.fixable_ids + [self.nonfixable_id],
                        "mode": "batched"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not actionable", r.json()["detail"])

    def test_finding_from_other_run_rejected(self):
        db = SessionLocal()
        run2 = ScanRun(status="completed", score=90, summary="{}", scope={})
        db.add(run2)
        db.commit()
        db.refresh(run2)
        f = Finding(run_id=run2.id, finding_key="perf.high_cpu",
                    category="performance", severity="warning",
                    title="High CPU", detail="cpu", evidence={})
        db.add(f)
        db.commit()
        db.refresh(f)
        db.close()
        r = self._post({"finding_ids": [f.id], "mode": "per_item"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not in this run", r.json()["detail"])

    def test_per_item_cap_10(self):
        db = SessionLocal()
        extra = []
        for i in range(11):
            extra.append(Finding(run_id=self.run_id, finding_key="perf.high_cpu",
                                 category="performance", severity="warning",
                                 title=f"High CPU {i}", detail="cpu", evidence={}))
        db.add_all(extra)
        db.commit()
        ids = [f.id for f in extra]
        db.close()
        r = self._post({"finding_ids": ids, "mode": "per_item"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("batched", r.json()["detail"].lower())

    def test_batched_allows_over_10(self):
        db = SessionLocal()
        extra = []
        for i in range(11):
            extra.append(Finding(run_id=self.run_id, finding_key="perf.high_cpu",
                                 category="performance", severity="warning",
                                 title=f"High CPU {i}", detail="cpu", evidence={}))
        db.add_all(extra)
        db.commit()
        ids = [f.id for f in extra]
        db.close()
        r = self._post({"finding_ids": ids, "mode": "batched"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["count"], 1)

    def test_invalid_mode_rejected(self):
        r = self._post({"finding_ids": self.fixable_ids, "mode": "sideways"})
        self.assertEqual(r.status_code, 400)

    def test_empty_selection_rejected(self):
        r = self._post({"finding_ids": [], "mode": "per_item"})
        self.assertEqual(r.status_code, 400)

    def test_run_not_found(self):
        client, token = self._client()
        r = client.post("/api/v1/netopt/runs/999999/optimize",
                        json={"finding_ids": self.fixable_ids, "mode": "per_item"},
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 404)


# ═══════════════════════ VLAN awareness + NO-FLAT guardrail ════════════════

VLAN_FIXTURE = [
    {"name": "Default", "vlan": None, "subnet": "192.168.1.1/24",
     "purpose": "corporate", "enabled": True, "dhcp": True,
     "dhcp_start": "", "dhcp_stop": ""},
    {"name": "WiFi", "vlan": 5, "subnet": "192.168.5.1/24",
     "purpose": "corporate", "enabled": True, "dhcp": True,
     "dhcp_start": "", "dhcp_stop": ""},
    {"name": "Production", "vlan": 4, "subnet": "192.168.4.1/24",
     "purpose": "corporate", "enabled": True, "dhcp": True,
     "dhcp_start": "", "dhcp_stop": ""},
    {"name": "Management", "vlan": 8, "subnet": "192.168.8.1/24",
     "purpose": "corporate", "enabled": True, "dhcp": True,
     "dhcp_start": "", "dhcp_stop": ""},
    {"name": "Kids", "vlan": 9, "subnet": "192.168.9.1/24",
     "purpose": "corporate", "enabled": True, "dhcp": True,
     "dhcp_start": "", "dhcp_stop": ""},
    {"name": "RCTF", "vlan": 10, "subnet": "192.168.10.1/24",
     "purpose": "corporate", "enabled": True, "dhcp": True,
     "dhcp_start": "", "dhcp_stop": ""},
]


def _unprofiled_port(**kw):
    p = {"port_idx": 1, "name": "Port 1", "up": True, "speed_mbps": 1000,
         "max_speed_mbps": 1000, "native_vlan": None, "tagged_vlans": [],
         "link_down_count": 0, "tx_errors": 0, "rx_errors": 0,
         "is_uplink": False}
    p.update(kw)
    return p


def _dev_with_port(device_type, name, port):
    return dev_snap(name=name, device_type=device_type,
                    unifi={"version": "x", "upgradable": False, "uplink_mac": "",
                           "fixed_ip": None, "ports": [port]})


class VlanAwarenessTest(unittest.TestCase):
    def test_network_map_builds_from_fixture(self):
        m = rules.build_vlan_map(VLAN_FIXTURE)
        self.assertIn("5", m)
        self.assertEqual(m["5"]["name"], "WiFi")
        self.assertEqual(m["5"]["subnet"], "192.168.5.1/24")
        self.assertEqual(m["5"]["purpose"], "corporate")
        self.assertTrue(m["5"]["enabled"])
        # the untagged default network keys under DEFAULT_NETWORK_KEY, vlan=None
        self.assertIn(rules.DEFAULT_NETWORK_KEY, m)
        self.assertIsNone(m[rules.DEFAULT_NETWORK_KEY]["vlan"])
        self.assertEqual(m[rules.DEFAULT_NETWORK_KEY]["name"], "Default")

    def test_vlan_story_native_default_plus_tagged(self):
        m = rules.build_vlan_map(VLAN_FIXTURE)
        story = rules.vlan_story(
            {"native_network": "Default", "native_vlan": None,
             "tagged_vlans": [9, 10]}, m)
        self.assertEqual(story, "native Default (.1.1/24), tagged Kids(9)/RCTF(10)")

    def test_vlan_story_unassigned(self):
        m = rules.build_vlan_map(VLAN_FIXTURE)
        self.assertEqual(rules.vlan_story({"native_vlan": None, "tagged_vlans": []}, m),
                         "native (unassigned)")

    def test_suggested_network_by_device_class(self):
        m = rules.build_vlan_map(VLAN_FIXTURE)
        self.assertEqual(rules.suggested_network("ap", m)["name"], "WiFi")
        self.assertEqual(rules.suggested_network("switch", m)["name"], "Management")
        self.assertEqual(rules.suggested_network("server", m)["name"], "Production")
        # the catch-all default is never a recommendation
        self.assertIsNone(rules.suggested_network("ap",
                                                  rules.build_vlan_map([VLAN_FIXTURE[0]])))

    def test_port_no_profile_names_wifi_for_ap(self):
        d = _dev_with_port("ap", "U6 Mesh", _unprofiled_port())
        f = by_key(rules.evaluate(snap([d], networks=VLAN_FIXTURE)),
                   "hyg.port_no_profile")[0]
        self.assertEqual(f["evidence"]["suggested_network"]["name"], "WiFi")
        self.assertIn("WiFi vlan5", f["evidence"]["suggested_action"])
        self.assertIn("access point", f["evidence"]["suggested_action"])

    def test_port_no_profile_names_management_for_switch(self):
        d = _dev_with_port("switch", "HouseSwitch", _unprofiled_port())
        f = by_key(rules.evaluate(snap([d], networks=VLAN_FIXTURE)),
                   "hyg.port_no_profile")[0]
        self.assertEqual(f["evidence"]["suggested_network"]["name"], "Management")
        self.assertIn("Management vlan8", f["evidence"]["suggested_action"])

    def test_uplink_port_suggests_trunk_not_single_network(self):
        d = _dev_with_port("switch", "HouseSwitch",
                           _unprofiled_port(is_uplink=True, port_idx=8))
        f = by_key(rules.evaluate(snap([d], networks=VLAN_FIXTURE)),
                   "hyg.port_no_profile")[0]
        self.assertIn("trunk", f["evidence"]["suggested_action"].lower())

    def test_suggested_action_for_appends_risk_and_respects_guardrail(self):
        action = rules.suggested_action_for(
            "hyg.port_no_profile",
            {"suggested_action": "Assign WiFi vlan5 (.5.1/24) as the native network "
                                "for U6 Mesh Port 1."})
        # high-risk port change -> blast radius + plan-first note appended
        self.assertIn("PLAN FIRST", action)
        # an adversarial flattening candidate is suppressed, never recommended
        self.assertEqual(rules.suggested_action_for(
            "hyg.port_no_profile",
            {"suggested_action": "just put everything on one network"}), "")

    def test_no_flat_guardrail_suppresses_flattening_candidates(self):
        adversarial = "just put everything on one network and remove all VLAN tags"
        self.assertTrue(rules.is_flattening(adversarial))
        cleaned = rules.apply_no_flat_guardrail([
            {"finding_key": "hyg.port_no_profile", "category": "hygiene",
             "severity": "info", "device_id": None, "interface": "1",
             "title": "t", "detail": "d",
             "evidence": {"suggested_action": adversarial}}])
        self.assertEqual(cleaned[0]["evidence"]["suggested_action"], "")
        self.assertIn("design change", cleaned[0]["evidence"]["guardrail_flag"].lower())

    def test_normal_port_fixes_still_fire(self):
        d = _dev_with_port("ap", "U6 Mesh", _unprofiled_port())
        fs = rules.evaluate(snap([d], networks=VLAN_FIXTURE))
        f = by_key(fs, "hyg.port_no_profile")[0]
        self.assertTrue(f["evidence"].get("suggested_action"))
        self.assertIn("WiFi", f["evidence"]["suggested_action"])

    def test_anti_flatten_phrasing_not_flagged(self):
        # our own 'never collapse the VLANs' wording is the ANTI-flatten
        # statement — it must not trip the guardrail
        self.assertFalse(rules.is_flattening(
            "Assign the VLAN trunk to the port — never collapse the VLANs "
            "onto a single network."))

    def test_build_vlan_context_story(self):
        # the handoff example: port 7 on the Google AP shows native Default +
        # its tagged context
        d = _dev_with_port("ap", "Google AP", _unprofiled_port(
            port_idx=7, name="Google WAN", native_vlan=None,
            native_network="Default", tagged_vlans=[9, 10],
            tagged_networks=["Kids", "RCTF"]))
        ctx = network_opt.build_vlan_context(snap([d], networks=VLAN_FIXTURE))
        self.assertEqual(len(ctx), 1)
        port = ctx[0]["ports"][0]
        self.assertEqual(port["port_idx"], 7)
        self.assertEqual(port["story"],
                         "native Default (.1.1/24), tagged Kids(9)/RCTF(10)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
