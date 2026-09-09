"""Named model profile resolution for ChemEx-Lit.

A *profile* is a named bundle of ``text`` / ``vision`` / ``reasoning`` model
endpoints. Profiles live in a user-controlled YAML file (default
``~/.config/chemex-lit/models.yaml``) and are selected explicitly via
``--profile`` on the CLI.

Invariants preserved:

- Credentials are *never* stored in the file. Each model references an
  environment variable name via ``api_key_env`` and the secret is read from
  ``os.environ`` at call time (see :func:`chemex_lit.config.ModelSpec.api_key`).
- The profile layer only selects model endpoints; it does not touch the rest
  of :class:`chemex_lit.config.AppConfig` (mineru, pipeline, output_dir).
- Loading is purely additive: when no profile file exists or no profile is
  selected, callers fall back to the packaged defaults.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chemex_lit.errors import ConfigurationError

logger = logging.getLogger(__name__)


class ProfileSpec(BaseModel):
    """One LLM endpoint inside a profile.

    Structurally compatible with :class:`chemex_lit.config.ModelSpec` so the
    resolved profile can be injected into :class:`AppConfig.models` without
    field-by-field translation. Optional fields default to ``None`` so a
    profile may omit them and inherit the packaged defaults.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    api_key_env: str
    timeout: int | None = Field(default=None, ge=10)
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=256)
    response_format: str | None = None
    extra_payload: dict[str, Any] = Field(default_factory=dict)
    retries: int | None = Field(default=None, ge=1, le=6)

    @field_validator("base_url")
    @classmethod
    def _trim_url(cls, value: str) -> str:
        return value.rstrip("/")


class HostRouteSpec(BaseModel):
    """A non-secret host-side routing policy for one channel."""

    model_config = ConfigDict(extra="forbid")

    policy: str
    model: str | None = None
    fallbacks: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    """A named bundle of text/vision/reasoning endpoints."""

    model_config = ConfigDict(extra="forbid")

    text: ProfileSpec
    vision: ProfileSpec
    reasoning: ProfileSpec | None = None
    host_routes: dict[Literal["text", "table", "structure", "adjudication"], HostRouteSpec] = Field(
        default_factory=dict
    )


class ProfilesFile(BaseModel):
    """Top-level schema for ``models.yaml``.

    The optional ``default_profile`` selects which profile is used when the
    caller does not pass ``--profile``. ``profiles`` maps name -> endpoint
    bundle. Unknown keys are rejected so typos surface immediately.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    default_profile: str | None = None
    profiles: dict[str, Profile] = Field(default_factory=dict)


def default_models_path() -> Path:
    """Return the user models.yaml path under the OS-appropriate config dir.

    Uses :mod:`platformdirs` so the location follows platform conventions:

    - Linux: ``~/.config/chemex-lit/models.yaml``
    - macOS: ``~/Library/Application Support/chemex-lit/models.yaml``
    - Windows: ``%LOCALAPPDATA%\\chemex-lit\\models.yaml``
    """

    from platformdirs import user_config_dir

    return Path(user_config_dir("chemex-lit", appauthor=False)) / "models.yaml"


def load_profiles_file(path: Path | str | None = None) -> ProfilesFile | None:
    """Load a profiles file from disk.

    Args:
        path: Optional explicit path. When ``None``, the user models.yaml at
            :func:`default_models_path` is used if it exists. When an explicit
            path is given, it must exist.

    Returns:
        Parsed :class:`ProfilesFile`, or ``None`` when no file is present at
        the resolved location.

    Raises:
        ConfigurationError: If the file exists but cannot be parsed or fails
            schema validation.
    """

    if path is None:
        resolved = default_models_path()
        if not resolved.is_file():
            return None
    else:
        resolved = Path(path)
        if not resolved.is_file():
            raise ConfigurationError(f"Models file not found: {resolved}")

    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {resolved}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"Models file {resolved} must contain a mapping at the top level"
        )

    try:
        return ProfilesFile.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"Invalid models file {resolved}: {exc}") from exc


def packaged_profiles() -> ProfilesFile:
    """Return the profiles shipped in :mod:`chemex_lit.resources`.

    Used as a documentation sample and as the source for ``chemex-lit models
    list`` when the user has not created their own file yet.
    """

    resource = resources.files("chemex_lit.resources").joinpath("default_models.yaml")
    raw = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    return ProfilesFile.model_validate(raw)


def resolve_profile_name(
    explicit: str | None,
    profiles_file: ProfilesFile | None,
) -> str | None:
    """Resolve which profile name applies given the priority chain.

    Priority: explicit ``--profile`` > file ``default_profile`` > ``None``
    (caller falls back to packaged defaults).
    """

    if explicit is not None:
        return explicit
    if profiles_file is not None and profiles_file.default_profile is not None:
        return profiles_file.default_profile
    return None


def select_profile(
    profiles_file: ProfilesFile | None,
    name: str | None,
) -> tuple[Profile | None, str | None]:
    """Return ``(profile, source_label)`` for the requested name.

    Args:
        profiles_file: Loaded user file (may be ``None``).
        name: Profile name to select. ``None`` means no profile requested.

    Returns:
        Tuple of ``(profile_or_None, source_label_or_None)``. The
        ``source_label`` is ``"profile:<name>"`` when a profile is selected,
        ``None`` otherwise (so the caller can record ``models_source``).

    Raises:
        ConfigurationError: If a name is requested but missing from the file,
            or if a default is declared in the file but absent from
            ``profiles``.
    """

    if name is None:
        return None, None

    if profiles_file is None or not profiles_file.profiles:
        raise ConfigurationError(
            f"Profile {name!r} requested but no profiles file is available. "
            f"Create one at {default_models_path()} or pass --config."
        )

    if name not in profiles_file.profiles:
        available = ", ".join(sorted(profiles_file.profiles)) or "<none>"
        raise ConfigurationError(
            f"Profile {name!r} not found in models file. Available: {available}"
        )

    return profiles_file.profiles[name], f"profile:{name}"
