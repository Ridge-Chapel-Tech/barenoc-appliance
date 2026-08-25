#!/usr/bin/env python3
"""Attestation snapshot tests (2026-08-25): contents + settings-hash integrity.

Run from src/api:
    python3 -m unittest test_attestation -v
"""

import os
import unittest

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import compliance  # noqa: E402


class AttestationContentsTest(unittest.TestCase):
    def test_contains_every_control_with_state_and_since(self):
        snap = compliance.attestation({}, appliance_version="2026.08.25.a")
        for key in compliance.CONTROL_KEYS:
            self.assertIn(key, snap["controls"])
            self.assertIn("state", snap["controls"][key])
            self.assertIn("enabled_since", snap["controls"][key])
            self.assertIn("baseline", snap["controls"][key])
        self.assertEqual(snap["appliance_version"], "2026.08.25.a")
        self.assertEqual(snap["settings_hash_algorithm"], "sha256")
        self.assertEqual(snap["audit_log_export"], "/api/v1/audit-log/export")
        self.assertTrue(snap["non_negotiable"])
        self.assertIn("schema_version", snap)

    def test_enabled_since_present_after_change(self):
        env = {}
        compliance.set_control("retention", "strict", env=env, persist=False)
        snap = compliance.attestation(env)
        self.assertIsNotNone(snap["controls"]["retention"]["enabled_since"])
        self.assertIsNone(snap["controls"]["llm_egress"]["enabled_since"])


class HashIntegrityTest(unittest.TestCase):
    def test_hash_matches_settings_source(self):
        env = {}
        compliance.set_control("llm_egress", "local", env=env, persist=False)
        controls = compliance.get_controls(env)
        snap = compliance.attestation(env)
        self.assertEqual(snap["settings_hash"],
                         compliance.settings_hash(controls))

    def test_tamper_with_settings_changes_hash(self):
        env = {}
        h1 = compliance.settings_hash(compliance.get_controls(env))
        compliance.set_control("retention", "strict", env=env, persist=False)
        h2 = compliance.settings_hash(compliance.get_controls(env))
        self.assertNotEqual(h1, h2)

    def test_tamper_with_since_changes_hash(self):
        env = {}
        compliance.set_control("telemetry", "off", env=env, persist=False)
        h1 = compliance.settings_hash(compliance.get_controls(env))
        # a different provenance timestamp must also change the hash
        env["COMPLIANCE_TELEMETRY_SINCE"] = "2026-01-01T00:00:00Z"
        h2 = compliance.settings_hash(compliance.get_controls(env))
        self.assertNotEqual(h1, h2)

    def test_hash_is_deterministic(self):
        env = {}
        a = compliance.settings_hash(compliance.get_controls(env))
        b = compliance.settings_hash(compliance.get_controls(dict(env)))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
