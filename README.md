# jev_reel_trainer

Train an Instagram Reels feed toward chosen content (dogs, cats, other animals) **without
following, reposting, commenting, DM-ing or saving** — by watching reels on your behalf and
judging each frame with a Jev-style decision engine.

Two judging engines are available:

| `--judge` | What it is | Cost |
|---|---|---|
| `none` | No judging at all. The search query already guarantees the topic. | free, no model, fastest |
| `local` | A small VLM (Qwen3-VL-4B) under llama.cpp, read out as typed probabilities | free, on-device |
| `jev` | TypeSafe Jev on Vercel AI Gateway | paid (not free-tier eligible) |

## Why this exists

Passive scrolling cannot create a new interest. Reels are sourced from *accounts you follow*
plus *reels similar to ones you recently engaged with*, so a tech-only feed never surfaces a dog.
Worse, a naive loop that advances after a fixed delay teaches "low interest" on everything.

This tool attacks that in two ways:

1. **Builds its own supply** — harvests real reel URLs through a search engine, watches them,
   then walks the authors' profiles to grow the pool (`crawl.py`).
2. **Sends only private signals** — watch time is Mosseri's #1 ranking signal and it notifies
   nobody, saves nothing and pollutes no list. Non-targets are cut in under two seconds, which
   Meta's own Reels system card explicitly models as a negative signal.

## Layout

```
SKILL.md             agent-facing skill definition (run this repo as a skill)
reel_trainer.py      the feed loop (watch-to-completion / instant skip, small-post-only likes)
crawl.py             feed-independent supply building (search -> watch -> author profiles)
discover.py          seed harvesting via a search engine + author profile walking
local_judge.py       local VLM used like Jev (single-token logprob readout)
jev_client.py        cloud Jev (Vercel AI Gateway, OpenAI-compatible Decisions API)
memory.py            query and processed-reel memory (so each run uses different themes)
serve_local.sh       start the local judge server
requirements.txt
.env.example
```

## Setup

```bash
cd jev_reel_trainer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # only needed for --judge jev
```

### Attach to your real, logged-in Brave (recommended)

Chrome / Brave / Edge **136 and later ignore `--remote-debugging-port` for the default
profile**, so a fresh profile would mean logging in again. The extension route
(`playwright-ext`) cannot be used from a standalone Python process either. The working route is
Brave's **authorized remote debugging**:

1. Open `brave://inspect/#remote-debugging` in Brave and turn on **Allow remote debugging**.
   The choice is stored in `Local State` and survives restarts.
2. Run with `--cdp`. Each connection may show an approval prompt in Brave — click allow.

`--cdp` reads `DevToolsActivePort` from the profile and connects over `ws://`. HTTP discovery
(`/json/version`) returns 404 on that route; Playwright can still attach over `ws://`.

Your open tabs are never touched (a new tab is used) and the browser is never closed.

### Alternative: a dedicated profile

Without `--cdp`, a dedicated profile (`.browser-profile/`) is launched and you log in to
Instagram once. It is gitignored.

## Local judge (free, on-device)

```bash
huggingface-cli download Qwen/Qwen3-VL-4B-Instruct-GGUF \
  --include "Qwen3VL-4B-Instruct-Q4_K_M.gguf" "mmproj-Qwen3VL-4B-Instruct-F16.gguf" \
  --local-dir ~/models/Qwen3-VL-4B-Instruct-GGUF

./serve_local.sh                                  # llama-server on :8081 (Metal)
.venv/bin/python local_judge.py --health
.venv/bin/python local_judge.py --image frame.jpg --context "caption text"
```

The model weights (~3.1 GB) are **not** committed and must be downloaded separately.
Note that `huggingface-cli` also fills `~/.cache/huggingface/xet`; removing only `~/models`
leaves ~3 GB behind.

How it works, in brief:

1. Image + context + the allowed label list become one prompt.
2. Exactly one token is generated (`max_tokens=1, temperature=0, logprobs=true, top_logprobs=N`).
3. Label probabilities are read from the distribution at that position.
4. Labels that never appeared get a floor value, then everything is renormalized.

No free-form generation, no parsing — the local equivalent of Jev's `choice` / `noul`.

## Building supply (`crawl.py`) + memory

```bash
# 1. inspect memory (used themes, unused themes, pending, processed reels)
.venv/bin/python crawl.py --plan

# 2. queue fresh queries, spreading across categories
.venv/bin/python crawl.py \
  --add-query "site:instagram.com reel corgi" \
  --add-query "site:instagram.com reel jumping spider"

# 3. run it (20 minute session cap, likes on small posts only)
.venv/bin/python crawl.py --cdp --judge none --interact like --session-minutes 20
```

What it does:

1. **Harvests real reel URLs through a search engine** in the real browser — tag pages are
   broken on the web and plain-HTTP harvesting fails outright, so this is the only stable entry.
2. Opens each reel and (optionally) judges it.
3. For targets: **watches to the end (+ loops)**, harvests the author and hashtags, and queues the
   **author's profile** for expansion (a dog account is all dogs).
4. For non-targets: leaves immediately — no wasted watch time.
5. Prints the hit rate every `--report-every` reels.

### Memory (`memory/`, gitignored)

| File | Purpose |
|---|---|
| `queries.jsonl` | executed queries with their yield (`found` / `processed` / `animal_rate_pct`) |
| `pending.txt` | queries queued for the next run |
| `seen_reels.json` | processed reel shortcodes (**prevents duplicates across runs**) |
| `runs.jsonl` | per-run summaries |

Queries already in history are skipped (`--allow-repeat` overrides).
The agent reads `--plan`, picks unused themes and queues new queries, so each run pulls in a
different direction — dogs, then cats, then insects.

### Like policy

IG does not display a like count on reels, so the gate uses **comment and repost counts**.
With `--interact like`, a reel is liked only when **both** are at or below their thresholds
(default 50). Follow / repost / comment / DM / save are **not implemented at all**.

## Running the feed loop

```bash
.venv/bin/python reel_trainer.py --cdp --judge local          # scroll the feed you have
.venv/bin/python reel_trainer.py --cdp --judge none --steps 50
```

| Option | Default | Meaning |
|---|---|---|
| `--steps` | `0` | reels to process; `0` loops forever |
| `--cdp [URL]` | off | attach to the running browser (`--cdp` alone means auto-discovery) |
| `--judge` | `local` | `none` / `local` / `jev` |
| `--interact` | `view-only` | `like` enables the small-post like gate |
| `--loops` | `1` | extra loops watched on target reels |
| `--max-dwell` | `45.0` | max watch seconds per reel |
| `--dry-run` / `--probe` | off | exercise the loop / judge one reel and print details |

## Jev questions

Three predicates are asked in a single request
(`POST https://ai-gateway.vercel.sh/v1/decisions`):

```json
{
  "model": "typesafe-ai/jev-1.14.0",
  "input": [{"role": "user", "content": [
    {"type": "input_text", "text": "Instagram reel frame. Caption: ..."},
    {"type": "input_image", "image_url": "data:image/jpeg;base64,..."}
  ]}],
  "questions": [
    {"type": "predicate", "name": "is_dog", "instructions": "Does the image clearly show a dog?"},
    {"type": "predicate", "name": "is_cat", "instructions": "Does the image clearly show a cat?"}
  ]
}
```

## Instagram web quirks (all verified on a real profile)

1. **A background tab never decodes `<video>`.** Full-page screenshots come out solid grey;
   `page.bring_to_front()` is mandatory.
2. **Clicking the video element navigates to the permalink** and turns the `...` menu into the
   post menu. Never click the video — bring the tab forward and muted-play instead.
3. **Tag pages are broken on the web.** `/explore/tags/<tag>/` rewrites to
   `/explore/search/keyword/?q=...` and renders "Something went wrong", whether clicked or
   opened directly. Hashtag crawling is impossible.
4. **Plain-HTTP search harvesting does not work.** DuckDuckGo rate-limits almost immediately,
   Bing ignores `site:`, Reddit returns 403. The real browser is the only reliable route.
5. **Keyword search returns accounts, not content** ("puppy" surfaces DJs and people).
   `site:instagram.com reel <keyword>` is far better.
6. **An already-liked reel flips `aria-label` to `Unlike`** — used to avoid double taps and to
   verify the action landed.
7. **The web reel `...` menu has no "Not interested"** (only Report / Go to post / Share to... /
   Copy link / Embed / About this account).
8. **`Your Algorithm` and `Reset suggested content` are mobile-only.** They are free, harmless
   and the strongest levers, so asking the user for three taps on their phone is worthwhile.

## Measured numbers

| Path | On-target rate |
|---|---|
| Reel feed (tech-heavy account) | 0–2% |
| **Search-engine seeds** | **100% (12/12)** |
| Local judge on a dog reel | `dog = 1.00` |
| Local judge on a katydid | `animal = 0.9999` |
| Local judge on people / a PCB | `none = 0.9999` |
| Local judge latency | 4.6–7.6 s (M1 Pro / Metal, image encoding included) — ~6–7 s of *prefill*, generation is 0 ms |

Because search seeds measured 100% on-target, `--judge none` is the sensible default for
crawling; keep a judge only for the feed loop and for measurement (`--sample-judge`).

## Known limitations

- **Jev is not free-tier eligible.** With a Hobby account the monthly $5 credit does not cover
  `typesafe-ai/jev*`; the API returns `403 no_providers_available`. AI Gateway credits are
  required. No free-tier model supports vision, so image judging always costs money on Gateway.
- **Jev's image input is an experimental TypeSafe feature**; accuracy is worse than on text.
- **Nothing is guaranteed.** With no follows, only watch time and occasional likes are sent, so
  expect **days to weeks** of repeated sessions. Judgement comes from the `animal_rate_pct`
  trend in `runs/`.
- **The browser tab is brought to the front** while running (needed to decode video), so run it
  when you are not using the browser.
- **Keep search-engine queries to 3–6 per session**, and cap author profile walks with
  `--max-profiles`, to stay clear of bot detection.
- Automation may violate Instagram's terms. Use at your own risk and respect the rate limits.
