from __future__ import annotations

import pytest
from pydantic import ValidationError

from chemex_lit.config import config_fingerprint, load_config
from chemex_lit.models import CompoundRef, ReactionRecord


def test_packaged_config_loads() -> None:
    config = load_config()
    assert config.schema_version == 1
    assert config.pipeline.extraction_workers == 3


def test_explicit_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEMEX_TEXT_MODEL", "test-model")
    assert load_config().models.text.model == "test-model"


def test_config_fingerprint_is_stable() -> None:
    assert config_fingerprint(load_config()) == config_fingerprint(load_config())


def test_record_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReactionRecord.model_validate(
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
