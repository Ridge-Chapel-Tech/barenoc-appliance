#!/usr/bin/env python3
"""Tests for the support bundle (redaction + structure).

Runs inside the barenoc-api container (needs SQLAlchemy/FastAPI). Uses a
scratch sqlite DB; docker-log fetch is mocked so no socket is needed.

    docker compose exec api python3 -m unittest test_support -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="support-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db  # noqa: E402
from routes import support  # noqa: E402


class RedactTest(unittest.TestCase):
    def test_sk_api_key(self):
        line = "provider key=sk-abc123def456ghi789 loaded"
        self.assertNotIn("sk-abc123def456ghi789", support.redact(line))
        self.assertIn("sk-***", support.redact(line))

    def test_bearer_token(self):
        self.assertNotIn("abcdefghij123456", support.redact("Authorization: Bearer abcdefghij123456"))

    def test_password_assignment(self):
        self.assertNotIn("hunter2", support.redact("UNIFI_PASSWORD=hunter2"))

    def test_generic_env_secret_key(self):
        out = support.redact("MY_APP_SECRET=s3cr3tvalue")
        self.assertNotIn("s3cr3tvalue", out)

    def test_private_key_block(self):
        blob = "-----BEGIN PRIVATE KEY-----\nAAAAsecret\n-----END PRIVATE KEY-----"
        out = support.redact(blob)
        self.assertNotIn("AAAAsecret", out)
        self.assertIn("***private-key***", out)

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.signature123456"
        self.assertNotIn("signature123456", support.redact(jwt))

    def test_authorization_header(self):
        out = support.redact("authorization: Bearer TOKENISHERE")
        self.assertNotIn("TOKENISHERE", out)


class FrameParseTest(unittest.TestCase):
    def test_multiplexed_frames(self):
        payload = b"hello\nworld\n"
        frame = b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
        self.assertEqual(support._parse_log_frames(frame), "hello\nworld\n")

    def test_plain_text_fallback(self):
        self.assertEqual(support._parse_log_frames(b"plain text"), "plain text")


class BundleTest(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()

    def test_bundle_redacts_logs_and_has_structure(self):
        fake_logs = (
            "2026-08-16T12:00:00Z INFO everything fine\n"
            "2026-08-16T12:00:01Z key sk-abc123def456ghi789 leaked\n"
            "2026-08-16T12:00:02Z ERROR boom\n"
        )
        with patch.object(support, "_docker_logs", return_value=fake_logs), \
             patch.object(support, "_config_presence",
                          return_value=[{"key": "TZ", "value": "America/New_York"}]):
            resp = support.export_bundle(
                support.BundleRequest(bug_description="wifi drops at night"),
                self.db, SimpleNamespace(username="admin"))
        text = resp.body.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("# BareNOC support bundle", text)
        self.assertIn("wifi drops at night", text)
        self.assertIn("## 2. App config", text)
        self.assertIn("ERROR boom", text)
        # the planted secret must be scrubbed everywhere
        self.assertNotIn("sk-abc123def456ghi789", text)
        self.assertIn("sk-***", text)

    def test_bundle_empty_description_ok(self):
        with patch.object(support, "_docker_logs", return_value=""), \
             patch.object(support, "_config_presence", return_value=[]):
            resp = support.export_bundle(support.BundleRequest(), self.db,
                                         SimpleNamespace(username="admin"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("(none provided)", resp.body.decode())


if __name__ == "__main__":
    unittest.main()
