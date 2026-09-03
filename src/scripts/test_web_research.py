#!/usr/bin/env python3
"""Tests for the L3 read-only web-research tool (feature F2).

Run from src/scripts:
    python3 -m unittest test_web_research -v

Hermetic: no network. SSRF/egress/cache/extraction paths are exercised with
IP literals + mocked DNS so the suite never touches the public web.
"""

import contextlib
import io
import os
import socket
import unittest
from unittest.mock import patch

import web_research


class EgressGateTest(unittest.TestCase):
    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEB_RESEARCH_ALLOWED", None)
            self.assertFalse(web_research.egress_allowed())

    def test_on_only_when_explicitly_one(self):
        with patch.dict(os.environ, {"WEB_RESEARCH_ALLOWED": "1"}):
            self.assertTrue(web_research.egress_allowed())
        with patch.dict(os.environ, {"WEB_RESEARCH_ALLOWED": "0"}):
            self.assertFalse(web_research.egress_allowed())

    def test_main_refuses_without_egress(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEB_RESEARCH_ALLOWED", None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = web_research.main(["search", "unifi firmware"])
        self.assertEqual(rc, 3)
        self.assertIn("opt-in egress", buf.getvalue())


class SSRFGuardTest(unittest.TestCase):
    def test_scheme_must_be_http_or_https(self):
        ok, _ = web_research.url_is_safe("ftp://example.com/x")
        self.assertFalse(ok)
        ok, _ = web_research.url_is_safe("file:///etc/passwd")
        self.assertFalse(ok)
        ok, _ = web_research.url_is_safe("not a url")
        self.assertFalse(ok)

    def test_literal_loopback_blocked(self):
        self.assertFalse(web_research.url_is_safe("http://127.0.0.1/")[0])
        self.assertFalse(web_research.url_is_safe("http://[::1]/")[0])

    def test_literal_private_blocked(self):
        for host in ("10.0.0.1", "172.16.0.1", "192.168.1.1",
                     "169.254.169.254", "100.64.0.1", "0.0.0.0"):
            ok, why = web_research.url_is_safe(f"http://{host}/")
            self.assertFalse(ok, f"{host} must be blocked: {why}")

    def test_literal_public_allowed(self):
        self.assertTrue(web_research.url_is_safe("https://8.8.8.8/")[0])
        self.assertTrue(web_research.url_is_safe("https://1.1.1.1/")[0])

    def test_hostname_requires_public_resolution(self):
        with patch.object(socket, "getaddrinfo",
                          return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6,
                                         "", ("8.8.8.8", 443))]):
            ok, _ = web_research.url_is_safe("https://example.com/")
            self.assertTrue(ok)

    def test_hostname_resolving_private_is_blocked(self):
        with patch.object(socket, "getaddrinfo",
                          return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6,
                                         "", ("192.168.1.5", 443))]):
            ok, why = web_research.url_is_safe("https://example.com/")
            self.assertFalse(ok)
            self.assertIn("non-public", why)

    def test_hostname_that_does_not_resolve_is_blocked(self):
        with patch.object(socket, "getaddrinfo",
                          side_effect=socket.gaierror("nxdomain")):
            ok, why = web_research.url_is_safe("https://nx.example/")
            self.assertFalse(ok)
            self.assertIn("does not resolve", why)


class CacheTest(unittest.TestCase):
    def test_cache_key_stable_and_scoped(self):
        a = web_research.cache_key("search", "unifi firmware")
        b = web_research.cache_key("search", "unifi firmware")
        c = web_research.cache_key("fetch", "unifi firmware")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_cache_roundtrip_and_expiry(self):
        import json
        import tempfile
        import time
        with tempfile.TemporaryDirectory(prefix="wr-cache-") as d:
            with patch.object(web_research, "cache_dir", return_value=d):
                web_research.write_cache("search", "q", {"ok": True}, 3600)
                got = web_research.read_cache("search", "q")
                self.assertEqual(got, {"ok": True})
                # expire it
                path = web_research._cache_path("search", "q")
                with open(path) as f:
                    doc = json.load(f)
                doc["expires_at"] = time.time() - 1
                with open(path, "w") as f:
                    json.dump(doc, f)
                self.assertIsNone(web_research.read_cache("search", "q"))


class ExtractTest(unittest.TestCase):
    def test_strip_html_keeps_title_heading_link(self):
        html = ("<html><head><title>The Doc</title><script>x()</script></head>"
                "<body><h1>Intro</h1><p>Hello <a href='https://a.b/c'>world</a></p></body></html>")
        text = web_research._strip_html(html)
        self.assertIn("The Doc", text)
        self.assertIn("Intro", text)
        self.assertIn("world", text)
        self.assertIn("https://a.b/c", text)
        self.assertNotIn("x()", text)

    def test_fetch_refuses_unsafe_url(self):
        with patch.dict(os.environ, {"WEB_RESEARCH_ALLOWED": "1"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = web_research.main(["fetch", "http://127.0.0.1/"])
        self.assertEqual(rc, 4)
        self.assertIn("unsafe URL", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
