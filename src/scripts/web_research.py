#!/usr/bin/env python3
"""Knowledge-layer L3 — read-only web research for Lily (the AI assistant).

Gives Lily two READ-ONLY egress surfaces so she can research questions
(docs, best practices, upstream releases) and ground her answers with cited
sources:

    python3 web_research.py search "<query>" [count]
    python3 web_research.py fetch  "<url>"

Design rules (feature F2 — "L3 research (Lily web fetch/search)"):

* **Opt-in egress, hard-gated.** The process refuses to touch the network
  unless ``WEB_RESEARCH_ALLOWED=1`` is set in its environment. The agent
  runner sets that flag ONLY when (a) the deployment-level egress toggle
  (compliance control ``web_research`` -> ``WEB_RESEARCH_ENABLED``) is on AND
  (b) the ticket opted in (``Ticket.web_research``). Guidance alone is not the
  gate — the tool itself is.
* **Read-only, never write-only.** Only HTTP GETs. No POST/PUT/PATCH/DELETE,
  no cookies/token storage, no outbound writes except the local result cache.
* **SSRF-guarded.** http/https only; every hop (initial URL + each redirect)
  is resolved and must be a public, globally-routable address. Loopback,
  private, link-local, CGNAT (100.64/10, Tailscale), multicast, reserved and
  unspecified addresses are refused — so the appliance's own admin surface and
  the customer's LAN can never be reached through this tool.
* **Cache per topic (the cost lever).** Search results are cached by query and
  fetch results by URL, with TTLs, so repeated research on the same topic does
  not re-egress (and re-burn LLM summarization on the same raw material).
  ``--no-cache`` skips the cache; ``--refresh`` re-fetches but still writes.

Stdlib only (urllib + ipaddress + html.parser) — deployable as a single file
next to the other appliance scripts.
"""

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ── egress gate ─────────────────────────────────────────────────────────────
# The ONLY way this tool touches the network. The runner exports this per-run
# (ticket opt-in AND deployment toggle). Nothing here overrides it.
EGRESS_ENV = "WEB_RESEARCH_ALLOWED"

# ── cache (per-topic cost lever) ────────────────────────────────────────────
DEFAULT_CACHE_DIR = "/opt/barenoc/volumes/cache/web_research"
SEARCH_TTL_S = int(os.getenv("WEB_RESEARCH_SEARCH_TTL_S", str(24 * 3600)))
FETCH_TTL_S = int(os.getenv("WEB_RESEARCH_FETCH_TTL_S", str(7 * 24 * 3600)))

# ── fetch limits ────────────────────────────────────────────────────────────
MAX_FETCH_BYTES = 2 * 1024 * 1024   # never pull more than 2 MiB per page
MAX_TEXT_CHARS = 24000              # readable text cap per page
MAX_LINKS = 40                      # link-destination cap per page
MAX_SEARCH_RESULTS = 8              # hard ceiling on search results returned
TIMEOUT_S = 25

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Bytes read across this process's HTTP GETs (reported for metering).
_BYTES_FETCHED = 0


def bytes_fetched() -> int:
    return _BYTES_FETCHED


def _reset_bytes_fetched() -> None:
    global _BYTES_FETCHED
    _BYTES_FETCHED = 0


def egress_allowed() -> bool:
    """True only when the runner explicitly opted this run into web egress."""
    return os.environ.get(EGRESS_ENV, "").strip() == "1"


def cache_dir() -> str:
    return os.environ.get("WEB_RESEARCH_CACHE_DIR", DEFAULT_CACHE_DIR)


def cache_key(kind: str, value: str) -> str:
    """Stable per-topic cache key: sha256(kind + NUL + normalized value)."""
    norm = " ".join((value or "").strip().split())
    return hashlib.sha256(f"{kind}\0{norm}".encode("utf-8")).hexdigest()


def _cache_path(kind: str, value: str) -> str:
    return os.path.join(cache_dir(), f"{cache_key(kind, value)}.json")


def read_cache(kind: str, value: str) -> "dict | None":
    """Return a fresh cached payload for (kind, value), else None."""
    try:
        with open(_cache_path(kind, value)) as f:
            doc = json.load(f)
        expires = float(doc.get("expires_at") or 0)
        if expires < time.time():
            return None
        return doc.get("payload")
    except Exception:
        return None


def write_cache(kind: str, value: str, payload: dict, ttl: int) -> None:
    """Best-effort cache write (never raises — the cost lever is optional)."""
    try:
        d = cache_dir()
        os.makedirs(d, exist_ok=True)
        doc = {
            "cached_at": time.time(),
            "expires_at": time.time() + ttl,
            "kind": kind,
            "payload": payload,
        }
        tmp = _cache_path(kind, value) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, _cache_path(kind, value))
    except Exception:
        pass


# ── SSRF guard ──────────────────────────────────────────────────────────────

def _ip_is_public(ip: str) -> bool:
    """A globally-routable unicast address only (blocks private/loopback/
    link-local/CGNAT/multicast/reserved/unspecified, IPv4 + IPv6)."""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def url_is_safe(url: str) -> "tuple[bool, str]":
    """(ok, reason) — http(s) only AND every DNS answer must be public-global.

    Fails CLOSED: a hostname that does not resolve is refused (we cannot prove
    it is not pointing at a private address)."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False, "unparseable URL"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"unsupported scheme: {scheme or '(none)'}"
    host = parts.hostname
    if not host:
        return False, "no hostname"

    # A literal IP host: check it directly (no DNS). A ValueError here means
    # it is NOT an IP literal — fall through to hostname resolution.
    try:
        if not ipaddress.ip_address(host).is_global:
            return False, "host is not a public address"
        return True, ""
    except ValueError:
        pass  # not a literal IP — resolve below

    # Hostname: every resolved address must be public. The default port is
    # irrelevant to the check; use 443 to avoid any scheme-specific nuance.
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"host does not resolve ({e})"
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False, "host resolved to no addresses"
    for addr in addrs:
        if not _ip_is_public(addr):
            return False, "host resolves to a non-public address"
    return True, ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop through the SSRF guard."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, why = url_is_safe(newurl)
        if not ok:
            raise urllib.error.URLError(
                f"refusing redirect to unsafe URL ({why}): {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_SafeRedirectHandler())


def _http_get(url: str) -> "tuple[int, str, str]":
    """Read-only GET. Returns (status, body_text, final_url); raises on failure."""
    global _BYTES_FETCHED
    ok, why = url_is_safe(url)
    if not ok:
        raise urllib.error.URLError(f"unsafe URL ({why}): {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "text/html,application/xhtml+xml,text/plain,"
                                "application/json,text/markdown,*/*;q=0.8"})
    resp = _opener().open(req, timeout=TIMEOUT_S)
    body = resp.read(MAX_FETCH_BYTES + 1)
    _BYTES_FETCHED += len(body)
    if len(body) > MAX_FETCH_BYTES:
        body = body[:MAX_FETCH_BYTES]
    charset = resp.headers.get_content_charset() or "utf-8"
    try:
        text = body.decode(charset, errors="replace")
    except (LookupError, ValueError):
        text = body.decode("utf-8", errors="replace")
    return (resp.status, text, resp.geturl())


# ── search ──────────────────────────────────────────────────────────────────

def _ddg_search(query: str, count: int) -> "list[dict]":
    """DuckDuckGo HTML search (keyless). Returns [{title, url, snippet}]."""
    qs = urllib.parse.urlencode({"q": query})
    status, raw, _ = _http_get(f"https://html.duckduckgo.com/html/?{qs}")
    if status != 200 or "result__a" not in raw:
        raise urllib.error.URLError("DuckDuckGo returned no usable results")
    blocks = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?class="result__snippet"[^>]*>(.*?)</a>', raw, re.S)
    out = []
    for url, title, snip in blocks[:count]:
        m = re.search(r"uddg=([^&]+)", url)
        real = html.unescape(m.group(1)) if m else url
        if real.startswith("//"):
            real = "https:" + real
        t = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        s = html.unescape(re.sub(r"<[^>]+>", "", snip)).strip()
        if t and real:
            out.append({"title": t, "url": real, "snippet": s})
    return out


_GITHUB_EXCLUDED = (
    "/search", "/features", "/topics", "/collections", "/login", "/signup",
    "/sponsors", "/about", "/enterprise", "/settings", "/pricing",
    "/customer-stories", "/readme", "/security", "/copilot",
)


def _github_search(query: str, count: int) -> "list[dict]":
    """GitHub search fallback (repos + issues — reliably reachable)."""
    terms = " ".join(query.split())
    seen = []
    for kind in ("repositories", "issues"):
        q = f"type:{kind}+{terms}"
        qs = urllib.parse.urlencode({"q": q})
        status, raw, _ = _http_get(f"https://github.com/search?{qs}")
        if status != 200:
            continue
        for m in re.finditer(
                r'href="(/(?:[A-Za-z0-9_.-]+)/(?:[A-Za-z0-9_.-]+)'
                r'(?:/issues|/discussions|/blob/[^"]*|/tree/[^"]*)?)"', raw):
            u = "https://github.com" + m.group(1)
            low = u.lower()
            if any(x in low for x in _GITHUB_EXCLUDED):
                continue
            if u not in seen and len(seen) < count:
                seen.append({"title": u, "url": u,
                             "snippet": f"GitHub {kind} result"})
    return seen


def search(query: str, count: int = 6) -> dict:
    """Search the web for `query`. Returns a fresh result doc (no caching —
    the CLI layer owns the per-topic cache so --no-cache/--refresh work)."""
    count = max(1, min(int(count), MAX_SEARCH_RESULTS))
    doc = {"ok": True, "kind": "search", "query": query,
           "cache_hit": False, "bytes_fetched": 0}
    try:
        try:
            results = _ddg_search(query, count)
            doc["engine"] = "duckduckgo"
        except Exception as e:
            results = _github_search(query, count)
            doc["engine"] = "github"
            doc["fallback_reason"] = str(e)[:200]
        doc["results"] = results[:count]
        doc["bytes_fetched"] = bytes_fetched()
    except Exception as e:
        doc["ok"] = False
        doc["error"] = f"search failed: {e}"
    return doc


# ── fetch ───────────────────────────────────────────────────────────────────

def _strip_html(raw: str) -> str:
    """Reduce an HTML page to readable text (title, headings, links, code)."""
    # Extract the title BEFORE dropping <head> (which contains <title>).
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if m:
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    # Drop script/style/noscript/svg/nav/footer/header blocks wholesale.
    raw = re.sub(
        r'(?is)<(script|style|noscript|svg|head|nav|header|footer)[^>]*>.*?</\1>',
        " ", raw)
    body = re.sub(r"(?is)<(br|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", raw)
    text = re.sub(
        r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>",
        lambda m: "\n## " + html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() + "\n",
        body)
    text = re.sub(
        r"(?is)<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        lambda m: html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        + f" [{m.group(1)[:80]}]",
        text)
    text = re.sub(
        r"(?is)<code[^>]*>(.*?)</code>",
        lambda m: "`" + html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() + "`",
        text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    seen, out = set(), []
    for line in lines:
        key = line[:60]
        if key in seen or line in ("Menu", "Skip to content", "Products", "Search"):
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= 220:
            out.append("… (truncated — fetch a more specific URL if you need more)")
            break
    return (("TITLE: " + title + "\n") if title else "") + "\n".join(out)


def _extract_links(raw: str) -> "list[str]":
    links = []
    for m in re.finditer(r"(?is)<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>", raw):
        href = html.unescape(m.group(1)).strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            links.append(href[:200])
        if len(links) >= MAX_LINKS:
            break
    return links


def fetch(url: str) -> dict:
    """Fetch a page and return its readable text (no caching — the CLI layer
    owns the per-topic cache so --no-cache/--refresh work)."""
    doc = {"ok": True, "kind": "fetch", "url": url, "cache_hit": False,
           "bytes_fetched": 0}
    try:
        status, raw, final_url = _http_get(url)
        doc["final_url"] = final_url
        doc["status"] = status
        doc["bytes_fetched"] = bytes_fetched()
        if not raw.strip():
            raise urllib.error.URLError("no response body")
        if not re.search(r"<(html|body|div|p|h1|h2|h3|table|article|main)", raw,
                         re.I):
            # Plain text / JSON / markdown: show raw, truncated.
            doc["title"] = ""
            doc["text"] = raw[:MAX_TEXT_CHARS]
        else:
            doc["title"] = ""
            doc["text"] = _strip_html(raw)[:MAX_TEXT_CHARS]
            doc["links"] = _extract_links(raw)
    except Exception as e:
        doc["ok"] = False
        doc["error"] = f"fetch failed: {e}"
    return doc


# ── CLI ─────────────────────────────────────────────────────────────────────

def _emit(doc: dict, code: int) -> int:
    print(json.dumps(doc, indent=2, sort_keys=True))
    return code


def main(argv=None) -> int:
    if not egress_allowed():
        return _emit({
            "ok": False,
            "error": ("web research is disabled — opt-in egress required. "
                      "Set WEB_RESEARCH_ALLOWED=1 (ticket web_research opt-in "
                      "AND the deployment WEB_RESEARCH_ENABLED toggle)."),
        }, 3)

    parser = argparse.ArgumentParser(
        prog="web_research.py",
        description="Read-only web fetch + search for Lily (L3 research).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="keyless web search")
    ps.add_argument("query")
    ps.add_argument("count", nargs="?", default=6, type=int)
    ps.add_argument("--no-cache", action="store_true",
                    help="skip reading AND writing the cache")
    ps.add_argument("--refresh", action="store_true",
                    help="ignore cached results but still write fresh ones")

    pf = sub.add_parser("fetch", help="fetch a page's readable text")
    pf.add_argument("url")
    pf.add_argument("--no-cache", action="store_true")
    pf.add_argument("--refresh", action="store_true")

    args = parser.parse_args(argv)

    # --no-cache / --refresh bypass the per-topic cache (cost lever stays on by
    # default; these flags are the explicit "go get fresh data" escape hatch).
    if args.no_cache:
        def _read(kind, value):
            return None
        def _write(kind, value, payload, ttl):
            pass
    elif args.refresh:
        def _read(kind, value):
            return None
        _write = write_cache
    else:
        _read = read_cache
        _write = write_cache

    if args.cmd == "search":
        cached = _read("search", args.query)
        if cached is not None:
            cached["cache_hit"] = True
            return _emit(cached, 0)
        _reset_bytes_fetched()
        doc = search(args.query, args.count)
        if doc.get("ok"):
            _write("search", args.query, doc, SEARCH_TTL_S)
        return _emit(doc, 0 if doc.get("ok") else 5)
    if args.cmd == "fetch":
        cached = _read("fetch", args.url)
        if cached is not None:
            cached["cache_hit"] = True
            return _emit(cached, 0)
        ok, why = url_is_safe(args.url)
        if not ok:
            return _emit({"ok": False, "kind": "fetch", "url": args.url,
                          "error": f"unsafe URL ({why})"}, 4)
        _reset_bytes_fetched()
        doc = fetch(args.url)
        if doc.get("ok"):
            _write("fetch", args.url, doc, FETCH_TTL_S)
        return _emit(doc, 0 if doc.get("ok") else 5)
    return 2


if __name__ == "__main__":
    sys.exit(main())
