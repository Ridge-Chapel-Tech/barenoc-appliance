#!/usr/bin/env python3
"""Front-door render tests — the login pages must NEVER 500.

2026-09-01 incident: v2026.08.31.a shipped with the
`from oidc import oidc_config, oauth_login_config` import dropped from
main.py (the #132 knowledge-layer keep-both merge removed it). Result:
`GET /` and `GET /login` raised `NameError: name 'oidc_config' is not
defined` → HTTP 500 on every box on that version whenever a logged-out
session hit the front door. Health endpoints stayed 200 so the deploy
looked green; no existing test touched the page routes.

These tests pin the invariant: anonymous requests to `/` and `/login`
render (200) or redirect (302) — never 500. They import `main` so any
future import-merge regression fails CI instead of shipping.

    cd src/api && python3 -m unittest test_front_door -v
"""

import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="front-door-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["SETUP_COMPLETE"] = "true"  # don't route anonymous / to /setup

from fastapi.testclient import TestClient  # noqa: E402
from database import init_db  # noqa: E402
from models import User  # noqa: E402  (register table metadata before init)

init_db()

import main  # noqa: E402

_PAGE_ROUTES = ("/", "/login")


class FrontDoorRenderTest(unittest.TestCase):
    """Anonymous front-door requests render or redirect — never a 500."""

    def setUp(self):
        self.c = TestClient(main.app)

    def test_oidc_names_resolve_in_main(self):
        """The login template render calls oidc_config()/oauth_login_config()
        from main's namespace — the exact regression that shipped in .31.a."""
        self.assertTrue(callable(main.oidc_config))
        self.assertTrue(callable(main.oauth_login_config))

    def test_root_and_login_never_500(self):
        for route in _PAGE_ROUTES:
            r = self.c.get(route)
            self.assertIn(
                r.status_code, (200, 302),
                f"{route} must render (200) or redirect (302), got {r.status_code}",
            )
            self.assertNotEqual(r.status_code, 500)

    def test_login_page_body_mentions_signin(self):
        r = self.c.get("/login")
        if r.status_code == 200:
            self.assertIn("login", r.text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
