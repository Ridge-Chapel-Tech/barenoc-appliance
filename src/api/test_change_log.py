#!/usr/bin/env python3
"""L2 change-log tests (2026-08-31): the append-only operational history.

Covers: event capture on each hook, customer-vs-agent filtering, the download
artifact rendering, and the "a change-log failure never breaks the main
operation" guard.

Run from src/api:
    python3 -m unittest test_change_log -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="change-log-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from database import SessionLocal, init_db  # noqa: E402
from models import (ChangeLogEntry, Device, Ticket, ScanRun, Finding)  # noqa: E402
import change_log  # noqa: E402
from change_log import record, query_entries  # noqa: E402

init_db()


def _db():
    return SessionLocal()


def _wipe(db):
    for model in (ChangeLogEntry, Finding, Ticket, ScanRun, Device):
        db.query(model).delete()
    db.commit()


def _add_device(db, name="sw-1", **kw):
    d = Device(name=name, ip_address=kw.pop("ip_address", "10.0.0.9"),
               device_type=kw.pop("device_type", "switch"), status="online",
               claimed=True, device_group="default", **kw)
    db.add(d)
    db.commit()
    return d


class RecordAndViewsTest(unittest.TestCase):
    """The event record + the two views + the download artifact."""

    def setUp(self):
        db = _db()
        _wipe(db)
        db.close()

    def test_record_appends_entry(self):
        db = _db()
        record(db, event_type="ticket_closed", actor="admin", asset="TKT-1",
               summary="Closed TKT-1: wifi down",
               detail="resolution text", links={"ticket_id": "TKT-1"},
               customer_visible=True)
        total, rows = query_entries(db, view="agent")
        self.assertEqual(total, 1)
        e = rows[0]
        self.assertEqual(e.event_type, "ticket_closed")
        self.assertEqual(e.actor, "admin")
        self.assertEqual(e.asset, "TKT-1")
        self.assertEqual(e.summary, "Closed TKT-1: wifi down")
        self.assertEqual(e.detail, "resolution text")
        self.assertEqual(e.links, {"ticket_id": "TKT-1"})
        self.assertTrue(e.customer_visible)
        db.close()

    def test_customer_view_filters_hidden_events(self):
        db = _db()
        record(db, event_type="device_adopted", actor="admin", asset="AP-1",
               summary="Adopted AP-1", customer_visible=True)
        record(db, event_type="settings_changed", actor="admin", asset="llm",
               summary="Settings changed: llm", customer_visible=False)
        total, rows = query_entries(db, view="customer")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].event_type, "device_adopted")
        # Agent view sees both.
        total_a, _ = query_entries(db, view="agent")
        self.assertEqual(total_a, 2)
        db.close()

    def test_entry_dict_agent_vs_customer(self):
        db = _db()
        record(db, event_type="firmware_updated", actor="system", asset="UCG",
               summary="Firmware updated on UCG", detail="1.0 → 2.0",
               links={"mac": "aa:bb"}, customer_visible=True)
        _, rows = query_entries(db, view="agent")
        agent = change_log.entry_dict(rows[0], view="agent")
        self.assertIn("detail", agent)
        self.assertIn("links", agent)
        customer = change_log.entry_dict(rows[0], view="customer")
        self.assertNotIn("detail", customer)
        self.assertNotIn("links", customer)
        db.close()

    def test_render_markdown_artifact(self):
        db = _db()
        record(db, event_type="finding_resolved", actor="admin", asset="perf.x",
               summary="Finding perf.x resolved (TKT-9)",
               links={"finding_key": "perf.x", "ticket_id": "TKT-9"},
               customer_visible=True)
        _, rows = query_entries(db, view="agent")
        md = change_log.render_markdown(rows, view="agent")
        self.assertIn("# BareNOC Change History", md)
        self.assertIn("Finding perf.x resolved", md)
        self.assertIn("perf.x", md)
        db.close()

    def test_render_json_artifact(self):
        db = _db()
        record(db, event_type="provisioned", actor="system", asset="laptop",
               summary="Provisioned endpoint laptop", customer_visible=True)
        _, rows = query_entries(db, view="customer")
        payload = change_log.render_json(rows, view="customer")
        self.assertIn('"events"', payload)
        self.assertIn("Provisioned endpoint laptop", payload)
        db.close()

    def test_record_never_raises_on_failure(self):
        """A change-log failure must NEVER break the main operation."""
        class BadDB:
            def add(self, *a, **k):
                raise RuntimeError("boom")
            def commit(self):
                pass
            def rollback(self):
                pass
        result = record(BadDB(), event_type="ticket_closed", actor="x",
                        summary="s")
        self.assertIsNone(result)

    def test_capture_finding_resolutions(self):
        db = _db()
        run = ScanRun(status="completed")
        db.add(run)
        db.commit()
        f = Finding(run_id=run.id, finding_key="perf.duplex_half",
                    category="performance", severity="warning", title="Half duplex",
                    fix_ticket_id="TKT-FIX1")
        db.add(f)
        db.commit()
        change_log.capture_finding_resolutions(db, "TKT-FIX1", "admin")
        total, rows = query_entries(db, view="agent")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].event_type, "finding_resolved")
        self.assertEqual(rows[0].links["finding_key"], "perf.duplex_half")
        self.assertEqual(rows[0].links["ticket_id"], "TKT-FIX1")
        db.close()


class HookCaptureTest(unittest.TestCase):
    """Each existing write point fires the change-log capture (best-effort)."""

    def setUp(self):
        db = _db()
        _wipe(db)
        db.close()

    def _ctx(self, role="admin", username="admin"):
        return {"user": SimpleNamespace(role=role, username=username),
                "groups": [], "auth_method": "password"}

    def test_adopt_and_revoke_hooks(self):
        from routes import devices
        db = _db()
        d = _add_device(db, name="ap-1")
        ctx = self._ctx()
        with patch("step_ca.device_cn", return_value="device-ap-1"), \
             patch("step_ca.mint_token", return_value="tok"), \
             patch("step_ca.root_fingerprint", return_value="fp"):
            devices.adopt_with_cert(d.id, body={"ttl": 600}, db=db, ctx=ctx)
        devices.revoke_adoption(d.id, db=db, ctx=ctx)
        total, rows = query_entries(db, view="agent")
        types = {r.event_type for r in rows}
        self.assertIn("device_adopted", types)
        self.assertIn("device_revoked", types)
        db.close()

    def test_device_config_changed_hook(self):
        from routes import devices
        from schemas import DeviceUpdate
        db = _db()
        d = _add_device(db, name="nas-01")
        devices.update_device(d.id, DeviceUpdate(name="NAS Renamed"),
                              db=db, ctx=self._ctx())
        total, rows = query_entries(db, view="agent")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].event_type, "device_config_changed")
        self.assertIn("name", rows[0].detail)
        db.close()

    def test_ticket_close_hook_resolves_findings(self):
        from routes import tickets
        from schemas import TicketUpdate
        db = _db()
        run = ScanRun(status="completed")
        db.add(run)
        db.commit()
        db.add(Finding(run_id=run.id, finding_key="perf.duplex_half",
                       category="performance", severity="warning",
                       title="Half duplex", fix_ticket_id="TKT-FIX1"))
        db.add(Ticket(ticket_id="TKT-FIX1", title="Fix half duplex",
                      priority="P3", status="open", source="optimize"))
        db.commit()
        tickets.update_ticket("TKT-FIX1", TicketUpdate(status="closed",
                                                       resolution="done"),
                              db=db, ctx=self._ctx())
        total, rows = query_entries(db, view="agent")
        types = {r.event_type for r in rows}
        self.assertIn("ticket_closed", types)
        self.assertIn("finding_resolved", types)
        closed = [r for r in rows if r.event_type == "ticket_closed"][0]
        self.assertEqual(closed.asset, "TKT-FIX1")
        db.close()

    def test_settings_changed_hook(self):
        from routes import settings as s
        db = _db()
        env = {}
        with patch.object(s, "_read_env_file", return_value=env), \
             patch.object(s, "_write_env_file", side_effect=lambda e: env.update(e)):
            r = s.update_section("general", {"customer_name": "Acme"},
                                 db=db,
                                 user=SimpleNamespace(role="admin", username="tester"))
        self.assertEqual(r["updated"], 1)
        total, rows = query_entries(db, view="agent")
        self.assertGreaterEqual(total, 1)
        self.assertIn("settings_changed", {e.event_type for e in rows})
        self.assertEqual(rows[0].asset, "general")
        db.close()


class ViewRouteTest(unittest.TestCase):
    """The /api/v1/environment/changes view + download endpoints."""

    def setUp(self):
        db = _db()
        _wipe(db)
        db.close()

    def _ctx(self, role="admin", username="admin"):
        return {"user": SimpleNamespace(role=role, username=username),
                "groups": [], "auth_method": "password"}

    def test_list_changes_customer_vs_agent(self):
        from routes import change_log as route
        db = _db()
        record(db, event_type="device_adopted", actor="admin", asset="ap-1",
               summary="Adopted ap-1", customer_visible=True)
        record(db, event_type="settings_changed", actor="admin", asset="llm",
               summary="Settings changed", customer_visible=False)
        cust = route.list_changes(view="customer", db=db, ctx=self._ctx(),
                                   limit=50, offset=0)
        self.assertEqual(cust["view"], "customer")
        self.assertEqual(cust["total"], 1)
        agent = route.list_changes(view="agent", db=db, ctx=self._ctx(),
                                   limit=50, offset=0)
        self.assertEqual(agent["total"], 2)
        db.close()

    def test_download_artifact(self):
        from routes import change_log as route
        db = _db()
        record(db, event_type="ticket_closed", actor="admin", asset="TKT-1",
               summary="Closed TKT-1", customer_visible=True)
        resp = route.download_changes(view="customer", format="markdown",
                                      db=db, ctx=self._ctx())
        self.assertIn("text/markdown", resp.media_type)
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("# BareNOC Change History", resp.body.decode())
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
