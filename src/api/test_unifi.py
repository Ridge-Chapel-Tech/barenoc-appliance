#!/usr/bin/env python3
"""Stdlib-only unit tests for UniFiClient session reuse + backoff (B4).

The UniFi controller locks accounts after too many rapid logins. These tests
pin the fix: password-authenticated clients share one controller session per
credential set (so repeated API calls reuse a single login instead of
re-authenticating every call), and 429/connection retries are spaced with
exponential backoff.

    cd src/api && python3 -m unittest test_unifi -v
"""

import io
import time
import unittest
import urllib.error
from http.cookiejar import Cookie
from unittest.mock import Mock, patch

import unifi
from unifi import UniFiClient


class _FakeResp:
    def __init__(self, payload=b"{}", headers=None):
        self._payload = payload
        self.headers = headers if headers is not None else {}

    def read(self):
        return self._payload


def _http_error(code, reason, body=b""):
    return urllib.error.HTTPError("https://192.0.2.1/api", code, reason,
                                  {}, io.BytesIO(body))


def _seed_session(client):
    """Mark the client's session as freshly logged-in with a TOKEN cookie."""
    client._session.logged_in_at = time.time()
    ck = Cookie(0, "TOKEN", "tok", None, False, "192.0.2.1", False, False,
                "/", True, False, None, False, None, None, {}, False)
    client._session.jar.set_cookie(ck)


class SessionReuseTest(unittest.TestCase):
    def setUp(self):
        # The module-level cache is shared across tests; clear it so a seeded
        # cookie from one test can't leak into the next.
        unifi._SESSION_CACHE.clear()

    def test_same_creds_share_session(self):
        a = UniFiClient("https://192.0.2.1", "admin", "secret")
        b = UniFiClient("https://192.0.2.1/", "admin", "secret")
        self.assertIs(a._session, b._session)

    def test_different_creds_get_new_session(self):
        a = UniFiClient("https://192.0.2.1", "admin", "secret")
        b = UniFiClient("https://192.0.2.1", "admin", "other")
        c = UniFiClient("https://192.0.2.2", "admin", "secret")
        self.assertIsNot(a._session, b._session)
        self.assertIsNot(a._session, c._session)

    def test_api_key_is_stateless_per_instance(self):
        a = UniFiClient("https://192.0.2.1", "admin", "", api_key="key")
        b = UniFiClient("https://192.0.2.1", "admin", "", api_key="key")
        self.assertIsNot(a._session, b._session)

    def test_login_reuses_active_session_without_posting(self):
        client = UniFiClient("https://192.0.2.1", "admin", "secret")
        _seed_session(client)
        with patch.object(unifi.UniFiClient, "_request") as req:
            self.assertTrue(client.login())
        req.assert_not_called()

    def test_login_retries_with_backoff(self):
        client = UniFiClient("https://192.0.2.1", "admin", "secret")
        calls = []

        def fake_request(method, path, data=None, headers=None, **kwargs):
            calls.append(path)
            # Two attempts fail (each tries /api/auth/login then /api/login),
            # then the third attempt's /api/auth/login succeeds.
            if len(calls) >= 5:
                return {"unique_id": "user"}
            return None

        with patch.object(unifi.UniFiClient, "_request",
                          side_effect=fake_request), \
             patch.object(unifi.time, "sleep") as sleep:
            self.assertTrue(client.login())

        self.assertEqual(len(calls), 5)
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(delays, [0.5, 1.0])


class BackoffTest(unittest.TestCase):
    def setUp(self):
        unifi._SESSION_CACHE.clear()

    def test_request_retries_429_with_backoff(self):
        client = UniFiClient("https://192.0.2.1", "admin", "secret")
        opener = Mock()
        opener.open.side_effect = [
            _http_error(429, "Too Many Requests", b'{"error":"throttle"}'),
            _FakeResp(b'{"meta":{"rc":"ok"}}'),
        ]
        with patch.object(unifi.urllib.request, "build_opener",
                          return_value=opener), \
             patch.object(unifi.time, "sleep") as sleep:
            result = client._request("GET", "/api/s/default/stat/device")

        self.assertEqual(result, {"meta": {"rc": "ok"}})
        self.assertEqual(opener.open.call_count, 2)
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(delays, [0.5])

    def test_request_relogins_once_on_401(self):
        client = UniFiClient("https://192.0.2.1", "admin", "secret")
        _seed_session(client)
        opener = Mock()
        opener.open.side_effect = [
            _http_error(401, "Unauthorized", b"session expired"),
            _FakeResp(b'{"meta":{"rc":"ok"}}'),
        ]
        login_calls = []

        def fake_login():
            login_calls.append(1)
            return True

        with patch.object(unifi.urllib.request, "build_opener",
                          return_value=opener), \
             patch.object(client, "login", side_effect=fake_login):
            result = client._request("GET", "/api/s/default/stat/device")

        self.assertEqual(result, {"meta": {"rc": "ok"}})
        self.assertEqual(login_calls, [1])   # exactly one re-login, then retry


if __name__ == "__main__":
    unittest.main(verbosity=2)
