"""Conservative deterministic reaction matching."""

from __future__ import annotations

import re
from typing import Any


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
