from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chemex_lit.config import config_fingerprint, load_config
from chemex_lit.models import CompoundRef, ReactionRecord


def test_packaged_config_loads() -> None:
    config = load_config()
    assert config.schema_version == 1
    assert config.pipeline.extraction_workers == 3
    assert config.models.text.model == "deepseek-v4-flash"
    assert config.models.reasoning is not None
    assert config.models.reasoning.model == "deepseek-v4-pro"
    assert config.models.reasoning.temperature is None
    assert config.models.reasoning.response_format is None


def test_explicit_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEMEX_TEXT_MODEL", "test-model")
    assert load_config().models.text.model == "test-model"


def test_reasoning_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEMEX_REASONING_MODEL", "reasoning-test-model")
    config = load_config()
    assert config.models.reasoning is not None
    assert config.models.reasoning.model == "reasoning-test-model"


def test_config_fingerprint_is_stable() -> None:
    assert config_fingerprint(load_config()) == config_fingerprint(load_config())


def test_config_fingerprint_changes_with_new_model_fields(tmp_path: Path) -> None:
    base_path = tmp_path / "base.yaml"
    variant_path = tmp_path / "variant.yaml"
    _ = base_path.write_text(
        """
models:
  reasoning:
    base_url: https://reasoning.example/v1
    model: reasoning-model
    api_key_env: TEST_REASONING_KEY
""".strip(),
        encoding="utf-8",
    )
    _ = variant_path.write_text(
        """
models:
  reasoning:
    base_url: https://reasoning.example/v1
    model: reasoning-model
    api_key_env: TEST_REASONING_KEY
    temperature: null
    response_format: null
    extra_payload:
      thinking:
        type: enabled
""".strip(),
        encoding="utf-8",
    )

    assert config_fingerprint(load_config(base_path)) != config_fingerprint(load_config(variant_path))


def test_record_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _ = ReactionRecord.model_validate(
            {
                "reaction_id": "r1",
                "confidence": 1,
                "review_status": "accepted",
                "unexpected": True,
            }
        )


def test_compound_normalizes_empty_values() -> None:
    compound = CompoundRef(label="  ", name=" acetone ", role="reactant")
    assert compound.label is None
    assert compound.name == "acetone"


def test_reasoning_spec_uses_configured_reasoning(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _ = config_path.write_text(
        """
models:
  reasoning:
    base_url: https://reasoning.example/v1
    model: reasoning-model
    api_key_env: TEST_REASONING_KEY
""".strip(),
        encoding="utf-8",
    )

    model, used_fallback = load_config(config_path).models.reasoning_spec()

    assert used_fallback is False
    assert model.model == "reasoning-model"


def test_reasoning_spec_falls_back_to_text(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _ = config_path.write_text(
        """
models:
  reasoning: null
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    model, used_fallback = config.models.reasoning_spec()

    assert used_fallback is True
    assert model == config.models.text
