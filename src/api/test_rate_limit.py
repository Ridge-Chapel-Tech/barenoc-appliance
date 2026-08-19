#!/usr/bin/env python3
"""Rate limiting tests (M2-T9).

    docker compose exec api python3 -m unittest test_rate_limit -v
"""
import os
import time
import unittest

import ratelimit  # noqa: E402
from ratelimit import RateLimiter, client_ip  # noqa: E402


def _reset_cfg():
    ratelimit._cfg_cache["at"] = 0.0
    ratelimit._cfg_cache["rules"] = []


class ParseRateTest(unittest.TestCase):
    def test_minute(self):
        self.assertEqual(ratelimit._parse_rate("10/minute"), (10, 60))

    def test_second(self):
        self.assertEqual(ratelimit._parse_rate("5/second"), (5, 1))
        self.assertEqual(ratelimit._parse_rate("5/s"), (5, 1))

    def test_hour(self):
        self.assertEqual(ratelimit._parse_rate("100/hour"), (100, 3600))

    def test_bad(self):
        self.assertIsNone(ratelimit._parse_rate("banana"))
        self.assertIsNone(ratelimit._parse_rate("10/fortnight"))
        self.assertIsNone(ratelimit._parse_rate("0/minute"))


class ClientIpTest(unittest.TestCase):
    def test_last_hop_wins(self):
        scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8"),
                             (b"host", b"x")], "client": ("9.9.9.9", 123)}
        self.assertEqual(client_ip(scope), "5.6.7.8")

    def test_no_xff_falls_back_to_peer(self):
        scope = {"headers": [], "client": ("9.9.9.9", 123)}
        self.assertEqual(client_ip(scope), "9.9.9.9")


def _set_test_limits():
    """Scope the small test limits to this module's own tests. Setting them at
    import time poisoned the WHOLE suite (the limiter is enabled + capped at
    10/min for every other test file — 08-19, 429s in test_network_opt)."""
    os.environ["RATE_LIMIT_ENABLED"] = "true"
    os.environ["RATE_LIMIT_LOGIN"] = "3/minute"
    os.environ["RATE_LIMIT_CHAT"] = "5/minute"
    os.environ["RATE_LIMIT_API"] = "10/minute"
    _reset_cfg()


def _clear_test_limits():
    for k in ("RATE_LIMIT_ENABLED", "RATE_LIMIT_LOGIN", "RATE_LIMIT_CHAT", "RATE_LIMIT_API"):
        os.environ.pop(k, None)
    _reset_cfg()

class RateLimiterTest(unittest.TestCase):
    def setUp(self):
        _set_test_limits()
        self.limiter = RateLimiter()
        self.path = "/api/v1/auth/login"
        self.ip = "10.0.0.1"

    def tearDown(self):
        _clear_test_limits()

    def test_allows_up_to_limit(self):
        results = [self.limiter.check(self.path, self.ip)[0] for _ in range(3)]
        self.assertEqual(results, [True, True, True])

    def test_exceeds_limit(self):
        for _ in range(3):
            self.limiter.check(self.path, self.ip)
        allowed, retry = self.limiter.check(self.path, self.ip)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)

    def test_window_resets(self):
        for _ in range(3):
            self.limiter.check(self.path, self.ip)
        # force a fresh window: move bucket window_start into the past
        key = (self.path, self.ip)
        b = self.limiter._buckets[key]
        b.window_start -= 61
        allowed, _ = self.limiter.check(self.path, self.ip)
        self.assertTrue(allowed)

    def test_ips_independent(self):
        for _ in range(3):
            self.limiter.check(self.path, "10.0.0.1")
        allowed, _ = self.limiter.check(self.path, "10.0.0.2")
        self.assertTrue(allowed)

    def test_non_api_paths_unlimited(self):
        allowed, _ = self.limiter.check("/wiki", self.ip)
        self.assertTrue(allowed)

    def test_disabled(self):
        os.environ["RATE_LIMIT_ENABLED"] = "false"
        try:
            _reset_cfg()
            for _ in range(20):
                allowed, _ = self.limiter.check(self.path, self.ip)
                self.assertTrue(allowed)
        finally:
            os.environ["RATE_LIMIT_ENABLED"] = "true"


class MiddlewareTest(unittest.TestCase):
    """End-to-end 429 via a minimal app wrapped in the middleware."""

    def setUp(self):
        _set_test_limits()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ratelimit import RateLimitMiddleware
        app = FastAPI()

        @app.get("/api/v1/auth/login")
        def login():
            return {"ok": True}

        @app.get("/api/v1/chat")
        def chat():
            return {"ok": True}

        @app.get("/wiki")
        def wiki():
            return {"ok": True}

        app.add_middleware(RateLimitMiddleware)
        self.client = TestClient(app)

    def test_429_after_limit(self):
        for _ in range(3):
            r = self.client.get("/api/v1/auth/login")
            self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/v1/auth/login")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)

    def test_different_xff_independent(self):
        for i in range(3):
            r = self.client.get("/api/v1/auth/login",
                                headers={"X-Forwarded-For": f"1.1.1.{i}"})
            self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/v1/auth/login",
                            headers={"X-Forwarded-For": "9.9.9.9"})
        self.assertEqual(r.status_code, 200)

    def test_non_api_not_limited(self):
        for _ in range(5):
            r = self.client.get("/wiki")
            self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
