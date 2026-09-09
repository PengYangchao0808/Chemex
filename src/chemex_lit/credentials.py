"""User credential store for ChemEx-Lit.

Credentials resolve in a fixed order:

1. The process environment (CI, batch runs, temporary overrides).
2. The user credential store ``auth.json`` (interactive convenience).

The store lives outside any repository, follows platform config
conventions (via :mod:`platformdirs`), and is always written atomically
with mode ``0600``. Secrets never appear in ``models.yaml``; that file
only references the logical credential *name* (``api_key_env``) that is
resolved through this module.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chemex_lit.errors import ConfigurationError

STORE_VERSION = 1
EXPIRING_SOON_DAYS = 7

StoreSource = Literal["env", "auth-store"]


class StoredCredential(BaseModel):
    """One stored secret with bookkeeping metadata."""

    model_config = ConfigDict(extra="forbid")

    value: str
    updated_at: str
    expires_at: str | None = None
    source: str = "manual"


class AuthStore(BaseModel):
    """Root object of the ``auth.json`` file."""

    model_config = ConfigDict(extra="forbid")

    version: int = STORE_VERSION
    credentials: dict[str, StoredCredential] = Field(default_factory=dict)


class CredentialResolution(BaseModel):
    """A resolved secret plus where it came from."""

    model_config = ConfigDict(extra="forbid")

    value: str
    source: StoreSource


def auth_store_path() -> Path:
    """Return the ``auth.json`` path under the OS-appropriate config dir.

    The ``CHEMEX_AUTH_STORE`` environment variable overrides the location
    (used by tests and multi-profile setups).
    """

    override = os.environ.get("CHEMEX_AUTH_STORE", "").strip()
    if override:
        return Path(override)
    from platformdirs import user_config_dir

    return Path(user_config_dir("chemex-lit", appauthor=False)) / "auth.json"


def load_auth_store(path: Path | None = None) -> AuthStore:
    """Load the credential store, returning an empty store when absent."""

    store_path = path or auth_store_path()
    if not store_path.is_file():
        return AuthStore()
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
        return AuthStore.model_validate(data)
    except (json.JSONDecodeError, OSError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid credential store {store_path}: {exc}") from exc


def resolve_credential(name: str) -> CredentialResolution | None:
    """Resolve one credential: environment first, then ``auth.json``.

    Returns ``None`` when the credential is set nowhere.
    """

    env_value = os.environ.get(name, "").strip()
    if env_value:
        return CredentialResolution(value=env_value, source="env")
    entry = load_auth_store().credentials.get(name)
    if entry is not None and entry.value.strip():
        return CredentialResolution(value=entry.value.strip(), source="auth-store")
    return None


def credential_value(name: str, *, what: str) -> str:
    """Return the resolved secret or raise a actionable :class:`ConfigurationError`."""

    resolution = resolve_credential(name)
    if resolution is None:
        raise ConfigurationError(
            f"Credential {name} is required for {what}; set it via "
            f"`chemex-lit auth set {name}` or the {name} environment variable"
        )
    return resolution.value


def credential_status(name: str) -> tuple[bool, str | None, int | None]:
    """Return ``(present, source, expires_in_days)`` for one credential.

    ``expires_in_days`` is only known for store-sourced entries that carry
    an ``expires_at``; a negative value means the stored credential has
    already expired.
    """

    resolution = resolve_credential(name)
    if resolution is None:
        return False, None, None
    days: int | None = None
    if resolution.source == "auth-store":
        entry = load_auth_store().credentials.get(name)
        if entry is not None:
            days = expires_in_days(entry)
    return True, resolution.source, days


def check_credential_status(name: str) -> tuple[bool, str | None, int | None]:
    """Like :func:`credential_status` but treats expired secrets as absent."""

    present, source, days = credential_status(name)
    if present and days is not None and days < 0:
        return False, source, days
    return present, source, days


def validate_credential_name(name: str) -> str:
    """Validate and normalize one credential name.

    Names are lookup keys referenced by ``api_key_env`` entries, so they
    follow environment-variable conventions: letters, digits, and
    underscores, without a leading digit.

    Raises:
        ConfigurationError: If the name is empty, malformed, or would be
            ambiguous (e.g. starts with a digit).
    """

    name = name.strip()
    if (
        not name
        or name[0].isdigit()
        or not all(character.isalnum() or character == "_" for character in name)
    ):
        raise ConfigurationError(
            f"Invalid credential name {name!r}; names are lookup keys such as "
            "MINERU_API_KEY (letters, digits, underscores; cannot start with a "
            "digit). The secret value is entered after the name, or piped via "
            "--stdin. Example: chemex-lit auth set MINERU_API_KEY"
        )
    return name


def set_credential(
    name: str,
    value: str,
    *,
    expires_at: str | None = None,
    store_path: Path | None = None,
) -> None:
    """Create or replace one stored credential (atomic write, mode 0600)."""

    name = validate_credential_name(name)
    if not value.strip():
        raise ConfigurationError(f"Credential {name} must not be empty")
    parsed_expiry = _validate_expiry(expires_at)
    store = load_auth_store(store_path)
    store.credentials[name] = StoredCredential(
        value=value.strip(),
        updated_at=_utc_now(),
        expires_at=parsed_expiry,
    )
    _atomic_write_json(store_path or auth_store_path(), store.model_dump(mode="json"))


def remove_credential(name: str, *, store_path: Path | None = None) -> bool:
    """Remove one stored credential; returns whether it existed."""

    name = validate_credential_name(name)
    path = store_path or auth_store_path()
    store = load_auth_store(path)
    if name not in store.credentials:
        return False
    del store.credentials[name]
    _atomic_write_json(path, store.model_dump(mode="json"))
    return True


def expires_in_days(entry: StoredCredential, *, now: datetime | None = None) -> int | None:
    """Whole days until the stored credential expires; ``None`` if unknown.

    Unparseable timestamps are treated as "no expiry known" so a corrupt
    optional field cannot break ``check`` or ``auth list``.
    """

    if entry.expires_at is None:
        return None
    try:
        expiry = _parse_iso(entry.expires_at)
    except ValueError:
        return None
    moment = now or datetime.now(timezone.utc)
    return (expiry - moment).days


def _validate_expiry(expires_at: str | None) -> str | None:
    if expires_at is None or not expires_at.strip():
        return None
    try:
        return _parse_iso(expires_at.strip()).isoformat()
    except ValueError as exc:
        raise ConfigurationError(
            f"Invalid --expires value {expires_at!r}; use an ISO date such as 2026-10-03"
        ) from exc


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* atomically with mode 0600 (temp file, then replace)."""

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".auth-", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
