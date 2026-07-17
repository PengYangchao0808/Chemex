"""Small explicit configuration surface for ChemEx-Lit."""

from __future__ import annotations

import hashlib
import json
import os
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chemex_lit.errors import ConfigurationError


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
    max_tokens: int = Field(default=8192, ge=256)
    retries: int = Field(default=3, ge=1, le=6)

    @field_validator("base_url")
    @classmethod
    def _trim_url(cls, value: str) -> str:
        return value.rstrip("/")

    def api_key(self) -> str:
        value = os.environ.get(self.api_key_env, "").strip()
        if not value:
            raise ConfigurationError(
                f"Environment variable {self.api_key_env} is required for model {self.model}"
            )
        return value


class ModelsConfig(ConfigModel):
    text: ModelSpec
    vision: ModelSpec


class PipelineConfig(ConfigModel):
    extraction_workers: int = Field(default=3, ge=1, le=3)
    image_workers: int = Field(default=1, ge=1, le=4)
    adjudicate_ambiguous: bool = False
    max_text_chars: int = Field(default=50000, ge=5000)


class AppConfig(ConfigModel):
    schema_version: int = 1
    output_dir: Path = Path("outputs")
    mineru: MinerUConfig
    models: ModelsConfig
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)


_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "CHEMEX_OUTPUT_DIR": ("output_dir",),
    "CHEMEX_MINERU_BASE_URL": ("mineru", "base_url"),
    "CHEMEX_MINERU_API_KEY_ENV": ("mineru", "api_key_env"),
    "CHEMEX_TEXT_BASE_URL": ("models", "text", "base_url"),
    "CHEMEX_TEXT_MODEL": ("models", "text", "model"),
    "CHEMEX_TEXT_API_KEY_ENV": ("models", "text", "api_key_env"),
    "CHEMEX_VISION_BASE_URL": ("models", "vision", "base_url"),
    "CHEMEX_VISION_MODEL": ("models", "vision", "model"),
    "CHEMEX_VISION_API_KEY_ENV": ("models", "vision", "api_key_env"),
}


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


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load packaged defaults, an optional user file, then explicit env overrides."""
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

    for env_name, target in _ENV_OVERRIDES.items():
        if env_name in os.environ:
            _set_path(base, target, os.environ[env_name])

    try:
        return AppConfig.model_validate(base)
    except Exception as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc


def config_fingerprint(config: AppConfig) -> str:
    """Return a stable hash of non-secret effective configuration."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
