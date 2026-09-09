from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

import chemex_lit.profiles as profiles_module
from chemex_lit.config import load_config
from chemex_lit.errors import ArtifactError, ConfigurationError
from chemex_lit.profiles import (
    Profile,
    ProfileSpec,
    ProfilesFile,
    default_models_path,
    load_profiles_file,
    packaged_profiles,
    resolve_profile_name,
    select_profile,
)
from chemex_lit.store import ArtifactStore, sha256_text

_UNSET = object()
_OVERRIDE_ENV_NAMES = (
    "CHEMEX_OUTPUT_DIR",
    "CHEMEX_MINERU_BASE_URL",
    "CHEMEX_MINERU_API_KEY_ENV",
    "CHEMEX_TEXT_BASE_URL",
    "CHEMEX_TEXT_MODEL",
    "CHEMEX_TEXT_API_KEY_ENV",
    "CHEMEX_VISION_BASE_URL",
    "CHEMEX_VISION_MODEL",
    "CHEMEX_VISION_API_KEY_ENV",
    "CHEMEX_REASONING_BASE_URL",
    "CHEMEX_REASONING_MODEL",
    "CHEMEX_REASONING_API_KEY_ENV",
)


def _clear_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OVERRIDE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _patch_missing_default_models_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        profiles_module,
        "default_models_path",
        lambda: tmp_path / "missing-models.yaml",
    )


def _profile_spec_dict(
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    timeout: int | None | object = _UNSET,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "base_url": base_url,
        "model": model,
        "api_key_env": api_key_env,
    }
    if timeout is not _UNSET:
        spec["timeout"] = timeout
    return spec


def _profile_entry(
    *,
    text_model: str,
    vision_model: str | None = None,
    reasoning_model: str | None = None,
    text_timeout: int | None | object = _UNSET,
) -> dict[str, object]:
    return {
        "text": _profile_spec_dict(
            base_url="https://text.example/v1",
            model=text_model,
            api_key_env="TEST_TEXT_KEY",
            timeout=text_timeout,
        ),
        "vision": _profile_spec_dict(
            base_url="https://vision.example/v1",
            model=vision_model or f"{text_model}-vision",
            api_key_env="TEST_VISION_KEY",
        ),
        "reasoning": _profile_spec_dict(
            base_url="https://reasoning.example/v1",
            model=reasoning_model or f"{text_model}-reasoning",
            api_key_env="TEST_REASONING_KEY",
        ),
    }


def _profiles_payload(
    *,
    profiles: dict[str, dict[str, object]],
    default_profile: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"version": 1, "profiles": profiles}
    if default_profile is not None:
        payload["default_profile"] = default_profile
    return payload


def _write_yaml(path: Path, payload: dict[str, object] | list[object]) -> Path:
    _ = path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _profile_model(model_name: str) -> Profile:
    return Profile(
        text=ProfileSpec(
            base_url="https://text.example/v1",
            model=model_name,
            api_key_env="TEST_TEXT_KEY",
        ),
        vision=ProfileSpec(
            base_url="https://vision.example/v1",
            model=f"{model_name}-vision",
            api_key_env="TEST_VISION_KEY",
        ),
        reasoning=ProfileSpec(
            base_url="https://reasoning.example/v1",
            model=f"{model_name}-reasoning",
            api_key_env="TEST_REASONING_KEY",
        ),
    )


def _config_sha256(config_dump: object) -> str:
    return sha256_text(json.dumps(config_dump, sort_keys=True, ensure_ascii=False))


def _initialise_store_with_profile(
    store: ArtifactStore,
    input_file: Path,
    *,
    profile: str | None,
    models_source: str,
) -> None:
    config_dump = {
        "models": {"text": {"model": "alpha"}},
        "profile": profile,
        "models_source": models_source,
    }
    store.initialise(
        run_id="r",
        input_path=input_file,
        input_sha256="a",
        config_dump=config_dump,
        config_sha256=_config_sha256(config_dump),
        version="1",
        prompt_versions={},
        models={"text": "alpha"},
        mode="auto",
        producer_plan={},
        profile=profile,
        models_source=models_source,
    )


def test_load_profiles_file_returns_none_when_default_path_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(profiles_module, "default_models_path", lambda: tmp_path / "missing.yaml")

    assert load_profiles_file() is None


def test_load_profiles_file_loads_explicit_path(tmp_path: Path) -> None:
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(profiles={"openai": _profile_entry(text_model="gpt-4o")}),
    )

    profiles_file = load_profiles_file(models_path)

    assert profiles_file is not None
    assert profiles_file.profiles["openai"].text.model == "gpt-4o"


def test_load_profiles_file_raises_on_missing_explicit_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Models file not found"):
        _ = load_profiles_file(tmp_path / "missing.yaml")


def test_load_profiles_file_raises_on_malformed_yaml(tmp_path: Path) -> None:
    models_path = tmp_path / "models.yaml"
    _ = models_path.write_text("profiles: [\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Invalid YAML"):
        _ = load_profiles_file(models_path)


def test_load_profiles_file_raises_on_non_mapping_root(tmp_path: Path) -> None:
    models_path = _write_yaml(tmp_path / "models.yaml", [1, 2, 3])

    with pytest.raises(ConfigurationError, match="must contain a mapping"):
        _ = load_profiles_file(models_path)


def test_load_profiles_file_raises_on_unknown_field(tmp_path: Path) -> None:
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(
            profiles={
                "openai": {
                    **_profile_entry(text_model="gpt-4o"),
                    "bogus_field": 1,
                }
            }
        ),
    )

    with pytest.raises(ConfigurationError, match="Invalid models file"):
        _ = load_profiles_file(models_path)


def test_packaged_profiles_loads() -> None:
    profiles_file = packaged_profiles()

    assert isinstance(profiles_file, ProfilesFile)
    assert "deepseek-glm" in profiles_file.profiles
    assert "openai" in profiles_file.profiles


def test_default_models_path_uses_platformdirs() -> None:
    path = default_models_path()

    assert str(path).endswith("models.yaml")
    assert "chemex-lit" in str(path)


def test_resolve_profile_name_priority() -> None:
    profiles_file = ProfilesFile(default_profile="foo")

    assert resolve_profile_name(None, profiles_file) == "foo"
    assert resolve_profile_name("bar", profiles_file) == "bar"
    assert resolve_profile_name(None, None) is None


def test_select_profile_returns_none_when_name_none() -> None:
    assert select_profile(None, None) == (None, None)


def test_select_profile_returns_profile_when_found() -> None:
    profiles_file = ProfilesFile(profiles={"foo": _profile_model("gpt-4o")})

    profile, source_label = select_profile(profiles_file, "foo")

    assert profile is not None
    assert profile.text.model == "gpt-4o"
    assert source_label == "profile:foo"


def test_select_profile_raises_when_no_file() -> None:
    with pytest.raises(ConfigurationError, match="no profiles file is available"):
        _ = select_profile(None, "foo")


def test_select_profile_raises_when_unknown_name() -> None:
    profiles_file = ProfilesFile(profiles={"foo": _profile_model("gpt-4o")})

    with pytest.raises(ConfigurationError, match="Available: foo"):
        _ = select_profile(profiles_file, "bar")


def test_load_config_no_profile_keeps_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    _patch_missing_default_models_path(monkeypatch, tmp_path)

    config = load_config()

    assert config.profile is None
    assert config.models_source == "default"
    assert config.models.text.model == "deepseek-v4-flash"


def test_load_config_explicit_profile_uses_packaged_samples_when_user_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    _patch_missing_default_models_path(monkeypatch, tmp_path)

    config = load_config(profile="deepseek-glm")

    assert config.profile == "deepseek-glm"
    assert config.models.text.model == "deepseek-v4-flash"


def test_load_config_with_profile_overlays_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(
            default_profile="openai",
            profiles={"openai": _profile_entry(text_model="gpt-4o")},
        ),
    )

    config = load_config(models_path=models_path)

    assert config.models.text.model == "gpt-4o"


def test_load_config_profile_optional_fields_inherit_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(
            profiles={"openai": _profile_entry(text_model="gpt-4o", text_timeout=_UNSET)}
        ),
    )

    config = load_config(models_path=models_path, profile="openai")

    assert config.models.text.timeout == 300


def test_load_config_profile_overrides_optional_field_when_specified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(
            profiles={"openai": _profile_entry(text_model="gpt-4o", text_timeout=120)}
        ),
    )

    config = load_config(models_path=models_path, profile="openai")

    assert config.models.text.timeout == 120


def test_load_config_default_profile_used_when_no_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(
            default_profile="openai",
            profiles={"openai": _profile_entry(text_model="gpt-4o")},
        ),
    )

    config = load_config(models_path=models_path)

    assert config.profile == "openai"


def test_load_config_explicit_profile_wins_over_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(
            default_profile="foo",
            profiles={
                "foo": _profile_entry(text_model="gpt-4o-mini"),
                "bar": _profile_entry(text_model="gpt-4o"),
            },
        ),
    )

    config = load_config(models_path=models_path, profile="bar")

    assert config.profile == "bar"
    assert config.models.text.model == "gpt-4o"


def test_load_config_emits_warning_when_env_overrides_shadow_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_override_env(monkeypatch)
    monkeypatch.setenv("CHEMEX_TEXT_MODEL", "gpt-bogus")
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(profiles={"foo": _profile_entry(text_model="gpt-4o")}),
    )

    with caplog.at_level(logging.WARNING):
        _ = load_config(models_path=models_path, profile="foo")

    assert any("CHEMEX_TEXT_MODEL" in record.getMessage() for record in caplog.records)


def test_load_config_no_warning_when_no_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_override_env(monkeypatch)
    _patch_missing_default_models_path(monkeypatch, tmp_path)
    monkeypatch.setenv("CHEMEX_TEXT_MODEL", "gpt-bogus")

    with caplog.at_level(logging.WARNING):
        _ = load_config()

    assert not any("CHEMEX_TEXT_MODEL" in record.getMessage() for record in caplog.records)


def test_load_config_env_var_still_overrides_profile_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_override_env(monkeypatch)
    monkeypatch.setenv("CHEMEX_TEXT_MODEL", "gpt-bogus")
    models_path = _write_yaml(
        tmp_path / "models.yaml",
        _profiles_payload(profiles={"foo": _profile_entry(text_model="gpt-4o")}),
    )

    config = load_config(models_path=models_path, profile="foo")

    assert config.models.text.model == "gpt-bogus"


def test_initialise_writes_profile_and_models_source(tmp_path: Path) -> None:
    input_file = tmp_path / "fake.pdf"
    _ = input_file.write_text("x", encoding="utf-8")
    store = ArtifactStore(tmp_path / "run")

    _initialise_store_with_profile(store, input_file, profile="foo", models_source="profile:foo")
    manifest = store.manifest()

    assert manifest["profile"] == "foo"
    assert manifest["models_source"] == "profile:foo"


def test_initialise_rejects_profile_change_on_resume(tmp_path: Path) -> None:
    input_file = tmp_path / "fake.pdf"
    _ = input_file.write_text("x", encoding="utf-8")
    store = ArtifactStore(tmp_path / "run")

    _initialise_store_with_profile(store, input_file, profile="foo", models_source="profile:foo")

    with pytest.raises(ArtifactError, match="Profile changed since run creation"):
        _initialise_store_with_profile(store, input_file, profile="bar", models_source="profile:bar")


def test_initialise_accepts_resume_with_same_profile(tmp_path: Path) -> None:
    input_file = tmp_path / "fake.pdf"
    _ = input_file.write_text("x", encoding="utf-8")
    store = ArtifactStore(tmp_path / "run")

    _initialise_store_with_profile(store, input_file, profile="foo", models_source="profile:foo")
    _initialise_store_with_profile(store, input_file, profile="foo", models_source="profile:foo")

    assert store.manifest()["profile"] == "foo"
