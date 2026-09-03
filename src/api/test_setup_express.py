#!/usr/bin/env python3
"""Express-wizard tests (home-ux, 2026-08-25).

Covers the acceptance criteria:
  1. Route binding (the 08-16 lesson) — the wizard's endpoints are registered.
  2. The 5-step express wizard + the "Advanced setup" expander (full 9-step
     path still present in the template).
  3. Every skipped step writes a correct home default.
  4. The express LLM card maps to the SAME compliance control the Security
     panel uses (COMPLIANCE_LLM_EGRESS + effective LLM_EGRESS).
  5. The updates step (F1) reuses /updates/status + /updates/schedule and
     the completion sweep never flips an explicit opt-out back on.

    cd src/api && python3 -m unittest test_setup_express -v
"""

import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="setup-express-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from fastapi import HTTPException  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402
from models import User  # noqa: E402
from auth import hash_password  # noqa: E402
from routes import setup as setup_routes  # noqa: E402
from routes import updates as updates_routes  # noqa: E402
import main as api_main  # noqa: E402


class RouteBindingTest(unittest.TestCase):
    """The 08-16 lesson: a route/template change that isn't registered ships
    dead. The express wizard's endpoints must be bound on the app."""

    @classmethod
    def setUpClass(cls):
        cls.paths = {getattr(r, "path", None) for r in api_main.app.routes}

    def test_setup_routes_bound(self):
        for p in ("/api/v1/setup/status", "/api/v1/setup/account",
                  "/api/v1/setup/complete", "/setup"):
            self.assertIn(p, self.paths)

    def test_wizard_dependencies_bound(self):
        # the express wizard calls these for the network + share steps.
        # Settings sections are served via the /api/v1/settings/{section}
        # pattern (GET + PUT) with "llm" special-cased; assert the pattern
        # is bound and every section the wizard needs resolves to a section
        # definition (or the llm special case) — the 08-16 dead-route lesson.
        self.assertIn("/api/v1/settings/{section}", self.paths)
        from routes.settings import SECTIONS
        for section in ("general", "policy", "backups", "email", "llm"):
            if section == "llm":
                continue  # special-cased inside the {section} handler
            self.assertIn(section, SECTIONS)
        for p in ("/api/v1/unifi/config", "/api/v1/unifi/test",
                  "/api/v1/compliance"):
            self.assertIn(p, self.paths)


class TemplateExpanderTest(unittest.TestCase):
    """The express wizard defaults to 5 steps; 'Advanced setup' restores the
    full 9-step path."""

    def _html(self):
        path = os.path.join(os.path.dirname(setup_routes.__file__),
                            "..", "templates", "setup.html")
        with open(path) as f:
            return f.read()

    def test_express_and_advanced_steps_present(self):
        html = self._html()
        for token in ("EXPRESS_STEPS", "ADVANCED_STEPS", "toggleAdvanced",
                      "Advanced setup"):
            self.assertIn(token, html)

    def test_full_9_step_path_intact(self):
        html = self._html()
        for key in ("account", "llm", "timezone", "site_name", "email",
                    "autonomy", "backups", "devices", "share"):
            self.assertIn(f"key: '{key}'", html)

    def test_updates_step_present_in_express(self):
        html = self._html()
        self.assertIn("{ key: 'updates'", html)
        self.assertIn("'Updates'", html)

    def test_updates_step_reuses_updates_endpoints(self):
        html = self._html()
        for token in ("/api/v1/updates/status", "/api/v1/updates/check",
                      "/api/v1/updates/now", "/api/v1/updates/schedule"):
            self.assertIn(token, html)

    def test_updates_step_defaults_on_sunday_3am(self):
        html = self._html()
        self.assertIn("updChoice = { auto: true, day: '0', hour: 3 }", html)

    def test_done_review_mentions_updates(self):
        html = self._html()
        self.assertIn("🔄 Updates:", html)


class HomeDefaultsTest(unittest.TestCase):
    """Every skipped express step writes a correct home default."""

    def test_apply_home_defaults_writes_the_home_set(self):
        env = {}
        out = setup_routes.apply_home_defaults(
            env, timezone="America/New_York", site_name="The Smith House")
        self.assertEqual(out["SETUP_COMPLETE"], "true")
        self.assertEqual(out["LLM_POLICY_PROFILE"], "autonomous")
        self.assertEqual(out["PI_AGENT_ENABLED"], "true")
        self.assertEqual(out["UNIFI_AUTOSYNC_ENABLED"], "true")
        self.assertEqual(out["UNIFI_AUTO_ADOPT"], "true")
        self.assertEqual(out["TZ"], "America/New_York")
        self.assertEqual(out["CUSTOMER_NAME"], "The Smith House")
        # email stays OFF until a recipient is added
        self.assertNotIn("ALERT_EMAIL", out)

    def test_apply_home_defaults_does_not_overwrite_existing_choices(self):
        env = {"LLM_POLICY_PROFILE": "balanced", "PI_AGENT_ENABLED": "false"}
        out = setup_routes.apply_home_defaults(env)
        self.assertEqual(out["LLM_POLICY_PROFILE"], "balanced")
        self.assertEqual(out["PI_AGENT_ENABLED"], "false")

    def test_apply_home_defaults_rejects_bad_timezone(self):
        with self.assertRaises(HTTPException):
            setup_routes.apply_home_defaults({}, timezone="Not/AZone")

    def test_skipped_llm_defaults_to_cloud_egress(self):
        from llm_providers import egress_mode
        out = setup_routes.apply_home_defaults({})
        self.assertEqual(egress_mode(out), "cloud")


class EgressMappingTest(unittest.TestCase):
    """The express LLM card maps to the SAME compliance control the Security
    panel uses (COMPLIANCE_LLM_EGRESS + effective LLM_EGRESS)."""

    def test_local_choice_maps_to_compliance_control(self):
        env = {}
        out = setup_routes.apply_home_defaults(env, llm_egress="local")
        self.assertEqual(out["COMPLIANCE_LLM_EGRESS"], "local")
        self.assertEqual(out["LLM_EGRESS"], "local")

    def test_cloud_choice_maps_to_compliance_control(self):
        env = {}
        out = setup_routes.apply_home_defaults(env, llm_egress="cloud")
        self.assertEqual(out["COMPLIANCE_LLM_EGRESS"], "cloud")
        self.assertEqual(out["LLM_EGRESS"], "cloud")


class CompleteEndpointTest(unittest.TestCase):
    """/setup/complete writes the defaults + the backup conf for the host
    poller (backups on, local defaults)."""

    def _complete(self, env, payload=None):
        init_db()
        db = SessionLocal()
        admin = User(username="admin-express-" + uuid.uuid4().hex[:8],
                     hashed_password=hash_password("x" * 8),
                     role="admin", is_active=True, must_change_password=False)
        db.add(admin)
        db.commit()
        captured_env = {}
        captured_conf = {}
        try:
            with patch.object(setup_routes, "_read_env_file", return_value=dict(env)), \
                 patch.object(setup_routes, "_write_env_file",
                              side_effect=lambda e: captured_env.update(e)), \
                 patch.object(setup_routes, "_write_backup_conf",
                              side_effect=lambda c: captured_conf.update(c)), \
                 patch.object(setup_routes, "_read_backup_conf", return_value={}):
                r = setup_routes.setup_complete(
                    data=setup_routes.SetupCompleteRequest(**(payload or {})),
                    db=db, user=SimpleNamespace(role="admin", username="admin"))
        finally:
            db.close()
        return r, captured_env, captured_conf

    def test_complete_writes_defaults_and_backup_conf(self):
        r, env, conf = self._complete({}, {"timezone": "America/New_York",
                                           "site_name": "Home",
                                           "llm_egress": "cloud"})
        self.assertIs(r["complete"], True)
        self.assertEqual(env["SETUP_COMPLETE"], "true")
        self.assertEqual(env["LLM_POLICY_PROFILE"], "autonomous")
        self.assertEqual(env["PI_AGENT_ENABLED"], "true")
        self.assertEqual(env["TZ"], "America/New_York")
        self.assertEqual(env["CUSTOMER_NAME"], "Home")
        self.assertEqual(env["COMPLIANCE_LLM_EGRESS"], "cloud")
        self.assertEqual(env["LLM_EGRESS"], "cloud")
        self.assertEqual(conf["USB_BACKUP_ENABLED"], "true")
        self.assertEqual(conf["USB_BACKUP_DAY"], "3")
        self.assertEqual(conf["USB_BACKUP_HOUR"], "2")

    def test_complete_writes_default_update_schedule(self):
        """Auto-update default-on (2026-08-25): a fresh install's completion
        path writes the default schedule (enabled, Sunday 03:00 local)."""
        tmp = tempfile.mkdtemp(prefix="setup-updates-")
        with patch.object(updates_routes, "STATUS_DIR", tmp):
            r, env, conf = self._complete({}, {"timezone": "America/New_York"})
        self.assertIs(r["complete"], True)
        self.assertTrue(os.path.exists(os.path.join(tmp, "update_schedule.conf")))
        with open(os.path.join(tmp, "update_schedule.conf")) as f:
            content = f.read()
        self.assertIn("enabled=true", content)
        self.assertIn("day=0", content)
        self.assertIn("hour=3", content)

    def test_complete_preserves_wizard_disabled_schedule(self):
        """F1: if the wizard turned auto-update OFF before /setup/complete,
        the completion sweep must not flip it back on (the conf file's
        existence is the opt-out marker)."""
        tmp = tempfile.mkdtemp(prefix="setup-updates-optout-")
        with patch.object(updates_routes, "STATUS_DIR", tmp):
            updates_routes._write_schedule({
                "mode": "recurring", "enabled": False,
                "day": "1", "hour": 2, "when": "", "fired": ""})
            r, env, conf = self._complete({}, {"timezone": "America/New_York"})
            sc = updates_routes._read_schedule()
            with open(os.path.join(tmp, "update_schedule.conf")) as f:
                content = f.read()
        self.assertIs(r["complete"], True)
        self.assertFalse(sc["enabled"])
        self.assertEqual(sc["day"], "1")
        self.assertIn("enabled=false", content)
        self.assertIn("day=1", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
