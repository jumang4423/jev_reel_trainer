---
name: jev-reel-trainer
description: Train an Instagram Reels feed toward chosen content (dogs, cats, other animals) without following, reposting, commenting, DM-ing or saving — using a local vision model as a Jev-style decision engine, or no judge at all when a search query already guarantees the topic. Use when the user asks to make their Reels show more of a kind of video, to fill a feed with animals/dogs/cats, to crawl or seed Instagram for reels, to judge reel frames locally (llama.cpp + Qwen3-VL), to run the jev_reel_trainer crawler on their real logged-in Brave, or to rotate varied search themes (puppy → speedy cat → weird bugs) so each run surfaces different content. Also handles the Jev-compatible cloud path (Vercel AI Gateway / TypeSafe Jev) when paid credits exist.
---

# jev-reel-trainer

Watch Reels **on the user's behalf** to steer their feed toward chosen content (dogs, cats,
other animals).

Judging is optional: a search query like `site:instagram.com reel puppy` already guarantees the
topic (measured 12/12 on-target), so `--judge none` is the fastest, dependency-free path. Use a
judge only for the feed loop (mixed content) and for measurement.

Repository: `~/PROJECTS/hacking/jev_reel_trainer`

## Core principles — breaking these defeats the point

1. **Never perform an action that affects other people.** No follow, repost, comment, DM or
   share. They notify someone or land on someone else's screen.
2. **Never save.** It fills the user's saved collection.
3. **Like only "small" posts** (`--interact like`). IG shows no like count on reels, so the gate
   uses **comment and repost counts** (default: both ≤ 50).
4. **Watch time is the strongest signal.** Mosseri ranks it #1, and it is anonymous, sends no
   notification and pollutes no list. So: watch targets to the end, cut non-targets within two
   seconds.
5. **No follows means no structural supply.** Instead, **harvest real reel URLs from a search
   engine** and **walk the authors' profiles** — the only entry points that actually work.

## Prerequisites

```bash
cd ~/PROJECTS/hacking/jev_reel_trainer
```

- Browser: attach to the user's real Brave over CDP (`--cdp`). They must enable
  `brave://inspect/#remote-debugging` → **Allow remote debugging** once. Each connection may show
  an approval prompt in Brave.
- Judge `local`: `./serve_local.sh` in another terminal, plus a downloaded Qwen3-VL-4B GGUF
  (~3.1 GB, Apache-2.0, not committed).
- Judge `jev`: `AI_GATEWAY_API_KEY` **and paid credits** (Jev is not free-tier eligible).

## The agent's job: invent a different query set every run

Memory lives in `memory/`, so repeating the same searches is a mistake.

```bash
# 1. inspect memory (used themes, unused themes, pending, processed reels)
./.venv/bin/python crawl.py --plan

# 2. pick 3-6 unused themes and queue queries, spreading across categories
./.venv/bin/python crawl.py \
  --add-query "site:instagram.com reel corgi" \
  --add-query "site:instagram.com reel jumping spider" \
  --add-query "site:instagram.com reel axolotl"

# 3. run it
./.venv/bin/python crawl.py --cdp --judge none --interact like --session-minutes 20
```

`--plan` lists `unused_themes` from a pool of ~55 spread over dogs / cats / small pets / birds /
wildlife / insects / aquatic & reptiles / farm & large / cross-cutting. Choosing across
categories is what produces "puppy → speedy cat → weird bugs".

For a wider mix of animals use `--targets any` (default `dog,cat`).

## Memory layout

| File | Purpose |
|---|---|
| `memory/queries.jsonl` | executed queries with yield (`found` / `processed` / `animal_rate_pct`) |
| `memory/pending.txt` | queries queued for the next run (`--add-query` writes, `--from-memory` consumes) |
| `memory/seen_reels.json` | processed reel shortcodes — **prevents duplicates across runs** |
| `memory/runs.jsonl` | per-run summaries |

Queries already in history are skipped by default (`--allow-repeat` overrides). `memory/` is
gitignored.

## Modes

| Command | Use |
|---|---|
| `crawl.py --cdp --judge none` | **build supply without depending on the feed (recommended)** |
| `crawl.py --cdp --judge local --sample-judge 10` | same, plus a 1-in-10 accuracy sample |
| `reel_trainer.py --cdp --judge local` | scroll the current feed, train it and measure it |
| `local_judge.py --image f.jpg --context "..."` | judge a single image and print probabilities |

Measure by reading `is_animal` / `watched` in `runs/crawl.jsonl` and `runs/reels.jsonl`. If the
feed's on-target rate climbs, it is working.

## Instagram web quirks you will hit (verified on a real profile)

1. **A background tab never decodes `<video>`.** Full-page screenshots are solid grey;
   `page.bring_to_front()` is mandatory.
2. **Clicking the video element navigates to the permalink** and turns the `...` menu into the
   post menu. Never click the video — bring the tab forward and muted-play instead.
3. **Tag pages are broken on the web.** `/explore/tags/<tag>/` rewrites to
   `/explore/search/keyword/?q=...` ("Something went wrong"), clicked or direct. No hashtag crawl.
4. **Plain-HTTP search harvesting fails.** DuckDuckGo rate-limits almost immediately, Bing ignores
   `site:`, Reddit returns 403. The real browser is the only reliable route.
5. **Keyword search returns accounts, not content** ("puppy" → DJs and people). Use
   `site:instagram.com reel <keyword>` instead.
6. **An already-liked reel flips `aria-label` to `Unlike`** — use it to avoid double taps and to
   verify the action landed.
7. **The web reel `...` menu has no "Not interested"** (only Report / Go to post / Share to... /
   Copy link / Embed / About this account). The nearest negative is Report → "I just don't like
   it" (off by default).
8. **`Your Algorithm` and `Reset suggested content` are mobile-only** (absent from the web UI).
   They are free, harmless and the strongest levers — worth asking the user for three taps.

## Measured numbers (the basis for this skill)

| Path | On-target rate |
|---|---|
| Reel feed (tech-heavy account) | 0–2% |
| **Search-engine seeds** | **100% (12/12)** |
| Local judge, dog reel | `dog = 1.00` |
| Local judge, katydid | `animal = 0.9999` |
| Local judge, people / PCB | `none = 0.9999` |
| Local judge latency | 4.6–7.6 s on an M1 Pro (Metal) — essentially all *prefill*, generation is 0 ms |

## Caveats — state these honestly

- **Jev is not free-tier eligible.** The Hobby $5 credit does not cover `typesafe-ai/jev*`
  (`403 no_providers_available`). No free-tier Gateway model supports vision.
- **The browser tab is brought to the front** while running, so run it when the user is not using
  the browser.
- **Keep search-engine queries to 3–6 per session** and cap profile walks with `--max-profiles`.
- **Nothing is guaranteed.** Without follows only watch time and occasional likes are sent, so
  expect days to weeks of sessions. Judge by the `animal_rate_pct` trend, never by one run.
- Automation may violate Instagram's terms. Use at your own risk and respect the rate limits.
