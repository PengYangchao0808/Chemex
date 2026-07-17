"""Release-gate metrics without model calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chemex_lit.evaluation.load import load_jsonl
from chemex_lit.evaluation.match import match_records


def evaluate_records(
    predicted: list[dict[str, Any]],
    gold: list[dict[str, Any]],
) -> dict[str, Any]:
    matches = match_records(predicted, gold)
    precision = len(matches) / len(predicted) if predicted else 0.0
    recall = len(matches) / len(gold) if gold else 0.0
    yield_matches = 0
    yield_comparable = 0
    structures_total = 0
    structures_present = 0
    for predicted_row, gold_row in matches:
        pred_yield = predicted_row.get("yield_pct")
        gold_yield = gold_row.get("yield_pct")
        if pred_yield is not None and gold_yield is not None:
            yield_comparable += 1
            if abs(float(pred_yield) - float(gold_yield)) <= 1.0:
                yield_matches += 1
        for compound in predicted_row.get("reactants", []) + predicted_row.get("products", []):
            if isinstance(compound, dict):
                structures_total += 1
                structures_present += bool(compound.get("smiles"))
    return {
        "predicted_count": len(predicted),
        "gold_count": len(gold),
        "matched_count": len(matches),
        "reaction_precision": round(precision, 4),
        "reaction_recall": round(recall, 4),
        "yield_accuracy": round(yield_matches / yield_comparable, 4) if yield_comparable else None,
        "structure_coverage": round(structures_present / structures_total, 4) if structures_total else 0.0,
    }


def evaluate_files(predicted_path: Path, gold_path: Path, output: Path | None = None) -> dict[str, Any]:
    report = evaluate_records(load_jsonl(predicted_path), load_jsonl(gold_path))
    if output is not None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
