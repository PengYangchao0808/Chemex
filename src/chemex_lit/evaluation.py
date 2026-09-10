"""Deterministic release-gate metrics: JSONL loading, matching, evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from chemex_lit.chemistry import StructureComparison, compare_structures, normalize_label
from chemex_lit.errors import ChemExError
from chemex_lit.models import (
    CompoundRef,
    GoldComparison,
    GoldParticipantComparison,
    GoldSource,
    ReactionRecord,
)
from chemex_lit.store import ArtifactStore


# ---------------------------------------------------------------------------
# Public gold-related types re-exported for convenience
# ---------------------------------------------------------------------------
__all__ = [
    "GoldComparison",
    "GoldParticipantComparison",
    "GoldSource",
    "align_gold",
    "build_gold_comparisons",
    "compare_participants",
    "evaluate_files",
    "evaluate_records",
    "load_gold",
    "load_jsonl",
    "match_records",
    "reaction_key",
]


# ---------------------------------------------------------------------------
# JSONL loading (existing, unchanged)
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a JSONL file into a list of dicts, raising on bad lines."""
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


# ---------------------------------------------------------------------------
# Reaction matching (existing, unchanged)
# ---------------------------------------------------------------------------


def reaction_key(record: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Build a deduplication key from reactant/product compound keys."""
    return (_compound_keys(record.get("reactants")), _compound_keys(record.get("products")))


def match_records(
    predicted: list[dict[str, Any]],
    gold: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Match predicted to gold records by reaction_id, then by reaction_key."""
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


# ---------------------------------------------------------------------------
# Gold loading (A)
# ---------------------------------------------------------------------------


def load_gold(path: Path) -> tuple[list[ReactionRecord], GoldSource]:
    """Load and validate a gold-standard JSONL file.

    Each non-empty line is validated against :class:`ReactionRecord`.
    Lines that fail validation are accumulated and reported with line
    numbers; any failure raises :class:`ChemExError`.

    Args:
        path: Path to the gold-standard JSONL file.

    Returns:
        A tuple of (validated records, GoldSource provenance).

    Raises:
        ChemExError: If any line fails validation.
    """
    raw_text = path.read_text(encoding="utf-8")
    lines = raw_text.splitlines()
    records: list[ReactionRecord] = []
    errors: list[str] = []
    schema_version = "unknown"
    entry_count = 0

    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        entry_count += 1
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {number}: invalid JSON ({exc})")
            continue
        if not isinstance(data, dict):
            errors.append(f"line {number}: expected JSON object")
            continue
        try:
            record = ReactionRecord.model_validate(data)
        except Exception as exc:
            errors.append(f"line {number}: {exc}")
            continue
        records.append(record)
        if record.schema_version:
            schema_version = record.schema_version

    if errors:
        raise ChemExError(
            f"Gold file validation failed ({len(errors)} error(s)): "
            + "; ".join(errors)
        )

    file_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    source = GoldSource(
        file_name=path.name,
        file_hash=file_hash,
        entry_count=entry_count,
        gold_schema_version=schema_version,
        normalization_policy="none",
    )
    return records, source


# ---------------------------------------------------------------------------
# Alignment (B)
# ---------------------------------------------------------------------------


def align_gold(
    predicted: list[ReactionRecord],
    gold: list[ReactionRecord],
) -> list[GoldComparison]:
    """Align predicted records to gold-standard records.

    Alignment precedence:
    1. Explicit ``reaction_id`` equality → ``"explicit_id"``.
    2. Evidence/position-based candidates via :func:`reaction_key`:
       - Exactly one candidate → ``"mapped"``.
       - Multiple candidates → ``"ambiguous"`` (never silently pick).
    3. Unmatched on either side → ``"unmatched_extracted"`` /
       ``"unmatched_gold"``.

    Duplicate ``reaction_id`` values in gold surface as ambiguity, never
    overwrite.  Structure equality is never used for alignment.

    Args:
        predicted: Extracted reaction records.
        gold: Gold-standard reaction records.

    Returns:
        A list of :class:`GoldComparison` rows.  ``participant_results``
        is left empty; caller should populate via :func:`compare_participants`.
    """
    # Build gold index by reaction_id, detecting duplicates.
    gold_by_id: dict[str, list[ReactionRecord]] = {}
    for g in gold:
        gold_by_id.setdefault(g.reaction_id, []).append(g)

    # Build predicted indexes.
    pred_by_id: dict[str, ReactionRecord] = {}
    for p in predicted:
        pred_by_id.setdefault(p.reaction_id, p)
    pred_by_key: dict[
        tuple[tuple[str, ...], tuple[str, ...]], list[ReactionRecord]
    ] = {}
    for p in predicted:
        pred_by_key.setdefault(_record_reaction_key(p), []).append(p)

    used_pred: set[int] = set()
    comparisons: list[GoldComparison] = []

    for g in gold:
        gid = g.reaction_id
        gold_dup_count = len(gold_by_id.get(gid, []))

        # Case 1: explicit_id match — but duplicate gold IDs → ambiguous
        if gold_dup_count > 1:
            candidate = pred_by_id.get(gid)
            if candidate is not None and id(candidate) not in used_pred:
                comparisons.append(
                    GoldComparison(
                        reaction_id=candidate.reaction_id,
                        gold_reaction_id=gid,
                        alignment="ambiguous",
                        alignment_basis=(
                            f"duplicate gold reaction_id {gid!r} "
                            f"({gold_dup_count} occurrences)"
                        ),
                        gold_source=_placeholder_source(),
                    )
                )
                used_pred.add(id(candidate))
            else:
                comparisons.append(
                    GoldComparison(
                        reaction_id=f"<gold:{gid}>",
                        gold_reaction_id=gid,
                        alignment="unmatched_gold",
                        alignment_basis=(
                            f"duplicate gold reaction_id {gid!r} "
                            f"({gold_dup_count} occurrences)"
                        ),
                        gold_source=_placeholder_source(),
                    )
                )
            continue

        # Case 2: explicit_id match (unique gold ID)
        candidate = pred_by_id.get(gid)
        if candidate is not None and id(candidate) not in used_pred:
            comparisons.append(
                GoldComparison(
                    reaction_id=candidate.reaction_id,
                    gold_reaction_id=gid,
                    alignment="explicit_id",
                    alignment_basis="reaction_id equality",
                    gold_source=_placeholder_source(),
                )
            )
            used_pred.add(id(candidate))
            continue

        # Case 3: evidence/position-based candidates
        key = _record_reaction_key(g)
        options = [
            item for item in pred_by_key.get(key, []) if id(item) not in used_pred
        ]
        if len(options) == 1:
            chosen = options[0]
            comparisons.append(
                GoldComparison(
                    reaction_id=chosen.reaction_id,
                    gold_reaction_id=gid,
                    alignment="mapped",
                    alignment_basis="reaction_key match",
                    gold_source=_placeholder_source(),
                )
            )
            used_pred.add(id(chosen))
        elif len(options) > 1:
            comparisons.append(
                GoldComparison(
                    reaction_id=f"<gold:{gid}>",
                    gold_reaction_id=gid,
                    alignment="ambiguous",
                    alignment_basis=(
                        f"multiple candidates ({len(options)}) "
                        f"for reaction_key {key!r}"
                    ),
                    gold_source=_placeholder_source(),
                )
            )
        else:
            # Unmatched gold
            comparisons.append(
                GoldComparison(
                    reaction_id=f"<gold:{gid}>",
                    gold_reaction_id=gid,
                    alignment="unmatched_gold",
                    alignment_basis="no candidate found",
                    gold_source=_placeholder_source(),
                )
            )

    # Unmatched predictions
    for p in predicted:
        if id(p) not in used_pred:
            comparisons.append(
                GoldComparison(
                    reaction_id=p.reaction_id,
                    gold_reaction_id=None,
                    alignment="unmatched_extracted",
                    alignment_basis="no gold match",
                    gold_source=_placeholder_source(),
                )
            )

    return comparisons


# ---------------------------------------------------------------------------
# Participant comparison (C)
# ---------------------------------------------------------------------------


def compare_participants(
    predicted_record: ReactionRecord,
    gold_record: ReactionRecord,
) -> list[GoldParticipantComparison]:
    """Compare participants between a predicted and a gold record.

    Participants are matched by (role, normalized label/name), with
    structure identity (SMILES) as a fallback for unmatched participants
    within the same role group.  Participants present on one side only
    produce ``missing_extracted`` or ``missing_gold`` comparisons.
    Gold participants without SMILES produce ``uncomparable`` with
    reason ``"gold structure not annotated"``.

    Args:
        predicted_record: The extracted reaction record.
        gold_record: The gold-standard reaction record.

    Returns:
        A list of :class:`GoldParticipantComparison`, one per participant
        from both sides.
    """
    pred_parts = predicted_record.reactants + predicted_record.products
    gold_parts = gold_record.reactants + gold_record.products

    # Group by role
    pred_by_role: dict[str, list[CompoundRef]] = {}
    for part in pred_parts:
        pred_by_role.setdefault(part.role, []).append(part)
    gold_by_role: dict[str, list[CompoundRef]] = {}
    for part in gold_parts:
        gold_by_role.setdefault(part.role, []).append(part)

    all_roles = sorted(set(pred_by_role) | set(gold_by_role))
    results: list[GoldParticipantComparison] = []

    for role in all_roles:
        p_list = list(pred_by_role.get(role, []))
        g_list = list(gold_by_role.get(role, []))

        # Primary match: normalized label/name
        matched: list[tuple[CompoundRef, CompoundRef]] = []
        unmatched_p = list(p_list)
        unmatched_g = list(g_list)

        for p in list(unmatched_p):
            pk = _participant_key(p)
            if not pk:
                continue
            for g in list(unmatched_g):
                gk = _participant_key(g)
                if gk and pk == gk:
                    matched.append((p, g))
                    unmatched_p.remove(p)
                    unmatched_g.remove(g)
                    break

        # Fallback match: SMILES identity within same role
        for p in list(unmatched_p):
            ps = _norm_smiles(p.smiles)
            if not ps:
                continue
            for g in list(unmatched_g):
                gs = _norm_smiles(g.smiles)
                if gs and ps == gs:
                    matched.append((p, g))
                    unmatched_p.remove(p)
                    unmatched_g.remove(g)
                    break

        # Emit matched pairs
        for p, g in matched:
            pid = _part_id(p)
            gid = _part_id(g)
            g_smiles = g.smiles.strip() if g.smiles else ""
            p_smiles = p.smiles.strip() if p.smiles else ""

            if not g_smiles:
                results.append(
                    GoldParticipantComparison(
                        participant_id=pid,
                        gold_participant_id=gid,
                        comparison="uncomparable",
                        incomparable_reason="gold structure not annotated",
                    )
                )
            elif not p_smiles:
                results.append(
                    GoldParticipantComparison(
                        participant_id=pid,
                        gold_participant_id=gid,
                        comparison="uncomparable",
                        incomparable_reason="extracted structure not annotated",
                    )
                )
            else:
                cmp = compare_structures(p_smiles, g_smiles)
                mapped_level = _map_comparison_level(cmp)
                results.append(
                    GoldParticipantComparison(
                        participant_id=pid,
                        gold_participant_id=gid,
                        comparison=mapped_level,
                        incomparable_reason=(
                            cmp.detail if mapped_level == "uncomparable" else None
                        ),
                    )
                )

        # Unmatched predicted → missing in gold
        for p in unmatched_p:
            results.append(
                GoldParticipantComparison(
                    participant_id=_part_id(p),
                    gold_participant_id=None,
                    comparison="missing_gold",
                )
            )

        # Unmatched gold → missing in predicted
        for g in unmatched_g:
            results.append(
                GoldParticipantComparison(
                    participant_id=f"<gold:{_part_id(g)}>",
                    gold_participant_id=_part_id(g),
                    comparison="missing_extracted",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Evaluate records (D) — v2 with new metrics, legacy keys preserved
# ---------------------------------------------------------------------------


def evaluate_records(
    predicted: list[dict[str, Any]],
    gold: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute evaluation metrics between predicted and gold records.

    Returns all legacy keys (predicted_count, gold_count, matched_count,
    reaction_precision, reaction_recall, yield_accuracy, structure_coverage)
    plus v2 metrics with ``metrics_version: "2.0"``.

    All divisions are zero-safe: ``None`` when denominator is 0, matching
    the existing ``yield_accuracy`` pattern.
    """
    matches = match_records(predicted, gold)
    precision = len(matches) / len(predicted) if predicted else 0.0
    recall = len(matches) / len(gold) if gold else 0.0

    # Legacy metrics (unchanged semantics)
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
        for compound in predicted_row.get("reactants", []) + predicted_row.get(
            "products", []
        ):
            if isinstance(compound, dict):
                structures_total += 1
                structures_present += bool(compound.get("smiles"))

    # Build typed records for gold-aware participant comparison
    pred_records = _safe_validate_records(predicted)
    gold_records = _safe_validate_records(gold)

    # Build match lookup by reaction_id for typed comparison
    gold_by_id: dict[str, ReactionRecord] = {
        r.reaction_id: r for r in gold_records
    }

    # Participant-level structure and stereo counts
    structure_identical = 0
    structure_comparable = 0
    structure_uncomparable = 0
    stereo_identical = 0
    stereo_comparable = 0
    stereo_uncomparable = 0
    uncomparable_by_reason: dict[str, int] = {}

    for pred_row, _gold_row in matches:
        rid = pred_row.get("reaction_id", "")
        gold_rec = gold_by_id.get(rid)
        if gold_rec is None:
            continue

        # Find matching typed predicted record
        pred_rec = next(
            (r for r in pred_records if r.reaction_id == rid), None
        )
        if pred_rec is None:
            continue

        participants = compare_participants(pred_rec, gold_rec)
        for pc in participants:
            cmp = pc.comparison
            if cmp in ("missing_extracted", "missing_gold"):
                continue
            if cmp == "uncomparable":
                structure_uncomparable += 1
                stereo_uncomparable += 1
                reason = pc.incomparable_reason or "unknown"
                uncomparable_by_reason[reason] = (
                    uncomparable_by_reason.get(reason, 0) + 1
                )
                continue

            # Structure comparable
            structure_comparable += 1
            if cmp == "identical":
                structure_identical += 1

            # Stereo: among comparable pairs with stereo info
            if cmp in ("identical", "stereo_only", "stereo_specificity_differs"):
                if cmp == "identical":
                    stereo_identical += 1
                    stereo_comparable += 1
                elif cmp in ("stereo_only", "stereo_specificity_differs"):
                    stereo_comparable += 1

    report: dict[str, Any] = {
        # Legacy keys (unchanged semantics)
        "predicted_count": len(predicted),
        "gold_count": len(gold),
        "matched_count": len(matches),
        "reaction_precision": round(precision, 4),
        "reaction_recall": round(recall, 4),
        "yield_accuracy": (
            round(yield_matches / yield_comparable, 4)
            if yield_comparable
            else None
        ),
        "structure_coverage": (
            round(structures_present / structures_total, 4)
            if structures_total
            else 0.0
        ),
        # V2 metrics
        "metrics_version": "2.0",
        "matched_gold_count": len(matches),
        "structure_comparable_count": structure_comparable,
        "structure_agreement": (
            round(structure_identical / structure_comparable, 4)
            if structure_comparable
            else None
        ),
        "structure_agreement_identical": structure_identical,
        "structure_agreement_comparable": structure_comparable,
        "structure_uncomparable_count": structure_uncomparable,
        "stereo_comparable_count": stereo_comparable,
        "stereo_agreement": (
            round(stereo_identical / stereo_comparable, 4)
            if stereo_comparable
            else None
        ),
        "stereo_agreement_identical": stereo_identical,
        "stereo_agreement_comparable": stereo_comparable,
        "stereo_uncomparable_count": stereo_uncomparable,
        "uncomparable_by_reason": uncomparable_by_reason,
    }
    return report


# ---------------------------------------------------------------------------
# Evaluate files (E)
# ---------------------------------------------------------------------------


def evaluate_files(
    predicted_path: Path,
    gold_path: Path,
    store: ArtifactStore | None = None,
) -> dict[str, Any]:
    """Evaluate predicted vs gold JSONL files.

    When *store* is provided, persists ``evaluation.json`` and
    ``gold_comparison.jsonl`` (the latter via :mod:`chemex_lit.models`
    only — no new store helpers are added).

    Args:
        predicted_path: Path to predicted records JSONL.
        gold_path: Path to gold-standard records JSONL.
        store: Optional artifact store for persistence.

    Returns:
        The evaluation metrics dict.
    """
    report = evaluate_records(load_jsonl(predicted_path), load_jsonl(gold_path))
    if store is not None:
        store.write_json("evaluation.json", report)
        # Build and persist gold comparison details
        gold_comparisons = build_gold_comparisons(
            _safe_validate_records(load_jsonl(predicted_path)),
            gold_path,
        )
        store.write_jsonl(
            "gold_comparison.jsonl",
            [gc.model_dump(mode="json") for gc in gold_comparisons],
        )
    return report


# ---------------------------------------------------------------------------
# Build gold comparisons (F)
# ---------------------------------------------------------------------------


def build_gold_comparisons(
    predicted_records: list[ReactionRecord],
    gold_path: Path,
    *,
    store: ArtifactStore | None = None,
) -> list[GoldComparison]:
    """Load gold, align, and compare participants.

    Composes :func:`load_gold`, :func:`align_gold`, and
    :func:`compare_participants` into a single convenience call for
    CLI/review consumers.

    Args:
        predicted_records: Extracted reaction records.
        gold_path: Path to the gold-standard JSONL file.
        store: Optional artifact store (unused currently, reserved for
            future gold-source persistence).

    Returns:
        A list of :class:`GoldComparison` with populated
        ``participant_results``.
    """
    gold_records, _gold_source = load_gold(gold_path)
    comparisons = align_gold(predicted_records, gold_records)

    # Build lookup for populating participant_results
    pred_by_id = {r.reaction_id: r for r in predicted_records}
    gold_by_id = {r.reaction_id: r for r in gold_records}

    for gc in comparisons:
        if gc.alignment in ("unmatched_extracted", "unmatched_gold", "ambiguous"):
            continue
        pred_rec = pred_by_id.get(gc.reaction_id)
        gold_rec = gold_by_id.get(gc.gold_reaction_id) if gc.gold_reaction_id else None
        if pred_rec is not None and gold_rec is not None:
            gc.participant_results = compare_participants(pred_rec, gold_rec)

    return comparisons


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _compound_keys(value: Any) -> tuple[str, ...]:
    """Extract sorted normalized label/name/smiles keys from a compound list."""
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


_COMPARISON_LEVELS = Literal[
    "identical",
    "connectivity_differs",
    "stereo_only",
    "stereo_specificity_differs",
    "charge_salt_isotope_differs",
    "missing_extracted",
    "missing_gold",
    "uncomparable",
]


def _map_comparison_level(cmp: StructureComparison) -> _COMPARISON_LEVELS:
    if not cmp.comparable or cmp.level == "missing_one_side":
        return "uncomparable"
    return cmp.level  # type: ignore[return-value]


def _record_reaction_key(
    record: ReactionRecord | dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Build a reaction_key from a typed record or raw dict."""
    if isinstance(record, ReactionRecord):
        reactants = [
            {"label": c.label, "name": c.name, "smiles": c.smiles}
            for c in record.reactants
        ]
        products = [
            {"label": c.label, "name": c.name, "smiles": c.smiles}
            for c in record.products
        ]
        return (tuple(sorted(_compound_keys(reactants))),
                tuple(sorted(_compound_keys(products))))
    return reaction_key(record)


def _participant_key(compound: CompoundRef) -> str:
    """Normalized identity key for a participant (label or name)."""
    raw = compound.label or compound.name
    if not raw:
        return ""
    return normalize_label(raw)


def _part_id(compound: CompoundRef) -> str:
    """Stable participant identifier string."""
    return compound.label or compound.name or "<unlabelled>"


def _norm_smiles(smiles: str | None) -> str:
    """Strip whitespace from SMILES for identity comparison."""
    if not smiles:
        return ""
    return smiles.strip()


def _placeholder_source() -> GoldSource:
    """Minimal GoldSource for alignment rows (populated fully later)."""
    return GoldSource(
        file_name="<alignment>",
        file_hash="",
        entry_count=0,
        gold_schema_version="1.0",
    )


def _safe_validate_records(
    rows: list[dict[str, Any]],
) -> list[ReactionRecord]:
    """Best-effort parse of dicts into ReactionRecord, skipping failures."""
    result: list[ReactionRecord] = []
    for row in rows:
        try:
            result.append(ReactionRecord.model_validate(row))
        except Exception:
            continue
    return result
