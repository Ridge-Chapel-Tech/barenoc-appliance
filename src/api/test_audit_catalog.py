#!/usr/bin/env python3
"""Audit catalog tests (2026-08-26): completeness + framework mapping +
secret-field / required-field guards.

Run from src/api:
    python3 -m unittest test_audit_catalog -v
"""

import glob
import os
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="audit-catalog-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from audit_catalog import (  # noqa: E402
    EVENT_CATALOG,
    catalog_summary,
    has_secret_fields,
    payload_bytes,
    required_fields,
    validate_event,
)

# backup_* event types are emitted dynamically by routes.system.record_backup_event
# ("backup_" + result) rather than a literal in a log_event call.
DYNAMIC_EVENTS = ("backup_start", "backup_success", "backup_failure", "backup_restore")


class CatalogCompletenessTest(unittest.TestCase):
    """Every framework-mapped event type has a real log_event call site."""

    def _root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _call_site_event_types(self) -> set:
        types = set()
        for path in glob.glob(os.path.join(self._root(), "**", "*.py"), recursive=True):
            base = os.path.basename(path)
            if base.startswith("test_"):
                continue
            with open(path) as f:
                content = f.read()
            # log_event(db, "type", ...) and _le(db, "type", ...) — 2nd arg.
            for m in re.finditer(
                    r"\b(?:log_event|_le)\(\s*[^,\n]+,\s*([\"'])([^\"']+)\1",
                    content):
                types.add(m.group(2))
            # _audit_event("type", actor, data) — 1st arg (scheduler).
            for m in re.finditer(
                    r"\b_audit_event\(\s*([\"'])([^\"']+)\1", content):
                types.add(m.group(2))
        return types

    def test_every_catalog_event_has_a_call_site(self):
        sites = self._call_site_event_types()
        system_path = os.path.join(self._root(), "api", "routes", "system.py")
        with open(system_path) as f:
            system_src = f.read()
        backup_generator_ok = ('f"backup_{result}"' in system_src
                               or '"backup_" + result' in system_src)
        missing = []
        for key in EVENT_CATALOG:
            if key in sites:
                continue
            if key in DYNAMIC_EVENTS and backup_generator_ok:
                continue
            missing.append(key)
        self.assertEqual(missing, [],
                         f"catalog events without a call site: {missing}")

    def test_catalog_summary_shape(self):
        s = catalog_summary()
        self.assertEqual(s["event_types_active"], len(EVENT_CATALOG))
        self.assertTrue(s["hash_chained"])
        self.assertIn("retention", s["summary"].lower())
        self.assertGreater(s["frameworks"]["soc2"], 0)
        self.assertGreater(s["frameworks"]["pci"], 0)
        self.assertGreater(s["frameworks"]["hipaa"], 0)

    def test_new_events_are_framework_mapped(self):
        for key in ("credential_access", "export_download", "backup_success",
                    "scheduler_health", "update_schedule_change"):
            self.assertIn(key, EVENT_CATALOG)
            self.assertTrue(EVENT_CATALOG[key]["frameworks"],
                            f"{key} must map to at least one framework")


class SecretFieldGuardTest(unittest.TestCase):
    def test_flags_secret_keys(self):
        found = has_secret_fields({"password": "x", "ssh_key": "y",
                                   "api_key": "z"})
        self.assertIn("password", found)
        self.assertIn("ssh_key", found)
        self.assertIn("api_key", found)

    def test_fingerprint_and_hash_are_not_secret(self):
        self.assertEqual(has_secret_fields({"ssh_key_fingerprint": "fp"}), [])
        self.assertEqual(has_secret_fields({"sha256_hash": "abc"}), [])

    def test_nested_secret_detection(self):
        found = has_secret_fields({"outer": {"token": "x"}})
        self.assertIn("outer.token", found)

    def test_validate_missing_required_fields(self):
        problems = validate_event("credential_access", {"device_id": 1})
        self.assertIn("missing required field 'credential_type'", problems)
        self.assertIn("missing required field 'action'", problems)

    def test_validate_secret_field(self):
        problems = validate_event("export_download", {"kind": "audit_log", "secret": "x"})
        self.assertTrue(any("secret" in p for p in problems))

    def test_payload_bytes_small(self):
        self.assertLess(payload_bytes({"a": 1, "b": [1, 2, 3]}), 300)


class AttestationCatalogTest(unittest.TestCase):
    """The attestation export surfaces the catalog summary (item 6)."""

    def test_attestation_includes_catalog(self):
        import compliance
        snap = compliance.attestation(env={})
        self.assertIn("audit_catalog", snap)
        cat = snap["audit_catalog"]
        self.assertEqual(cat["event_types_active"], len(EVENT_CATALOG))
        self.assertTrue(cat["hash_chained"])
        self.assertIn("audit event types active, hash-chained", cat["summary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
