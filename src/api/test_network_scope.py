#!/usr/bin/env python3
"""Tests for the tunnel/CGNAT network-scope guard.

Pins the 2026-08-19 Starlink case: a remote box discovered a Starlink WAN
address over a tailnet link (CGNAT 100.64.0.0/10). That address (and
everything in 100.64.0.0/10) must never be scanned, discovered, claimed, or
adopted. Real customer LAN ranges (10/8, 172.16/12, 192.168/16) stay valid.

    python3 -m unittest test_network_scope -v
"""

import unittest

import network_scope


class TunnelCgnatGuardTest(unittest.TestCase):
    def test_starlink_case_pinned(self):
        self.assertTrue(network_scope.is_tunnel_or_cgnat("100.99.0.5"))

    def test_cgnat_bounds(self):
        self.assertTrue(network_scope.is_tunnel_or_cgnat("100.64.0.0"))
        self.assertTrue(network_scope.is_tunnel_or_cgnat("100.127.255.255"))
        self.assertTrue(network_scope.is_tunnel_or_cgnat("100.100.100.100"))

    def test_private_lan_stays_valid(self):
        for ip in ("192.0.2.207", "10.0.0.2", "172.16.0.1", "192.0.2.10"):
            self.assertFalse(network_scope.is_tunnel_or_cgnat(ip), ip)

    def test_ipv6_not_excluded(self):
        # IPv6 ULA / link-local are not CGNAT; the guard only pins IPv4
        # 100.64.0.0/10 today.
        self.assertFalse(network_scope.is_tunnel_or_cgnat("fd00::1"))

    def test_unparseable_is_excluded(self):
        self.assertTrue(network_scope.is_tunnel_or_cgnat(""))
        self.assertTrue(network_scope.is_tunnel_or_cgnat("not-an-ip"))
        self.assertTrue(network_scope.is_tunnel_or_cgnat(None))

    def test_zone_index_stripped(self):
        self.assertFalse(network_scope.is_tunnel_or_cgnat("192.0.2.1%eth0"))

    def test_subnet_overlap(self):
        self.assertTrue(network_scope.subnet_overlaps_tunnel("100.64.0.0/24"))
        self.assertTrue(network_scope.subnet_overlaps_tunnel("100.99.0.0/16"))
        self.assertFalse(network_scope.subnet_overlaps_tunnel("192.0.2.0/24"))

    def test_filter_valid_hosts(self):
        self.assertEqual(
            network_scope.filter_valid_hosts(["192.0.2.1", "100.99.0.5",
                                               "10.0.0.5"]),
            ["192.0.2.1", "10.0.0.5"],
        )

    def test_excluded_reason(self):
        self.assertEqual(
            network_scope.excluded_reason("100.99.0.5"),
            "cgnat/tunnel (100.64.0.0/10)",
        )
        self.assertIsNone(network_scope.excluded_reason("192.0.2.1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
