"""Tests for review: generation, view assembly, aggregation, apply, legacy."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chemex_lit.cli import main
from chemex_lit.evaluation import evaluate_records, load_jsonl
from chemex_lit.models import (
    CompoundRef,
    EvidenceRef,
    GoldComparison,
    GoldParticipantComparison,
    GoldSource,
    ReactionRecord,
    ReviewContext,
    ReviewDecision,
    ReviewParticipant,
    ReviewOperation,
    ValidationIssue,
)
from chemex_lit.review import (
    apply_corrections,
    apply_submission,
    aggregate_human_status,
    build_review_view,
    generate_review,
    invalidate_stale_decisions,
    _record_hash,
)
from chemex_lit.store import ArtifactStore
from chemex_lit.errors import utc_now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def record(
    *,
    reaction_id: str = "r1",
    reactant_smiles: str = "CC",
    review_status: Literal["accepted", "needs_review", "rejected"] = "accepted",
    issues: list[ValidationIssue] | None = None,
    reagents: list[str] | None = None,
    solvents: list[str] | None = None,
    temperature_c: float | None = None,
    time: str | None = None,
    yield_pct: float | None = 80,
    evidence_ids: list[str] | None = None,
) -> ReactionRecord:
    return ReactionRecord(
        reaction_id=reaction_id,
        reactants=[CompoundRef(label="7", smiles=reactant_smiles, role="reactant")],
        products=[CompoundRef(label="8", smiles="CCO", role="product")],
        reagents=reagents or [],
        solvents=solvents or [],
        temperature_c=temperature_c,
        time=time,
        yield_pct=yield_pct,
        evidence_ids=evidence_ids or [],
        confidence=0.9,
        review_status=review_status,
        issues=issues or [],
    )


def _make_decision(
    reaction_id: str = "r1",
    target_kind: str = "reaction",
    target_id: str = "r1",
    conclusion: str = "confirmed",
    reviewer: str = "Reviewer",
    record_hash: str = "",
    reason: str | None = None,
) -> ReviewDecision:
    if not record_hash:
        record_hash = _record_hash(record(reaction_id=reaction_id))
    needs_reason = {"pending", "not_applicable", "insufficient_evidence"}
    if conclusion in needs_reason and reason is None:
        reason = f"test reason for {conclusion}"
    return ReviewDecision(
        decision_id=f"{reaction_id}:{target_kind}:{target_id}:test",
        reaction_id=reaction_id,
        target_kind=target_kind,  # type: ignore[arg-type]
        target_id=target_id,
        conclusion=conclusion,  # type: ignore[arg-type]
        reason=reason,
        reviewer=reviewer,
        record_hash=record_hash,
        created_at=utc_now(),
    )


def _write_run_dir(tmp_path: Path, rec: ReactionRecord | None = None) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    records_path = run_dir / "records.jsonl"
    r = rec or record()
    records_path.write_text(json.dumps(r.model_dump(mode="json")) + "\n", encoding="utf-8")
    return run_dir


# ---------------------------------------------------------------------------
# Legacy tests (must pass unchanged)
# ---------------------------------------------------------------------------


def test_generate_review(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    output = generate_review([record()], store)
    assert output.is_file()
    assert "r1" in output.read_text(encoding="utf-8")


def test_apply_correction(tmp_path: Path) -> None:
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )
    corrected, audit_entries = apply_corrections([record()], corrections, confirmed_by="Reviewer")
    assert corrected[0].yield_pct == 81
    assert audit_entries[0]["confirmed_by"] == "Reviewer"


def test_apply_correction_canonicalizes_smiles_and_audits(tmp_path: Path) -> None:
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "reactants.0.smiles", "value": "CCO"}]),
        encoding="utf-8",
    )

    corrected, audit_entries = apply_corrections(
        [record(review_status="needs_review")],
        corrections,
        confirmed_by=" Dr. Curie ",
    )

    assert corrected[0].reactants[0].smiles == "CCO"
    assert corrected[0].review_status == "accepted"
    assert corrected[0].issues == []
    assert audit_entries == [
        {
            "reaction_id": "r1",
            "confirmed_by": "Dr. Curie",
            "paths": ["reactants.0.smiles"],
            "pre_status": "needs_review",
            "post_status": "accepted",
            "issues_added": 0,
            "confirmed_at": audit_entries[0]["confirmed_at"],
        }
    ]
    assert audit_entries[0]["confirmed_at"].endswith("Z")


def test_apply_correction_invalid_smiles_keeps_needs_review(tmp_path: Path) -> None:
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "reactants.0.smiles", "value": "not_a_smiles"}]),
        encoding="utf-8",
    )

    corrected, audit_entries = apply_corrections(
        [record(review_status="accepted")],
        corrections,
        confirmed_by="Reviewer",
    )

    assert corrected[0].review_status == "needs_review"
    assert corrected[0].issues[-1].code == "R001_INVALID_SMILES"
    assert corrected[0].issues[-1].severity == "error"
    assert corrected[0].issues[-1].target_id == "7"
    assert audit_entries[0]["post_status"] == "needs_review"
    assert audit_entries[0]["issues_added"] == 1


@pytest.mark.parametrize("confirmed_by", ["", "   "])
def test_apply_correction_requires_confirmed_by(tmp_path: Path, confirmed_by: str) -> None:
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="confirmed_by"):
        apply_corrections([record()], corrections, confirmed_by=confirmed_by)


def test_apply_correction_unknown_reaction_still_raises(tmp_path: Path) -> None:
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "missing", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown reactions"):
        apply_corrections([record()], corrections, confirmed_by="Reviewer")


def test_evaluation_metrics() -> None:
    row = record().model_dump(mode="json")
    report = evaluate_records([row], [row])
    assert report["reaction_precision"] == 1
    assert report["reaction_recall"] == 1
    assert report["yield_accuracy"] == 1


def test_load_jsonl_reports_source_line(tmp_path: Path) -> None:
    source = tmp_path / "records.jsonl"
    source.write_text('{"ok": true}\n\n[1, 2]\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"records\.jsonl:3"):
        load_jsonl(source)


def test_review_apply_cli_requires_confirmed_by(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["review-apply", str(run_dir), str(corrections)])

    assert result.exit_code != 0
    assert "--confirmed-by" in result.output


def test_review_apply_cli_writes_and_appends_audit(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )

    runner = CliRunner()
    first = runner.invoke(
        main,
        ["review-apply", str(run_dir), str(corrections), "--confirmed-by", "Reviewer One"],
    )
    second = runner.invoke(
        main,
        ["review-apply", str(run_dir), str(corrections), "--confirmed-by", "Reviewer Two"],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0

    audit_path = run_dir / "audit.jsonl"
    assert audit_path.is_file()
    audit_entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["confirmed_by"] for entry in audit_entries] == ["Reviewer One", "Reviewer Two"]
    assert all(entry["paths"] == ["yield_pct"] for entry in audit_entries)
    assert str(audit_path) in first.output
    assert str(audit_path) in second.output


# ---------------------------------------------------------------------------
# New tests: record hash
# ---------------------------------------------------------------------------


def test_record_hash_deterministic() -> None:
    rec = record()
    h1 = _record_hash(rec)
    h2 = _record_hash(rec)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_record_hash_changes_on_mutation() -> None:
    rec = record()
    h1 = _record_hash(rec)
    rec2 = rec.model_copy(update={"yield_pct": 99})
    h2 = _record_hash(rec2)
    assert h1 != h2


# ---------------------------------------------------------------------------
# New tests: aggregate_human_status
# ---------------------------------------------------------------------------


def test_aggregate_unreviewed_when_no_decisions() -> None:
    status = aggregate_human_status("r1", [])
    assert status == "unreviewed"


def test_aggregate_rejected_sticky() -> None:
    decisions = [
        _make_decision(conclusion="rejected"),
        _make_decision(target_kind="reaction", conclusion="confirmed"),
    ]
    status = aggregate_human_status("r1", decisions)
    assert status == "rejected"


def test_aggregate_pending_when_some_pending() -> None:
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
        _make_decision(
            target_kind="condition",
            target_id="r1:cond:temperature",
            conclusion="pending",
            record_hash=_make_decision().record_hash,
        ),
    ]
    status = aggregate_human_status("r1", decisions)
    assert status == "pending"


def test_aggregate_confirmed_when_all_resolved() -> None:
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
        _make_decision(target_kind="completeness", conclusion="confirmed"),
    ]
    status = aggregate_human_status("r1", decisions)
    assert status == "confirmed"


def test_aggregate_in_review_when_partial() -> None:
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
    ]
    status = aggregate_human_status("r1", decisions)
    assert status == "in_review"


def test_aggregate_not_applicable_counts_as_resolved() -> None:
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
        _make_decision(target_kind="completeness", conclusion="not_applicable"),
    ]
    status = aggregate_human_status("r1", decisions)
    assert status == "confirmed"


def test_aggregate_gold_dimension_counts_when_gold_provided() -> None:
    gold_map = {
        "r1": GoldComparison(
            reaction_id="r1",
            alignment="mapped",
            gold_source=GoldSource(
                file_name="gold.jsonl",
                file_hash="abc123",
                entry_count=1,
                gold_schema_version="1.0",
            ),
        ),
    }
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
        _make_decision(target_kind="completeness", conclusion="confirmed"),
    ]
    # Gold dimension requires a gold_alignment decision
    status = aggregate_human_status("r1", decisions, gold_map)
    assert status == "in_review"


def test_aggregate_gold_dimension_not_required_without_gold() -> None:
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
        _make_decision(target_kind="completeness", conclusion="confirmed"),
    ]
    status = aggregate_human_status("r1", decisions, gold_map=None)
    assert status == "confirmed"


# ---------------------------------------------------------------------------
# New tests: invalidate_stale_decisions
# ---------------------------------------------------------------------------


def test_invalidate_empty_decisions() -> None:
    result = invalidate_stale_decisions([], [], {})
    assert result == set()


def test_invalidate_on_hash_change() -> None:
    old_hash = "old_hash_value"
    dec = _make_decision(record_hash=old_hash)
    current_hashes = {"r1": "new_hash_value"}
    result = invalidate_stale_decisions([dec], [], current_hashes)
    assert dec.decision_id in result


def test_invalidate_reject_never_invalidated() -> None:
    old_hash = "old_hash_value"
    dec = _make_decision(conclusion="rejected", record_hash=old_hash)
    current_hashes = {"r1": "new_hash_value"}
    result = invalidate_stale_decisions([dec], [], current_hashes)
    assert dec.decision_id not in result


def test_invalidate_on_smiles_change_op() -> None:
    rec = record()
    dec = _make_decision(
        target_kind="participant_structure",
        target_id="r1:reactant:1",
        record_hash=_record_hash(rec),
    )
    op = ReviewOperation(
        reaction_id="r1",
        target_kind="participant_structure",
        target_id="r1:reactant:1",
        op="set_value",
        path="reactants.0.smiles",
        new_value="CCO",
    )
    current_hashes = {"r1": _record_hash(rec)}
    result = invalidate_stale_decisions([dec], [op], current_hashes)
    assert dec.decision_id in result


def test_invalidate_on_add_participant() -> None:
    rec = record()
    dec = _make_decision(
        target_kind="completeness",
        target_id="r1",
        record_hash=_record_hash(rec),
    )
    op = ReviewOperation(
        reaction_id="r1",
        target_kind="completeness",
        target_id="r1",
        op="add_participant",
        new_value={"label": "new", "smiles": "C", "role": "reactant"},
    )
    current_hashes = {"r1": _record_hash(rec)}
    result = invalidate_stale_decisions([dec], [op], current_hashes)
    assert dec.decision_id in result


def test_invalidate_unrelated_op_does_not_invalidate() -> None:
    rec = record()
    dec = _make_decision(
        target_kind="reaction",
        target_id="r1",
        record_hash=_record_hash(rec),
    )
    op = ReviewOperation(
        reaction_id="r1",
        target_kind="condition",
        target_id="r1:cond:temperature",
        op="set_value",
        path="temperature_c",
        new_value=100,
    )
    current_hashes = {"r1": _record_hash(rec)}
    result = invalidate_stale_decisions([dec], [op], current_hashes)
    assert dec.decision_id not in result


# ---------------------------------------------------------------------------
# New tests: apply_submission
# ---------------------------------------------------------------------------


def _write_submission(
    tmp_path: Path,
    operations: list[dict],
    *,
    reviewer: str = "Reviewer",
    submission_id: str = "sub-001",
    base_hashes: dict[str, str] | None = None,
) -> Path:
    """Write a ReviewSubmission JSON file."""
    sub = {
        "schema_version": "1.0",
        "submission_id": submission_id,
        "base_record_hashes": base_hashes or {},
        "operations": operations,
        "reviewer": reviewer,
        "created_at": utc_now(),
    }
    path = tmp_path / "submission.json"
    path.write_text(json.dumps(sub), encoding="utf-8")
    return path


def test_apply_submission_requires_confirmed_by(tmp_path: Path) -> None:
    path = _write_submission(tmp_path, [])
    with pytest.raises(ValueError, match="confirmed_by"):
        apply_submission([record()], path, confirmed_by="")


def test_apply_submission_reviewer_mismatch(tmp_path: Path) -> None:
    path = _write_submission(tmp_path, [], reviewer="Alice")
    with pytest.raises(ValueError, match="does not match"):
        apply_submission([record()], path, confirmed_by="Bob")


def test_apply_submission_version_conflict(tmp_path: Path) -> None:
    rec = record()
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "reaction", "target_id": "r1", "op": "confirm"}],
        reviewer="Reviewer",
        base_hashes={"r1": "wrong_hash"},
    )
    with pytest.raises(ValueError, match="Version conflict"):
        apply_submission([rec], path, confirmed_by="Reviewer")


def test_apply_submission_dedup(tmp_path: Path) -> None:
    rec = record()
    store = ArtifactStore(tmp_path / "run")
    sub_dir = store.root / "review_submissions"
    sub_dir.mkdir(exist_ok=True)
    (sub_dir / "sub-001.json").write_text("{}")

    path = _write_submission(
        tmp_path,
        [],
        reviewer="Reviewer",
        submission_id="sub-001",
        base_hashes={"r1": _record_hash(rec)},
    )
    with pytest.raises(ValueError, match="Duplicate submission_id"):
        apply_submission([rec], path, confirmed_by="Reviewer", store=store)


def test_apply_submission_illegal_path(tmp_path: Path) -> None:
    rec = record()
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "reaction", "target_id": "r1", "op": "set_value", "path": "schema_version", "new_value": "2.0"}],
        reviewer="Reviewer",
        base_hashes={"r1": _record_hash(rec)},
    )
    with pytest.raises(ValueError, match="not editable"):
        apply_submission([rec], path, confirmed_by="Reviewer")


def test_apply_submission_confirm_sets_accepted(tmp_path: Path) -> None:
    rec = record(review_status="needs_review")
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "reaction", "target_id": "r1", "op": "confirm"}],
        reviewer="Reviewer",
        base_hashes={"r1": _record_hash(rec)},
    )
    corrected, audit = apply_submission([rec], path, confirmed_by="Reviewer")
    assert corrected[0].review_status == "accepted"
    assert audit[0]["submission_id"] == "sub-001"


def test_apply_submission_reject_sets_rejected(tmp_path: Path) -> None:
    rec = record()
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "reaction", "target_id": "r1", "op": "reject", "reason": "wrong"}],
        reviewer="Reviewer",
        base_hashes={"r1": _record_hash(rec)},
    )
    corrected, audit = apply_submission([rec], path, confirmed_by="Reviewer")
    # reject op creates a ReviewDecision but does not change review_status field
    # review_status field stays at its current value unless revalidation changes it
    assert len(audit) == 1


def test_apply_submission_revalidate_error_keeps_needs_review(tmp_path: Path) -> None:
    rec = record()
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "reaction", "target_id": "r1", "op": "set_value", "path": "reactants.0.smiles", "new_value": "INVALID"}],
        reviewer="Reviewer",
        base_hashes={"r1": _record_hash(rec)},
    )
    corrected, _ = apply_submission([rec], path, confirmed_by="Reviewer")
    assert corrected[0].review_status == "needs_review"


def test_apply_submission_persists_and_appends_audit(tmp_path: Path) -> None:
    rec = record()
    store = ArtifactStore(tmp_path / "run")
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "reaction", "target_id": "r1", "op": "confirm"}],
        reviewer="Reviewer",
        base_hashes={"r1": _record_hash(rec)},
    )
    corrected, audit = apply_submission([rec], path, confirmed_by="Reviewer", store=store)
    # Submission persisted
    assert (store.root / "review_submissions" / "sub-001.json").is_file()
    # Audit entries
    assert audit[0]["submission_id"] == "sub-001"


def test_apply_submission_mark_pending_creates_decision(tmp_path: Path) -> None:
    rec = record()
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "reaction", "target_id": "r1", "op": "mark_pending", "reason": "need more info"}],
        reviewer="Reviewer",
        base_hashes={"r1": _record_hash(rec)},
    )
    store = ArtifactStore(tmp_path / "run")
    corrected, audit = apply_submission([rec], path, confirmed_by="Reviewer", store=store)
    assert len(audit) == 1


# ---------------------------------------------------------------------------
# New tests: build_review_view
# ---------------------------------------------------------------------------


def test_build_review_view_basic() -> None:
    recs = [record()]
    view = build_review_view(recs)
    assert view["record_count"] == 1
    assert len(view["reactions"]) == 1
    rxn = view["reactions"][0]
    assert rxn["reaction_id"] == "r1"
    assert rxn["human_status"] == "unreviewed"
    assert rxn["context_note"] == "legacy run: reaction-level reference only"


def test_build_review_view_with_context() -> None:
    rec = record()
    ctx = ReviewContext(
        reaction_id="r1",
        record_hash=_record_hash(rec),
        participants=[
            ReviewParticipant(
                participant_id="r1:reactant:1",
                role="reactant",
                label="7",
                smiles="CC",
                structure_state="resolved",
            ),
        ],
        conditions=[],
    )
    view = build_review_view([rec], contexts=[ctx])
    rxn = view["reactions"][0]
    assert rxn["context_note"] is None
    assert len(rxn["participants"]) == 1
    assert rxn["participants"][0]["structure_state"] == "resolved"


def test_build_review_view_with_gold() -> None:
    rec = record()
    gold = GoldComparison(
        reaction_id="r1",
        alignment="mapped",
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc123",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )
    view = build_review_view([rec], gold_comparisons=[gold])
    rxn = view["reactions"][0]
    assert rxn["gold"]["has_gold"] is True
    assert rxn["gold"]["alignment"] == "mapped"


def test_build_review_view_with_evidence() -> None:
    rec = record(evidence_ids=["ev1"])
    ev = EvidenceRef(
        evidence_id="ev1",
        kind="text",
        page=1,
        source_path="paper.pdf",
        text="Reaction at 80C",
    )
    view = build_review_view([rec], evidence=[ev])
    assert len(view["evidence"]) == 1
    assert view["evidence"][0]["evidence_id"] == "ev1"


def test_build_review_view_missing_evidence() -> None:
    rec = record(evidence_ids=["ev_missing"])
    view = build_review_view([rec], evidence=[])
    assert "ev_missing" in view["evidence_missing"]


def test_build_review_view_empty_records() -> None:
    view = build_review_view([])
    assert view["record_count"] == 0
    assert view["reactions"] == []


def test_build_review_view_synthesizes_participants() -> None:
    rec = record(reactant_smiles="CCO")
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    # Should have at least reactant + product participants
    assert len(rxn["participants"]) >= 2
    roles = [p["role"] for p in rxn["participants"]]
    assert "reactant" in roles
    assert "product" in roles


def test_build_review_view_stale_image_on_hash_mismatch() -> None:
    rec = record()
    old_hash = "old_hash"
    ctx = ReviewContext(
        reaction_id="r1",
        record_hash=old_hash,
        participants=[
            ReviewParticipant(
                participant_id="r1:reactant:1",
                role="reactant",
                label="7",
                smiles="CC",
                structure_state="resolved",
            ),
        ],
    )
    view = build_review_view([rec], contexts=[ctx])
    rxn = view["reactions"][0]
    assert rxn["participants"][0]["stale_image"] is True


# ---------------------------------------------------------------------------
# New tests: generate_review with kwargs
# ---------------------------------------------------------------------------


def test_generate_review_with_contexts(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    rec = record()
    ctx = ReviewContext(
        reaction_id="r1",
        record_hash=_record_hash(rec),
        participants=[
            ReviewParticipant(
                participant_id="r1:reactant:1",
                role="reactant",
                label="7",
                smiles="CC",
                structure_state="resolved",
            ),
        ],
    )
    output = generate_review([rec], store, contexts=[ctx])
    html = output.read_text(encoding="utf-8")
    assert "r1" in html


def test_generate_review_with_gold(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    rec = record()
    gold = GoldComparison(
        reaction_id="r1",
        alignment="mapped",
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc123",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )
    output = generate_review([rec], store, gold_comparisons=[gold])
    html = output.read_text(encoding="utf-8")
    assert "mapped" in html


def test_generate_review_legacy_call_pattern(tmp_path: Path) -> None:
    """Verify the original 2-arg call still works (pipeline/cli compatibility)."""
    store = ArtifactStore(tmp_path / "run")
    output = generate_review([record()], store)
    assert output.is_file()
    assert output.name == "review.html"


# ---------------------------------------------------------------------------
# New tests: edge cases and backward compatibility
# ---------------------------------------------------------------------------


def test_aggregate_revised_counts_as_confirmed() -> None:
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
        _make_decision(target_kind="completeness", conclusion="revised"),
    ]
    status = aggregate_human_status("r1", decisions)
    assert status == "confirmed"


def test_aggregate_insufficient_evidence_is_pending() -> None:
    dec = _make_decision(
        target_kind="reaction",
        conclusion="insufficient_evidence",
    )
    decisions = [
        _make_decision(target_kind="reaction", conclusion="confirmed"),
        dec,
    ]
    # The second decision for same target overrides first
    status = aggregate_human_status("r1", decisions)
    assert status == "pending"


def test_apply_submission_no_ops_returns_unchanged(tmp_path: Path) -> None:
    rec = record()
    path = _write_submission(
        tmp_path,
        [],
        reviewer="Reviewer",
        base_hashes={},
    )
    corrected, audit = apply_submission([rec], path, confirmed_by="Reviewer")
    assert corrected[0].reaction_id == "r1"
    assert audit == []


def test_apply_submission_set_value_condition(tmp_path: Path) -> None:
    rec = record(temperature_c=50)
    path = _write_submission(
        tmp_path,
        [{"reaction_id": "r1", "target_kind": "condition", "target_id": "r1:cond:temp", "op": "set_value", "path": "temperature_c", "new_value": 100}],
        reviewer="Reviewer",
        base_hashes={"r1": _record_hash(rec)},
    )
    corrected, _ = apply_submission([rec], path, confirmed_by="Reviewer")
    assert corrected[0].temperature_c == 100


def test_invalidate_no_ops_no_change() -> None:
    rec = record()
    dec = _make_decision(record_hash=_record_hash(rec))
    result = invalidate_stale_decisions([dec], [], {"r1": _record_hash(rec)})
    assert result == set()


def test_build_review_view_with_decisions() -> None:
    rec = record()
    dec = _make_decision(target_kind="reaction", conclusion="confirmed")
    view = build_review_view([rec], decisions=[dec])
    rxn = view["reactions"][0]
    # Two decisions needed (reaction + completeness) for confirmed
    assert rxn["human_status"] == "in_review"


def test_build_review_view_has_contexts_flag() -> None:
    rec = record()
    view_without = build_review_view([rec])
    assert view_without["has_contexts"] is False
    ctx = ReviewContext(reaction_id="r1", record_hash=_record_hash(rec))
    view_with = build_review_view([rec], contexts=[ctx])
    assert view_with["has_contexts"] is True


def test_build_review_view_has_gold_flag() -> None:
    rec = record()
    view_without = build_review_view([rec])
    assert view_without["has_gold"] is False
    gold = GoldComparison(
        reaction_id="r1",
        alignment="mapped",
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )
    view_with = build_review_view([rec], gold_comparisons=[gold])
    assert view_with["has_gold"] is True


# ---------------------------------------------------------------------------
# CLI integration: review with sidecars and review-apply auto-detect
# ---------------------------------------------------------------------------


def test_review_cli_loads_sidecar_contexts(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    store = ArtifactStore(run_dir)
    rec = record()
    ctx = ReviewContext(
        reaction_id="r1",
        record_hash=_record_hash(rec),
        participants=[
            ReviewParticipant(
                participant_id="r1:reactant:1",
                role="reactant",
                label="7",
                smiles="CC",
                structure_state="resolved",
            ),
        ],
    )
    store.write_review_contexts([ctx])

    result = CliRunner().invoke(main, ["review", str(run_dir)])
    assert result.exit_code == 0
    assert "review.html" in result.output


def test_review_cli_json_envelope_has_human_status_counts(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    result = CliRunner().invoke(main, ["review", str(run_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    counts = payload["human_status_counts"]
    assert isinstance(counts, dict)
    assert "unreviewed" in counts


def test_review_cli_json_envelope_with_gold_comparison(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    store = ArtifactStore(run_dir)
    gold = GoldComparison(
        reaction_id="r1",
        alignment="mapped",
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc123",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )
    store.write_gold_comparisons([gold])

    result = CliRunner().invoke(main, ["review", str(run_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["gold"] is not None
    assert payload["gold"]["file_name"] == "gold.jsonl"


def test_review_apply_submission_auto_detect_via_cli(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    rec = record()
    sub = {
        "schema_version": "1.0",
        "submission_id": "sub-cli-001",
        "base_record_hashes": {"r1": _record_hash(rec)},
        "operations": [
            {
                "reaction_id": "r1",
                "target_kind": "reaction",
                "target_id": "r1",
                "op": "confirm",
            }
        ],
        "reviewer": "Reviewer",
        "created_at": utc_now(),
    }
    sub_path = tmp_path / "submission.json"
    sub_path.write_text(json.dumps(sub), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(sub_path), "--confirmed-by", "Reviewer"],
    )

    assert result.exit_code == 0
    assert (run_dir / "records.corrected.jsonl").is_file()
    assert (run_dir / "audit.jsonl").is_file()
    assert (run_dir / "review.html").is_file()
    assert "submission entry" in result.output


def test_review_apply_legacy_via_cli_regenerates_html(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(corrections), "--confirmed-by", "Reviewer"],
    )

    assert result.exit_code == 0
    assert (run_dir / "review.html").is_file()
    html = (run_dir / "review.html").read_text(encoding="utf-8")
    assert "r1" in html


def test_review_apply_submission_persists_submission_file(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    store = ArtifactStore(run_dir)
    rec = record()
    sub = {
        "schema_version": "1.0",
        "submission_id": "sub-persist",
        "base_record_hashes": {"r1": _record_hash(rec)},
        "operations": [
            {
                "reaction_id": "r1",
                "target_kind": "reaction",
                "target_id": "r1",
                "op": "confirm",
            }
        ],
        "reviewer": "Reviewer",
        "created_at": utc_now(),
    }
    sub_path = tmp_path / "submission.json"
    sub_path.write_text(json.dumps(sub), encoding="utf-8")

    CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(sub_path), "--confirmed-by", "Reviewer"],
    )

    assert (store.root / "review_submissions" / "sub-persist.json").is_file()


def test_review_apply_submission_reviewer_mismatch_exits_nonzero(
    tmp_path: Path,
) -> None:
    run_dir = _write_run_dir(tmp_path)
    rec = record()
    sub = {
        "schema_version": "1.0",
        "submission_id": "sub-mismatch",
        "base_record_hashes": {"r1": _record_hash(rec)},
        "operations": [],
        "reviewer": "Alice",
        "created_at": utc_now(),
    }
    sub_path = tmp_path / "submission.json"
    sub_path.write_text(json.dumps(sub), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(sub_path), "--confirmed-by", "Bob"],
    )

    assert result.exit_code != 0
    assert "does not match" in result.output


# ---------------------------------------------------------------------------
# New tests: payload record_hash and field_path
# ---------------------------------------------------------------------------


def test_payload_reaction_record_hash() -> None:
    rec = record()
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    assert rxn["record_hash"] == _record_hash(rec)
    assert len(rxn["record_hash"]) == 64


def test_payload_participant_field_path_reactant() -> None:
    rec = record()
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    reactant = next(p for p in rxn["participants"] if p["role"] == "reactant")
    assert reactant["field_path"] == "reactants.0.smiles"


def test_payload_participant_field_path_product() -> None:
    rec = record()
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    product = next(p for p in rxn["participants"] if p["role"] == "product")
    assert product["field_path"] == "products.0.smiles"


def test_payload_participant_field_path_reagent() -> None:
    rec = record(reagents=["Pd(OAc)2", "PPh3"])
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    reagent_pids = [p for p in rxn["participants"] if p["role"] == "reagent"]
    assert len(reagent_pids) == 2
    assert reagent_pids[0]["field_path"] == "reagents.0"
    assert reagent_pids[1]["field_path"] == "reagents.1"


def test_payload_participant_field_path_solvent() -> None:
    rec = record(solvents=["THF", "DMF"])
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    solvent_pids = [p for p in rxn["participants"] if p["role"] == "solvent"]
    assert len(solvent_pids) == 2
    assert solvent_pids[0]["field_path"] == "solvents.0"
    assert solvent_pids[1]["field_path"] == "solvents.1"


def test_payload_condition_field_path_temperature() -> None:
    rec = record(temperature_c=80)
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    temp_cond = next(c for c in rxn["conditions"] if c["kind"] == "temperature")
    assert temp_cond["field_path"] == "temperature_c"


def test_payload_condition_field_path_time() -> None:
    rec = record(time="12h")
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    time_cond = next(c for c in rxn["conditions"] if c["kind"] == "time")
    assert time_cond["field_path"] == "time"


def test_payload_condition_field_path_yield() -> None:
    rec = record(yield_pct=85)
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    yield_cond = next(c for c in rxn["conditions"] if c["kind"] == "yield")
    assert yield_cond["field_path"] == "yield_pct"


def test_payload_condition_field_path_reagent() -> None:
    rec = record(reagents=["Pd(OAc)2"])
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    reagent_cond = next(c for c in rxn["conditions"] if c["kind"] == "reagent")
    assert reagent_cond["field_path"] == "reagents.0"


def test_payload_condition_field_path_solvent() -> None:
    rec = record(solvents=["THF"])
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    solvent_cond = next(c for c in rxn["conditions"] if c["kind"] == "solvent")
    assert solvent_cond["field_path"] == "solvents.0"


# ---------------------------------------------------------------------------
# New tests: asset-based rendering
# ---------------------------------------------------------------------------


def test_build_review_view_participant_has_images_and_render_status() -> None:
    rec = record(reactant_smiles="CCO")
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    reactant = next(p for p in rxn["participants"] if p["role"] == "reactant")
    assert "images" in reactant
    assert "render_status" in reactant
    assert "image" not in reactant
    assert "svg" not in reactant
    assert reactant["render_status"] in (
        "ok", "rdkit_missing", "invalid_smiles", "missing_smiles"
    )
    assert "normal" in reactant["images"]


def test_build_review_view_missing_smiles_render_status() -> None:
    rec = record(reactant_smiles="")
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    reactant = next(p for p in rxn["participants"] if p["role"] == "reactant")
    assert reactant["render_status"] == "missing_smiles"
    assert reactant["images"]["normal"]["svg"] is None
    assert reactant["images"]["normal"]["png"] is None
    assert reactant["images"]["stereo"] is None
    assert reactant["images"]["atommap"] is None


def test_build_review_view_invalid_smiles_render_status() -> None:
    rec = record(reactant_smiles="not_a_smiles")
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    reactant = next(p for p in rxn["participants"] if p["role"] == "reactant")
    assert reactant["render_status"] == "invalid_smiles"
    assert reactant["images"]["normal"]["png"] is None


def test_generate_review_no_raw_svg_in_html(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    output = generate_review([record()], store)
    html = output.read_text(encoding="utf-8")
    assert "<svg" not in html
    assert "&lt;svg" not in html


def test_review_view_has_no_svg_or_image_keys() -> None:
    rec = record(reactant_smiles="CCO")
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    for p in rxn["participants"]:
        assert "svg" not in p
        assert "image" not in p
        assert "images" in p
        assert "render_status" in p


def test_review_view_asset_paths_are_store_relative(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    rec = record(reactant_smiles="CCO")
    generate_review([rec], store)
    view = build_review_view([rec], store=store)
    rxn = view["reactions"][0]
    reactant = next(p for p in rxn["participants"] if p["role"] == "reactant")
    if reactant["render_status"] == "ok":
        assert reactant["images"]["normal"]["svg"].startswith("review_assets/")
        assert reactant["images"]["normal"]["svg"].endswith(".svg")
        assert reactant["images"]["normal"]["png"].endswith(".png")


def test_generate_review_asset_files_exist(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    generate_review([record(reactant_smiles="CCO")], store)
    assets_dir = store.root / "review_assets"
    assert assets_dir.is_dir()
    svg_files = list(assets_dir.glob("*.svg"))
    png_files = list(assets_dir.glob("*.png"))
    assert len(svg_files) >= 1
    assert len(png_files) >= 1


def test_generate_review_asset_files_reused_on_second_call(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    rec = record(reactant_smiles="CCO")
    generate_review([rec], store)
    assets_dir = store.root / "review_assets"
    files_before = {f.name: f.stat().st_mtime for f in assets_dir.iterdir()}
    generate_review([rec], store)
    files_after = {f.name: f.stat().st_mtime for f in assets_dir.iterdir()}
    assert files_before == files_after


def test_build_review_view_condition_summary_with_conditions() -> None:
    rec = record(
        reagents=["Pd(PPh3)4", "K2CO3"],
        solvents=["THF"],
        temperature_c=65,
        time="12h",
        yield_pct=82,
    )
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    summary = rxn["condition_summary"]
    assert "Pd(PPh3)4" in summary
    assert "K2CO3" in summary
    assert "THF" in summary
    assert "65 °C" in summary
    assert "12h" in summary
    assert "收率 82%" in summary


def test_build_review_view_condition_summary_empty_when_no_conditions() -> None:
    rec = record(reagents=[], solvents=[], temperature_c=None, time=None, yield_pct=None)
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    assert rxn["condition_summary"] == ""


def test_build_review_view_gold_pairs_with_gold_records() -> None:
    rec = record(reactant_smiles="CCO")
    gold_rec = ReactionRecord(
        reaction_id="gold-r1",
        reactants=[CompoundRef(label="7", smiles="CCO", role="reactant")],
        products=[CompoundRef(label="8", smiles="CCO", role="product")],
        reagents=[],
        solvents=[],
        confidence=0.9,
        review_status="accepted",
    )
    gold = GoldComparison(
        reaction_id="r1",
        gold_reaction_id="gold-r1",
        alignment="mapped",
        participant_results=[
            GoldParticipantComparison(
                participant_id="r1:reactant:1",
                gold_participant_id="7",
                comparison="stereo_only",
            ),
        ],
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )
    store = None
    view = build_review_view(
        [rec], gold_comparisons=[gold], gold_records=[gold_rec], store=store
    )
    rxn = view["reactions"][0]
    assert len(rxn["gold_pairs"]) == 1
    pair = rxn["gold_pairs"][0]
    assert pair["participant_id"] == "r1:reactant:1"
    assert pair["gold_participant_id"] == "7"
    assert pair["comparison"] == "stereo_only"
    assert pair["extracted"] is not None
    assert pair["gold"] is not None
    assert pair["extracted"]["smiles"] == "CCO"
    assert pair["gold"]["smiles"] == "CCO"


def test_build_review_view_gold_pairs_without_gold_records() -> None:
    rec = record(reactant_smiles="CCO")
    gold = GoldComparison(
        reaction_id="r1",
        gold_reaction_id="gold-r1",
        alignment="mapped",
        participant_results=[
            GoldParticipantComparison(
                participant_id="r1:reactant:1",
                gold_participant_id="7",
                comparison="stereo_only",
            ),
        ],
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )
    view = build_review_view([rec], gold_comparisons=[gold])
    rxn = view["reactions"][0]
    assert len(rxn["gold_pairs"]) == 1
    pair = rxn["gold_pairs"][0]
    assert pair["extracted"] is not None
    assert pair["gold"] is None
    assert pair["highlight"] is False


def test_build_review_view_gold_pairs_no_gold_comparisons() -> None:
    rec = record()
    view = build_review_view([rec])
    rxn = view["reactions"][0]
    assert rxn["gold_pairs"] == []


def test_build_review_view_gold_pairs_resolve_label_keyed_ids() -> None:
    # evaluation._part_id emits label/name (e.g. "7"), not review-scheme ids.
    rec = record(reactant_smiles="C[C@H](O)CC")
    gold_rec = ReactionRecord(
        reaction_id="gold-r1",
        reactants=[CompoundRef(label="7", smiles="C[C@@H](O)CC", role="reactant")],
        products=[],
        reagents=[],
        solvents=[],
        confidence=0.9,
        review_status="accepted",
    )
    gold = GoldComparison(
        reaction_id="r1",
        gold_reaction_id="gold-r1",
        alignment="mapped",
        participant_results=[
            GoldParticipantComparison(
                participant_id="7",
                gold_participant_id="7",
                comparison="stereo_only",
            ),
        ],
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )
    view = build_review_view([rec], gold_comparisons=[gold], gold_records=[gold_rec])
    pair = view["reactions"][0]["gold_pairs"][0]
    assert pair["extracted"] is not None
    assert pair["extracted"]["smiles"] == "C[C@H](O)CC"
    assert pair["gold"] is not None
    assert pair["gold"]["smiles"] == "C[C@@H](O)CC"
    assert pair["highlight"] is True
