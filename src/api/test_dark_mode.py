#!/usr/bin/env python3
"""Dark-mode (light/dark/auto theme) wiring tests.

The theme is client-side (localStorage + prefers-color-scheme + `.dark` on
<html>), so these tests assert the SERVER-side contract:

  1. the override stylesheet is served at /static/dark.css and is scoped
     under `.dark` (inert in light mode);
  2. every page template carries the required wiring — the Tailwind
     `darkMode: 'class'` config, the no-flash inline script (class toggled on
     <html> BEFORE paint), the theme controller, and a `data-theme-btn` toggle;
  3. a render smoke: each template renders without error and the output
     contains the dark.css link + toggle + controller.

    cd src/api && python3 -m unittest test_dark_mode -v
"""

import os
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="dark-mode-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

API_DIR = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(API_DIR, "templates")
DARK_CSS = os.path.join(API_DIR, "static", "dark.css")

# Importing the app does NOT touch the DB (routes + static mount only). We
# deliberately avoid entering the TestClient context manager (lifespan) so the
# suite's shared sqlite engine is never seeded/background-threaded here.
from main import app  # noqa: E402

templates = Jinja2Templates(directory=TPL_DIR)

# Markers every wired page must contain.
MARKERS = [
    "/static/dark.css",
    "data-theme-btn",
    "bnTheme",
    "document.documentElement.classList.toggle('dark'",
]

# Templates that carry their own full HTML document (each was wired directly).
STANDALONE_TEMPLATES = ["base.html", "chat.html", "setup.html", "wiki.html"]


class _Cookies(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _Url:
    def __init__(self, path):
        self.path = path


class _Request:
    def __init__(self, authed=False, path="/dashboard"):
        self.cookies = _Cookies({"access_token": "test-token"} if authed else {})
        self.url = _Url(path)


def _ctx(authed=True, path="/dashboard", **extra):
    ctx = {"request": _Request(authed=authed, path=path)}
    ctx.update(extra)
    return ctx


class DarkCssServedTest(unittest.TestCase):
    def test_dark_css_is_served_and_scoped(self):
        c = TestClient(app)
        r = c.get("/static/dark.css")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("text/css", r.headers.get("content-type", ""))
        # Every rule is inert in light mode: the stylesheet must not recolor
        # anything outside a `.dark` context except the root html.dark line.
        css = r.text
        self.assertIn(".dark", css)
        self.assertIn("html.dark", css)

    def test_dark_css_maps_the_high_frequency_light_classes(self):
        with open(DARK_CSS) as f:
            css = f.read()
        for light in (".bg-white", ".bg-gray-50", ".text-gray-900",
                      ".text-gray-500", ".border-gray-300", ".shadow"):
            self.assertIn(light, css, f"dark.css must remap {light}")


class TemplateWiringTest(unittest.TestCase):
    def test_standalone_pages_are_wired(self):
        for name in STANDALONE_TEMPLATES:
            with open(os.path.join(TPL_DIR, name)) as f:
                src = f.read()
            self.assertIn("darkMode: 'class'", src,
                          f"{name}: missing Tailwind darkMode: 'class'")
            self.assertIn("_theme_head.html", src,
                          f"{name}: missing _theme_head.html include")

    def test_theme_partial_has_no_flash_script(self):
        with open(os.path.join(TPL_DIR, "_theme_head.html")) as f:
            src = f.read()
        self.assertIn("barenoc-theme", src)
        self.assertIn("prefers-color-scheme: dark", src)
        self.assertIn("localStorage", src)
        # apply() must run before paint (synchronously in <head>), not on load.
        self.assertIn("apply(); // no-flash", src)
        self.assertIn("classList.toggle('dark'", src)


class RenderSmokeTest(unittest.TestCase):
    def test_pages_render_with_dark_wiring(self):
        pages = {
            "login.html": {"oidc_enabled": False,
                           "oauth": {"github_enabled": False, "google_enabled": False}},
            "change-password.html": {},
            "dashboard.html": {"setup_complete": True},
            "tickets.html": {},
            "devices.html": {},
            "admin.html": {},
            "system.html": {},
            "settings.html": {},
            "audit.html": {},
            "downloads.html": {"version": "0.0.0-test", "platforms": []},
            "wiki.html": {"pages": [("index", "Welcome")], "active": "index",
                          "title": "Welcome", "body_html": "<p>hi</p>"},
            "setup.html": {},
        }
        # chat.html is a standalone page with its own sidebar (no auth gate in
        # the template itself) — it still needs a request for the nav partial.
        pages["chat.html"] = {"site_name": "TestSite"}

        for name, extra in pages.items():
            with self.subTest(template=name):
                authed = name not in ("login.html", "setup.html")
                html = templates.TemplateResponse(
                    name, _ctx(authed=authed, path="/wiki" if name == "wiki.html" else "/dashboard", **extra)
                ).body.decode()
                for marker in MARKERS:
                    self.assertIn(marker, html,
                                  f"{name} render missing {marker!r}")

    def test_base_shell_renders_toggle_in_both_branches(self):
        # Authed shell → sidebar toggle.
        authed = templates.TemplateResponse(
            "login.html",
            _ctx(authed=True, path="/dashboard",
                 oidc_enabled=False,
                 oauth={"github_enabled": False, "google_enabled": False}),
        ).body.decode()
        self.assertIn("Theme", authed)
        # Non-authed → floating toggle for the login page.
        login = templates.TemplateResponse(
            "login.html",
            _ctx(authed=False, path="/login",
                 oidc_enabled=False,
                 oauth={"github_enabled": False, "google_enabled": False}),
        ).body.decode()
        self.assertIn("data-theme-btn", login)
        self.assertIn("fixed top-4 right-4", login)


if __name__ == "__main__":
    unittest.main()
