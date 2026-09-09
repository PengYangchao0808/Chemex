"""Typed errors used by the public pipeline and CLI."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 form with a ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ChemExError(RuntimeError):
    """Base class for expected ChemEx-Lit failures."""


class ConfigurationError(ChemExError):
    """Raised when configuration is missing or invalid."""


class ExternalServiceError(ChemExError):
    """Raised when MinerU or an LLM service fails."""


class ArtifactError(ChemExError):
    """Raised when persisted pipeline state is invalid."""


class ExtractionError(ChemExError):
    """Raised when an extractor returns unusable output."""
