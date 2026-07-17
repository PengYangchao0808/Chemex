from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemex_lit.evaluation import evaluate_records
from chemex_lit.evaluation.load import load_jsonl
from chemex_lit.models import CompoundRef, ReactionRecord
from chemex_lit.review import apply_corrections, generate_review


def record() -> ReactionRecord:
    return ReactionRecord(
        reaction_id="r1",
        reactants=[CompoundRef(label="7", smiles="CC", role="reactant")],
        products=[CompoundRef(label="8", smiles="CCO", role="product")],
        yield_pct=80,
        confidence=0.9,
        review_status="accepted",
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
    assert apply_corrections([record()], corrections)[0].yield_pct == 81


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
