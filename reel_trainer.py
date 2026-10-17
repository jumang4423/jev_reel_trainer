#!/usr/bin/env python3
"""jev_reel_trainer — scroll Instagram Reels and have each reel judged, then
like the ones you want (dogs/cats by default) and skip the rest.

This implementation bakes in the Instagram web quirks found by testing on a
real Brave profile:

  1. A background tab never decodes <video>.
     -> page.bring_to_front() is mandatory; a full-page screenshot is solid grey.
  2. Clicking the video element navigates to the permalink (/reels/<code>/) and the
     `...` menu turns into the post menu.
     -> Never click the video; bring the tab forward and muted play() to decode it.
  3. drawImage into a canvas avoids the play-button overlay that an element
     screenshot would include.
     -> Frames are captured via canvas -> dataURL.
  4. The web reel `...` menu has no "Not interested"
     (only Report / Go to post / Share to... / Copy link / Embed / About this account).
     -> The nearest negative is Report -> "I just don't like it" (off by default, --negative report).
  5. An already-liked reel flips aria-label to "Unlike".
     -> Used to avoid double taps and to verify the action landed.

Warning: automation may violate Instagram's terms. Use at your own risk and respect
the rate limits (--min-delay / --max-delay / --max-likes-per-hour).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: pip install -r requirements.txt")

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("Playwright is required: pip install -r requirements.txt && python -m playwright install")

from jev_client import DEFAULT_MODEL, JevError, decide_with_images, parse_answers, summarize_usage

try:  # works on Pillow 10+ and older releases
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover
    RESAMPLE = Image.LANCZOS

# --------------------------------------------------------------------------
# Jev image limits (from TypeSafe docs)
#   PNG/JPEG only / 16MiB per file / 280-1568px per side /
#   78,400-2,458,624 total pixels / 32MiB per whole request
# --------------------------------------------------------------------------
MAX_SIDE = 1568
MIN_SIDE = 280
MAX_PIXELS = 2_458_624

BROWSER_CANDIDATES = [
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

# Profiles of running browsers (where to look for DevToolsActivePort).
# Brave/Chrome/Edge >= 136 ignore --remote-debugging-port for the default profile, so we
# use the authorized remote debugging enabled by the brave://inspect toggle.
DEFAULT_PROFILE_DIRS = [
    "~/Library/Application Support/BraveSoftware/Brave-Browser",
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Application Support/Microsoft Edge",
]

# --------------------------------------------------------------------------
# JS executed inside the page (kept as strings so braces never clash with f-strings)
# --------------------------------------------------------------------------
JS_READY = """() => {
  const H = window.innerHeight;
  let ok = false;
  document.querySelectorAll('video').forEach((v) => {
    const r = v.getBoundingClientRect();
    const vis = Math.min(r.bottom, H) - Math.max(r.top, 0);
    if (r.width > 200 && vis > 300 && v.videoWidth > 0) ok = true;
  });
  return ok;
}"""

JS_PLAY = """() => {
  const H = window.innerHeight;
  document.querySelectorAll('video').forEach((v) => {
    const r = v.getBoundingClientRect();
    const vis = Math.min(r.bottom, H) - Math.max(r.top, 0);
    if (r.width > 200 && vis > 300) {
      try { v.muted = true; if (v.play) v.play().catch(() => {}); } catch (e) {}
    }
  });
  return true;
}"""

# Grab the frame of the reel currently in the viewport via canvas
JS_FRAME = """() => {
  const H = window.innerHeight;
  let best = null;
  document.querySelectorAll('video').forEach((v) => {
    const r = v.getBoundingClientRect();
    const vis = Math.min(r.bottom, H) - Math.max(r.top, 0);
    if (r.width > 200 && vis > 300 && v.videoWidth > 0) {
      if (!best || vis > best.vis) best = { el: v, vis };
    }
  });
  if (!best) return null;
  const c = document.createElement('canvas');
  c.width = best.el.videoWidth;
  c.height = best.el.videoHeight;
  c.getContext('2d').drawImage(best.el, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.9);
}"""

# Author and caption of the current reel.
# Note: an IG reel caption is neither an <a> nor an ancestor of the video; it is
#     drawn as overlay divs/spans. Picking the container that stays short while
#     holding the longest text yields author+caption (verified by measurement).
JS_REEL_TEXT = """() => {
  const H = window.innerHeight;
  let best = '';
  document.querySelectorAll('div, span, h1').forEach((el) => {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t.length < 12 || t.length > 400 || t.length <= best.length) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.top < 0 || r.bottom > H) return;
    if (r.height > 300 || r.width > 900) return;   // do not grab the whole feed
    best = t;
  });
  return best.slice(0, 400);
}"""

# Return the center of the first in-viewport button with a matching svg[aria-label]
JS_BUTTON = """(label) => {
  const H = window.innerHeight;
  const out = [];
  document.querySelectorAll('svg[aria-label="' + label + '"]').forEach((s) => {
    const r = s.getBoundingClientRect();
    if (r.width === 0) return;
    if (r.top < 0 || r.bottom > H) return;
    out.push({ x: r.x + r.width / 2, y: r.y + r.height / 2, area: r.width * r.height });
  });
  out.sort((a, b) => b.area - a.area);
  return out[0] || null;
}"""

JS_HAS_LABEL = """(label) => {
  const H = window.innerHeight;
  return Array.from(document.querySelectorAll('svg[aria-label="' + label + '"]')).some((s) => {
    const r = s.getBoundingClientRect();
    return r.width > 0 && r.top >= 0 && r.bottom <= H;
  });
}"""

# Duration and playback position of the on-screen video (used to judge completion)
JS_VIDEO_INFO = """() => {
  const H = window.innerHeight;
  let best = null;
  document.querySelectorAll('video').forEach((v) => {
    const r = v.getBoundingClientRect();
    const vis = Math.min(r.bottom, H) - Math.max(r.top, 0);
    if (r.width > 150 && vis > 150 && v.videoWidth > 0 && (!best || vis > best.vis)) {
      best = { vis, duration: v.duration, currentTime: v.currentTime, paused: v.paused };
    }
  });
  return best;
}"""

# Action-bar counters (comment and repost counts).
# Note: IG does not display a like count on reels; only comment and repost are shown.
JS_ENGAGEMENT = """() => {
  const H = window.innerHeight;
  const labelY = {};
  document.querySelectorAll('svg[aria-label]').forEach((s) => {
    const r = s.getBoundingClientRect();
    const al = s.getAttribute('aria-label');
    if (r.width > 0 && r.top >= 0 && r.bottom <= H && (al === 'Comment' || al === 'Repost' || al === 'Like')) {
      labelY[al] = r.y + r.height / 2;
    }
  });
  const nums = [];
  document.querySelectorAll('span,div').forEach((el) => {
    if (el.children.length) return;
    const t = (el.textContent || '').trim();
    if (!/^[0-9][0-9.,]*[KkMm]?$/.test(t)) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.top < 0 || r.bottom > H) return;
    nums.push({ t, y: r.y + r.height / 2, x: Math.round(r.x) });
  });
  const pick = (label) => {
    if (!(label in labelY)) return null;
    let best = null;
    nums.forEach((n) => {
      const d = Math.abs(n.y - labelY[label]);
      if (d < 34 && (!best || d < best.d)) best = { d, t: n.t };
    });
    return best ? best.t : null;
  };
  return { comments: pick('Comment'), reposts: pick('Repost'), like_label: pick('Like') };
}"""



# --------------------------------------------------------------------------
# Normalize an image so it fits Jev's limits
# --------------------------------------------------------------------------
def normalize_for_jev(img: "Image.Image") -> "Image.Image":
    w, h = img.size
    if max(w, h) > MAX_SIDE:  # clamp the long side to 1568
        s = MAX_SIDE / max(w, h)
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), RESAMPLE)
        w, h = img.size
    if min(w, h) < MIN_SIDE:  # bring the short side up to 280
        s = MIN_SIDE / min(w, h)
        img = img.resize((int(w * s + 0.5), int(h * s + 0.5)), RESAMPLE)
        w, h = img.size
    if w * h > MAX_PIXELS:  # total pixel cap
        s = (MAX_PIXELS / (w * h)) ** 0.5
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), RESAMPLE)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------
def find_browser() -> str | None:
    for p in BROWSER_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("brave", "google-chrome", "chrome", "chromium"):
        w = shutil.which(name)
        if w:
            return w
    return None


def launch_context(playwright, profile_dir: Path, headless: bool):
    args = [
        "--autoplay-policy=no-user-gesture-required",  # allow muted autoplay
        "--disable-blink-features=AutomationControlled",  # slightly dodge automation detection
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    kw = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=args,
        viewport={"width": 1440, "height": 1000},
        locale="ja-JP",
    )
    exe = find_browser()
    if exe:
        return playwright.chromium.launch_persistent_context(executable_path=exe, **kw)
    print(
        "[warn] Brave/Chrome not found; falling back to the bundled Chromium. "
        "It may be unable to decode H.264, in which case frames cannot be captured.",
        file=sys.stderr,
    )
    return playwright.chromium.launch_persistent_context(**kw)


def resolve_cdp_endpoint(explicit: str, profile_dir: str | None) -> str:
    """Resolve the CDP endpoint.

    - An explicit URL (``http://`` / ``ws://``) is used as-is.
    - ``auto`` looks for ``DevToolsActivePort`` and builds
      ``ws://127.0.0.1:<port><path>`` from it.

    Brave/Chrome/Edge >= 136 ignore ``--remote-debugging-port`` for the default profile.
    Turning on the ``brave://inspect/#remote-debugging`` toggle instead makes Brave
    write ``DevToolsActivePort`` into the profile and listen with authorization
    (HTTP /json/version returns 404 on that route, but ws:// works).
    """
    if explicit and explicit != "auto":
        return explicit

    candidates = (
        [Path(profile_dir).expanduser()]
        if profile_dir
        else [Path(p).expanduser() for p in DEFAULT_PROFILE_DIRS]
    )
    for d in candidates:
        f = d / "DevToolsActivePort"
        if f.exists():
            lines = f.read_text().splitlines()
            if len(lines) >= 2 and lines[0].strip():
                return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
    raise SystemExit(
        "DevToolsActivePort not found.\n"
        "Open brave://inspect/#remote-debugging in Brave and turn on\n"
        "\"Allow remote debugging\" (it is stored in Local State and survives restarts)."
    )


def ensure_login(page, timeout_s: int = 600) -> None:
    """Wait until we are logged in; if not, ask the human to log in."""
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    asked = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.locator('a[href="/reels/"]').count() > 0:
            return
        if not asked:
            print(">>> Please log in to Instagram in Brave (waiting until it is done)", flush=True)
            asked = True
        time.sleep(3)
    raise SystemExit("could not confirm a logged-in session")


# --------------------------------------------------------------------------
# Reel actions
# --------------------------------------------------------------------------
def reel_code(page) -> str:
    m = re.search(r"/reels?/([A-Za-z0-9_-]+)", page.url)
    return m.group(1) if m else ""


def wait_visible_video(page, timeout_ms: int = 15000) -> bool:
    try:
        page.wait_for_function(JS_READY, timeout=timeout_ms)
        return True
    except PWTimeout:
        return False


def nudge_play(page) -> None:
    try:
        page.evaluate(JS_PLAY)
    except Exception:
        pass


def next_reel(page, timeout_ms: int = 8000) -> None:
    prev = page.url
    page.keyboard.press("ArrowDown")
    try:
        page.wait_for_function("(u) => location.href !== u", arg=prev, timeout=timeout_ms)
    except PWTimeout:
        page.keyboard.press("End")
        page.wait_for_timeout(1500)
    nudge_play(page)
    wait_visible_video(page)


def blank_range(jpeg: bytes) -> int:
    """Metric used to reject near-black (not yet decoded) frames.

    Returns the spread between the darkest and brightest grey values. It collapses
    toward 0 when only metadata is loaded and no real frame has been decoded.
    """
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    hist = img.histogram()
    lo = next((i for i, c in enumerate(hist) if c), 255)
    hi = next((i for i in range(255, -1, -1) if hist[i]), 0)
    return int(hi - lo)


def capture_frame(page, out_dir: Path | None, tries: int = 4, min_range: int = 16) -> tuple[str, str] | None:
    """Return (saved path or '', base64 JPEG), or None when no frame could be captured.

    Right after playback starts, readyState can be 4 while no real frame exists yet, so
    retry at short intervals until a non-flat frame is captured.
    """
    raw: bytes | None = None
    for attempt in range(max(1, tries)):
        data_url = page.evaluate(JS_FRAME)
        if data_url and "," in data_url:
            raw = base64.b64decode(data_url.split(",", 1)[1])
            if blank_range(raw) >= min_range:
                break
        if attempt < tries - 1:
            nudge_play(page)
            page.wait_for_timeout(700)
    if raw is None:
        return None

    img = normalize_for_jev(Image.open(io.BytesIO(raw)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    payload = buf.getvalue()
    b64 = base64.b64encode(payload).decode()

    path = ""
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = str(out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{random.randint(1000, 9999)}.jpg")
        Path(path).write_bytes(payload)
    return path, b64


def reel_text(page) -> str:
    try:
        return page.evaluate(JS_REEL_TEXT) or ""
    except Exception:
        return ""


def find_visible_button(page, label: str):
    try:
        return page.evaluate(JS_BUTTON, label)
    except Exception:
        return None


def has_visible_label(page, label: str) -> bool:
    try:
        return bool(page.evaluate(JS_HAS_LABEL, label))
    except Exception:
        return False


def close_overlays(page) -> None:
    for _ in range(2):
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)


def do_like(page) -> str:
    if has_visible_label(page, "Unlike"):  # already liked
        return "already_liked"
    btn = find_visible_button(page, "Like")
    if not btn:
        return "like_button_not_found"
    page.mouse.click(btn["x"], btn["y"])
    page.wait_for_timeout(900)
    return "liked" if has_visible_label(page, "Unlike") else "like_failed"


def do_not_interested(page) -> str:
    """Report -> "I just don't like it", because web reels have no Not interested."""
    more = find_visible_button(page, "More")
    if not more:
        return "more_button_not_found"
    page.mouse.click(more["x"], more["y"])
    page.wait_for_timeout(900)
    try:
        page.get_by_text("Report", exact=True).first.click(timeout=4000)
        page.wait_for_timeout(900)
        page.get_by_text("I just don't like it", exact=True).first.click(timeout=4000)
        page.wait_for_timeout(1200)
    except PWTimeout:
        close_overlays(page)
        return "report_flow_failed"
    close_overlays(page)
    return "not_interested"


def do_skip(page) -> str:
    # Send no negative signal at all; just keep the dwell short.
    return "skipped"


def _to_int(t: str | None) -> int | None:
    """Turn '44' / '1.2K' / '3M' into an integer."""
    if not t:
        return None
    s = t.strip().replace(",", "")
    mult = 1
    if s[-1:] in ("K", "k"):
        mult, s = 1_000, s[:-1]
    elif s[-1:] in ("M", "m"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def engagement(page) -> dict:
    """Comment and repost counts of the on-screen reel (IG shows no like count)."""
    try:
        raw = page.evaluate(JS_ENGAGEMENT) or {}
    except Exception:
        return {"comments": None, "reposts": None}
    return {"comments": _to_int(raw.get("comments")), "reposts": _to_int(raw.get("reposts"))}


def dwell_until_end(page, loops: int = 1, cap: float = 45.0) -> float:
    """Watch the video to the end (plus optional loops). Watch time is the strongest
    positive signal and notifies nobody.

    Returns: the number of seconds actually waited
    """
    try:
        info = page.evaluate(JS_VIDEO_INFO) or {}
    except Exception:
        info = {}
    dur = info.get("duration") or 0
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0 or dur > 600:
        dur = 8.0  # conservative fallback when unknown
    total = min(dur, cap) * (1 + max(0, loops))
    page.wait_for_timeout(int(total * 1000))
    return total


def maybe_like(page, args) -> str:
    """The "only like small posts" policy.

    IG does not show a like count on reels, so we judge by comment and repost counts.
    Only like when both are at or below their thresholds (i.e. react to unremarkable posts).
    """
    if getattr(args, "interact", "view-only") != "like":
        return "like_off"
    if has_visible_label(page, "Unlike"):
        return "already_liked"

    e = engagement(page)
    c, r = e.get("comments"), e.get("reposts")
    if c is None or r is None:
        return "like_skipped_unknown_engagement"
    if c > args.like_max_comments or r > args.like_max_reposts:
        return "like_skipped_popular"

    btn = find_visible_button(page, "Like")
    if not btn:
        return "like_button_not_found"
    page.mouse.click(btn["x"], btn["y"])
    page.wait_for_timeout(900)
    return "liked" if has_visible_label(page, "Unlike") else "like_failed"


# --------------------------------------------------------------------------
# Judging
# --------------------------------------------------------------------------
def build_questions(targets: str) -> list[dict]:
    qs = [
        {
            "type": "predicate",
            "name": "is_dog",
            "instructions": "Does the image clearly show a dog? (plush toys, drawings, CGI => false)",
        },
        {
            "type": "predicate",
            "name": "is_cat",
            "instructions": "Does the image clearly show a cat? (plush toys, drawings, CGI => false)",
        },
    ]
    if targets == "any":
        qs.append(
            {
                "type": "predicate",
                "name": "is_animal",
                "instructions": "Is the main subject a real animal? (people, objects, scenery, CGI => false)",
            }
        )
    return qs


def score_of(answers: dict, targets: str) -> tuple[float, str]:
    def p(name: str) -> float:
        v = answers.get(name, {}).get("probability")
        return float(v) if isinstance(v, (int, float)) else 0.0

    if targets == "any":
        s = p("is_animal")
        return s, "is_animal"
    dog, cat = p("is_dog"), p("is_cat")
    return (dog, "is_dog") if dog >= cat else (cat, "is_cat")


def judge(
    api_key: str,
    model: str,
    text: str,
    image_b64: str,
    questions: list[dict],
    targets: str,
    backend: str = "jev",
    local_url: str | None = None,
):
    """The judge itself. backend=jev uses Vercel AI Gateway, backend=local uses llama.cpp."""
    if backend == "local":
        import local_judge  # lazy import (unnecessary when not using the local judge)

        return local_judge.decide_local(
            local_url or local_judge.DEFAULT_BASE_URL, image_b64, text or "", targets
        )

    prompt = (
        "One frame extracted from an Instagram reel video."
        "Supporting text such as the caption is included. Answer the questions with probabilities.\n"
        f"--- text ---\n{text or '(none)'}"
    )
    resp = decide_with_images(api_key, prompt, [image_b64], questions, model=model)
    answers = parse_answers(resp)
    score, key = score_of(answers, targets)
    return score, key, answers, resp


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
class Logger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("a", encoding="utf-8")

    def write(self, rec: dict) -> None:
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("AI_GATEWAY_API_KEY")
    if args.judge == "jev" and not api_key and not args.dry_run:
        sys.exit("set AI_GATEWAY_API_KEY in the environment (not needed for --judge local)")

    profile_dir = Path(args.profile).expanduser().resolve()
    frame_dir = Path(args.frames_dir).expanduser().resolve() if args.frames_dir else None
    logger = Logger(Path(args.log).expanduser().resolve())

    questions = build_questions(args.targets)
    like_times: list[float] = []
    reports_done = 0
    stats = {"judged": 0, "errors": 0}
    actions_tally: dict[str, int] = {}
    consec_errors = 0
    seen: set[str] = set()

    attached = False
    with sync_playwright() as p:
        if args.cdp:
            endpoint = resolve_cdp_endpoint(args.cdp, args.cdp_profile_dir)
            print(
                ">>> Attaching to the running browser over CDP. Approve the dialog in Brave if it appears"
                f"（{endpoint.split('/devtools/')[0]}/...）",
                flush=True,
            )
            browser = p.chromium.connect_over_cdp(endpoint, timeout=180000)
            ctx = browser.contexts[0]  # the default context, which carries the real profile's cookies
            page = ctx.new_page()  # do not touch the tabs you already have open
            attached = True
        else:
            ctx = launch_context(p, profile_dir, args.headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()  # <- quirk 1: without this the video is never decoded

        ensure_login(page)
        page.goto("https://www.instagram.com/reels/", wait_until="domcontentloaded")
        page.bring_to_front()
        nudge_play(page)
        wait_visible_video(page)

        print(f"=== jev_reel_trainer start (judge={args.judge}"
              + (f", model={DEFAULT_MODEL}" if args.judge == "jev" else f", url={args.local_url or 'http://127.0.0.1:8081'}")
              + f", targets={args.targets}, threshold={args.threshold}, negative={args.negative}) ===", flush=True)

        step = 0
        while args.steps == 0 or step < args.steps:
            step += 1
            try:
                code = reel_code(page)
                if not code:
                    next_reel(page)
                    continue

                frame = capture_frame(page, frame_dir)
                if frame is None:
                    # no frame (decode failure) -> nudge playback and try once more
                    nudge_play(page)
                    if not wait_visible_video(page, 8000):
                        stats["errors"] += 1
                        print(f"[{step}] code={code} frame=None (decode ng) -> next", flush=True)
                        next_reel(page)
                        continue
                    frame = capture_frame(page, frame_dir)
                    if frame is None:
                        stats["errors"] += 1
                        next_reel(page)
                        continue

                path, b64 = frame
                text = reel_text(page)
                is_animal = False
                dwell_s = 0.0

                if args.dry_run:
                    action, score, key = "dry_run", None, None
                    answers, resp = {}, {}
                else:
                    print(f"[{step}] {code} frame={len(b64)//1024}KB text={text[:50]!r}", flush=True)
                    t0 = time.time()
                    score, key, answers, resp = judge(
                        api_key, DEFAULT_MODEL, text, b64, questions, args.targets,
                        args.judge, args.local_url,
                    )
                    stats["judged"] += 1
                    consec_errors = 0
                    print(f"[{step}] {args.judge} {time.time() - t0:.1f}s  {key}={score:.3f}", flush=True)
                    is_animal = score >= args.threshold
                    if is_animal:
                        # * strongest positive signal: watch to the end (+ loops).
                        #   anonymous, no notification, no polluted lists.
                        dwell_s = dwell_until_end(page, loops=args.loops, cap=args.max_dwell)
                        now = time.time()
                        like_times = [t for t in like_times if now - t < 3600]
                        if len(like_times) >= args.max_likes_per_hour:
                            action = "like_rate_limited"
                        else:
                            action = maybe_like(page, args)
                            if action == "liked":
                                like_times.append(now)
                        print(f"[{step}] watched {dwell_s:.1f}s -> {action}", flush=True)
                    else:
                        # * cut within 2 seconds (a negative signal Meta's system card explicitly predicts)
                        page.wait_for_timeout(random.randint(700, 1500))
                        if args.negative == "report" and reports_done < args.max_reports:
                            action = do_not_interested(page)
                            if action == "not_interested":
                                reports_done += 1
                        else:
                            action = do_skip(page)

                actions_tally[action] = actions_tally.get(action, 0) + 1
                rec = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "step": step,
                    "code": code,
                    "url": page.url,
                    "action": action,
                    "score": score,
                    "score_key": key,
                    "is_animal": is_animal,
                    "dwell_s": round(dwell_s, 2),
                    "engagement": engagement(page) if is_animal else None,
                    "seen_before": code in seen,
                    "frame": path,
                    "text": text[:200],
                    "answers": {k: v.get("probability") for k, v in (answers or {}).items()},
                    "confidence": {k: v.get("confidence") for k, v in (answers or {}).items()},
                    **summarize_usage(resp or {}),
                }
                logger.write(rec)
                seen.add(code)

                line = f"[{step}] {code} score={score if score is None else round(score, 3)} -> {action}"
                print(line, flush=True)

                if args.probe:
                    print(json.dumps({k: v for k, v in rec.items() if k in
                                      ("answers", "confidence", "cost", "usage")}, ensure_ascii=False, indent=2))
                    break

                if is_animal:
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
                next_reel(page)

            except KeyboardInterrupt:
                print("\nstopped", flush=True)
                break
            except JevError as e:
                stats["errors"] += 1
                consec_errors += 1
                detail = str(e).split(": ", 2)[-1].strip()
                print(f"[{step}] ✗ Jev error (HTTP {e.status}): {detail[:220]}", flush=True)
                if not e.retryable:
                    print("  -> this model is unavailable on this account (not free-tier eligible). "
                          "AI Gateway credits are required.", flush=True)
                if consec_errors >= args.max_errors:
                    print(f"  -> stopping after {consec_errors} consecutive failures", flush=True)
                    break
                time.sleep(2)
                try:
                    next_reel(page)   # advance so we never wedge on one reel
                except Exception:
                    pass
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                consec_errors += 1
                print(f"[{step}] unexpected: {type(e).__name__}: {str(e)[:200]}", flush=True)
                if consec_errors >= args.max_errors:
                    print(f"  -> stopping after {consec_errors} consecutive failures", flush=True)
                    break
                time.sleep(2)
                try:
                    next_reel(page)
                except Exception:
                    pass

        print("=== summary:", json.dumps({**stats, "actions": actions_tally}, ensure_ascii=False), flush=True)
        logger.close()
        if attached:
            print(">>> detaching from the browser (it stays open)", flush=True)
        else:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Judge Instagram Reels and train the feed")
    ap.add_argument("--profile", default=".browser-profile", help="Playwright profile directory (log in once, manually)")
    ap.add_argument("--cdp", default=None, nargs="?", const="auto",
                    help="attach to a running browser over CDP (no login needed). 'auto' discovers "
                         "DevToolsActivePort, or pass ws://127.0.0.1:PORT/devtools/browser/... directly")
    ap.add_argument("--cdp-profile-dir", default=None,
                    help="profile directory to search for DevToolsActivePort (default: Brave/Chrome/Edge)")
    ap.add_argument("--judge", choices=["jev", "local"], default="jev",
                    help="judging engine. jev = Jev on Vercel AI Gateway (paid), local = a VLM under llama.cpp (free, on-device)")
    ap.add_argument("--local-url", default=None,
                    help="local judge server URL (default http://127.0.0.1:8081)")
    ap.add_argument("--steps", type=int, default=0, help="number of reels to process; 0 loops forever (default)")
    ap.add_argument("--targets", choices=["dog,cat", "any"], default="dog,cat",
                    help="what to like; 'any' accepts every animal")
    ap.add_argument("--threshold", type=float, default=0.90, help="probability threshold for a like")
    ap.add_argument("--negative", choices=["skip", "report"], default="skip",
                    help="behaviour for non-target reels; report = Report -> I just don't like it")
    ap.add_argument("--max-reports", type=int, default=30, help="max report-style actions per run")
    ap.add_argument("--max-likes-per-hour", type=int, default=120, help="max likes per hour")
    ap.add_argument("--min-delay", type=float, default=2.5, help="minimum delay before the next reel, in seconds")
    ap.add_argument("--max-delay", type=float, default=7.0, help="maximum delay before the next reel, in seconds")
    ap.add_argument("--max-errors", type=int, default=5, help="stop after this many consecutive judge failures")
    ap.add_argument("--interact", choices=["view-only", "like"], default="view-only",
                    help="how to interact with target reels. view-only = just watch (default), like = like small posts only")
    ap.add_argument("--like-max-comments", type=int, default=50,
                    help="with --interact like, like only when comments are at most this")
    ap.add_argument("--like-max-reposts", type=int, default=50,
                    help="with --interact like, like only when reposts are at most this")
    ap.add_argument("--loops", type=int, default=1, help="extra loops to watch target reels for (stretches watch time)")
    ap.add_argument("--max-dwell", type=float, default=45.0, help="max watch seconds per reel")
    ap.add_argument("--log", default="runs/reels.jsonl", help="JSONL log output path")
    ap.add_argument("--frames-dir", default=None, help="save frame JPEGs when set")
    ap.add_argument("--dry-run", action="store_true", help="dry run: exercise the loop without judging or acting")
    ap.add_argument("--probe", action="store_true", help="judge a single reel and print the details")
    ap.add_argument("--headless", action="store_true", help="headless (not recommended: videos may not decode)")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
