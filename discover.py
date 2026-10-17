"""リールの発見（シード収穫）。

実機検証で分かったこと:
  - HTTP での検索エンジン収穫は使えない（DuckDuckGo はすぐレート制限、Bing は site: を無視、
    Reddit は 403）。**実ブラウザでの検索が唯一安定して動く**。
  - IG のタグページ（/explore/tags/...）は Web では壊れている（クリックでも直リンクでも
    /explore/search/keyword/?q=... に化けて "Something went wrong"）。
  - 検索エンジンで `site:instagram.com reel <keyword>` を引くと、実在の犬リールの URL が取れる。
  - パーマリンク（/reel/<code>/）は、ログイン済みの実ブラウザなら普通に開いて再生できる。

したがって「ランダム生成」ではなく **検索エンジンのインデックス → 実在 URL** を種にして、
そこから **投稿者プロフィールを辿って** 供給を増やす。
"""

from __future__ import annotations

import re
import urllib.parse

REEL_CODE_RE = re.compile(r"instagram\.com/(?:reel|reels)/([A-Za-z0-9_-]{6,})")
HASHTAG_RE = re.compile(r"#([0-9A-Za-z_\u3040-\u30ff\u4e00-\u9fff]{2,40})")

# 実測でブロックされなかったエンジン（実ブラウザで開く前提）
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
    """検索エンジン経由で実在のリール shortcode を集める（実ブラウザで実行）。"""
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
    """投稿者のプロフィールから、その人のリール shortcode を集める。"""
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
    """``reel_text()`` の戻り（"author • Follow caption..."）を分解する。"""
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
