#!/usr/bin/env python3
"""Tests for the emailer transports — vendor-managed notify, SMTP/Gmail, and
the configured-semantics gate (smtp_configured).

emailer.py is stdlib-only, so this suite runs anywhere (dev box included):

    python3 -m unittest test_emailer -v
"""

import json
import unittest
from unittest.mock import patch

import emailer


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def close(self):
        pass


def _vendor_send(cfg: dict, to, subject, text, html="", env=None):
    """Call _send_via_vendor with the notify config + urlopen mocked. Returns
    (ok, err, captured) where captured holds the outgoing request."""
    env = env or {}
    captured = {}

    def fake_open(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["payload"] = json.loads(req.data.decode())
        captured["method"] = req.get_method()
        return _FakeResp(json.dumps({"ok": True, "id": "resend-123"}).encode())

    with patch.object(emailer, "load_notify_config", return_value=cfg), \
         patch.object(emailer.urllib.request, "urlopen", side_effect=fake_open):
        ok, err = emailer._send_via_vendor(env, to, subject, text, html)
    return ok, err, captured


class VendorTransportTest(unittest.TestCase):
    def test_payload_shape_and_auth(self):
        env = {"CUSTOMER_NAME": "My Home Network", "EMAIL_REPLY_TO": "me@example.com"}
        ok, err, cap = _vendor_send(
            {"url": "https://notify.example/fn", "token": "shared-token"},
            ["a@x.com", "b@x.com"], "Subj", "plain body", "<p>html</p>", env)
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertEqual(cap["method"], "POST")
        self.assertEqual(cap["url"], "https://notify.example/fn")
        self.assertEqual(cap["headers"]["Authorization"], "Bearer shared-token")
        self.assertEqual(cap["headers"]["Content-type"], "application/json")
        p = cap["payload"]
        self.assertEqual(p["to"], ["a@x.com", "b@x.com"])
        self.assertEqual(p["subject"], "Subj")
        self.assertEqual(p["text"], "plain body")
        self.assertEqual(p["html"], "<p>html</p>")
        self.assertEqual(p["from_name"], "My Home Network")
        self.assertEqual(p["reply_to"], "me@example.com")
        self.assertIn("nonce", p)

    def test_no_token_fails_cleanly(self):
        ok, err, _ = _vendor_send({"url": "https://notify.example/fn", "token": ""}, ["a@x.com"], "s", "t")
        self.assertFalse(ok)
        self.assertIn("token not configured", err)

    def test_no_html_and_no_reply_to_omitted(self):
        env = {"CUSTOMER_NAME": "Acme"}
        ok, err, cap = _vendor_send(
            {"url": "https://notify.example/fn", "token": "t"}, ["a@x.com"], "s", "t", "", env)
        self.assertTrue(ok)
        p = cap["payload"]
        self.assertNotIn("html", p)
        self.assertNotIn("reply_to", p)
        self.assertEqual(p["from_name"], "Acme")

    def test_http_error_surfaces_note(self):
        from urllib.error import HTTPError
        import urllib.request as ur

        def boom(req, timeout=None, context=None):
            raise HTTPError(req.full_url, 502, "bad", None,
                            _FakeResp(json.dumps({"note": "resend down"}).encode()))
        with patch.object(emailer, "load_notify_config",
                          return_value={"url": "https://notify.example/fn", "token": "t"}), \
             patch.object(ur, "urlopen", side_effect=boom):
            ok, err = emailer._send_via_vendor({}, ["a@x.com"], "s", "t", "")
        self.assertFalse(ok)
        self.assertIn("resend down", err)


class NotifyConfigTest(unittest.TestCase):
    def test_load_from_secret_file_with_url_fallback(self):
        with patch.object(emailer, "_read_notify_secret",
                          return_value={"url": "", "token": "tok"}), \
             patch.object(emailer, "_read_email_env",
                          return_value={"NOTIFY_URL": "https://override/fn"}):
            self.assertEqual(emailer.load_notify_config(),
                             {"url": "https://override/fn", "token": "tok"})

    def test_load_from_secret_file_with_default_url(self):
        with patch.object(emailer, "_read_notify_secret",
                          return_value={"url": "", "token": "tok"}), \
             patch.object(emailer, "_read_email_env", return_value={}):
            cfg = emailer.load_notify_config()
            self.assertEqual(cfg["token"], "tok")
            self.assertIn("/functions/v1/notify", cfg["url"])

    def test_vendor_configured_requires_token(self):
        with patch.object(emailer, "load_notify_config",
                          return_value={"url": "u", "token": ""}):
            self.assertFalse(emailer.vendor_configured())
        with patch.object(emailer, "load_notify_config",
                          return_value={"url": "u", "token": "t"}):
            self.assertTrue(emailer.vendor_configured())


class ConfiguredSemanticsTest(unittest.TestCase):
    def test_default_transport_is_vendor(self):
        with patch.object(emailer, "_read_email_env", return_value={}):
            self.assertEqual(emailer.transport_mode(), "vendor")

    def test_explicit_transport_choices(self):
        with patch.object(emailer, "_read_email_env",
                          return_value={"EMAIL_TRANSPORT": "smtp"}):
            self.assertEqual(emailer.transport_mode(), "smtp")
        with patch.object(emailer, "_read_email_env",
                          return_value={"EMAIL_TRANSPORT": "vendor"}):
            self.assertEqual(emailer.transport_mode(), "vendor")

    def test_smtp_overrides_vendor_when_unset(self):
        env = {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}
        with patch.object(emailer, "_read_email_env", return_value=env):
            self.assertEqual(emailer.transport_mode(), "smtp")

    def test_vendor_managed_counts_as_configured(self):
        with patch.object(emailer, "_read_email_env", return_value={}), \
             patch.object(emailer, "load_notify_config",
                          return_value={"url": "u", "token": "t"}):
            self.assertTrue(emailer.smtp_configured())

    def test_explicit_smtp_without_creds_is_unconfigured(self):
        with patch.object(emailer, "_read_email_env",
                          return_value={"EMAIL_TRANSPORT": "smtp"}):
            self.assertFalse(emailer.smtp_configured())

    def test_smtp_creds_configured(self):
        env = {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}
        with patch.object(emailer, "_read_email_env", return_value=env):
            self.assertTrue(emailer.smtp_configured())


class NonceTest(unittest.TestCase):
    def test_nonce_is_stable_for_identical_input(self):
        a = emailer._vendor_nonce(["a@x.com"], "s", "t")
        b = emailer._vendor_nonce(["a@x.com"], "s", "t")
        self.assertEqual(a, b)
        self.assertIn("-", a)

    def test_nonce_differs_for_different_input(self):
        a = emailer._vendor_nonce(["a@x.com"], "s", "t")
        b = emailer._vendor_nonce(["a@x.com"], "s", "different body")
        self.assertNotEqual(a, b)

    def test_from_name_fallback(self):
        self.assertEqual(emailer._vendor_from_name({}), "BareNOC")
        self.assertEqual(emailer._vendor_from_name({"CUSTOMER_NAME": "My Site"}), "My Site")
        self.assertEqual(emailer._vendor_from_name({"SITE_ID": "site-1"}), "site-1")


if __name__ == "__main__":
    unittest.main()
