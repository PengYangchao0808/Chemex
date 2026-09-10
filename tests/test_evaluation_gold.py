"""Tests for gold-standard alignment, comparison, and layered metrics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemex_lit.evaluation import (
    align_gold,
    build_gold_comparisons,
    compare_participants,
    evaluate_records,
    load_gold,
)
from chemex_lit.errors import ChemExError
from chemex_lit.models import CompoundRef, ReactionRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rxn(
    rid: str = "r1",
    reactants: list[CompoundRef] | None = None,
    products: list[CompoundRef] | None = None,
    *,
    yield_pct: float | None = 80.0,
) -> ReactionRecord:
    default_reactants = [CompoundRef(label="A", smiles="CC", role="reactant")]
    default_products = [CompoundRef(label="B", smiles="CCO", role="product")]
    return ReactionRecord(
        reaction_id=rid,
        reactants=reactants if reactants is not None else default_reactants,
        products=products if products is not None else default_products,
        yield_pct=yield_pct,
        confidence=0.9,
        review_status="accepted",
    )


def _gold_jsonl(path: Path, records: list[ReactionRecord]) -> Path:
    lines = [r.model_dump_json() for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_gold
# ---------------------------------------------------------------------------


class TestLoadGold:
    def test_valid_file(self, tmp_path: Path) -> None:
        gold_file = _gold_jsonl(tmp_path / "gold.jsonl", [_rxn()])
        records, source = load_gold(gold_file)
        assert len(records) == 1
        assert records[0].reaction_id == "r1"
        assert source.entry_count == 1
        assert source.file_name == "gold.jsonl"
        assert len(source.file_hash) == 64
        assert source.gold_schema_version == "1.0"
        assert source.normalization_policy == "none"

    def test_empty_lines_skipped(self, tmp_path: Path) -> None:
        content = _rxn().model_dump_json() + "\n\n\n"
        gold_file = tmp_path / "gold.jsonl"
        gold_file.write_text(content, encoding="utf-8")
        records, source = load_gold(gold_file)
        assert len(records) == 1
        assert source.entry_count == 1

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        gold_file = tmp_path / "gold.jsonl"
        gold_file.write_text("not json\n", encoding="utf-8")
        with pytest.raises(ChemExError, match="line 1"):
            load_gold(gold_file)

    def test_invalid_schema_raises_with_line_numbers(self, tmp_path: Path) -> None:
        gold_file = tmp_path / "gold.jsonl"
        gold_file.write_text(
            '{"reaction_id": "r1", "confidence": 0.5, "review_status": "accepted"}\n'
            '{"bad_field": true}\n',
            encoding="utf-8",
        )
        with pytest.raises(ChemExError, match="line 2"):
            load_gold(gold_file)

    def test_multiple_errors_reported(self, tmp_path: Path) -> None:
        gold_file = tmp_path / "gold.jsonl"
        gold_file.write_text("bad1\nbad2\n", encoding="utf-8")
        with pytest.raises(ChemExError, match="2 error"):
            load_gold(gold_file)


# ---------------------------------------------------------------------------
# align_gold
# ---------------------------------------------------------------------------


class TestAlignGold:
    def test_explicit_id_match(self, tmp_path: Path) -> None:
        pred = [_rxn("r1")]
        gold = [_rxn("r1")]
        comparisons = align_gold(pred, gold)
        assert len(comparisons) == 1
        assert comparisons[0].alignment == "explicit_id"
        assert comparisons[0].reaction_id == "r1"
        assert comparisons[0].gold_reaction_id == "r1"

    def test_mapped_by_key(self) -> None:
        pred = [_rxn("pred-1")]
        gold = [_rxn("gold-1")]
        comparisons = align_gold(pred, gold)
        mapped = [c for c in comparisons if c.alignment == "mapped"]
        assert len(mapped) == 1
        assert mapped[0].gold_reaction_id == "gold-1"

    def test_ambiguous_multiple_candidates(self) -> None:
        pred = [_rxn("p1"), _rxn("p2")]
        gold = [_rxn("g1")]
        comparisons = align_gold(pred, gold)
        ambiguous = [c for c in comparisons if c.alignment == "ambiguous"]
        assert len(ambiguous) == 1
        assert "multiple candidates" in (ambiguous[0].alignment_basis or "")

    def test_ambiguous_never_silent(self) -> None:
        pred = [_rxn("p1"), _rxn("p2")]
        gold = [_rxn("g1")]
        comparisons = align_gold(pred, gold)
        for c in comparisons:
            if c.alignment == "ambiguous":
                assert c.alignment_basis is not None
                assert "2" in c.alignment_basis

    def test_shuffled_gold_order(self) -> None:
        pred = [_rxn("r1"), _rxn("r2"), _rxn("r3")]
        gold = [_rxn("r3"), _rxn("r1"), _rxn("r2")]
        comparisons = align_gold(pred, gold)
        explicit = [c for c in comparisons if c.alignment == "explicit_id"]
        assert len(explicit) == 3
        ids = {c.gold_reaction_id for c in explicit}
        assert ids == {"r1", "r2", "r3"}

    def test_duplicate_gold_ids_surface_as_ambiguous(self) -> None:
        pred = [_rxn("r1")]
        gold = [_rxn("r1"), _rxn("r1")]
        comparisons = align_gold(pred, gold)
        ambiguous = [c for c in comparisons if c.alignment == "ambiguous"]
        assert len(ambiguous) >= 1
        assert "duplicate" in (ambiguous[0].alignment_basis or "").lower()

    def test_unmatched_extracted(self) -> None:
        pred = [_rxn("r1")]
        gold: list[ReactionRecord] = []
        comparisons = align_gold(pred, gold)
        assert len(comparisons) == 1
        assert comparisons[0].alignment == "unmatched_extracted"

    def test_unmatched_gold(self) -> None:
        pred: list[ReactionRecord] = []
        gold = [_rxn("r1")]
        comparisons = align_gold(pred, gold)
        assert len(comparisons) == 1
        assert comparisons[0].alignment == "unmatched_gold"

    def test_nothing_disappears(self) -> None:
        pred = [
            _rxn(
                "r1",
                reactants=[CompoundRef(label="A", smiles="CC", role="reactant")],
                products=[CompoundRef(label="B", smiles="CCO", role="product")],
            ),
            _rxn(
                "r2",
                reactants=[CompoundRef(label="X", smiles="CCCl", role="reactant")],
                products=[CompoundRef(label="Y", smiles="CCN", role="product")],
            ),
        ]
        gold = [
            _rxn(
                "r2",
                reactants=[CompoundRef(label="X", smiles="CCCl", role="reactant")],
                products=[CompoundRef(label="Y", smiles="CCN", role="product")],
            ),
            _rxn(
                "r3",
                reactants=[CompoundRef(label="Q", smiles="c1ccccc1", role="reactant")],
                products=[CompoundRef(label="W", smiles="c1ccccc1O", role="product")],
            ),
        ]
        comparisons = align_gold(pred, gold)
        alignments = {c.alignment for c in comparisons}
        assert "unmatched_extracted" in alignments
        assert "unmatched_gold" in alignments

    def test_alignment_never_by_structure_equality(self) -> None:
        pred = [_rxn("pred-1")]
        gold = [_rxn("gold-1")]
        comparisons = align_gold(pred, gold)
        for c in comparisons:
            assert c.alignment != "structure_match"


# ---------------------------------------------------------------------------
# compare_participants
# ---------------------------------------------------------------------------


class TestCompareParticipants:
    def test_identical_structures(self) -> None:
        pred = _rxn("r1")
        gold = _rxn("r1")
        results = compare_participants(pred, gold)
        assert len(results) >= 1
        identical = [r for r in results if r.comparison == "identical"]
        assert len(identical) >= 1

    def test_connectivity_differs(self) -> None:
        pred = _rxn(
            "r1",
            products=[CompoundRef(label="B", smiles="CCCO", role="product")],
        )
        gold = _rxn(
            "r1",
            products=[CompoundRef(label="B", smiles="c1ccccc1", role="product")],
        )
        results = compare_participants(pred, gold)
        conn = [r for r in results if r.comparison == "connectivity_differs"]
        assert len(conn) >= 1

    def test_stereo_only_enantiomer(self) -> None:
        pred = _rxn(
            "r1",
            products=[
                CompoundRef(label="P", smiles="C[C@@H](O)CC", role="product")
            ],
        )
        gold = _rxn(
            "r1",
            products=[
                CompoundRef(label="P", smiles="C[C@H](O)CC", role="product")
            ],
        )
        results = compare_participants(pred, gold)
        stereo = [r for r in results if r.comparison in ("stereo_only", "stereo_specificity_differs")]
        assert len(stereo) >= 1

    def test_stereo_ez_flip(self) -> None:
        pred = _rxn(
            "r1",
            products=[
                CompoundRef(label="P", smiles=r"C/C=C\C", role="product")
            ],
        )
        gold = _rxn(
            "r1",
            products=[
                CompoundRef(label="P", smiles=r"C/C=C/C", role="product")
            ],
        )
        results = compare_participants(pred, gold)
        stereo = [r for r in results if r.comparison in ("stereo_only", "stereo_specificity_differs")]
        assert len(stereo) >= 1

    def test_unspecified_stereo_classified(self) -> None:
        pred = _rxn(
            "r1",
            products=[
                CompoundRef(label="P", smiles="CC(O)CC", role="product")
            ],
        )
        gold = _rxn(
            "r1",
            products=[
                CompoundRef(label="P", smiles="C[C@@H](O)CC", role="product")
            ],
        )
        results = compare_participants(pred, gold)
        stereo = [r for r in results if r.comparison in ("stereo_only", "stereo_specificity_differs")]
        assert len(stereo) >= 1

    def test_missing_gold_structure_uncomparable(self) -> None:
        pred = _rxn("r1")
        gold = _rxn(
            "r1",
            products=[CompoundRef(label="B", smiles=None, role="product")],
        )
        results = compare_participants(pred, gold)
        uncomparable = [r for r in results if r.comparison == "uncomparable"]
        assert len(uncomparable) >= 1
        assert any(
            "not annotated" in (r.incomparable_reason or "") for r in uncomparable
        )

    def test_missing_extracted_participant(self) -> None:
        pred = _rxn("r1",
            reactants=[CompoundRef(label="A", smiles="CC", role="reactant")],
            products=[],
        )
        gold = _rxn("r1",
            reactants=[CompoundRef(label="A", smiles="CC", role="reactant")],
            products=[CompoundRef(label="B", smiles="CCO", role="product")],
        )
        results = compare_participants(pred, gold)
        missing = [r for r in results if r.comparison == "missing_extracted"]
        assert len(missing) >= 1

    def test_missing_gold_participant(self) -> None:
        pred = _rxn("r1",
            reactants=[CompoundRef(label="A", smiles="CC", role="reactant")],
            products=[CompoundRef(label="B", smiles="CCO", role="product")],
        )
        gold = _rxn("r1",
            reactants=[CompoundRef(label="A", smiles="CC", role="reactant")],
            products=[],
        )
        results = compare_participants(pred, gold)
        missing = [r for r in results if r.comparison == "missing_gold"]
        assert len(missing) >= 1

    def test_match_by_label_not_index(self) -> None:
        pred = _rxn(
            "r1",
            reactants=[
                CompoundRef(label="X", smiles="CC", role="reactant"),
                CompoundRef(label="Y", smiles="CCC", role="reactant"),
            ],
            products=[CompoundRef(label="B", smiles="CCO", role="product")],
        )
        gold = _rxn(
            "r1",
            reactants=[
                CompoundRef(label="Y", smiles="CCC", role="reactant"),
                CompoundRef(label="X", smiles="CC", role="reactant"),
            ],
            products=[CompoundRef(label="B", smiles="CCO", role="product")],
        )
        results = compare_participants(pred, gold)
        identical = [r for r in results if r.comparison == "identical"]
        assert len(identical) == 3

    def test_charge_salt_isotope_differs(self) -> None:
        pred = _rxn(
            "r1",
            products=[CompoundRef(label="P", smiles="[Na+].CC([O-])=O", role="product")],
        )
        gold = _rxn(
            "r1",
            products=[CompoundRef(label="P", smiles="CC(=O)O.[Na]", role="product")],
        )
        results = compare_participants(pred, gold)
        charge = [r for r in results if r.comparison == "charge_salt_isotope_differs"]
        assert len(charge) >= 1


# ---------------------------------------------------------------------------
# evaluate_records v2
# ---------------------------------------------------------------------------


class TestEvaluateRecordsV2:
    def test_legacy_keys_preserved(self) -> None:
        row = _rxn().model_dump(mode="json")
        report = evaluate_records([row], [row])
        assert report["reaction_precision"] == 1
        assert report["reaction_recall"] == 1
        assert report["yield_accuracy"] == 1
        assert "predicted_count" in report
        assert "gold_count" in report
        assert "matched_count" in report
        assert "structure_coverage" in report

    def test_metrics_version_2(self) -> None:
        row = _rxn().model_dump(mode="json")
        report = evaluate_records([row], [row])
        assert report["metrics_version"] == "2.0"

    def test_new_metric_keys_present(self) -> None:
        row = _rxn().model_dump(mode="json")
        report = evaluate_records([row], [row])
        assert "matched_gold_count" in report
        assert "structure_comparable_count" in report
        assert "structure_agreement" in report
        assert "stereo_comparable_count" in report
        assert "stereo_agreement" in report
        assert "structure_uncomparable_count" in report
        assert "stereo_uncomparable_count" in report
        assert "uncomparable_by_reason" in report

    def test_structure_agreement_identical(self) -> None:
        row = _rxn().model_dump(mode="json")
        report = evaluate_records([row], [row])
        assert report["structure_agreement"] == 1.0
        assert report["structure_agreement_identical"] == report["structure_agreement_comparable"]

    def test_zero_division_safe_empty(self) -> None:
        report = evaluate_records([], [])
        assert report["structure_agreement"] is None
        assert report["stereo_agreement"] is None
        assert report["yield_accuracy"] is None
        assert report["structure_coverage"] == 0.0

    def test_zero_division_safe_no_matches(self) -> None:
        pred = [_rxn(
            "r1",
            reactants=[CompoundRef(label="A", smiles="CC", role="reactant")],
            products=[CompoundRef(label="B", smiles="CCO", role="product")],
        ).model_dump(mode="json")]
        gold = [_rxn(
            "r2",
            reactants=[CompoundRef(label="X", smiles="c1ccccc1", role="reactant")],
            products=[CompoundRef(label="Y", smiles="c1ccccc1O", role="product")],
        ).model_dump(mode="json")]
        report = evaluate_records(pred, gold)
        assert report["matched_count"] == 0
        assert report["structure_agreement"] is None

    def test_stereo_agreement_identical(self) -> None:
        row = _rxn().model_dump(mode="json")
        report = evaluate_records([row], [row])
        assert report["stereo_agreement"] == 1.0


# ---------------------------------------------------------------------------
# build_gold_comparisons (integration)
# ---------------------------------------------------------------------------


class TestBuildGoldComparisons:
    def test_basic(self, tmp_path: Path) -> None:
        gold_file = _gold_jsonl(tmp_path / "gold.jsonl", [_rxn("r1")])
        comparisons = build_gold_comparisons([_rxn("r1")], gold_file)
        assert len(comparisons) == 1
        assert comparisons[0].alignment == "explicit_id"
        assert len(comparisons[0].participant_results) >= 1

    def test_participant_results_populated(self, tmp_path: Path) -> None:
        gold_file = _gold_jsonl(tmp_path / "gold.jsonl", [_rxn("r1")])
        comparisons = build_gold_comparisons([_rxn("r1")], gold_file)
        for gc in comparisons:
            if gc.alignment in ("explicit_id", "mapped"):
                assert len(gc.participant_results) > 0


# ---------------------------------------------------------------------------
# evaluate_files with gold comparison persistence
# ---------------------------------------------------------------------------


class TestEvaluateFiles:
    def test_gold_comparison_jsonl_written(self, tmp_path: Path) -> None:
        from chemex_lit.evaluation import evaluate_files
        from chemex_lit.store import ArtifactStore

        pred_file = tmp_path / "pred.jsonl"
        gold_file = tmp_path / "gold.jsonl"
        row = _rxn().model_dump(mode="json")
        pred_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
        gold_file.write_text(json.dumps(row) + "\n", encoding="utf-8")

        store = ArtifactStore(tmp_path / "run")
        report = evaluate_files(pred_file, gold_file, store=store)
        assert report["metrics_version"] == "2.0"
        assert (tmp_path / "run" / "evaluation.json").is_file()
        assert (tmp_path / "run" / "gold_comparison.jsonl").is_file()

    def test_no_store_no_persistence(self, tmp_path: Path) -> None:
        from chemex_lit.evaluation import evaluate_files

        pred_file = tmp_path / "pred.jsonl"
        gold_file = tmp_path / "gold.jsonl"
        row = _rxn().model_dump(mode="json")
        pred_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
        gold_file.write_text(json.dumps(row) + "\n", encoding="utf-8")

        report = evaluate_files(pred_file, gold_file, store=None)
        assert report["metrics_version"] == "2.0"


# ---------------------------------------------------------------------------
# Duplicate labels edge case
# ---------------------------------------------------------------------------


class TestDuplicateLabels:
    def test_duplicate_labels_in_gold(self) -> None:
        pred = [_rxn("r1")]
        gold = [
            _rxn(
                "r1",
                reactants=[
                    CompoundRef(label="A", smiles="CC", role="reactant"),
                    CompoundRef(label="A", smiles="CCC", role="reactant"),
                ],
            )
        ]
        results = compare_participants(pred[0], gold[0])
        assert len(results) > 0
