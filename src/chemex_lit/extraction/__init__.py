"""Shared normalization for the three extraction channels."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Literal

from chemex_lit.models import CompoundRef, ReactionCandidate, StructureCandidate


def stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return f"{prefix}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:12]}"


def _compounds(
    value: Any,
    role: Literal["reactant", "product", "reagent", "unknown"],
) -> list[CompoundRef]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        value = [value]
    if not isinstance(value, list):
        return []
    result: list[CompoundRef] = []
    for item in value:
        if isinstance(item, dict):
            label = item.get("label") or item.get("compound_label") or item.get("id")
            name = item.get("name") or item.get("compound_name")
            smiles = item.get("smiles")
            result.append(CompoundRef(label=label, name=name, smiles=smiles, role=role))
        elif item is not None and str(item).strip():
            result.append(CompoundRef(label=str(item).strip(), role=role))
    return result


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"\s*[;,]\s*", value.strip())
        return [part for part in parts if part]
    if isinstance(value, list):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def reaction_candidates_from_payload(
    payload: Any,
    source: Literal["text", "table", "scheme"],
    evidence_ids: Iterable[str],
) -> list[ReactionCandidate]:
    """Normalize common LLM response variants into the stable candidate schema."""
    if isinstance(payload, dict):
        rows = payload.get("reactions", payload.get("data", payload))
    else:
        rows = payload
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []

    evidence = sorted(set(evidence_ids))
    candidates: list[ReactionCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        reactants = row.get("reactants", row.get("substrates", row.get("starting_materials")))
        products = row.get("products", row.get("product"))
        confidence = _number(row.get("confidence"))
        normalized = {
            "source": source,
            "reactants": reactants,
            "products": products,
            "reagents": row.get("reagents", row.get("catalysts")),
            "solvents": row.get("solvents", row.get("solvent")),
            "temperature_c": row.get("temperature_c", row.get("temperature")),
            "time": row.get("time", row.get("reaction_time")),
            "yield_pct": row.get("yield_pct", row.get("yield")),
            "evidence": evidence,
        }
        try:
            candidates.append(
                ReactionCandidate(
                    candidate_id=stable_id(source, normalized),
                    source=source,
                    reactants=_compounds(reactants, "reactant"),
                    products=_compounds(products, "product"),
                    reagents=_strings(normalized["reagents"]),
                    solvents=_strings(normalized["solvents"]),
                    temperature_c=_number(normalized["temperature_c"]),
                    time=str(normalized["time"]).strip() if normalized["time"] else None,
                    yield_pct=_bounded_percent(_number(normalized["yield_pct"])),
                    evidence_ids=evidence,
                    confidence=confidence if confidence is not None else 0.5,
                )
            )
        except ValueError:
            continue
    return candidates


def structure_candidates_from_payload(
    payload: Any,
    evidence_ids: Iterable[str],
) -> list[StructureCandidate]:
    if isinstance(payload, dict):
        rows = payload.get("structures", payload.get("candidates", payload))
    else:
        rows = payload
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []

    evidence = sorted(set(evidence_ids))
    result: list[StructureCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        smiles = row.get("smiles") or row.get("canonical_smiles")
        if not smiles:
            continue
        label = row.get("compound_label") or row.get("label") or row.get("compound_id")
        confidence = _number(row.get("confidence"))
        raw = {"label": label, "smiles": smiles, "evidence": evidence}
        try:
            result.append(
                StructureCandidate(
                    candidate_id=stable_id("structure", raw),
                    compound_label=str(label).strip() if label is not None else None,
                    smiles=str(smiles).strip(),
                    evidence_ids=evidence,
                    confidence=confidence if confidence is not None else 0.5,
                )
            )
        except ValueError:
            continue
    return result


def load_external_structures(path: Path) -> list[StructureCandidate]:
    """Load human-supplied StructureCandidate JSONL without a second pipeline."""
    result: list[StructureCandidate] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        if "candidate_id" in data:
            result.append(StructureCandidate.model_validate(data))
        else:
            parsed = structure_candidates_from_payload(data, [f"manual-line-{number}"])
            result.extend(parsed)
    return result


def _bounded_percent(value: float | None) -> float | None:
    return value if value is None or 0 <= value <= 100 else None


__all__ = [
    "load_external_structures",
    "reaction_candidates_from_payload",
    "stable_id",
    "structure_candidates_from_payload",
]
