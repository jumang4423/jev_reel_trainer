"""Reel discovery (seed harvesting).

Findings from testing on the real browser:
  - Harvesting search engines over plain HTTP does not work (DuckDuckGo rate-limits
    almost immediately, Bing ignores `site:`, Reddit returns 403).
    **Searching from the real browser is the only reliable route.**
  - Instagram tag pages (/explore/tags/...) are broken on the web: clicking a hashtag or
    opening the URL directly both rewrite to
    /explore/search/keyword/?q=... which renders "Something went wrong".
  - `site:instagram.com reel <keyword>` on a search engine returns real reel URLs.
  - Reel permalinks (/reel/<code>/) open and play normally in a logged-in real browser.

So instead of generating random URLs, we seed from **search-engine hits -> real URLs** and then
grow the supply by **walking the author's profile**.
"""

from __future__ import annotations

import re
import urllib.parse

REEL_CODE_RE = re.compile(r"instagram\.com/(?:reel|reels)/([A-Za-z0-9_-]{6,})")
HASHTAG_RE = re.compile(r"#([0-9A-Za-z_\u3040-\u30ff\u4e00-\u9fff]{2,40})")

# Engines that did not block us (opened in the real browser)
SEARCH_ENGINES = {
    "brave": "https://search.brave.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}&num=30",
}

DEFAULT_QUERIES = [
    "site:instagram.com reel puppy",
    "site:instagram.com reel dog",
    "site:instagram.com reel kitten",
    "site:instagram.com reel dogsofinstagram",
    "site:instagram.com reel cat",
    "site:instagram.com reel dogtok",
]

JS_HREFS = "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"


def harvest_search(page, query: str, engine: str = "brave", wait_ms: int = 2600) -> list[str]:
    """Collect shortcodes of real reels via a search engine (runs in the real browser)."""
    tpl = SEARCH_ENGINES.get(engine)
    if not tpl:
        raise ValueError(f"unknown engine: {engine}")
    page.goto(tpl.format(q=urllib.parse.quote(query)), wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(wait_ms)

    codes: list[str] = []
    for href in page.evaluate(JS_HREFS) or []:
        m = REEL_CODE_RE.search(href or "")
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
    return codes


def profile_reel_codes(page, username: str, scrolls: int = 3, wait_ms: int = 2200) -> list[str]:
    """Collect reel shortcodes from an author's profile page."""
    page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(wait_ms)

    codes: list[str] = []
    for _ in range(max(0, scrolls)):
        for href in page.evaluate(JS_HREFS) or []:
            m = REEL_CODE_RE.search(href or "")
            if m and m.group(1) not in codes:
                codes.append(m.group(1))
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(wait_ms)
    return codes


def split_author_and_caption(text: str) -> tuple[str, str]:
    """Split the output of ``reel_text()`` ("author • Follow caption...") into parts."""
    if not text:
        return "", ""
    head = text.split("•", 1)[0].strip()
    author = head.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", author or ""):
        author = ""
    body = text.split("•", 1)[1].strip() if "•" in text else text
    body = re.sub(r"^Follow\s*", "", body).strip()
    return author, body


def hashtags_of(text: str) -> list[str]:
    return [m.group(1).lower() for m in HASHTAG_RE.finditer(text or "")]


def reel_url(code: str) -> str:
    return f"https://www.instagram.com/reel/{code}/"
