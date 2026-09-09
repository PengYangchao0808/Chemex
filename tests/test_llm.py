from __future__ import annotations

import json
from typing import Literal

import httpx
import pytest

from chemex_lit.config import ModelSpec
from chemex_lit.errors import ExternalServiceError, ExtractionError
from chemex_lit.llm import LLMClient, PromptRegistry, extract_json


def test_extract_json_strips_fences_and_thinking() -> None:
    assert extract_json('<thinking>ignore</thinking>```json\n{"ok": true}\n```') == {"ok": True}


def test_extract_json_finds_embedded_object() -> None:
    assert extract_json('Answer: {"value": 3} done') == {"value": 3}


def test_extract_json_rejects_incomplete_data() -> None:
    with pytest.raises(ExtractionError):
        extract_json('{"value":')


def test_llm_client_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        assert payload["temperature"] == 0.0
        assert payload["max_tokens"] == 8192
        assert payload["response_format"] == {"type": "json_object"}
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"reactions": []}'}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = ModelSpec(
        base_url="https://example.test/v1",
        model="model",
        api_key_env="TEST_LLM_KEY",
    )
    assert LLMClient(client).complete(model, "prompt") == {"reactions": []}


@pytest.mark.parametrize(
    ("temperature", "response_format", "expected_present"),
    [
        (None, None, set()),
        (0.0, "json_object", {"temperature", "response_format"}),
    ],
)
def test_llm_payload_omits_or_keeps_optional_keys(
    monkeypatch: pytest.MonkeyPatch,
    temperature: float | None,
    response_format: Literal["json_object"] | None,
    expected_present: set[str],
) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        assert ("temperature" in payload) is ("temperature" in expected_present)
        assert ("response_format" in payload) is ("response_format" in expected_present)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = ModelSpec(
        base_url="https://example.test/v1",
        model="model",
        api_key_env="TEST_LLM_KEY",
        temperature=temperature,
        response_format=response_format,
    )

    assert LLMClient(client).complete(model, "prompt") == {"ok": True}


def test_llm_payload_omits_max_tokens_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        assert "max_tokens" not in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = ModelSpec(
        base_url="https://example.test/v1",
        model="model",
        api_key_env="TEST_LLM_KEY",
        max_tokens=None,
    )

    assert LLMClient(client).complete(model, "prompt") == {"ok": True}


def test_llm_payload_merges_extra_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        assert payload["thinking"] == {"type": "enabled"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = ModelSpec(
        base_url="https://example.test/v1",
        model="model",
        api_key_env="TEST_LLM_KEY",
        extra_payload={"thinking": {"type": "enabled"}},
    )

    assert LLMClient(client).complete(model, "prompt") == {"ok": True}


def test_llm_payload_rejects_reserved_extra_payload_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    model = ModelSpec(
        base_url="https://example.test/v1",
        model="model",
        api_key_env="TEST_LLM_KEY",
        extra_payload={"model": "other-model"},
    )

    with pytest.raises(ExternalServiceError, match="reserved payload keys: model"):
        LLMClient().complete(model, "prompt")


def test_prompt_registry_renders_packaged_prompt() -> None:
    registry = PromptRegistry()

    assert registry.versions["text"] == "1.0.0"
    rendered = registry.render(
        "text",
        {"<<CONTENT>>": "example evidence", "<<PAGE_CONTEXT>>": "page 1"},
    )
    assert "example evidence" in rendered


def test_prompt_registry_rejects_unknown_prompt() -> None:
    with pytest.raises(Exception, match="Unknown prompt"):
        _ = PromptRegistry().render("missing", {})
