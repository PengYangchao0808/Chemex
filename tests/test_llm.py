from __future__ import annotations

import json

import httpx
import pytest

from chemex_lit.config import ModelSpec
from chemex_lit.errors import ExtractionError
from chemex_lit.llm.client import LLMClient, extract_json
from chemex_lit.llm.prompts import PromptRegistry


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
        assert payload["temperature"] == 0
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
        PromptRegistry().render("missing", {})
