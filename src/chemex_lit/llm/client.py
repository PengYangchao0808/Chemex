"""One synchronous OpenAI-compatible LLM client."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Sequence

import httpx

from chemex_lit.config import ModelSpec
from chemex_lit.errors import ExternalServiceError, ExtractionError

logger = logging.getLogger(__name__)

_THINKING = re.compile(r"<(?:thinking|think)>.*?</(?:thinking|think)>", re.DOTALL)


def extract_json(text: str) -> Any:
    """Extract the first complete JSON object or array from an LLM response."""
    cleaned = _THINKING.sub("", text).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    starts = [position for position in (cleaned.find("{"), cleaned.find("[")) if position >= 0]
    if not starts:
        raise ExtractionError("LLM response did not contain JSON")
    start = min(starts)
    opening = cleaned[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise ExtractionError(f"Malformed JSON in LLM response: {exc}") from exc
    raise ExtractionError("LLM response contained incomplete JSON")


def encode_image(path: Path, max_dim: int = 1600) -> str:
    """Encode an image as a compact PNG data URI."""
    import base64
    import io

    from PIL import Image

    with Image.open(path) as image:
        image.load()
        if max(image.size) > max_dim:
            image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{payload}"


class LLMClient:
    """OpenAI-compatible JSON client with one retry policy."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def complete(
        self,
        model: ModelSpec,
        prompt: str,
        images: Sequence[Path] = (),
    ) -> Any:
        content: str | list[dict[str, Any]]
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            parts.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": encode_image(path)},
                }
                for path in images
            )
            content = parts
        else:
            content = prompt

        payload: dict[str, Any] = {
            "model": model.model,
            "messages": [{"role": "user", "content": content}],
        }
        if model.temperature is not None:
            payload["temperature"] = model.temperature
        if model.max_tokens is not None:
            payload["max_tokens"] = model.max_tokens
        if model.response_format is not None:
            payload["response_format"] = {"type": model.response_format}
        collisions = {key for key in model.extra_payload if key in {"model", "messages"}}
        if collisions:
            joined = ", ".join(sorted(collisions))
            raise ExternalServiceError(f"extra_payload may not override reserved payload keys: {joined}")
        payload.update(model.extra_payload)
        url = self._endpoint(model.base_url)
        headers = {
            "Authorization": f"Bearer {model.api_key()}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(model.timeout, connect=min(30, model.timeout))
        last_error: Exception | None = None

        for attempt in range(1, model.retries + 1):
            try:
                if self._client is not None:
                    response = self._client.post(url, json=payload, headers=headers)
                else:
                    with httpx.Client(timeout=timeout) as client:
                        response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
                content_text = body["choices"][0]["message"]["content"]
                return extract_json(content_text)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if not retryable or attempt == model.retries:
                    break
                delay = min(2 ** (attempt - 1), 8)
                logger.warning("LLM request failed; retrying in %ss: %s", delay, exc)
                time.sleep(delay)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ExternalServiceError(f"Invalid LLM response: {exc}") from exc

        raise ExternalServiceError(
            f"LLM request failed after {model.retries} attempt(s): {last_error}"
        )

    @staticmethod
    def _endpoint(base_url: str) -> str:
        base = base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"
