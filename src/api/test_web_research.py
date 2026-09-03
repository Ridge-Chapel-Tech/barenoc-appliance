#!/usr/bin/env python3
"""L3 research (feature F2) API-side tests: compliance control, the
pi-agent secret-file mirror, and the per-ticket opt-in schema.

Run from src/api:
    python3 -m unittest test_web_research -v
"""

import json
import os
import tempfile
import unittest
import datetime
from unittest.mock import patch

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import compliance  # noqa: E402


class ComplianceControlTest(unittest.TestCase):
    def test_web_research_defaults_off(self):
        c = compliance.get_controls({})
        self.assertIn("web_research", c)
        self.assertEqual(c["web_research"]["state"], "off")
        self.assertEqual(c["web_research"]["baseline"], "off")
        self.assertEqual(c["web_research"]["kind"], "bool")

    def test_mirror_effective_env(self):
        env = {}
        compliance.set_control("web_research", "on", env=env, persist=False)
        self.assertEqual(env["WEB_RESEARCH_ENABLED"], "true")
        c = compliance.get_controls(env)
        self.assertIsNotNone(c["web_research"]["enabled_since"])

    def test_preset_keeps_web_research_off(self):
        # Research egress is opt-in even under the compliance baseline: the
        # baseline must never silently open an outbound web channel.
        env = {}
        compliance.apply_preset(env=env, persist=False)
        self.assertEqual(compliance.get_controls(env)["web_research"]["state"],
                         "off")


class SecretFileMirrorTest(unittest.TestCase):
    def test_write_web_research_secret(self):
        from routes import settings as s
        tmp = tempfile.mkdtemp(prefix="webres-")
        secret = os.path.join(tmp, "web_research.json")
        with patch.object(s, "_read_env_file",
                          return_value={"WEB_RESEARCH_ENABLED": "true"}), \
             patch.object(s, "WEB_RESEARCH_SECRET_FILE", secret):
            s._write_web_research_secret()
        with open(secret) as f:
            self.assertEqual(json.load(f), {"enabled": True})

    def test_write_web_research_secret_defaults_off(self):
        from routes import settings as s
        tmp = tempfile.mkdtemp(prefix="webres-")
        secret = os.path.join(tmp, "web_research.json")
        with patch.object(s, "_read_env_file", return_value={}), \
             patch.object(s, "WEB_RESEARCH_SECRET_FILE", secret):
            s._write_web_research_secret()
        with open(secret) as f:
            self.assertEqual(json.load(f), {"enabled": False})


class TicketSchemaTest(unittest.TestCase):
    def test_create_and_update_carry_web_research(self):
        from schemas import TicketCreate, TicketUpdate, TicketResponse
        c = TicketCreate(title="latest UniFi firmware?", web_research=True)
        self.assertTrue(c.web_research)
        self.assertFalse(TicketCreate(title="x").web_research)
        u = TicketUpdate(web_research=False)
        self.assertFalse(u.web_research)
        self.assertIsNone(TicketUpdate().web_research)

        # response model tolerates the field (present on the DB model)
        r = TicketResponse.model_validate({
            "id": 1, "ticket_id": "TKT-20260902-0001", "title": "t",
            "description": None, "priority": "P3", "status": "open",
            "source": "manual", "web_research": True,
            "created_at": datetime.datetime.utcnow(),
            "updated_at": datetime.datetime.utcnow(),
        })
        self.assertTrue(r.web_research)


if __name__ == "__main__":
    unittest.main(verbosity=2)
