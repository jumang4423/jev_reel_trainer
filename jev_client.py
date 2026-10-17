"""Jev（TypeSafe AI）クライアント — Vercel AI Gateway 経由。

画像つきの判定は OpenAI 互換 Decisions API（``POST /v1/decisions``）を使う。
これは Vercel の changelog で画像入力の例が示されている唯一の HTTP 経路。
素の decision API（``/v1/evaluate``）は state に画像を入れる HTTP 形式が
公式ドキュメントに無いため、画像ありのときは使わない。

依存は標準ライブラリのみ（urllib）。リトライと形ゆらぎの吸収だけ自前でやる。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = os.environ.get("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh")
DEFAULT_MODEL = os.environ.get("JEV_MODEL", "typesafe-ai/jev-1.14.0")

# 画像を送れる経路（OpenAI 互換 Decisions API）
IMAGE_PATH = "/v1/decisions"
# テキストのみの判定経路（decision 用の素の API）
TEXT_PATH = "/v1/evaluate"


class JevError(RuntimeError):
    """Gateway / Jev 側のエラー。

    ``status`` が入っていれば HTTP ステータス。4xx（429 を除く）は再試行しても
    無駄なので、呼び出し側は即座に失敗させる。
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


def _extra_headers() -> dict[str, str]:
    """`JEV_EXTRA_HEADERS='{"TypeSafe-Danger-Zone-Images":"true"}'` のような
    追加ヘッダを環境変数から読む。Gateway が自動で付けてくれる場合は不要。"""
    raw = os.environ.get("JEV_EXTRA_HEADERS")
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _post(path: str, payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **_extra_headers(),
    }
    req = urllib.request.Request(DEFAULT_BASE_URL + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise JevError(f"HTTP {e.code} from {path}: {detail}", status=e.code) from None
    except urllib.error.URLError as e:
        raise JevError(f"network error: {e}") from None


def decide_with_images(
    api_key: str,
    text: str,
    images_jpeg_b64: list[str],
    questions: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    timeout: float = 45.0,
    retries: int = 2,
) -> dict[str, Any]:
    """画像つきで Jev を呼ぶ。

    Args:
        text: リールのキャプション等のテキスト state。
        images_jpeg_b64: base64（data URL の prefix なし）の JPEG。1枚以上。
        questions: ``[{"type": "predicate", "name": ..., "instructions": ...}, ...]``
    """
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for b64 in images_jpeg_b64:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"})

    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "questions": questions,
    }

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _post(IMAGE_PATH, payload, api_key, timeout)
        except JevError as e:
            last = e
            if not e.retryable or attempt >= retries:
                raise
            time.sleep(1.0 + attempt)
    raise last  # type: ignore[misc]


def decide_text(
    api_key: str,
    state: Any,
    questions: dict[str, Any],
    model: str = DEFAULT_MODEL,
    timeout: float = 45.0,
    retries: int = 1,
) -> dict[str, Any]:
    """テキストのみの判定（画像なし）。questions は ``{id: {...}}`` 形式。"""
    payload = {"model": model, "state": state, "questions": questions}
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _post(TEXT_PATH, payload, api_key, timeout)
        except JevError as e:
            last = e
            if attempt < retries:
                time.sleep(1.0 + attempt)
    raise last  # type: ignore[misc]


def parse_answers(resp: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """answers の形ゆらぎ（list / dict、predicate / boolean）を吸収して

    ``{name: {"probability": float | None, "choice": str | None, "confidence": float | None}}``
    を返す。確率が素の OpenAI 形式でも TypeSafe 形式でも動くようにしている。
    """
    result: dict[str, dict[str, Any]] = {}

    # providerMetadata.typesafe.confidence.<question id>
    conf_map: dict[str, Any] = {}
    pm = resp.get("providerMetadata")
    if isinstance(pm, dict):
        ts = pm.get("typesafe")
        if isinstance(ts, dict) and isinstance(ts.get("confidence"), dict):
            conf_map = ts["confidence"]

    raw = resp.get("answers")
    items: list[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "value": v} for k, v in raw.items()]

    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("id")
        if not name:
            continue

        prob = it.get("probability")
        if prob is None and isinstance(it.get("probabilities"), dict):
            probs = it["probabilities"]
            prob = probs.get("true", probs.get(True))

        conf = it.get("confidence", conf_map.get(name))
        result[str(name)] = {
            "probability": float(prob) if isinstance(prob, (int, float)) else None,
            "choice": it.get("choice") if isinstance(it.get("choice"), str) else None,
            "confidence": float(conf) if isinstance(conf, (int, float)) else None,
            "raw": it,
        }
    return result


def summarize_usage(resp: dict[str, Any]) -> dict[str, Any]:
    """ログ用に usage / コストだけ抜き出す（形は経路によって違うので緩く拾う）。"""
    out: dict[str, Any] = {}
    usage = resp.get("usage")
    if isinstance(usage, dict):
        out["usage"] = usage
    pm = resp.get("providerMetadata")
    if isinstance(pm, dict) and isinstance(pm.get("gateway"), dict):
        gw = pm["gateway"]
        out["cost"] = gw.get("cost")
        out["generation_id"] = gw.get("generationId")
    return out
