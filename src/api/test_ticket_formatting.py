#!/usr/bin/env python3
"""Tests for readable ticket threads (ticket-formatting pass).

Covers:
  1. Route binding (the 2026-08-16 lesson): the jobs-result + progress-note
     decorators bind to the intended endpoints.
  2. Render tests — execute the SHIPPED chat.html markdown-lite renderer
     under node and assert a long list collapses behind "Show more", bullet /
     numbered lines become real lists, and HTML in a note stays escaped.
  3. The downstream answer formatter (tone_filter.structure_answer /
     ellipsize) normalizes lists + truncates with the shared word-boundary
     ellipsis rule (never a silent mid-list cut).

    cd src/api && python3 -m unittest test_ticket_formatting -v
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="ticket-formatting-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"


class AnswerStructureTest(unittest.TestCase):
    """The downstream result formatter (tone_filter) turns the agent's raw
    final answer into grouped, line-per-item plain text."""

    def test_bullets_normalized_and_grouped(self):
        from tone_filter import structure_answer
        raw = ("Here's what I found:\n"
               "* ap-1 is online\n"
               "• ap-2 is online\n"
               "a paragraph in between\n"
               "- ap-3 is wired")
        out = structure_answer(raw)
        self.assertIn("- ap-1 is online", out)
        self.assertIn("- ap-2 is online", out)
        self.assertIn("- ap-3 is wired", out)
        # the paragraph is separated from the list blocks by blank lines
        self.assertIn("a paragraph in between", out)
        self.assertIn("\n\n- ap-3", out)

    def test_numbered_items_normalized(self):
        from tone_filter import structure_answer
        out = structure_answer("Steps:\n1. reboot\n2) re-check")
        self.assertIn("1. reboot", out)
        self.assertIn("2. re-check", out)
        self.assertNotIn("2)", out)

    def test_header_gets_blank_line_separation(self):
        from tone_filter import structure_answer
        out = structure_answer("Devices online now:\n- ap-1\n- ap-2")
        self.assertEqual(out, "Devices online now:\n\n- ap-1\n- ap-2")

    def test_no_html_is_added(self):
        from tone_filter import structure_answer
        out = structure_answer("plain <answer> with\n- a list")
        self.assertNotIn("<ul", out)
        self.assertNotIn("<li", out)
        self.assertIn("- a list", out)


class EllipsizeTest(unittest.TestCase):
    """The answer path uses the SAME word-boundary ellipsis rule as the
    progress cap — a truncation is never silent."""

    def test_short_text_passes_through(self):
        from tone_filter import ellipsize
        self.assertEqual(ellipsize("short", 800), "short")

    def test_long_text_gets_ellipsis_on_word_boundary(self):
        from tone_filter import ellipsize
        text = ("word " * 300)[:900]  # 900 chars of "word " runs
        out = ellipsize(text, 800)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 801)
        # the retained text (minus the ellipsis) is a clean prefix of the
        # input — i.e. the cut lands on a word boundary, never mid-word
        self.assertTrue(text.startswith(out[:-1]))

    def test_never_silent_mid_list_cut(self):
        from tone_filter import ellipsize
        out = ellipsize("- " + "x" * 1000, 400)
        self.assertTrue(out.endswith("…"))


class RouteBindingTest(unittest.TestCase):
    """Route-level guard (2026-08-16 lesson): decorators must bind to the
    intended endpoints — a misbound decorator 422s for a whole release day."""

    def test_jobs_result_and_progress_routes_bind(self):
        from fastapi.testclient import TestClient
        from main import app

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/jobs/result"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "report_job_result")

        route = next(r for r in app.routes
                     if getattr(r, "path", "") == "/api/v1/tickets/{ticket_id}/progress"
                     and "POST" in getattr(r, "methods", []))
        self.assertEqual(route.endpoint.__name__, "add_progress_note")

        c = TestClient(app)
        r = c.post("/api/v1/jobs/result",
                   json={"ticket_id": "TKT-20260826-0001",
                         "action": "network_info", "success": True})
        self.assertNotEqual(r.status_code, 422, f"route misbound: {r.text}")
        self.assertEqual(r.status_code, 401)


class ChatRendererTest(unittest.TestCase):
    """Execute the SHIPPED chat.html markdown-lite renderer under node — the
    assertion locks the real JS code path, not a Python re-implementation.
    Skips (not fails) when node isn't installed (e.g. a bare VM host)."""

    @staticmethod
    def _read(name):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "templates", name), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _extract_js_function(html, name):
        """Pull a top-level `function name(...) { ... }` out of the template."""
        start = html.index("function " + name + "(")
        brace = html.index("{", start)
        depth = 0
        i = brace
        while i < len(html):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    return html[start:i + 1]
            i += 1
        raise ValueError("unbalanced braces in function " + name)

    @staticmethod
    def _extract_var(html, name):
        m = re.search(r"var " + re.escape(name) + r" = (.+?);\n", html)
        if not m:
            m = re.search(r"var " + re.escape(name) + r" = (.+?);", html)
        if not m:
            raise ValueError("var " + name + " not found")
        return "var " + name + " = " + m.group(1) + ";\n"

    def _run_render(self, text):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available — cannot execute the shipped renderer")
        html = self._read("chat.html")
        script = (
            self._extract_var(html, "NOTE_COLLAPSE_LINES")
            + self._extract_var(html, "NOTE_BULLET_RE")
            + self._extract_var(html, "NOTE_NUM_RE")
            + self._extract_js_function(html, "esc") + "\n"
            + self._extract_js_function(html, "markdownLite") + "\n"
            + self._extract_js_function(html, "noteBody") + "\n"
            + "const chunks=[];process.stdin.on('data',function(d){chunks.push(d);});\n"
            + "process.stdin.on('end',function(){\n"
            + "  var t=JSON.parse(chunks.join(''));\n"
            + "  process.stdout.write(JSON.stringify({md:markdownLite(t),"
            + " body:noteBody('agent_completed',t)}));\n"
            + "});\n"
        )
        proc = subprocess.run([node, "-e", script],
                              input=json.dumps(text),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, "node failed: " + proc.stderr)
        return json.loads(proc.stdout)

    def test_bullets_render_as_a_list(self):
        r = self._run_render("Devices:\n- ap-1\n- ap-2\n\nWired:\n- pc-1")
        self.assertIn('<ul class="note-list">', r["md"])
        self.assertIn("<li>ap-1</li>", r["md"])
        self.assertIn("<li>pc-1</li>", r["md"])

    def test_numbered_lines_render_as_ordered_list(self):
        r = self._run_render("Steps:\n1. reboot\n2. re-check")
        self.assertIn('<ol class="note-list">', r["md"])
        self.assertIn("<li>reboot</li>", r["md"])
        self.assertIn("<li>re-check</li>", r["md"])

    def test_long_list_collapses_behind_show_more(self):
        lines = ["Connected devices:"] + ["- device-%02d" % i for i in range(1, 21)]
        r = self._run_render("\n".join(lines))
        body = r["body"]
        self.assertIn("<details", body)
        self.assertIn("Show more (9 more lines)", body)
        preview = body.split("<details")[0]
        # first 12 lines stay visible (header + device-01..device-11) …
        self.assertIn("device-01", preview)
        self.assertIn("device-11", preview)
        # … and the rest is folded behind "Show more"
        self.assertNotIn("device-12", preview)
        self.assertIn("device-12", body)
        self.assertIn("device-20", body)
        self.assertIn('<ul class="note-list">', body)

    def test_html_in_note_stays_escaped(self):
        r = self._run_render("bad <script>alert(1)</script> and <b>bold</b>")
        self.assertIn("&lt;script&gt;", r["md"])
        self.assertNotIn("<script>", r["md"])
        self.assertIn("&lt;b&gt;", r["md"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
