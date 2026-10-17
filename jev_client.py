"""Jev (TypeSafe AI) client — via Vercel AI Gateway.

Image decisions go through the OpenAI-compatible Decisions API (``POST /v1/decisions``).
That is the only HTTP route Vercel documents with an image-input example.
The plain decision API (``/v1/evaluate``) has no documented HTTP shape for
putting images into ``state``, so we do not use it when images are involved.

Standard library only (urllib). Retries and response-shape tolerance are hand-rolled.
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

# Route that accepts images (OpenAI-compatible Decisions API)
IMAGE_PATH = "/v1/decisions"
# Text-only decision route (plain decision API)
TEXT_PATH = "/v1/evaluate"


class JevError(RuntimeError):
    """Error raised by the Gateway / Jev.

    ``status`` holds the HTTP status when available. Retrying 4xx (except 429) is
    pointless, so the caller fails fast.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


def _extra_headers() -> dict[str, str]:
    """Read extra headers such as `JEV_EXTRA_HEADERS='{"TypeSafe-Danger-Zone-Images":"true"}'`
    from the environment. Not needed when the Gateway adds them for you."""
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
    """Call Jev with images.

    Args:
        text: text state such as the reel caption.
        images_jpeg_b64: base64 JPEG (no data-URL prefix). One or more.
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
    """Text-only decision (no images). ``questions`` uses the ``{id: {...}}`` form."""
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
    """Absorb the various ``answers`` shapes (list / dict, predicate / boolean) and

    ``{name: {"probability": float | None, "choice": str | None, "confidence": float | None}}``
    return the dict below. Works whether probabilities come in the plain OpenAI shape
    or the TypeSafe shape.
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
    """Pull just usage / cost for logging (shapes differ per route, so parse loosely)."""
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
