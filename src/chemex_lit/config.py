"""Small explicit configuration surface for ChemEx-Lit."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chemex_lit.errors import ConfigurationError
from chemex_lit.profiles import (
    HostRouteSpec,
    Profile,
    ProfilesFile,
    load_profiles_file,
    packaged_profiles,
    resolve_profile_name,
    select_profile,
)

logger = logging.getLogger(__name__)


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MinerUConfig(ConfigModel):
    base_url: str = "https://mineru.net"
    api_key_env: str = "MINERU_API_KEY"
    timeout: int = Field(default=600, ge=30)
    poll_interval: float = Field(default=5.0, ge=0.1)
    language: str = "en"
    backend: str = "pipeline"


class ModelSpec(ConfigModel):
    base_url: str
    model: str
    api_key_env: str
    timeout: int = Field(default=300, ge=10)
    temperature: float | None = 0.0
    max_tokens: int | None = Field(default=8192, ge=256)
    response_format: Literal["json_object"] | None = "json_object"
    extra_payload: dict[str, Any] = Field(default_factory=dict)
    retries: int = Field(default=3, ge=1, le=6)

    @field_validator("base_url")
    @classmethod
    def _trim_url(cls, value: str) -> str:
        return value.rstrip("/")

    def api_key(self) -> str:
        from chemex_lit.credentials import credential_value

        return credential_value(self.api_key_env, what=f"model {self.model}")


class ModelsConfig(ConfigModel):
    text: ModelSpec
    vision: ModelSpec
    reasoning: ModelSpec | None = None

    def reasoning_spec(self) -> tuple[ModelSpec, bool]:
        """Return the effective reasoning model and whether text fallback was used.

        Returns:
            A tuple of ``(model_spec, used_fallback)``.
        """
        if self.reasoning is None:
            return self.text, True
        return self.reasoning, False


class PipelineConfig(ConfigModel):
    extraction_workers: int = Field(default=3, ge=1, le=3)
    image_workers: int = Field(default=1, ge=1, le=4)
    adjudicate_ambiguous: bool = False
    max_text_chars: int = Field(default=50000, ge=5000)


class AppConfig(ConfigModel):
    schema_version: int = 1
    output_dir: Path = Path("outputs")
    profile: str | None = None
    models_source: str = "default"
    host_routes: dict[str, HostRouteSpec] = Field(default_factory=dict)
    mineru: MinerUConfig
    models: ModelsConfig
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)


# Env vars that override model endpoint selection. When a profile is active,
# these still win (preserving debug escape hatches) but emit a warning so the
# user knows the run is not actually using the named profile's model.
_MODEL_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "CHEMEX_TEXT_BASE_URL": ("models", "text", "base_url"),
    "CHEMEX_TEXT_MODEL": ("models", "text", "model"),
    "CHEMEX_TEXT_API_KEY_ENV": ("models", "text", "api_key_env"),
    "CHEMEX_VISION_BASE_URL": ("models", "vision", "base_url"),
    "CHEMEX_VISION_MODEL": ("models", "vision", "model"),
    "CHEMEX_VISION_API_KEY_ENV": ("models", "vision", "api_key_env"),
    "CHEMEX_REASONING_BASE_URL": ("models", "reasoning", "base_url"),
    "CHEMEX_REASONING_MODEL": ("models", "reasoning", "model"),
    "CHEMEX_REASONING_API_KEY_ENV": ("models", "reasoning", "api_key_env"),
}

_NON_MODEL_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "CHEMEX_OUTPUT_DIR": ("output_dir",),
    "CHEMEX_MINERU_BASE_URL": ("mineru", "base_url"),
    "CHEMEX_MINERU_API_KEY_ENV": ("mineru", "api_key_env"),
}

_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {**_NON_MODEL_ENV_OVERRIDES, **_MODEL_ENV_OVERRIDES}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    current = data
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value


def _apply_profile_models(
    base: dict[str, Any],
    profile: Profile,
) -> dict[str, Any]:
    """Overlay a profile's text/vision/reasoning onto a config dict.

    Each :class:`ProfileSpec` may omit optional fields (timeout, max_tokens,
    ...). When omitted, the existing value in ``base['models']`` is preserved
    so the profile inherits sensible defaults from the packaged YAML.
    """

    models = dict(base.get("models") or {})

    def overlay(slot: str, spec: dict[str, Any]) -> None:
        merged = dict(models.get(slot) or {})
        for key, value in spec.items():
            if value is None:
                continue
            merged[key] = value
        models[slot] = merged

    overlay("text", profile.text.model_dump(exclude_none=True))
    overlay("vision", profile.vision.model_dump(exclude_none=True))
    if profile.reasoning is not None:
        overlay("reasoning", profile.reasoning.model_dump(exclude_none=True))
    else:
        models.pop("reasoning", None)

    return {
        **base,
        "models": models,
        "host_routes": {
            channel: route.model_dump(mode="json")
            for channel, route in profile.host_routes.items()
        },
    }


def _warn_env_overrides_active(profile_name: str | None) -> None:
    """Emit warnings for any ``CHEMEX_*`` model env vars that override a profile.

    These remain functional as a debug escape hatch, but benchmark runs that
    rely on profile reproducibility need to know the active profile is being
    shadowed by ad-hoc environment configuration.
    """

    if profile_name is None:
        return
    shadowing = [name for name in _MODEL_ENV_OVERRIDES if name in os.environ]
    if not shadowing:
        return
    logger.warning(
        "Profile %r active but %d CHEMEX_* model env override(s) will take "
        "precedence: %s. Unset them for reproducible benchmark runs.",
        profile_name,
        len(shadowing),
        ", ".join(sorted(shadowing)),
    )


def load_config(
    path: Path | str | None = None,
    *,
    profile: str | None = None,
    models_path: Path | str | None = None,
) -> AppConfig:
    """Load packaged defaults, optional user YAML, optional profile, then env overrides.

    Resolution order (highest priority last):

    1. Packaged ``default_config.yaml``.
    2. User YAML via ``path`` (merged on top).
    3. Resolved profile from ``models_path`` (defaults to the user
       ``~/.config/chemex-lit/models.yaml``). The profile overlays
       ``models.{text,vision,reasoning}`` only; optional fields inherit from
       step 1/2.
    4. ``CHEMEX_*`` environment variables (debug escape hatch; warns when a
       profile is active).
    """

    default_resource = resources.files("chemex_lit.resources").joinpath("default_config.yaml")
    base = yaml.safe_load(default_resource.read_text(encoding="utf-8")) or {}

    if path is not None:
        user_path = Path(path)
        if not user_path.is_file():
            raise ConfigurationError(f"Configuration file not found: {user_path}")
        user = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
        if not isinstance(user, dict):
            raise ConfigurationError("Configuration root must be a mapping")
        base = _merge(base, user)

    profiles_file: ProfilesFile | None = load_profiles_file(models_path)
    if profiles_file is None and models_path is None and profile is not None:
        profiles_file = packaged_profiles()
    resolved_profile_name = resolve_profile_name(profile, profiles_file)
    selected_profile, source_label = select_profile(profiles_file, resolved_profile_name)
    if selected_profile is not None:
        base = _apply_profile_models(base, selected_profile)
        base["profile"] = resolved_profile_name
        base["models_source"] = source_label

    for env_name, target in _ENV_OVERRIDES.items():
        if env_name in os.environ:
            _set_path(base, target, os.environ[env_name])

    _warn_env_overrides_active(resolved_profile_name)

    if "profile" not in base:
        base["profile"] = None
    if "models_source" not in base:
        base["models_source"] = "default"

    try:
        return AppConfig.model_validate(base)
    except Exception as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc


def config_fingerprint(config: AppConfig) -> str:
    """Return a stable hash of non-secret effective configuration."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
