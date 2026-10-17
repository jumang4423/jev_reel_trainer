#!/usr/bin/env python3
"""crawl.py — 検索エンジンで対象リールを掘り、投稿者プロフィールで増やし、完走で訓練する。

`reel_trainer.py --mode feed` が「今のフィードを回す」のに対し、こちらは
**フィードに依存せず供給を作る**モード。メモリー（memory/）を持ち、
実行のたびに違うテーマのクエリを使える。

使い方（エージェントの典型フロー）:
    ./crawl.py --plan                                  # 1. メモリーを見る（使ったテーマ・未使用テーマ）
    ./crawl.py --add-query "site:instagram.com reel corgi" \
               --add-query "site:instagram.com reel mantis"   # 2. 新しいクエリを積む
    ./crawl.py --cdp --judge local --from-memory       # 3. 実行（判定はローカル Jev）

他人に影響する操作（フォロー / リポスト / コメント / DM / 保存）は一切しない。
Like は `--interact like` のときだけ、かつ **小さい投稿のみ**（コメント数・リポスト数が閾値以下）。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("Playwright が必要です: pip install -r requirements.txt")

import discover as dc
import reel_trainer as rt
from memory import Memory


def process_reel(page, code: str, origin: str, args, stats: dict, queue: deque,
                 logger, seen_profiles: set) -> dict:
    page.goto(dc.reel_url(code), wait_until="domcontentloaded")
    page.bring_to_front()
    page.wait_for_timeout(1200)
    rt.nudge_play(page)
    if not rt.wait_visible_video(page, 12000):
        return {"code": code, "action": "no_video", "origin": origin}

    text = rt.reel_text(page)
    author, caption = dc.split_author_and_caption(text)
    tags = dc.hashtags_of(text)

    # ---- 判定（--judge none ならモデルを一切使わない）----
    judge_s = 0.0
    score = None
    key = "query"
    measured = False
    watch = True
    reason = "query_assumed"

    if args.judge == "none":
        # 検索クエリで内容はほぼ保証されている（実測 12本すべて 100% 対象）。
        # 必要なときだけキーワードゲートで「明らかな外れ」を弾く（無料・モデル不要）。
        if args.keyword_gate:
            blob = f"{caption} {' '.join(tags)}".lower()
            kws = [k.strip().lower() for k in args.keyword_gate.split(",") if k.strip()]
            if kws and not any(k in blob for k in kws):
                watch = False
                reason = "keyword_gate_miss"
        # 測定用のサンプリング判定（これだけはモデルが要る）
        if watch and args.sample_judge and stats["reels"] % args.sample_judge == 0:
            frame = rt.capture_frame(page, None)
            if frame is not None:
                t0 = time.time()
                try:
                    score, key, _, _ = rt.judge(
                        os.environ.get("AI_GATEWAY_API_KEY", ""), rt.DEFAULT_MODEL, text, frame[1],
                        rt.build_questions(args.targets), args.targets, "local", args.local_url)
                    judge_s = time.time() - t0
                    measured = True
                    reason = f"sampled {key}={score:.2f}"
                except Exception as e:  # noqa: BLE001
                    reason = f"sample_judge_failed: {type(e).__name__}"
    else:
        frame = rt.capture_frame(page, None)
        if frame is None:
            return {"code": code, "action": "no_frame", "origin": origin}
        t0 = time.time()
        score, key, _, _ = rt.judge(
            os.environ.get("AI_GATEWAY_API_KEY", ""), rt.DEFAULT_MODEL, text, frame[1],
            rt.build_questions(args.targets), args.targets, args.judge, args.local_url)
        judge_s = time.time() - t0
        measured = True
        watch = score >= args.threshold
        reason = f"{key}={score:.2f}"

    dwell_s = 0.0
    action = "not_watched"
    if watch:
        dwell_s = rt.dwell_until_end(page, loops=args.loops, cap=args.max_dwell)
        action = rt.maybe_like(page, args)
        stats["animal"] += 1
        if origin:
            stats["per_query"].setdefault(origin, {"processed": 0, "animal": 0})["animal"] += 1
        if author and author not in seen_profiles and len(seen_profiles) < args.max_profiles:
            seen_profiles.add(author)
            queue.append(("profile", author, origin))
    else:
        stats["non_animal"] += 1

    stats["reels"] += 1
    if origin:
        stats["per_query"].setdefault(origin, {"processed": 0, "animal": 0})["processed"] += 1
    if stats["reels"] % args.report_every == 0:
        rate = stats["animal"] / max(1, stats["reels"]) * 100
        print(f"--- 進捗: {stats['reels']}本中 動物 {stats['animal']}本（{rate:.0f}%）---", flush=True)

    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "code": code, "origin": origin, "author": author,
        "watched": watch, "measured": measured, "reason": reason,
        "judge": args.judge, "score": score, "score_key": key,
        "judge_s": round(judge_s, 2), "dwell_s": round(dwell_s, 2), "action": action,
        "hashtags": tags[:15], "caption": caption[:160],
        "engagement": rt.engagement(page) if watch else None,
    }
    if logger:
        logger.write(rec)
    tag = f"{key}={score:.2f}" if measured else args.judge
    print(f"  {code} {tag:14s} dwell={dwell_s:5.1f}s {action:26s} author={author or '-'}", flush=True)
    return rec


def run(args: argparse.Namespace) -> int:
    mem = None if args.no_memory else Memory(Path(args.memory).expanduser().resolve())

    # --- メモリー操作だけのモード ---
    if args.add_query:
        if mem is None:
            sys.exit("--add-query はメモリーが必要です（--no-memory と併用不可）")
        added, skipped = mem.add_pending(args.add_query)
        for q in added:
            print(f"  + 追加: {q}")
        for q in skipped:
            print(f"  - 既にあるのでスキップ: {q}")
        if not args.from_memory and not args.query:
            print(f"pending は {len(mem.pending())} 件になりました。"
                  f"実行するには --from-memory を付けてください。")
            return 0

    if args.plan:
        if mem is None:
            sys.exit("--plan はメモリーが必要です")
        print(json.dumps(mem.report(), ensure_ascii=False, indent=2))
        return 0

    if args.judge == "jev" and not os.environ.get("AI_GATEWAY_API_KEY"):
        sys.exit("--judge jev には AI_GATEWAY_API_KEY が必要です（--judge local なら不要）")

    # --- 実行するクエリを決める ---
    if args.query:
        queries = list(args.query)
    elif args.from_memory and mem is not None:
        queries = mem.consume_pending()
        if not queries:
            print(">>> pending が空です。--add-query で積むか --query で直接指定してください。", flush=True)
            return 0
    elif mem is not None and mem.pending():
        queries = mem.consume_pending()
    else:
        queries = list(dc.DEFAULT_QUERIES)

    if mem is not None and not args.allow_repeat:
        used = mem.used_queries()
        fresh = [q for q in queries if q not in used]
        for q in queries:
            if q in used:
                print(f"  (履歴にあるのでスキップ: {q})", flush=True)
        queries = fresh
        if not queries:
            print(">>> すべて実行済みのクエリでした。--allow-repeat か --add-query を使ってください。", flush=True)
            return 0

    print(f">>> クエリ {len(queries)} 件を実行します", flush=True)

    logger = rt.Logger(Path(args.log).expanduser().resolve())
    stats: dict = {"reels": 0, "animal": 0, "non_animal": 0,
                   "per_query": {q: {"found": 0, "processed": 0, "animal": 0} for q in queries}}
    seen_codes: set[str] = set()
    seen_reels_before = mem.seen_reels() if mem else set()
    seen_profiles: set[str] = set()
    queue: deque = deque(("search", q, q) for q in queries)
    started = time.time()

    with sync_playwright() as p:
        attached = False
        if args.cdp is not None:
            endpoint = rt.resolve_cdp_endpoint(args.cdp, args.cdp_profile_dir)
            print(f">>> 既存ブラウザへ接続（{endpoint.split('/devtools/')[0]}/...）", flush=True)
            browser = p.chromium.connect_over_cdp(endpoint, timeout=180000)
            ctx = browser.contexts[0]
            page = ctx.new_page()
            attached = True
        else:
            ctx = rt.launch_context(p, Path(args.profile).expanduser().resolve(), args.headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.bring_to_front()
        rt.ensure_login(page)
        print(f"=== crawl start (judge={args.judge}, engine={args.engine}, "
              f"interact={args.interact}, threshold={args.threshold}) ===", flush=True)

        while queue:
            if args.session_minutes and (time.time() - started) / 60 >= args.session_minutes:
                print(f">>> セッション上限 {args.session_minutes} 分に到達", flush=True)
                break
            if args.max_reels and stats["reels"] >= args.max_reels:
                print(f">>> --max-reels {args.max_reels} に到達", flush=True)
                break

            kind, val, origin = queue.popleft()
            try:
                if kind == "search":
                    codes = dc.harvest_search(page, val, args.engine)
                    codes = [c for c in codes if c not in seen_codes and c not in seen_reels_before]
                    print(f"[search] {val!r} -> {len(codes)} 件（未処理のみ）", flush=True)
                    stats["per_query"].setdefault(val, {"found": 0, "processed": 0, "animal": 0})["found"] = len(codes)
                    if args.query_limit:
                        codes = codes[: args.query_limit]
                    queue.extend(("reel", c, val) for c in codes)
                elif kind == "profile":
                    codes = dc.profile_reel_codes(page, val, scrolls=args.profile_scrolls)
                    codes = [c for c in codes if c not in seen_codes and c not in seen_reels_before]
                    print(f"[profile] @{val} -> {len(codes)} 件（未処理のみ）", flush=True)
                    if args.query_limit:
                        codes = codes[: args.query_limit]
                    queue.extend(("reel", c, origin) for c in codes)
                else:
                    if val in seen_codes or val in seen_reels_before:
                        continue
                    seen_codes.add(val)
                    process_reel(page, val, origin, args, stats, queue, logger, seen_profiles)
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
            except KeyboardInterrupt:
                print("\n停止しました", flush=True)
                break
            except Exception as e:  # noqa: BLE001
                print(f"  ! {kind} {val}: {type(e).__name__}: {str(e)[:140]}", flush=True)
                time.sleep(2)

        rate = stats["animal"] / max(1, stats["reels"]) * 100
        summary = {
            **{k: v for k, v in stats.items() if k != "per_query"},
            "animal_rate_pct": round(rate, 1),
            "profiles_queued": len(seen_profiles),
            "minutes": round((time.time() - started) / 60, 1),
        }
        print("=== summary:", json.dumps(summary, ensure_ascii=False), flush=True)
        logger.close()

        if mem is not None:
            for q, s in stats["per_query"].items():
                if s.get("found") or s.get("processed"):
                    mem.record_query(q, args.engine, s.get("found", 0),
                                     s.get("processed", 0), s.get("animal", 0))
            mem.mark_reels(sorted(seen_codes))
            mem.record_run(summary, queries)
            print(f">>> メモリー更新: クエリ {len(stats['per_query'])} 件 / リール {len(seen_codes)} 件を記録", flush=True)

        if attached:
            print(">>> 既存ブラウザから切断します（ブラウザは閉じません）", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="検索エンジン→プロフィール巡回で対象リールを掘って訓練する")
    ap.add_argument("--cdp", default=None, nargs="?", const="auto", help="既存ブラウザへ CDP 接続（推奨）")
    ap.add_argument("--cdp-profile-dir", default=None)
    ap.add_argument("--profile", default=".browser-profile")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--judge", choices=["none", "jev", "local"], default="local",
                    help="none=判定しない（検索クエリで内容が保証されるので最速・モデル不要）")
    ap.add_argument("--local-url", default=None)
    ap.add_argument("--keyword-gate", default=None,
                    help="--judge none のとき、キャプション/タグにこの語（カンマ区切り）が無ければ見ない")
    ap.add_argument("--sample-judge", type=int, default=0,
                    help="--judge none でも N 本に 1 本だけ判定して動物率を測る（モデルが必要）")

    ap.add_argument("--memory", default="memory", help="メモリーのディレクトリ")
    ap.add_argument("--no-memory", action="store_true", help="メモリーを使わない（重複チェックも無し）")
    ap.add_argument("--plan", action="store_true", help="メモリーの内容を表示して終了")
    ap.add_argument("--add-query", action="append", default=[], help="クエリを pending に積む（複数指定可）")
    ap.add_argument("--query", action="append", default=[], help="この実行だけで使うクエリ（複数指定可）")
    ap.add_argument("--from-memory", action="store_true", help="pending のクエリを実行する")
    ap.add_argument("--allow-repeat", action="store_true", help="履歴にあるクエリも実行する")
    ap.add_argument("--query-limit", type=int, default=0, help="1 クエリから拾うリール数の上限（0=無制限）")

    ap.add_argument("--engine", choices=["brave", "google"], default="brave", help="検索エンジン（実ブラウザで開く）")
    ap.add_argument("--targets", choices=["dog,cat", "any"], default="dog,cat",
                    help="完走させる対象。多様な動物を流したいなら any")
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--interact", choices=["view-only", "like"], default="view-only")
    ap.add_argument("--like-max-comments", type=int, default=50)
    ap.add_argument("--like-max-reposts", type=int, default=50)
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument("--max-dwell", type=float, default=45.0)
    ap.add_argument("--profile-scrolls", type=int, default=3)
    ap.add_argument("--max-profiles", type=int, default=40, help="1 セッションで巡回する投稿者の上限")
    ap.add_argument("--max-reels", type=int, default=0, help="処理するリール上限（0=無制限）")
    ap.add_argument("--session-minutes", type=float, default=40.0, help="セッションの上限時間（分）")
    ap.add_argument("--min-delay", type=float, default=2.5)
    ap.add_argument("--max-delay", type=float, default=7.0)
    ap.add_argument("--report-every", type=int, default=20, help="何本ごとにヒット率を表示するか")
    ap.add_argument("--log", default="runs/crawl.jsonl")
    return ap


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
