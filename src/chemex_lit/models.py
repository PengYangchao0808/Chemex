"""The complete stable data contract for the ChemEx-Lit v1 pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Base model that rejects accidental schema drift."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RunRequest(StrictModel):
    """User request for a new or resumed pipeline run."""

    pdf_path: Path
    output_dir: Path
    external_structures: Path | None = None
    resume: bool = False


class EvidenceRef(StrictModel):
    """Traceable source evidence supporting an extracted claim."""

    evidence_id: str
    kind: Literal["text", "table", "image"]
    page: int | None = None
    source_path: str
    asset_path: str | None = None
    text: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class DocumentBundle(StrictModel):
    """Canonical in-memory representation of one parsed paper."""

    document_id: str
    markdown: str
    images: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class CompoundRef(StrictModel):
    """A compound mention, optionally resolved to a chemical structure."""

    label: str | None = None
    name: str | None = None
    smiles: str | None = None
    role: Literal["reactant", "product", "reagent", "unknown"] = "unknown"

    @field_validator("label", "name", "smiles", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class ReactionCandidate(StrictModel):
    """Reaction assertion emitted by text, table, or scheme extraction."""

    candidate_id: str
    source: Literal["text", "table", "scheme"]
    reactants: list[CompoundRef] = Field(default_factory=list)
    products: list[CompoundRef] = Field(default_factory=list)
    reagents: list[str] = Field(default_factory=list)
    solvents: list[str] = Field(default_factory=list)
    temperature_c: float | None = None
    time: str | None = None
    yield_pct: float | None = Field(default=None, ge=0, le=100)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class StructureCandidate(StrictModel):
    """SMILES assertion emitted by vision extraction or a human."""

    candidate_id: str
    compound_label: str | None = None
    smiles: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)

    @field_validator("smiles")
    @classmethod
    def _non_empty_smiles(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("smiles must not be empty")
        return value


class ValidationIssue(StrictModel):
    """Machine-readable validation finding."""

    code: str
    severity: Literal["info", "warning", "error"]
    target_id: str
    message: str
    suggested_value: str | None = None


class ReactionRecord(StrictModel):
    """Stable v1 public reaction record."""

    schema_version: Literal["1.0"] = "1.0"
    reaction_id: str
    reactants: list[CompoundRef] = Field(default_factory=list)
    products: list[CompoundRef] = Field(default_factory=list)
    reagents: list[str] = Field(default_factory=list)
    solvents: list[str] = Field(default_factory=list)
    temperature_c: float | None = None
    time: str | None = None
    yield_pct: float | None = Field(default=None, ge=0, le=100)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    review_status: Literal["accepted", "needs_review", "rejected"]
    issues: list[ValidationIssue] = Field(default_factory=list)


class RunSummary(StrictModel):
    """Small, JSON-safe summary returned by the pipeline and CLI."""

    run_id: str
    status: Literal["success", "completed_empty", "partial", "failed"]
    records_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    output_dir: str
    stages: dict[str, str] = Field(default_factory=dict)
