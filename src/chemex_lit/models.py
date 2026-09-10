"""The complete stable data contract for the ChemEx-Lit v1 pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chemex_lit.errors import ChemExError


RunMode = Literal["auto", "semi", "agent"]
RunStatus = Literal[
    "running",
    "awaiting_input",
    "ready",
    "success",
    "completed_empty",
    "partial",
    "failed",
    "cancelled",
]
ProducerKind = Literal["cli_model", "human", "host_agent"]
Channel = Literal["text", "table", "structure", "adjudication"]

_CANONICAL_MODES: Final[tuple[str, ...]] = get_args(RunMode)


def normalize_mode(value: str) -> RunMode:
    """Return the canonical :data:`RunMode` for ``value``.

    Args:
        value: Raw mode string from a CLI flag or a stored manifest.

    Returns:
        The canonical run mode.

    Raises:
        ChemExError: If ``value`` is not a canonical mode.
    """

    if value in _CANONICAL_MODES:
        return cast(RunMode, value)
    valid = ", ".join(_CANONICAL_MODES)
    raise ChemExError(f"Unknown mode: {value!r}. Valid modes: {valid}.")


class StrictModel(BaseModel):
    """Base model that rejects accidental schema drift."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ProducerSpec(StrictModel):
    """Declares who fulfills one extraction channel within a run."""

    kind: ProducerKind
    model: str | None = None
    provider: str | None = None
    policy: str | None = None
    fallbacks: list[str] | None = None


class ProducerPlan(StrictModel):
    """Declares producers for every extraction and adjudication channel."""

    text: ProducerSpec
    table: ProducerSpec
    structure: ProducerSpec
    adjudication: ProducerSpec

    def channel(self, channel: Channel) -> ProducerSpec:
        """Returns the producer spec configured for one channel."""

        return getattr(self, channel)


class TaskImageAsset(StrictModel):
    """Image asset shipped with a task for visual inspection."""

    path: str
    evidence_id: str | None = None
    context: str | None = None


class TaskAssets(StrictModel):
    """Inline text and image assets attached to an extraction task."""

    text: str | None = None
    images: list[TaskImageAsset] = Field(default_factory=list)


class ExtractionTask(StrictModel):
    """Core-generated task envelope shared across all run modes."""

    task_id: str
    kind: Channel
    instruction_version: str
    instructions: str = ""
    output_schema_version: str
    evidence_ids: list[str] = Field(default_factory=list)
    input_artifacts: list[str] = Field(default_factory=list)
    assets: TaskAssets = Field(default_factory=TaskAssets)
    status: Literal["awaiting", "fulfilled"] = "awaiting"


class SubmissionProducer(StrictModel):
    """Identifies an external task fulfiller submitting task outputs."""

    kind: Literal["human", "host_agent"]
    client_name: str | None = None
    client_version: str | None = None
    model: str | None = None
    policy: str | None = None
    attempt: int | None = Field(default=None, ge=1)


class CandidateSubmission(StrictModel):
    """Task-bounded candidate payloads awaiting Core validation and ID assignment."""

    task_id: str
    producer: SubmissionProducer
    outputs: list[dict[str, Any]]

    @field_validator("outputs")
    @classmethod
    def _forbid_candidate_id(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for index, item in enumerate(value):
            if "candidate_id" in item:
                raise ValueError(
                    "outputs must not include candidate_id "
                    f"(found in outputs[{index}])"
                )
        return value


class AdjudicationDecision(StrictModel):
    """External accept-or-keep-review decision for one assembled reaction."""

    task_id: str
    reaction_id: str
    decision: Literal["accept", "keep_review"]
    rationale: str | None = None
    producer: SubmissionProducer


class ProvenanceEntry(StrictModel):
    """Stable sidecar metadata describing how one candidate was produced."""

    candidate_id: str
    task_id: str
    channel: Channel
    producer_kind: ProducerKind
    provider: str | None = None
    model: str | None = None
    policy: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    prompt_version: str | None = None
    instruction_version: str | None = None
    input_hash: str | None = None
    submission_hash: str | None = None
    client_name: str | None = None
    client_version: str | None = None
    created_at: str


class RunRequest(StrictModel):
    """User request for a new or resumed pipeline run."""

    pdf_path: Path
    output_dir: Path
    external_structures: Path | None = None
    resume: bool = False
    mode: RunMode = "auto"


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


class TaskChannelStatus(StrictModel):
    """Await/fulfilled counts for one external task kind."""

    awaiting: int = Field(default=0, ge=0)
    fulfilled: int = Field(default=0, ge=0)
    awaiting_task_ids: list[str] = Field(default_factory=list)


class RunSummary(StrictModel):
    """Small, JSON-safe summary returned by the pipeline and CLI.

    This model is the machine-readable envelope shared by the ``run``,
    ``resume``, ``status``, ``cancel``, and ``submit --json`` commands.
    All paths (``run_dir``) use forward slashes regardless of platform.
    """

    run_id: str
    status: RunStatus
    records_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    run_dir: str
    stages: dict[str, str] = Field(default_factory=dict)
    awaiting: list[str] = Field(default_factory=list)
    tasks: dict[str, TaskChannelStatus] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Review sidecar models — data foundation for the review workbench.
#
# These models are versioned independently of ReactionRecord (which stays v1).
# "revised" is an *event* (op in ReviewOperation), not a HumanReviewStatus.
# ---------------------------------------------------------------------------

HumanReviewStatus = Literal[
    "unreviewed", "in_review", "confirmed", "pending", "rejected"
]
"""Aggregated human review status for a reaction.

Used by ReviewDecision aggregation logic downstream.  ``"revised"`` is an
event (an operation kind in :class:`ReviewOperation`), **not** a status.
"""


class ReviewParticipant(StrictModel):
    """One participant in the review context for a reaction.

    ``participant_id`` is a stable string that does **not** depend on list
    position.  The recommended scheme is ``{reaction_id}:{role}:{ordinal}``
    where *ordinal* is a 1-based counter among participants sharing the same
    role within the reaction (e.g. ``"rxn-001:reagent:1"``,
    ``"rxn-001:reagent:2"``).
    """

    schema_version: Literal["1.0"] = "1.0"
    participant_id: str
    role: Literal[
        "reactant",
        "product",
        "reagent",
        "catalyst",
        "ligand",
        "solvent",
        "additive",
        "unknown",
    ]
    label: str | None = None
    name: str | None = None
    smiles: str | None = None
    structure_state: Literal["resolved", "unresolved", "not_attempted"] = "not_attempted"
    candidate_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    field_evidence_ids: list[str] = Field(default_factory=list)


class ReviewConditionItem(StrictModel):
    """One condition row in the review context for a reaction.

    ``condition_id`` is a stable string independent of list position.
    ``value`` preserves the verbatim text from the extraction output and is
    never normalised away, even when ``numeric_value`` is also populated.
    ``field_evidence_ids`` may be empty for legacy runs that only had
    reaction-level evidence references.
    """

    schema_version: Literal["1.0"] = "1.0"
    condition_id: str
    kind: Literal[
        "temperature",
        "time",
        "yield",
        "reagent",
        "solvent",
        "amount",
        "equivalents",
        "concentration",
        "catalyst_loading",
        "atmosphere",
        "pressure",
        "addition_order",
        "stage",
        "light",
        "electrochemistry",
        "workup",
        "ee",
        "er",
        "dr",
        "other",
    ]
    value: str | None = None
    numeric_value: float | None = None
    unit: str | None = None
    stage_index: int | None = None
    extraction_state: Literal[
        "extracted", "not_reported_in_output", "not_covered_by_pipeline"
    ] = "extracted"
    evidence_ids: list[str] = Field(default_factory=list)
    field_evidence_ids: list[str] = Field(default_factory=list)


class ReviewContext(StrictModel):
    """Per-reaction review context built from a :class:`ReactionRecord`.

    ``record_hash`` is the SHA-256 of the serialised ReactionRecord JSON that
    this context was derived from.  ``context_schema_note`` carries optional
    degradation messages (e.g. "legacy run without field evidence").
    """

    schema_version: Literal["1.0"] = "1.0"
    reaction_id: str
    record_hash: str
    participants: list[ReviewParticipant] = Field(default_factory=list)
    conditions: list[ReviewConditionItem] = Field(default_factory=list)
    stage_count: int = Field(default=1, ge=0)
    reaction_evidence_ids: list[str] = Field(default_factory=list)
    context_schema_note: str | None = None


class ReviewDecision(StrictModel):
    """One human decision on one review target.

    ``decision_id`` is globally unique.  ``record_hash`` captures the record
    version the reviewer decided upon.

    ``reason`` is **required** when ``conclusion`` is one of
    ``"pending"``, ``"not_applicable"``, or ``"insufficient_evidence"``,
    enforced by a model-level validator.
    """

    schema_version: Literal["1.0"] = "1.0"
    decision_id: str
    reaction_id: str
    target_kind: Literal[
        "reaction",
        "condition",
        "participant_identity",
        "participant_structure",
        "stereo",
        "gold_alignment",
        "completeness",
    ]
    target_id: str
    conclusion: Literal[
        "confirmed",
        "revised",
        "pending",
        "not_applicable",
        "rejected",
        "insufficient_evidence",
    ]
    reason: str | None = None
    reviewer: str
    record_hash: str
    created_at: str

    @field_validator("reviewer")
    @classmethod
    def _non_empty_reviewer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewer must be non-empty")
        return value

    @model_validator(mode="after")
    def _require_reason_for_preliminary_conclusions(self) -> ReviewDecision:
        needs_reason = {"pending", "not_applicable", "insufficient_evidence"}
        if self.conclusion in needs_reason and not self.reason:
            raise ValueError(
                f"reason is required when conclusion is {self.conclusion!r}"
            )
        return self


class ReviewOperation(StrictModel):
    """A single atomic operation inside a :class:`ReviewSubmission`.

    ``"revised"`` is an operation event, **not** a status.
    """

    reaction_id: str
    target_kind: Literal[
        "reaction",
        "condition",
        "participant_identity",
        "participant_structure",
        "stereo",
        "gold_alignment",
        "completeness",
    ]
    target_id: str
    op: Literal[
        "set_value",
        "confirm",
        "mark_pending",
        "mark_not_applicable",
        "reject",
        "add_participant",
        "remove_participant",
        "change_role",
    ]
    path: str | None = None
    old_value: Any | None = None
    new_value: Any | None = None
    reason: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewSubmission(StrictModel):
    """Offline review package for batch import or sync.

    ``submission_id`` is a unique string used for deduplication.
    ``base_record_hashes`` maps each reaction_id to the record hash the
    reviewer saw when making decisions, enabling conflict detection.
    """

    schema_version: Literal["1.0"] = "1.0"
    submission_id: str
    base_record_hashes: dict[str, str] = Field(default_factory=dict)
    operations: list[ReviewOperation] = Field(default_factory=list)
    reviewer: str
    created_at: str

    @field_validator("reviewer")
    @classmethod
    def _non_empty_reviewer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewer must be non-empty")
        return value


class GoldSource(StrictModel):
    """Provenance of a gold-standard benchmark file."""

    file_name: str
    file_hash: str
    entry_count: int = Field(ge=0)
    gold_schema_version: str
    normalization_policy: str | None = None


class GoldParticipantComparison(StrictModel):
    """Comparison result for one participant against a gold-standard entry."""

    participant_id: str
    gold_participant_id: str | None = None
    comparison: Literal[
        "identical",
        "connectivity_differs",
        "stereo_only",
        "stereo_specificity_differs",
        "charge_salt_isotope_differs",
        "missing_extracted",
        "missing_gold",
        "uncomparable",
    ]
    incomparable_reason: str | None = None


class GoldComparison(StrictModel):
    """Per-reaction gold-standard comparison diff.

    ``alignment`` describes how the extracted reaction was matched to the gold
    entry.  ``alignment_basis`` carries optional evidence for the mapping
    decision.
    """

    schema_version: Literal["1.0"] = "1.0"
    reaction_id: str
    gold_reaction_id: str | None = None
    alignment: Literal[
        "explicit_id",
        "mapped",
        "ambiguous",
        "unmatched_extracted",
        "unmatched_gold",
    ]
    alignment_basis: str | None = None
    participant_results: list[GoldParticipantComparison] = Field(default_factory=list)
    gold_source: GoldSource
