"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

import chemex_lit.profiles as profiles_module


@pytest.fixture(autouse=True)
def _isolated_auth_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the credential store at a per-test file so real user data is never touched."""

    monkeypatch.setenv("CHEMEX_AUTH_STORE", str(tmp_path / "auth.json"))


@pytest.fixture(autouse=True)
def _isolated_models_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep profile resolution hermetic: ignore any real user ``models.yaml``."""

    monkeypatch.setattr(
        profiles_module,
        "default_models_path",
        lambda: tmp_path / "missing-models.yaml",
    )
