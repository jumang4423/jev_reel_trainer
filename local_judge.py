"""ローカルで動く「画像対応 Jev」— llama.cpp（llama-server）版。

TypeSafe Jev の代わりに、小さな VLM（既定は Qwen3-VL-4B）を llama-server で動かし、
**テキストを生成させずに確率だけ読む**方式で型付きの判断を得る。

仕組み（Jev / OpenJev と同じ思想）:
  1. 画像＋文脈＋「取れるラベル一覧」を 1 つのプロンプトにする
  2. ``max_tokens=1, temperature=0, logprobs=true, top_logprobs=N`` で**1 トークンだけ**生成させる
  3. その位置の分布（top_logprobs）から、各ラベルの確率を読む
  4. ラベルに無いものは床（floor）を敷いて正規化する

つまり「最初の 1 位置のスコアを読む」だけで、自由文の生成もパースも無い。
Jev の ``choice`` / ``noul`` に相当するものがローカルで得られる。

起動は ``serve_local.sh`` を参照。
"""

from __future__ import annotations

import base64
import json
import math
import os
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8081")

# ラベルが top_logprobs に現れなかったときの下限（過信を防ぐ）
LABEL_FLOOR = 1e-4

# リールの主対象を 4 択で聞く。頭のトークンが衝突せず、意味が排他的になるよう定義する。
# （"other" だと人物まで「犬猫以外」に入ってしまうため "animal" と "none" を分ける）
SPECIES_LABELS = ["dog", "cat", "animal", "none"]

SPECIES_CRITERIA = {
    "dog": "a real dog is clearly the main subject",
    "cat": "a real cat is clearly the main subject",
    "animal": "another real animal (not a dog or cat) is clearly the main subject",
    "none": "no real animal is the main subject "
            "(a person, an object, a screen, text, a drawing or CGI character, scenery, food, etc.)",
}


class LocalLLMError(RuntimeError):
    """llama-server 側のエラー。"""


def _post(path: str, payload: dict[str, Any], base_url: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise LocalLLMError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise LocalLLMError(f"network error: {e} （llama-server は起動していますか？）") from None


def health(base_url: str = DEFAULT_BASE_URL, timeout: float = 5.0) -> dict[str, Any]:
    """llama-server が生きているか。``/health`` を叩く。"""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def first_token_distribution(
    base_url: str,
    image_b64: str | None,
    system: str,
    user: str,
    timeout: float = 180.0,
    top_logprobs: int = 20,
) -> tuple[dict[str, float], dict[str, Any]]:
    """1 トークンだけ生成し、その位置の分布 ``{token: probability}`` を返す。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": user}]
    if image_b64:
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
        )

    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "max_tokens": 1,          # ← 生成させない。1 位置だけ読む
        "temperature": 0.0,
        "logprobs": True,
        "top_logprobs": top_logprobs,
    }
    resp = _post("/v1/chat/completions", payload, base_url, timeout)

    try:
        top_list = resp["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    except (KeyError, IndexError, TypeError):
        raise LocalLLMError(
            "logprobs を取得できませんでした（llama.cpp のビルドが古い可能性）: "
            + json.dumps(resp, ensure_ascii=False)[:400]
        ) from None

    dist: dict[str, float] = {}
    for item in top_list:
        tok = str(item.get("token") or "").strip().lower()
        lp = item.get("logprob")
        if tok and isinstance(lp, (int, float)):
            prob = math.exp(float(lp))
            # 同じ語でも "other" / " other" / "Other" / " Other" が別トークンとして
            # 並ぶため、辞書に上書きすると高確率側が潰れる。最大値を採る。
            if prob > dist.get(tok, 0.0):
                dist[tok] = prob
    return dist, resp


def score_labels(dist: dict[str, float], labels: list[str]) -> dict[str, float]:
    """分布からラベルの確率を取り出して正規化する。"""
    raw: dict[str, float] = {}
    for lab in labels:
        key = lab.strip().lower()
        if key in dist:
            raw[lab] = dist[key]
            continue
        # 「doggy」のような揺れを軽く拾う（前方一致）。無ければ床。
        near = [v for k, v in dist.items() if k.startswith(key[:3])]
        raw[lab] = max(near) if near else LABEL_FLOOR
    total = sum(raw.values()) or 1.0
    return {k: v / total for k, v in raw.items()}


def choose(
    base_url: str,
    image_b64: str | None,
    context: str,
    question: str,
    labels: list[str],
    criteria: dict[str, str] | None = None,
    timeout: float = 180.0,
    top_logprobs: int = 20,
) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    """Jev の ``choice`` 相当。(確率, 生の分布, 生レスポンス) を返す。"""
    system = (
        "You are a decision model. You never write sentences or explanations. "
        "You answer in English with exactly one word from the allowed labels."
    )
    if criteria:
        label_block = "\n".join(f"- {lab}: {criteria.get(lab, '')}" for lab in labels)
        user = (
            f"{context}\n\n"
            f"Question: {question}\n"
            f"Allowed labels (pick exactly one):\n{label_block}\n"
            "Answer with exactly one label word from the list, and nothing else."
        )
    else:
        user = (
            f"{context}\n\n"
            f"Question: {question}\n"
            f"Allowed labels: {', '.join(labels)}\n"
            "Answer with exactly one label from the list, and nothing else."
        )
    dist, resp = first_token_distribution(base_url, image_b64, system, user, timeout, top_logprobs)
    return score_labels(dist, labels), dist, resp


def decide_local(
    base_url: str,
    image_b64: str,
    context: str = "",
    targets: str = "dog,cat",
    timeout: float = 180.0,
) -> tuple[float, str, dict[str, dict[str, Any]], dict[str, Any]]:
    """``reel_trainer`` から呼ぶ用。Jev 版と同じ形で返す。

    Returns:
        (score, score_key, answers, extra)
    """
    probs, dist, resp = choose(
        base_url,
        image_b64,
        context,
        "What is the main subject of this image?",
        SPECIES_LABELS,
        criteria=SPECIES_CRITERIA,
        timeout=timeout,
    )
    p_dog = probs.get("dog", 0.0)
    p_cat = probs.get("cat", 0.0)
    p_other_animal = probs.get("animal", 0.0)
    p_animal = p_dog + p_cat + p_other_animal

    answers: dict[str, dict[str, Any]] = {
        "is_dog": {"probability": p_dog, "confidence": max(p_dog, 1.0 - p_dog)},
        "is_cat": {"probability": p_cat, "confidence": max(p_cat, 1.0 - p_cat)},
        "is_animal": {"probability": p_animal, "confidence": max(p_animal, 1.0 - p_animal)},
    }
    if targets == "any":
        score, key = p_animal, "is_animal"
    else:
        score, key = (p_dog, "is_dog") if p_dog >= p_cat else (p_cat, "is_cat")

    extra = {"local_dist": dist, "local_labels": probs, "local_raw_tokens": sorted(dist.items(), key=lambda kv: -kv[1])[:8]}
    return score, key, answers, extra


# --------------------------------------------------------------------------
# 単体テスト用 CLI
# --------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="ローカル VLM を Jev のように使う（1 トークン読み出し）")
    ap.add_argument("--image", help="画像パス（JPEG/PNG）")
    ap.add_argument("--context", default="", help="補助テキスト（キャプション等）")
    ap.add_argument("--question", default="What is the main subject of this image?")
    ap.add_argument("--labels", default=",".join(SPECIES_LABELS))
    ap.add_argument("--url", default=DEFAULT_BASE_URL)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--health", action="store_true", help="llama-server の生存確認だけ")
    args = ap.parse_args(argv)

    if args.health:
        print(json.dumps(health(args.url), ensure_ascii=False))
        return 0

    img_b64 = None
    if args.image:
        img_b64 = base64.b64encode(open(args.image, "rb").read()).decode()

    labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    probs, dist, resp = choose(args.url, img_b64, args.context, args.question, labels,
                               top_logprobs=args.top)
    print("labels :", json.dumps({k: round(v, 4) for k, v in probs.items()}, ensure_ascii=False))
    print("top    :", json.dumps([(t, round(p, 4)) for t, p in
                                  sorted(dist.items(), key=lambda kv: -kv[1])[:10]], ensure_ascii=False))
    usage = resp.get("usage") or {}
    timings = resp.get("timings") or {}
    print("usage  :", usage, "| timings(ms):", {k: round(v) for k, v in timings.items()
                                                if isinstance(v, (int, float))})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
