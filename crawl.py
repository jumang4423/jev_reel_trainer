#!/usr/bin/env python3
"""crawl.py — mine target reels via a search engine, grow them through author profiles,
and train the feed by watching to completion.

Where `reel_trainer.py` scrolls the feed you already have, this mode
**builds its own supply without depending on the feed**. It keeps memory (memory/)
so every run can use a different set of themes.

Typical agent flow:
    ./crawl.py --plan                                  # 1. inspect memory (used / unused themes)
    ./crawl.py --add-query "site:instagram.com reel corgi" \
               --add-query "site:instagram.com reel mantis"   # 2. queue new queries
    ./crawl.py --cdp --from-memory                     # 3. run it

No action that affects other people is ever taken (no follow / repost / comment / DM / save).
A like happens only with `--interact like`, and only on **small posts** (comments and
reposts below their thresholds).
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
    sys.exit("Playwright is required: pip install -r requirements.txt")

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

    # ---- judging (with --judge none no model is used at all) ----
    judge_s = 0.0
    score = None
    key = "query"
    measured = False
    watch = True
    reason = "query_assumed"

    if args.judge == "none":
        # The search query already guarantees the topic (12/12 reels measured on target).
        # Only when needed, a keyword gate drops obvious misses (free, no model).
        if args.keyword_gate:
            blob = f"{caption} {' '.join(tags)}".lower()
            kws = [k.strip().lower() for k in args.keyword_gate.split(",") if k.strip()]
            if kws and not any(k in blob for k in kws):
                watch = False
                reason = "keyword_gate_miss"
        # Sampled judging for measurement (this part does need a model)
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
        print(f"--- progress: {stats['animal']}/{stats['reels']} on-target ({rate:.0f}%) ---", flush=True)

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

    # --- memory-only modes ---
    if args.add_query:
        if mem is None:
            sys.exit("--add-query needs memory (cannot be combined with --no-memory)")
        added, skipped = mem.add_pending(args.add_query)
        for q in added:
            print(f"  + queued: {q}")
        for q in skipped:
            print(f"  - skipped, already known: {q}")
        if not args.from_memory and not args.query:
            print(f"pending now holds {len(mem.pending())} queries. "
                  f"Add --from-memory to run them.")
            return 0

    if args.plan:
        if mem is None:
            sys.exit("--plan needs memory")
        print(json.dumps(mem.report(), ensure_ascii=False, indent=2))
        return 0

    if args.judge == "jev" and not os.environ.get("AI_GATEWAY_API_KEY"):
        sys.exit("--judge jev needs AI_GATEWAY_API_KEY (not needed for --judge local)")

    # --- decide which queries to run ---
    if args.query:
        queries = list(args.query)
    elif args.from_memory and mem is not None:
        queries = mem.consume_pending()
        if not queries:
            print(">>> pending is empty. Queue queries with --add-query or pass --query.", flush=True)
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
                print(f"  (skipped, already in history: {q})", flush=True)
        queries = fresh
        if not queries:
            print(">>> every query was already executed. Use --allow-repeat or --add-query.", flush=True)
            return 0

    print(f">>> running {len(queries)} queries", flush=True)

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
            print(f">>> connecting to the running browser ({endpoint.split('/devtools/')[0]}/...)", flush=True)
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
                print(f">>> reached the {args.session_minutes} minute session limit", flush=True)
                break
            if args.max_reels and stats["reels"] >= args.max_reels:
                print(f">>> reached --max-reels {args.max_reels}", flush=True)
                break

            kind, val, origin = queue.popleft()
            try:
                if kind == "search":
                    codes = dc.harvest_search(page, val, args.engine)
                    codes = [c for c in codes if c not in seen_codes and c not in seen_reels_before]
                    print(f"[search] {val!r} -> {len(codes)} new reels", flush=True)
                    stats["per_query"].setdefault(val, {"found": 0, "processed": 0, "animal": 0})["found"] = len(codes)
                    if args.query_limit:
                        codes = codes[: args.query_limit]
                    queue.extend(("reel", c, val) for c in codes)
                elif kind == "profile":
                    codes = dc.profile_reel_codes(page, val, scrolls=args.profile_scrolls)
                    codes = [c for c in codes if c not in seen_codes and c not in seen_reels_before]
                    print(f"[profile] @{val} -> {len(codes)} new reels", flush=True)
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
                print("\nstopped", flush=True)
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
            print(f">>> memory updated: {len(stats['per_query'])} queries / {len(seen_codes)} reels recorded", flush=True)

        if attached:
            print(">>> detaching from the browser (it stays open)", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Mine target reels via a search engine and author profiles, then train by watching")
    ap.add_argument("--cdp", default=None, nargs="?", const="auto", help="attach to the running browser over CDP (recommended)")
    ap.add_argument("--cdp-profile-dir", default=None)
    ap.add_argument("--profile", default=".browser-profile")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--judge", choices=["none", "jev", "local"], default="local",
                    help="none = do not judge (the query guarantees the topic: fastest, no model needed)")
    ap.add_argument("--local-url", default=None)
    ap.add_argument("--keyword-gate", default=None,
                    help="with --judge none, skip reels whose caption/tags lack any of these comma-separated words")
    ap.add_argument("--sample-judge", type=int, default=0,
                    help="with --judge none, judge 1 in N reels to measure the hit rate (needs a model)")

    ap.add_argument("--memory", default="memory", help="memory directory")
    ap.add_argument("--no-memory", action="store_true", help="disable memory (also disables duplicate checks)")
    ap.add_argument("--plan", action="store_true", help="print the memory contents and exit")
    ap.add_argument("--add-query", action="append", default=[], help="queue a query in pending (repeatable)")
    ap.add_argument("--query", action="append", default=[], help="query used only for this run (repeatable)")
    ap.add_argument("--from-memory", action="store_true", help="run the queries queued in pending")
    ap.add_argument("--allow-repeat", action="store_true", help="also run queries that are already in history")
    ap.add_argument("--query-limit", type=int, default=0, help="max reels to take from one query (0 = unlimited)")

    ap.add_argument("--engine", choices=["brave", "google"], default="brave", help="search engine (opened in the real browser)")
    ap.add_argument("--targets", choices=["dog,cat", "any"], default="dog,cat",
                    help="what to watch to completion; use any for a wider mix of animals")
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--interact", choices=["view-only", "like"], default="view-only")
    ap.add_argument("--like-max-comments", type=int, default=50)
    ap.add_argument("--like-max-reposts", type=int, default=50)
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument("--max-dwell", type=float, default=45.0)
    ap.add_argument("--profile-scrolls", type=int, default=3)
    ap.add_argument("--max-profiles", type=int, default=40, help="max author profiles to walk per session")
    ap.add_argument("--max-reels", type=int, default=0, help="max reels to process (0 = unlimited)")
    ap.add_argument("--session-minutes", type=float, default=40.0, help="session time limit in minutes")
    ap.add_argument("--min-delay", type=float, default=2.5)
    ap.add_argument("--max-delay", type=float, default=7.0)
    ap.add_argument("--report-every", type=int, default=20, help="print the hit rate every N reels")
    ap.add_argument("--log", default="runs/crawl.jsonl")
    return ap


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
