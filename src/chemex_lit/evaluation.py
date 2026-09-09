"""Deterministic release-gate metrics: JSONL loading, matching, evaluation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from chemex_lit.store import ArtifactStore


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Expected object at {path}:{number}")
        rows.append(row)
    return rows


def reaction_key(record: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (_compound_keys(record.get("reactants")), _compound_keys(record.get("products")))


def match_records(
    predicted: list[dict[str, Any]],
    gold: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    by_id = {row.get("reaction_id"): row for row in predicted if row.get("reaction_id")}
    by_key: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]] = {}
    for row in predicted:
        by_key.setdefault(reaction_key(row), []).append(row)

    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used: set[int] = set()
    for gold_row in gold:
        candidate = by_id.get(gold_row.get("reaction_id"))
        if candidate is None:
            options = by_key.get(reaction_key(gold_row), [])
            candidate = next((item for item in options if id(item) not in used), None)
        if candidate is not None and id(candidate) not in used:
            matches.append((candidate, gold_row))
            used.add(id(candidate))
    return matches


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


def evaluate_files(
    predicted_path: Path,
    gold_path: Path,
    store: ArtifactStore | None = None,
) -> dict[str, Any]:
    report = evaluate_records(load_jsonl(predicted_path), load_jsonl(gold_path))
    if store is not None:
        store.write_json("evaluation.json", report)
    return report


def _compound_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            raw = item.get("label") or item.get("name") or item.get("smiles")
        else:
            raw = item
        if raw:
            result.append(re.sub(r"\s+", "", str(raw)).lower())
    return tuple(sorted(result))
