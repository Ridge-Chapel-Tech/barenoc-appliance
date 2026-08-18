#!/usr/bin/env python3
"""Wiki consistency tests — index rows ↔ page files ↔ serving whitelist.

The wiki is served by slug (main.py WIKI_PAGES + _render_wiki). A page that
exists as a .md file but is missing from WIKI_PAGES is unreachable (the slug
falls back to "index"), and an index row pointing at a missing file 404s the
content. These tests keep all three views in agreement without importing
main.py (no FastAPI/SQLAlchemy needed — pure file/regex checks).

    python3 -m unittest test_wiki -v
"""

import os
import re
import unittest

WIKI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki")
MAIN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")

_INDEX_LINK_RE = re.compile(r"\]\(/wiki/([a-z0-9-]+)\)")
_WIKI_PAGES_ENTRY_RE = re.compile(r"\(\"([a-z0-9-]+)\",\s*\"[^\"]+\"\)")


def _index_slugs():
    with open(os.path.join(WIKI_DIR, "index.md")) as f:
        return set(_INDEX_LINK_RE.findall(f.read()))


def _page_files():
    return {
        name[:-3]
        for name in os.listdir(WIKI_DIR)
        if name.endswith(".md") and name != "index.md"
    }


def _serving_slugs():
    with open(MAIN_PY) as f:
        src = f.read()
    m = re.search(r"WIKI_PAGES\s*=\s*\[(.*?)\]", src, re.S)
    if not m:
        return set()
    return set(_WIKI_PAGES_ENTRY_RE.findall(m.group(1)))


class WikiIndexTest(unittest.TestCase):
    def test_every_index_row_has_a_page_file(self):
        missing = sorted(slug for slug in _index_slugs()
                         if not os.path.isfile(os.path.join(WIKI_DIR, f"{slug}.md")))
        self.assertEqual(missing, [],
                         f"index.md links to wiki pages with no .md file: {missing}")

    def test_every_page_file_has_an_index_row(self):
        orphans = sorted(_page_files() - _index_slugs())
        self.assertEqual(orphans, [],
                         f"wiki .md files with no index.md row: {orphans}")


class WikiServingListTest(unittest.TestCase):
    def test_serving_whitelist_matches_page_files(self):
        """No page is unreachable (missing from WIKI_PAGES) and no WIKI_PAGES
        entry points at a missing file. WIKI_PAGES additionally carries the
        'index' entry (the welcome page)."""
        files = _page_files()
        serving = _serving_slugs()
        self.assertTrue(serving, "could not parse WIKI_PAGES from main.py")
        expected = files | {"index"}
        self.assertEqual(serving, expected,
                         "WIKI_PAGES and wiki/*.md disagree "
                         f"(missing from list: {sorted(expected - serving)}, "
                         f"stale entries: {sorted(serving - expected)})")

    def test_index_and_serving_list_agree(self):
        self.assertEqual(_index_slugs(), _serving_slugs() - {"index"},
                         "index.md rows and WIKI_PAGES disagree")


if __name__ == "__main__":
    unittest.main()
