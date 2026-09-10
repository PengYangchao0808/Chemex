"""Contract tests for review sidecar models."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from chemex_lit.models import (
    GoldComparison,
    GoldParticipantComparison,
    GoldSource,
    HumanReviewStatus,
    ReviewConditionItem,
    ReviewContext,
    ReviewDecision,
    ReviewOperation,
    ReviewParticipant,
    ReviewSubmission,
    StructureImageAsset,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal valid payloads for every new model.
# ---------------------------------------------------------------------------

def _review_participant_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "participant_id": "rxn-001:reagent:1",
        "role": "reagent",
    }
    base.update(overrides)
    return base


def _review_condition_item_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "condition_id": "cond-001",
        "kind": "temperature",
    }
    base.update(overrides)
    return base


def _review_context_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reaction_id": "rxn-001",
        "record_hash": "sha256:" + "a" * 64,
    }
    base.update(overrides)
    return base


def _review_decision_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decision_id": "dec-001",
        "reaction_id": "rxn-001",
        "target_kind": "reaction",
        "target_id": "rxn-001",
        "conclusion": "confirmed",
        "reviewer": "Dr. Smith",
        "record_hash": "sha256:" + "b" * 64,
        "created_at": "2026-09-10T00:00:00Z",
    }
    base.update(overrides)
    return base


def _review_operation_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reaction_id": "rxn-001",
        "target_kind": "condition",
        "target_id": "cond-001",
        "op": "set_value",
    }
    base.update(overrides)
    return base


def _review_submission_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "submission_id": "sub-001",
        "reviewer": "Dr. Smith",
        "created_at": "2026-09-10T00:00:00Z",
    }
    base.update(overrides)
    return base


def _gold_source_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "file_name": "benchmark.jsonl",
        "file_hash": "sha256:" + "c" * 64,
        "entry_count": 42,
        "gold_schema_version": "1.0",
    }
    base.update(overrides)
    return base


def _gold_participant_comparison_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "participant_id": "rxn-001:product:1",
        "comparison": "identical",
    }
    base.update(overrides)
    return base


def _gold_comparison_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reaction_id": "rxn-001",
        "alignment": "mapped",
        "gold_source": _gold_source_payload(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Round-trip: validate → dump → re-validate produces identical instance.
# ---------------------------------------------------------------------------


class TestReviewParticipantRoundTrip:
    def test_minimal(self) -> None:
        payload = _review_participant_payload()
        instance = ReviewParticipant.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewParticipant.model_validate(dumped) == instance

    def test_all_fields(self) -> None:
        payload = _review_participant_payload(
            label="A",
            name="Acetone",
            smiles="CC(=O)C",
            structure_state="resolved",
            candidate_ids=["sc-1"],
            evidence_ids=["ev-1"],
        )
        instance = ReviewParticipant.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewParticipant.model_validate(dumped) == instance

    def test_schema_version_defaults(self) -> None:
        instance = ReviewParticipant.model_validate(_review_participant_payload())
        assert instance.schema_version == "1.0"


class TestReviewConditionItemRoundTrip:
    def test_minimal(self) -> None:
        payload = _review_condition_item_payload()
        instance = ReviewConditionItem.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewConditionItem.model_validate(dumped) == instance

    def test_all_fields(self) -> None:
        payload = _review_condition_item_payload(
            value="25 °C",
            numeric_value=25.0,
            unit="°C",
            stage_index=0,
            extraction_state="extracted",
            evidence_ids=["ev-1"],
            field_evidence_ids=["ev-2"],
        )
        instance = ReviewConditionItem.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewConditionItem.model_validate(dumped) == instance

    def test_verbatim_value_preserved_with_numeric(self) -> None:
        payload = _review_condition_item_payload(
            value="room temperature",
            numeric_value=22.0,
            unit="°C",
        )
        instance = ReviewConditionItem.model_validate(payload)
        assert instance.value == "room temperature"
        assert instance.numeric_value == 22.0

    def test_default_extraction_state(self) -> None:
        instance = ReviewConditionItem.model_validate(_review_condition_item_payload())
        assert instance.extraction_state == "extracted"


class TestReviewContextRoundTrip:
    def test_minimal(self) -> None:
        payload = _review_context_payload()
        instance = ReviewContext.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewContext.model_validate(dumped) == instance

    def test_nested_participants_and_conditions(self) -> None:
        payload = _review_context_payload(
            participants=[_review_participant_payload()],
            conditions=[_review_condition_item_payload()],
            stage_count=2,
            reaction_evidence_ids=["ev-1"],
            context_schema_note="legacy run",
        )
        instance = ReviewContext.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewContext.model_validate(dumped) == instance
        assert len(instance.participants) == 1
        assert len(instance.conditions) == 1

    def test_stage_count_defaults_to_one(self) -> None:
        instance = ReviewContext.model_validate(_review_context_payload())
        assert instance.stage_count == 1


class TestReviewDecisionRoundTrip:
    def test_confirmed(self) -> None:
        payload = _review_decision_payload()
        instance = ReviewDecision.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewDecision.model_validate(dumped) == instance

    def test_pending_with_reason(self) -> None:
        payload = _review_decision_payload(
            conclusion="pending",
            reason="needs more data",
        )
        instance = ReviewDecision.model_validate(payload)
        assert instance.conclusion == "pending"
        assert instance.reason == "needs more data"


class TestReviewOperationRoundTrip:
    def test_set_value(self) -> None:
        payload = _review_operation_payload(
            path="temperature_c",
            old_value=25.0,
            new_value=30.0,
            reason="typo",
            evidence_ids=["ev-1"],
        )
        instance = ReviewOperation.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewOperation.model_validate(dumped) == instance

    def test_confirm(self) -> None:
        payload = _review_operation_payload(op="confirm")
        instance = ReviewOperation.model_validate(payload)
        assert instance.op == "confirm"


class TestReviewSubmissionRoundTrip:
    def test_minimal(self) -> None:
        payload = _review_submission_payload()
        instance = ReviewSubmission.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewSubmission.model_validate(dumped) == instance

    def test_with_operations_and_hashes(self) -> None:
        payload = _review_submission_payload(
            base_record_hashes={"rxn-001": "sha256:" + "a" * 64},
            operations=[_review_operation_payload()],
        )
        instance = ReviewSubmission.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert ReviewSubmission.model_validate(dumped) == instance


class TestGoldSourceRoundTrip:
    def test_full(self) -> None:
        payload = _gold_source_payload(normalization_policy="canonical-tautomer")
        instance = GoldSource.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert GoldSource.model_validate(dumped) == instance


class TestGoldParticipantComparisonRoundTrip:
    def test_identical(self) -> None:
        payload = _gold_participant_comparison_payload()
        instance = GoldParticipantComparison.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert GoldParticipantComparison.model_validate(dumped) == instance

    def test_uncomparable_with_reason(self) -> None:
        payload = _gold_participant_comparison_payload(
            comparison="uncomparable",
            incomparable_reason="salt form",
            gold_participant_id="gold-p-1",
        )
        instance = GoldParticipantComparison.model_validate(payload)
        assert instance.comparison == "uncomparable"


class TestGoldComparisonRoundTrip:
    def test_minimal(self) -> None:
        payload = _gold_comparison_payload()
        instance = GoldComparison.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert GoldComparison.model_validate(dumped) == instance

    def test_with_participant_results(self) -> None:
        payload = _gold_comparison_payload(
            gold_reaction_id="gold-rxn-1",
            alignment_basis="shared_inchi_key",
            participant_results=[_gold_participant_comparison_payload()],
        )
        instance = GoldComparison.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert GoldComparison.model_validate(dumped) == instance


# ---------------------------------------------------------------------------
# Strictness: extra fields are rejected (extra="forbid").
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ReviewParticipant, _review_participant_payload()),
        (ReviewConditionItem, _review_condition_item_payload()),
        (ReviewContext, _review_context_payload()),
        (ReviewDecision, _review_decision_payload()),
        (ReviewOperation, _review_operation_payload()),
        (ReviewSubmission, _review_submission_payload()),
        (GoldSource, _gold_source_payload()),
        (GoldParticipantComparison, _gold_participant_comparison_payload()),
        (GoldComparison, _gold_comparison_payload()),
    ],
)
def test_extra_fields_rejected(
    model: type[Any], payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        model.model_validate({**payload, "unexpected_field": True})


# ---------------------------------------------------------------------------
# Validator correctness: reviewer non-empty, reason required for preliminary.
# ---------------------------------------------------------------------------


class TestReviewDecisionValidators:
    def test_empty_reviewer_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reviewer"):
            ReviewDecision.model_validate(_review_decision_payload(reviewer=""))

    def test_whitespace_reviewer_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reviewer"):
            ReviewDecision.model_validate(_review_decision_payload(reviewer="   "))

    @pytest.mark.parametrize("conclusion", ["pending", "not_applicable", "insufficient_evidence"])
    def test_reason_required_for_preliminary_conclusions(
        self, conclusion: str
    ) -> None:
        with pytest.raises(ValidationError, match="reason"):
            ReviewDecision.model_validate(
                _review_decision_payload(conclusion=conclusion, reason=None)
            )

    @pytest.mark.parametrize("conclusion", ["confirmed", "revised", "rejected"])
    def test_reason_not_required_for_final_conclusions(self, conclusion: str) -> None:
        instance = ReviewDecision.model_validate(
            _review_decision_payload(conclusion=conclusion, reason=None)
        )
        assert instance.conclusion == conclusion
        assert instance.reason is None

    def test_pending_with_reason_passes(self) -> None:
        instance = ReviewDecision.model_validate(
            _review_decision_payload(
                conclusion="pending",
                reason="awaiting NMR data",
            )
        )
        assert instance.conclusion == "pending"
        assert instance.reason == "awaiting NMR data"


class TestReviewSubmissionValidators:
    def test_empty_reviewer_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reviewer"):
            ReviewSubmission.model_validate(_review_submission_payload(reviewer=""))

    def test_whitespace_reviewer_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reviewer"):
            ReviewSubmission.model_validate(_review_submission_payload(reviewer="   "))


# ---------------------------------------------------------------------------
# Literal type alias: HumanReviewStatus.
# ---------------------------------------------------------------------------


class TestHumanReviewStatus:
    @pytest.mark.parametrize(
        "value",
        ["unreviewed", "in_review", "confirmed", "pending", "rejected"],
    )
    def test_valid_statuses(self, value: str) -> None:
        assert value in HumanReviewStatus.__args__  # type: ignore[attr-defined]

    def test_revised_not_in_statuses(self) -> None:
        assert "revised" not in HumanReviewStatus.__args__  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Literal exhaustiveness: every Literal member is accepted.
# ---------------------------------------------------------------------------


class TestLiteralExhaustiveness:
    @pytest.mark.parametrize(
        "role",
        [
            "reactant", "product", "reagent", "catalyst",
            "ligand", "solvent", "additive", "unknown",
        ],
    )
    def test_participant_role(self, role: str) -> None:
        instance = ReviewParticipant.model_validate(
            _review_participant_payload(role=role)
        )
        assert instance.role == role

    @pytest.mark.parametrize(
        "kind",
        [
            "temperature", "time", "yield", "reagent", "solvent", "amount",
            "equivalents", "concentration", "catalyst_loading", "atmosphere",
            "pressure", "addition_order", "stage", "light",
            "electrochemistry", "workup", "ee", "er", "dr", "other",
        ],
    )
    def test_condition_kind(self, kind: str) -> None:
        instance = ReviewConditionItem.model_validate(
            _review_condition_item_payload(kind=kind)
        )
        assert instance.kind == kind

    @pytest.mark.parametrize(
        "state",
        ["resolved", "unresolved", "not_attempted"],
    )
    def test_structure_state(self, state: str) -> None:
        instance = ReviewParticipant.model_validate(
            _review_participant_payload(structure_state=state)
        )
        assert instance.structure_state == state

    @pytest.mark.parametrize(
        "extraction_state",
        ["extracted", "not_reported_in_output", "not_covered_by_pipeline"],
    )
    def test_extraction_state(self, extraction_state: str) -> None:
        instance = ReviewConditionItem.model_validate(
            _review_condition_item_payload(extraction_state=extraction_state)
        )
        assert instance.extraction_state == extraction_state

    @pytest.mark.parametrize(
        "conclusion",
        ["confirmed", "revised", "pending", "not_applicable", "rejected", "insufficient_evidence"],
    )
    def test_decision_conclusion(self, conclusion: str) -> None:
        payload = _review_decision_payload(conclusion=conclusion)
        if conclusion in ("pending", "not_applicable", "insufficient_evidence"):
            payload["reason"] = "justified"
        instance = ReviewDecision.model_validate(payload)
        assert instance.conclusion == conclusion

    @pytest.mark.parametrize(
        "op",
        [
            "set_value", "confirm", "mark_pending", "mark_not_applicable",
            "reject", "add_participant", "remove_participant", "change_role",
        ],
    )
    def test_operation_op(self, op: str) -> None:
        instance = ReviewOperation.model_validate(
            _review_operation_payload(op=op)
        )
        assert instance.op == op

    @pytest.mark.parametrize(
        "alignment",
        ["explicit_id", "mapped", "ambiguous", "unmatched_extracted", "unmatched_gold"],
    )
    def test_gold_alignment(self, alignment: str) -> None:
        instance = GoldComparison.model_validate(
            _gold_comparison_payload(alignment=alignment)
        )
        assert instance.alignment == alignment

    @pytest.mark.parametrize(
        "comparison",
        [
            "identical", "connectivity_differs", "stereo_only",
            "stereo_specificity_differs", "charge_salt_isotope_differs",
            "missing_extracted", "missing_gold", "uncomparable",
        ],
    )
    def test_gold_participant_comparison(self, comparison: str) -> None:
        instance = GoldParticipantComparison.model_validate(
            _gold_participant_comparison_payload(comparison=comparison)
        )
        assert instance.comparison == comparison

    @pytest.mark.parametrize(
        "target_kind",
        [
            "reaction", "condition", "participant_identity",
            "participant_structure", "stereo", "gold_alignment", "completeness",
        ],
    )
    def test_target_kind_decision(self, target_kind: str) -> None:
        instance = ReviewDecision.model_validate(
            _review_decision_payload(target_kind=target_kind)
        )
        assert instance.target_kind == target_kind

    @pytest.mark.parametrize(
        "target_kind",
        [
            "reaction", "condition", "participant_identity",
            "participant_structure", "stereo", "gold_alignment", "completeness",
        ],
    )
    def test_target_kind_operation(self, target_kind: str) -> None:
        instance = ReviewOperation.model_validate(
            _review_operation_payload(target_kind=target_kind)
        )
        assert instance.target_kind == target_kind


# ---------------------------------------------------------------------------
# StructureImageAsset
# ---------------------------------------------------------------------------


class TestStructureImageAssetRoundTrip:
    def test_minimal(self) -> None:
        payload: dict[str, Any] = {
            "asset_id": "abc123-def456",
            "render_status": "ok",
        }
        instance = StructureImageAsset.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert StructureImageAsset.model_validate(dumped) == instance

    def test_full(self) -> None:
        payload: dict[str, Any] = {
            "asset_id": "abc123-def456",
            "svg_path": "review_assets/abc123-def456.svg",
            "png_path": "review_assets/abc123-def456.png",
            "structure_hash": "a" * 16,
            "render_status": "ok",
            "error": None,
        }
        instance = StructureImageAsset.model_validate(payload)
        dumped = instance.model_dump(mode="json", exclude_none=True)
        assert StructureImageAsset.model_validate(dumped) == instance
        assert instance.svg_path == "review_assets/abc123-def456.svg"

    def test_error_status_with_message(self) -> None:
        payload: dict[str, Any] = {
            "asset_id": "bad",
            "render_status": "invalid_smiles",
            "error": "Cannot parse SMILES",
        }
        instance = StructureImageAsset.model_validate(payload)
        assert instance.render_status == "invalid_smiles"
        assert instance.error == "Cannot parse SMILES"


@pytest.mark.parametrize(
    "status",
    ["ok", "rdkit_missing", "invalid_smiles", "missing_smiles"],
)
def test_structure_image_asset_render_statuses(status: str) -> None:
    instance = StructureImageAsset.model_validate(
        {"asset_id": "test", "render_status": status}
    )
    assert instance.render_status == status


def test_structure_image_asset_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError, match="not permitted"):
        StructureImageAsset.model_validate(
            {"asset_id": "test", "render_status": "ok", "extra": True}
        )
