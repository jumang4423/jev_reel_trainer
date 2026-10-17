#!/usr/bin/env python3
"""jev_reel_trainer — Instagram リールを無限スクロールし、各リールのフレームを
TypeSafe Jev（Vercel AI Gateway）に判定させ、対象（既定は犬・猫）なら Like、
それ以外はスキップ（任意で「I just don't like it」）する。

この実装は実機検証（あなたの Brave を Playwright で操作）で判明した
Instagram Web リールのクセを織り込んでいる:

  1. 背景タブだと <video> がデコードされない
     -> page.bring_to_front() が必須。ページ全体のスクショはグレー一色になる。
  2. 動画要素をクリックするとパーマリンク（/reels/<code>/）に遷移し、
     `...` メニューが「投稿メニュー」に変わってしまう
     -> 動画は絶対にクリックしない。前面化＋ミュート play() でデコードさせる。
  3. 要素スクショより canvas へ drawImage したほうがオーバーレイ（再生ボタン）が乗らない
     -> フレーム取得は canvas -> dataURL。
  4. Web 版リールの `...` には "Not interested" が無い
     （Report / Go to post / Share to... / Copy link / Embed / About this account のみ）
     -> 準ネガティブは Report -> "I just don't like it"（既定はオフ、--negative report）。
  5. Like 済みは aria-label が "Unlike" に変わる
     -> 二重押し防止と操作の検証にこれを使う。

注意: 自動操作は Instagram の利用規約に触れる可能性がある。自己責任で、かつ
レート制限（--min-delay / --max-delay / --max-likes-per-hour）を守って使うこと。
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
    sys.exit("Pillow が必要です: pip install -r requirements.txt")

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("Playwright が必要です: pip install -r requirements.txt && python -m playwright install")

from jev_client import DEFAULT_MODEL, JevError, decide_with_images, parse_answers, summarize_usage

try:  # Pillow 10 以降でも古くても動くように
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover
    RESAMPLE = Image.LANCZOS

# --------------------------------------------------------------------------
# Jev の画像制限（TypeSafe docs）
#   PNG/JPEG のみ / 1ファイル 16MiB / 各辺 280〜1568px /
#   総画素数 78,400〜2,458,624 / リクエスト全体 32MiB
# --------------------------------------------------------------------------
MAX_SIDE = 1568
MIN_SIDE = 280
MAX_PIXELS = 2_458_624

BROWSER_CANDIDATES = [
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

# 既存ブラウザのプロファイル（DevToolsActivePort を探す場所）。
# Brave/Chrome/Edge ≧136 では既定プロファイルに --remote-debugging-port が効かないため、
# brave://inspect のトグルで有効化した「認可付きリモートデバッグ」を使う。
DEFAULT_PROFILE_DIRS = [
    "~/Library/Application Support/BraveSoftware/Brave-Browser",
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Application Support/Microsoft Edge",
]

# --------------------------------------------------------------------------
# ページ内で実行する JS（文字列で持つ。f-string と波括弧が衝突しないように）
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

# 現在ビューポートに出ているリールのフレームを canvas 経由で取り出す
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

# 現在のリールの作者名・キャプション。
# 注: IG のリールはキャプションが <a> でも動画の祖先でもなく、オーバーレイの
#     div/span として描画される。ビューポート内で「短い高さのまま最も長い
#     テキストを持つコンテナ」を選ぶと作者＋キャプションが取れる（実測で確認）。
JS_REEL_TEXT = """() => {
  const H = window.innerHeight;
  let best = '';
  document.querySelectorAll('div, span, h1').forEach((el) => {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t.length < 12 || t.length > 400 || t.length <= best.length) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.top < 0 || r.bottom > H) return;
    if (r.height > 300 || r.width > 900) return;   // フィード全体を掴まない
    best = t;
  });
  return best.slice(0, 400);
}"""

# ビューポート内にあるボタン（svg[aria-label=...]）の中心座標を返す
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

# 現在表示中のリール動画の長さ・再生位置（完走判定に使う）
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

# アクションバーの数値（コメント数・リポスト数）。
# 注: IG のリールは「いいね数」を表示しない。表示されるのは comment と repost のみ。
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
# 画像の正規化（Jev の制限に収める）
# --------------------------------------------------------------------------
def normalize_for_jev(img: "Image.Image") -> "Image.Image":
    w, h = img.size
    if max(w, h) > MAX_SIDE:  # 長辺を 1568 に
        s = MAX_SIDE / max(w, h)
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), RESAMPLE)
        w, h = img.size
    if min(w, h) < MIN_SIDE:  # 短辺を 280 以上に
        s = MIN_SIDE / min(w, h)
        img = img.resize((int(w * s + 0.5), int(h * s + 0.5)), RESAMPLE)
        w, h = img.size
    if w * h > MAX_PIXELS:  # 画素数上限
        s = (MAX_PIXELS / (w * h)) ** 0.5
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), RESAMPLE)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


# --------------------------------------------------------------------------
# ブラウザ
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
        "--autoplay-policy=no-user-gesture-required",  # ミュート自動再生を許可
        "--disable-blink-features=AutomationControlled",  # 自動化検知を少し避ける
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
        "[warn] Brave/Chrome が見つからないため同梱 Chromium を使います。"
        "H.264 を再生できずフレームが取れない可能性があります。",
        file=sys.stderr,
    )
    return playwright.chromium.launch_persistent_context(**kw)


def resolve_cdp_endpoint(explicit: str, profile_dir: str | None) -> str:
    """CDP のエンドポイントを決める。

    - 明示 URL（``http://`` / ``ws://``）ならそのまま使う
    - ``auto`` なら ``DevToolsActivePort`` を探して
      ``ws://127.0.0.1:<port><path>`` を組み立てる。

    Brave/Chrome/Edge ≧136 では既定プロファイルに ``--remote-debugging-port`` が
    効かない。代わりに ``brave://inspect/#remote-debugging`` のトグルを ON にすると
    Brave がプロファイル直下に ``DevToolsActivePort`` を書き、認可付きで待ち受ける
    （HTTP の /json/version は 404 になるが、ws:// は使える）。
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
        "DevToolsActivePort が見つかりません。\n"
        "Brave のアドレスバーで brave://inspect/#remote-debugging を開き、\n"
        "「Allow remote debugging」を ON にしてください（Local State に保存され、再起動後も有効）。"
    )


def ensure_login(page, timeout_s: int = 600) -> None:
    """ログイン済みになるまで待つ。未ログインなら人間にログインしてもらう。"""
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    asked = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.locator('a[href="/reels/"]').count() > 0:
            return
        if not asked:
            print(">>> Brave の Instagram でログインしてください（完了まで待機します）", flush=True)
            asked = True
        time.sleep(3)
    raise SystemExit("ログインを確認できませんでした")


# --------------------------------------------------------------------------
# リール操作
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
    """ほぼ真っ黒（＝デコード前）のフレームを弾くための指標。

    グレースケールのヒストグラムで「最小輝度と最大輝度の差」を返す。
    動画のメタデータだけ読めて実フレームが未デコードだと 0 に近くなる。
    """
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    hist = img.histogram()
    lo = next((i for i, c in enumerate(hist) if c), 255)
    hi = next((i for i in range(255, -1, -1) if hist[i]), 0)
    return int(hi - lo)


def capture_frame(page, out_dir: Path | None, tries: int = 4, min_range: int = 16) -> tuple[str, str] | None:
    """(保存パス or '', base64 JPEG) を返す。フレームが取れなければ None。

    再生直後は readyState が 4 でも実フレームが未デコードで真っ黒になることがあるため、
    非単調なフレームが得られるまで短い間隔で撮り直す。
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
    if has_visible_label(page, "Unlike"):  # 既に Like 済み
        return "already_liked"
    btn = find_visible_button(page, "Like")
    if not btn:
        return "like_button_not_found"
    page.mouse.click(btn["x"], btn["y"])
    page.wait_for_timeout(900)
    return "liked" if has_visible_label(page, "Unlike") else "like_failed"


def do_not_interested(page) -> str:
    """Report -> 「I just don't like it」。Web リールには Not interested が無いため。"""
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
    # ネガティブ信号を一切送らない場合。滞在時間を短くするだけ。
    return "skipped"


def _to_int(t: str | None) -> int | None:
    """'44' / '1.2K' / '3M' を整数にする。"""
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
    """表示中のリールのコメント数・リポスト数（いいね数は IG が出さない）。"""
    try:
        raw = page.evaluate(JS_ENGAGEMENT) or {}
    except Exception:
        return {"comments": None, "reposts": None}
    return {"comments": _to_int(raw.get("comments")), "reposts": _to_int(raw.get("reposts"))}


def dwell_until_end(page, loops: int = 1, cap: float = 45.0) -> float:
    """動画を最後まで見る（＋ループ）。視聴時間は最も強いポジティブ信号で、他人に通知されない。

    Returns: 実際に待った秒数
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
        dur = 8.0  # 不明なら控えめに
    total = min(dur, cap) * (1 + max(0, loops))
    page.wait_for_timeout(int(total * 1000))
    return total


def maybe_like(page, args) -> str:
    """「小さい投稿だけ Like」ポリシー。

    IG のリールは「いいね数」を表示しないので、コメント数とリポスト数で判定する。
    両方が閾値以下のときだけ Like する（＝目立たない投稿にだけ反応する）。
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
# 判定
# --------------------------------------------------------------------------
def build_questions(targets: str) -> list[dict]:
    qs = [
        {
            "type": "predicate",
            "name": "is_dog",
            "instructions": "画像にはっきりと犬が写っていますか？（ぬいぐるみ・イラスト・CG は false）",
        },
        {
            "type": "predicate",
            "name": "is_cat",
            "instructions": "画像にはっきりと猫が写っていますか？（ぬいぐるみ・イラスト・CG は false）",
        },
    ]
    if targets == "any":
        qs.append(
            {
                "type": "predicate",
                "name": "is_animal",
                "instructions": "画像の主役は実在の動物ですか？（人物・物・風景・CG キャラは false）",
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
    """判定本体。backend=jev は Vercel AI Gateway、backend=local は llama.cpp。"""
    if backend == "local":
        import local_judge  # 遅延 import（ローカルを使わない場合は不要）

        return local_judge.decide_local(
            local_url or local_judge.DEFAULT_BASE_URL, image_b64, text or "", targets
        )

    prompt = (
        "Instagram のリール動画から抜き出した 1 フレームです。"
        "キャプション等のテキストも添えます。質問に確率で答えてください。\n"
        f"--- テキスト情報 ---\n{text or '(なし)'}"
    )
    resp = decide_with_images(api_key, prompt, [image_b64], questions, model=model)
    answers = parse_answers(resp)
    score, key = score_of(answers, targets)
    return score, key, answers, resp


# --------------------------------------------------------------------------
# ログ
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
# メインループ
# --------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("AI_GATEWAY_API_KEY")
    if args.judge == "jev" and not api_key and not args.dry_run:
        sys.exit("AI_GATEWAY_API_KEY を環境変数に設定してください（--judge local なら不要）")

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
                ">>> 既存ブラウザへ CDP 接続します。Brave に承認ダイアログが出たら許可してください"
                f"（{endpoint.split('/devtools/')[0]}/...）",
                flush=True,
            )
            browser = p.chromium.connect_over_cdp(endpoint, timeout=180000)
            ctx = browser.contexts[0]  # 既存プロファイルの Cookie を持つ既定コンテキスト
            page = ctx.new_page()  # あなたの開いているタブは触らない
            attached = True
        else:
            ctx = launch_context(p, profile_dir, args.headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()  # ← クセ1: これが無いと動画がデコードされない

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
                    # フレームが取れない（デコード失敗）→ 再生を促してもう一度
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
                        # ★ 最強のポジティブ信号：最後まで見る（＋ループ）。
                        #    匿名・通知なし・自分のリストを汚さない。
                        dwell_s = dwell_until_end(page, loops=args.loops, cap=args.max_dwell)
                        now = time.time()
                        like_times = [t for t in like_times if now - t < 3600]
                        if len(like_times) >= args.max_likes_per_hour:
                            action = "like_rate_limited"
                        else:
                            action = maybe_like(page, args)
                            if action == "liked":
                                like_times.append(now)
                        print(f"[{step}] 完走 {dwell_s:.1f}s -> {action}", flush=True)
                    else:
                        # ★ 2秒未満で切る（Meta の system card が予測対象にしているネガティブ信号）
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
                print("\n停止しました", flush=True)
                break
            except JevError as e:
                stats["errors"] += 1
                consec_errors += 1
                detail = str(e).split(": ", 2)[-1].strip()
                print(f"[{step}] ✗ Jev error (HTTP {e.status}): {detail[:220]}", flush=True)
                if not e.retryable:
                    print("  → このモデルはこのアカウントでは使えません（無料枠の対象外）。"
                          "AI Gateway のクレジット購入が必要です。", flush=True)
                if consec_errors >= args.max_errors:
                    print(f"  → 連続 {consec_errors} 回失敗したため停止します。", flush=True)
                    break
                time.sleep(2)
                try:
                    next_reel(page)   # 同じリールで固まらないように進める
                except Exception:
                    pass
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                consec_errors += 1
                print(f"[{step}] unexpected: {type(e).__name__}: {str(e)[:200]}", flush=True)
                if consec_errors >= args.max_errors:
                    print(f"  → 連続 {consec_errors} 回失敗したため停止します。", flush=True)
                    break
                time.sleep(2)
                try:
                    next_reel(page)
                except Exception:
                    pass

        print("=== summary:", json.dumps({**stats, "actions": actions_tally}, ensure_ascii=False), flush=True)
        logger.close()
        if attached:
            print(">>> 既存ブラウザから切断します（ブラウザは閉じません）", flush=True)
        else:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Instagram リールを Jev で判定してフィードを訓練する")
    ap.add_argument("--profile", default=".browser-profile", help="Playwright 用プロファイル（初回だけ手動ログイン）")
    ap.add_argument("--cdp", default=None, nargs="?", const="auto",
                    help="既存ブラウザに CDP 接続（ログイン不要）。'auto' で DevToolsActivePort を自動検出、"
                         "または ws://127.0.0.1:PORT/devtools/browser/... を直接指定")
    ap.add_argument("--cdp-profile-dir", default=None,
                    help="DevToolsActivePort を探すプロファイルディレクトリ（既定: Brave/Chrome/Edge）")
    ap.add_argument("--judge", choices=["jev", "local"], default="jev",
                    help="判定エンジン。jev=Vercel AI Gateway の Jev（有料）、local=llama.cpp の VLM（無料・ローカル）")
    ap.add_argument("--local-url", default=None,
                    help="ローカル判定サーバの URL（既定 http://127.0.0.1:8081）")
    ap.add_argument("--steps", type=int, default=0, help="処理するリール数。0 で無限ループ（既定）")
    ap.add_argument("--targets", choices=["dog,cat", "any"], default="dog,cat",
                    help="Like する対象。'any' で全動物")
    ap.add_argument("--threshold", type=float, default=0.90, help="Like を押す確率しきい値")
    ap.add_argument("--negative", choices=["skip", "report"], default="skip",
                    help="対象外のときの挙動。report = Report -> I just don't like it")
    ap.add_argument("--max-reports", type=int, default=30, help="1 実行あたりの report 系操作の上限")
    ap.add_argument("--max-likes-per-hour", type=int, default=120, help="1 時間あたりの Like 上限")
    ap.add_argument("--min-delay", type=float, default=2.5, help="次リールまでの最小待機秒")
    ap.add_argument("--max-delay", type=float, default=7.0, help="次リールまでの最大待機秒")
    ap.add_argument("--max-errors", type=int, default=5, help="連続で Jev 呼び出しに失敗したら停止する回数")
    ap.add_argument("--interact", choices=["view-only", "like"], default="view-only",
                    help="対象リールへの介入。view-only=視聴だけ（既定）、like=小さい投稿だけ Like")
    ap.add_argument("--like-max-comments", type=int, default=50,
                    help="--interact like のとき、コメント数がこれ以下なら Like 対象")
    ap.add_argument("--like-max-reposts", type=int, default=50,
                    help="--interact like のとき、リポスト数がこれ以下なら Like 対象")
    ap.add_argument("--loops", type=int, default=1, help="対象リールを何周余分に見るか（視聴時間を伸ばす）")
    ap.add_argument("--max-dwell", type=float, default=45.0, help="1 本あたりの最大視聴秒")
    ap.add_argument("--log", default="runs/reels.jsonl", help="JSONL ログの出力先")
    ap.add_argument("--frames-dir", default=None, help="指定するとフレーム JPEG を保存する")
    ap.add_argument("--dry-run", action="store_true", help="判定も操作もせずループの挙動だけ確認")
    ap.add_argument("--probe", action="store_true", help="1 本だけ判定して結果を詳細表示")
    ap.add_argument("--headless", action="store_true", help="ヘッドレス（非推奨: 動画がデコードされないことがある）")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
