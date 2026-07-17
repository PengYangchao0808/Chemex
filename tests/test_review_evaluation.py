from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chemex_lit.cli import main
from chemex_lit.evaluation import evaluate_records
from chemex_lit.evaluation.load import load_jsonl
from chemex_lit.models import CompoundRef, ReactionRecord, ValidationIssue
from chemex_lit.review import apply_corrections, generate_review


def record(
    *,
    reactant_smiles: str = "CC",
    review_status: Literal["accepted", "needs_review", "rejected"] = "accepted",
    issues: list[ValidationIssue] | None = None,
) -> ReactionRecord:
    return ReactionRecord(
        reaction_id="r1",
        reactants=[CompoundRef(label="7", smiles=reactant_smiles, role="reactant")],
        products=[CompoundRef(label="8", smiles="CCO", role="product")],
        yield_pct=80,
        confidence=0.9,
        review_status=review_status,
        issues=issues or [],
    )


def test_generate_review(tmp_path: Path) -> None:
    output = generate_review([record()], tmp_path / "review.html")
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


def _write_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    records_path = run_dir / "records.jsonl"
    records_path.write_text(json.dumps(record().model_dump(mode="json")) + "\n", encoding="utf-8")
    return run_dir
