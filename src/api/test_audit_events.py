#!/usr/bin/env python3
"""New audit events (2026-08-26): credential access, exports, backups.

Run from src/api:
    python3 -m unittest test_audit_events -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="audit-events-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from database import SessionLocal, init_db  # noqa: E402
from models import AuditLog, Device  # noqa: E402

init_db()


def _events(db, event_type=None):
    q = db.query(AuditLog)
    if event_type:
        q = q.filter(AuditLog.event_type == event_type)
    return q.all()


class CredentialAccessEventTest(unittest.TestCase):
    """The highest-value compliance event: a stored secret was decrypted/fetched."""

    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.query(Device).delete()
        db.commit()
        db.close()

    def _add_device(self):
        db = SessionLocal()
        d = Device(name="sw-1", ip_address="10.0.0.9", device_type="switch",
                   status="online", claimed=True, device_group="default",
                   ssh_user="root", ssh_key_fingerprint="fp", snmp_community="pub")
        db.add(d)
        db.commit()
        did = d.id
        db.close()
        return did

    def test_credentials_decrypt_logs_credential_access(self):
        from routes.devices import get_device_credentials
        did = self._add_device()
        ctx = {"user": SimpleNamespace(role="admin", username="admin"), "groups": []}
        db = SessionLocal()
        try:
            get_device_credentials(did, db=db, ctx=ctx)
        finally:
            db.close()
        db = SessionLocal()
        try:
            rows = _events(db, "credential_access")
            self.assertGreaterEqual(len(rows), 1)
            kinds = {r.data.get("credential_type") for r in rows}
            self.assertIn("snmp", kinds)
            # NEVER the secret itself
            for r in rows:
                raw = str(r.data)
                self.assertNotIn("pub", raw)   # the snmp_community value
                self.assertNotIn("hunter", raw)
                self.assertNotIn("PRIVATE KEY", raw)
        finally:
            db.close()

    def test_control_key_fetch_logs_credential_access(self):
        from routes.devices import device_control_key
        db = SessionLocal()
        ctx = {"user": SimpleNamespace(role="admin", username="admin"), "groups": []}
        with patch("control_key.ensure_control_key",
                   return_value={"public_key": "ssh-ed25519 AAAAtest", "private_key": "PRIVATE-KEY-MATERIAL"}):
            device_control_key(db=db, ctx=ctx)
        rows = _events(db, "credential_access")
        db.close()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[-1].data.get("credential_type"), "ssh")
        self.assertEqual(rows[-1].data.get("action"), "fetch_control_key")
        self.assertNotIn("PRIVATE-KEY", str(rows[-1].data))


class ExportEventTest(unittest.TestCase):
    """Export events: who pulled what (bundle / audit log / attestation)."""

    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def test_audit_log_export_logs_event(self):
        from routes import audit_log
        db = SessionLocal()
        audit_log.export(db=db, user=SimpleNamespace(role="admin", username="admin"))
        rows = _events(db, "export_download")
        db.close()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[-1].data.get("kind"), "audit_log")

    def test_support_bundle_export_logs_event(self):
        from routes import support
        db = SessionLocal()
        with patch.object(support, "build_bundle", return_value="# test bundle"):
            support.export_bundle(body=support.BundleRequest(), db=db,
                                  user=SimpleNamespace(role="admin", username="admin"))
        rows = _events(db, "export_download")
        db.close()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[-1].data.get("kind"), "support_bundle")

    def test_attestation_export_logs_event(self):
        from routes import compliance as compliance_routes
        import compliance
        db = SessionLocal()
        with patch.object(compliance, "attestation", return_value={"ok": True}):
            compliance_routes.export_attestation(
                db=db, user=SimpleNamespace(role="admin", username="admin"))
        rows = _events(db, "export_download")
        db.close()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[-1].data.get("kind"), "attestation")


class BackupEventTest(unittest.TestCase):
    """Backup lifecycle events + the status.json transition detector."""

    def setUp(self):
        db = SessionLocal()
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def test_record_backup_event_fires(self):
        from routes.system import record_backup_event
        db = SessionLocal()
        record_backup_event(db, "vm", "success", detail="snap-1")
        record_backup_event(db, "usb", "failure")
        record_backup_event(db, "app", "restore")
        rows = _events(db)
        db.close()
        types = {r.event_type for r in rows}
        self.assertIn("backup_success", types)
        self.assertIn("backup_failure", types)
        self.assertIn("backup_restore", types)
        success = [r for r in rows if r.event_type == "backup_success"][0]
        self.assertEqual(success.data.get("type"), "vm")
        self.assertEqual(success.data.get("result"), "success")

    def test_ingest_backup_status_detects_transition_once(self):
        from routes import system
        seen = os.path.join(tempfile.mkdtemp(prefix="backup-seen-"), "seen.json")
        db = SessionLocal()
        status = {"vm_snapshot_last": "vzdump-100-2026-08-26", "usb_last_backup": "none"}
        try:
            with patch.object(system, "BACKUP_SEEN_JSON", seen):
                self.assertEqual(system.ingest_backup_status(db, status=status)["ingested"], 1)
                # idempotent: the same status must not re-fire
                self.assertEqual(system.ingest_backup_status(db, status=status)["ingested"], 0)
        finally:
            db.close()
        db = SessionLocal()
        try:
            rows = _events(db, "backup_success")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].data.get("type"), "vm")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
