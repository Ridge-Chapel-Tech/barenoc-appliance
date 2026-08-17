#!/usr/bin/env python3
"""Static + render tests for the chat front-door shell (feat/chat-integration).

Covers the two user reports fixed here:
  1. Desktop chat must render INSIDE the app shell (sidebar intact) while the
     mobile path stays the standalone full-screen page (auto-detected by width).
  2. New chat opens at Juniper (the Queue Manager bot) with her greeting/help,
     NOT Lily's technician greeting.

These are cheap markup/JS assertions against the rendered template + raw file,
so they run in the api suite / CI without a browser.

    cd src/api && python3 -m unittest test_chat_shell -v
"""

import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="chat-shell-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

import main as api_main  # noqa: E402


def _template_dir():
    return os.path.join(os.path.dirname(os.path.abspath(api_main.__file__)), "templates")


def _read(name):
    with open(os.path.join(_template_dir(), name), encoding="utf-8") as f:
        return f.read()


def _request(path="/chat", cookie=None):
    from starlette.requests import Request
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
        "http_version": "1.1",
    }
    return Request(scope)


def _render_chat():
    tpl = api_main.templates.env.get_template("chat.html")
    return tpl.render(request=_request("/chat"), site_name="BareNOC")


def _render_base():
    tpl = api_main.templates.env.get_template("base.html")
    return tpl.render(request=_request("/dashboard", cookie="access_token=test"))


class ChatShellStaticTest(unittest.TestCase):
    """Static checks on the raw chat.html source."""

    def test_desktop_shell_embed_path_exists(self):
        raw = _read("chat.html")
        # The desktop path reuses the shared sidebar nav partial.
        self.assertIn('id="chat-sidebar"', raw)
        self.assertIn('{% include "_sidebar_nav.html" %}', raw)
        # Content pane sits beside the fixed w-60 sidebar on desktop.
        self.assertIn("md:pl-60", raw)

    def test_mobile_standalone_path_preserved(self):
        raw = _read("chat.html")
        # Full-screen standalone app (the existing mobile UX) is still there.
        self.assertIn("100dvh", raw)
        self.assertIn('id="screen-auth"', raw)
        self.assertIn('id="screen-thread"', raw)

    def test_auto_detect_by_width_and_resize(self):
        raw = _read("chat.html")
        self.assertIn("matchMedia('(min-width: 768px)')", raw)
        self.assertIn("applyShellLayout", raw)
        self.assertIn("addEventListener('resize'", raw)

    def test_default_target_resolves_to_juniper_bot(self):
        raw = _read("chat.html")
        # The default chat target is the API-resolved Queue Manager name.
        self.assertIn("names.queue_manager", raw)
        self.assertIn("/api/v1/chat/messages", raw)

    def test_juniper_greeting_fronts_and_lily_greeting_absent(self):
        raw = _read("chat.html")
        self.assertIn("I'm Juniper", raw)
        # Lily's hard-coded technician greeting (worker/main.py) must NOT be the
        # first thing a chat visitor sees.
        self.assertNotIn("I'm Lily", raw)

    def test_ticket_thread_and_dashboard_button_preserved(self):
        raw = _read("chat.html")
        # #16 ticket-status + #17 Dashboard button still wired.
        self.assertIn("🔄 Status", raw)
        self.assertIn("requestStatus", raw)
        self.assertIn('id="btn-dash"', raw)
        self.assertIn("/dashboard", raw)

    def test_front_desk_and_tickets_section_labels(self):
        # The home list must read as ONE history with two labeled sections —
        # the front-desk DM and each ticket thread — so a reply never lands in
        # the wrong conversation (the 08-17 "multiple sessions" report).
        raw = _read("chat.html")
        self.assertIn("Front desk — Juniper", raw)
        self.assertIn("Your tickets", raw)
        # The Juniper DM is a single list entry, not a merged chat session.
        self.assertIn("openJuniper", raw)
        self.assertIn('id="screen-juniper"', raw)

    def test_juniper_replies_link_ticket_ids_to_threads(self):
        raw = _read("chat.html")
        # Juniper's intake/summary/close replies render TKT-… ids as links that
        # jump to the ticket thread (linkifyTickets + the TKT pattern).
        self.assertIn("linkifyTickets", raw)
        self.assertIn("showThread", raw)
        self.assertIn("TKT-\\d{8}-\\d{4}", raw)

    def test_juniper_greeting_mentions_close(self):
        raw = _read("chat.html")
        self.assertIn("close TKT-…", raw)
        self.assertIn("close the ticket", raw)

    def test_scroll_sticks_only_when_near_bottom(self):
        # The 08-13 fix, restored for BOTH the Juniper DM and the ticket
        # thread (desktop + mobile share the same poll functions): only stick
        # to the bottom when already near it (<80px), otherwise preserve the
        # reading position so the 4s poll doesn't yank the scroll back down.
        raw = _read("chat.html")
        self.assertEqual(raw.count("const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;"), 2)
        self.assertEqual(raw.count("const fromBottom = box.scrollHeight - box.scrollTop;"), 2)
        self.assertEqual(raw.count("if (stick) box.scrollTop = box.scrollHeight;"), 2)
        self.assertEqual(raw.count("else if (fromBottom > 0) box.scrollTop = Math.max(0, box.scrollHeight - fromBottom);"), 2)


class ChatShellRenderTest(unittest.TestCase):
    """Render chat.html + base.html to prove the shared partial works end-to-end."""

    def test_chat_renders_sidebar_nav_links(self):
        html = _render_chat()
        for link in ("Dashboard", "Tickets", "Devices", "Settings", "Wiki", "Downloads"):
            self.assertIn(link, html)
        self.assertIn("Juniper", html)
        # Chat nav item is highlighted when on /chat.
        self.assertIn("bg-barenoc-700 text-white", html)

    def test_base_renders_sidebar_nav_links(self):
        html = _render_base()
        for link in ("Dashboard", "Tickets", "Chat", "Devices", "System", "Settings"):
            self.assertIn(link, html)

    def test_chat_render_has_no_lily_greeting(self):
        html = _render_chat()
        self.assertIn("I'm Juniper", html)
        self.assertNotIn("I'm Lily", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
