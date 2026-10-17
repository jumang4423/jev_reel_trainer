"""Memory of search queries and already-processed reels (shared across runs).

Goal: stop repeating the same search and make **different kinds of video** show up.
  - `queries.jsonl`  : history of executed queries with their yield (hit rate)
  - `pending.txt`    : queries queued for the next run (written by the agent)
  - `seen_reels.json`: shortcodes already processed (prevents duplicates)
  - `THEME_POOL`     : unused theme candidates the agent picks new queries from

The agent's job is to *think up queries*; execution and judging happen locally (crawl.py).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "memory"
MAX_SEEN_REELS = 5000

# Theme candidates the agent turns into new queries.
# Spread across categories so each run can pull in a different direction
# (dogs -> cats -> insects -> aquariums).
THEME_POOL = [
    # dogs
    "puppy", "dog", "dogtok", "dogsofinstagram", "goldenretriever", "shibainu",
    "corgi", "pug", "dachshund", "samoyed",
    # cats
    "kitten", "cat", "cattok", "catsofinstagram", "mainecoon", "bengalcat",
    # small pets
    "guineapig", "hamster", "rabbit", "ferret", "chinchilla",
    # birds
    "parrot", "cockatiel", "birdsoftiktok", "duckling", "owl",
    # wildlife
    "wildlife", "fox", "deer", "squirrel", "hedgehog", "raccoon",
    # insects (the "weird bugs" bucket)
    "insects", "beetle", "mantis", "jumping spider", "moth", "snail",
    # aquatic & reptiles
    "aquarium", "koi", "bettafish", "gecko", "turtle", "axolotl",
    # farm & large animals
    "horses", "goats", "cows", "sheep", "piglet", "alpaca",
    # cross-cutting
    "cute animals", "funny animals", "animal rescue", "baby animals", "animal friendship",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Memory:
    def __init__(self, root: Path | str = DEFAULT_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.queries_file = self.root / "queries.jsonl"
        self.pending_file = self.root / "pending.txt"
        self.reels_file = self.root / "seen_reels.json"
        self.runs_file = self.root / "runs.jsonl"

    # ---------------- queries ----------------
    def history(self) -> list[dict]:
        if not self.queries_file.exists():
            return []
        out = []
        for line in self.queries_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def used_queries(self) -> set[str]:
        return {r.get("query", "") for r in self.history() if r.get("query")}

    def last_seen(self, query: str) -> str | None:
        for r in reversed(self.history()):
            if r.get("query") == query:
                return r.get("ts")
        return None

    def record_query(self, query: str, engine: str, found: int, processed: int,
                     animal: int, theme: str = "", note: str = "") -> None:
        rec = {
            "ts": _now(), "query": query, "theme": theme, "engine": engine,
            "found": found, "processed": processed, "animal": animal,
            "animal_rate_pct": round(animal / processed * 100, 1) if processed else None,
            "note": note,
        }
        with self.queries_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---------------- pending queue ----------------
    def pending(self) -> list[str]:
        if not self.pending_file.exists():
            return []
        return [l.strip() for l in self.pending_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    def add_pending(self, queries: list[str]) -> tuple[list[str], list[str]]:
        """Append. Returns (added, skipped_because_already_seen)."""
        have = self.used_queries() | set(self.pending())
        added, skipped = [], []
        for q in queries:
            q = q.strip()
            if not q:
                continue
            if q in have:
                skipped.append(q)
                continue
            added.append(q)
            have.add(q)
        if added:
            with self.pending_file.open("a", encoding="utf-8") as fh:
                for q in added:
                    fh.write(q + "\n")
        return added, skipped

    def consume_pending(self) -> list[str]:
        items = self.pending()
        if items:
            self.pending_file.write_text("", encoding="utf-8")
        return items

    def clear_pending(self) -> None:
        self.pending_file.write_text("", encoding="utf-8")

    # ---------------- reels ----------------
    def seen_reels(self) -> set[str]:
        if not self.reels_file.exists():
            return set()
        try:
            data = json.loads(self.reels_file.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    def mark_reels(self, codes: list[str]) -> None:
        if not codes:
            return
        seen = self.seen_reels()
        seen.update(codes)
        if len(seen) > MAX_SEEN_REELS:  # Drop the oldest (JSON array, so order is roughly insertion order)
            seen = set(list(seen)[-MAX_SEEN_REELS:])
        self.reels_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")

    # ---------------- runs / report ----------------
    def record_run(self, summary: dict, queries: list[str]) -> None:
        with self.runs_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now(), "queries": queries, **summary}, ensure_ascii=False) + "\n")

    def unused_themes(self, limit: int = 40) -> list[str]:
        """Theme candidates not used in any query yet (hints for the agent's next pick)."""
        used_blob = " ".join(self.used_queries()).lower() + " " + " ".join(self.pending()).lower()
        return [t for t in THEME_POOL if t.lower() not in used_blob][:limit]

    def report(self) -> dict:
        hist = self.history()
        return {
            "memory_dir": str(self.root),
            "queries_used": len(hist),
            "reels_seen": len(self.seen_reels()),
            "pending": self.pending(),
            "recent_queries": [
                {"query": r.get("query"), "animal_rate_pct": r.get("animal_rate_pct"),
                 "processed": r.get("processed"), "ts": r.get("ts")}
                for r in hist[-8:]
            ],
            "unused_themes": self.unused_themes(60),
        }
