"""Single review generator/applier for the evidence review workbench.

Public API:
  - generate_review(records, store, **kwargs) -> Path
  - apply_corrections(records, corrections_path, *, confirmed_by) -> (records, audit)
  - apply_submission(records, submission_path, *, confirmed_by, store) -> (records, audit)
  - build_review_view(records, **kwargs) -> dict
  - aggregate_human_status(reaction_id, decisions) -> str
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

from chemex_lit.chemistry import (
    Validator,
    analyze_stereo,
    asset_basename,
    gold_highlight_atoms,
    render_structure,
)
from chemex_lit.errors import utc_now
from chemex_lit.models import (
    CompoundRef,
    EvidenceRef,
    GoldComparison,
    HumanReviewStatus,
    ReactionRecord,
    ReviewConditionItem,
    ReviewContext,
    ReviewDecision,
    ReviewOperation,
    ReviewParticipant,
    ReviewSubmission,
    ValidationIssue,
)
from chemex_lit.store import ArtifactStore

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    """Replace unsafe characters with underscores for store-relative paths."""
    return _SAFE_FILENAME.sub("_", name)


def _record_hash(record: ReactionRecord) -> str:
    """SHA-256 of the canonical JSON serialisation of a ReactionRecord."""
    return hashlib.sha256(record.model_dump_json().encode("utf-8")).hexdigest()


def _read_jsonl_defensive(
    store: ArtifactStore | None,
    relative: str,
    model: type,
) -> list[Any]:
    """Read JSONL artifacts from store, returning [] on missing file."""
    if store is None:
        return []
    path = store.root / relative
    if not path.is_file():
        return []
    result: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        result.append(model.model_validate_json(line))
    return result


def _read_json_defensive(
    store: ArtifactStore | None,
    relative: str,
) -> dict[str, Any] | None:
    """Read a JSON artifact from store, returning None on missing file."""
    if store is None:
        return None
    path = store.root / relative
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Stereo / structure analysis per participant
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StereoDisplay:
    """Rendered stereo analysis for one participant."""

    rdkit_available: bool
    centers: list[dict[str, Any]]
    bonds: list[dict[str, Any]]
    analysis_note: str | None = None


@dataclass(frozen=True)
class _ParticipantDisplay:
    """One participant card in the review view."""

    participant_id: str
    role: str
    label: str
    name: str
    smiles: str
    structure_state: str
    images: dict[str, Any]
    render_status: str
    stale_image: bool
    machine_issues: list[dict[str, Any]]
    evidence_ids: list[str]
    field_evidence_ids: list[str]
    has_field_evidence: bool
    stereo: _StereoDisplay


@dataclass(frozen=True)
class _ConditionDisplay:
    """One condition row in the review view."""

    condition_id: str
    kind: str
    value: str
    numeric_value: float | None
    unit: str
    stage_index: int | None
    extraction_state: str
    evidence_ids: list[str]
    field_evidence_ids: list[str]
    has_field_evidence: bool


@dataclass(frozen=True)
class _GoldDisplay:
    """Gold comparison section for one reaction."""

    reaction_id: str
    gold_reaction_id: str | None
    alignment: str
    alignment_basis: str | None
    participant_results: list[dict[str, Any]]
    has_gold: bool


@dataclass(frozen=True)
class _EvidenceDisplay:
    """Resolved evidence entry."""

    evidence_id: str
    kind: str
    page: int | None
    text: str
    asset_path: str | None


@dataclass(frozen=True)
class _ReactionView:
    """Complete view data for one reaction."""

    reaction_id: str
    review_status: str
    confidence: float
    machine_status: str
    human_status: str
    position: int
    participants: list[_ParticipantDisplay]
    conditions: list[_ConditionDisplay]
    reactants_text: str
    products_text: str
    reagents: list[str]
    solvents: list[str]
    temperature_c: float | None
    time: str
    yield_pct: float | None
    evidence_ids: list[str]
    issues: list[dict[str, Any]]
    gold: _GoldDisplay
    context_note: str | None


@dataclass(frozen=True)
class _EvidenceRefDisplay:
    """Lightweight evidence ref for the evidence viewer."""

    evidence_id: str
    kind: str
    page: int | None
    text: str
    asset_path: str | None


# ---------------------------------------------------------------------------
# View assembly
# ---------------------------------------------------------------------------


def _synthesize_participants(record: ReactionRecord) -> list[ReviewParticipant]:
    """Build participants from a ReactionRecord when no ReviewContext exists."""
    participants: list[ReviewParticipant] = []
    counter: dict[str, int] = {}
    for compound in record.reactants:
        counter["reactant"] = counter.get("reactant", 0) + 1
        participants.append(ReviewParticipant(
            participant_id=(
                f"{record.reaction_id}:reactant:{counter['reactant']}"
            ),
            role="reactant",
            label=compound.label,
            name=compound.name,
            smiles=compound.smiles,
            structure_state="resolved" if compound.smiles else "unresolved",
            evidence_ids=list(record.evidence_ids),
        ))
    for compound in record.products:
        counter["product"] = counter.get("product", 0) + 1
        participants.append(ReviewParticipant(
            participant_id=(
                f"{record.reaction_id}:product:{counter['product']}"
            ),
            role="product",
            label=compound.label,
            name=compound.name,
            smiles=compound.smiles,
            structure_state="resolved" if compound.smiles else "unresolved",
            evidence_ids=list(record.evidence_ids),
        ))
    for name in record.reagents:
        counter["reagent"] = counter.get("reagent", 0) + 1
        participants.append(ReviewParticipant(
            participant_id=(
                f"{record.reaction_id}:reagent:{counter['reagent']}"
            ),
            role="reagent",
            name=name,
            structure_state="not_attempted",
            evidence_ids=[],
        ))
    for name in record.solvents:
        counter["solvent"] = counter.get("solvent", 0) + 1
        participants.append(ReviewParticipant(
            participant_id=(
                f"{record.reaction_id}:solvent:{counter['solvent']}"
            ),
            role="solvent",
            name=name,
            structure_state="not_attempted",
            evidence_ids=[],
        ))
    return participants


def _synthesize_conditions(record: ReactionRecord) -> list[ReviewConditionItem]:
    """Build conditions from v1 fields when no ReviewContext exists."""
    conditions: list[ReviewConditionItem] = []
    if record.temperature_c is not None:
        conditions.append(ReviewConditionItem(
            condition_id=f"{record.reaction_id}:cond:temperature",
            kind="temperature",
            value=str(record.temperature_c),
            numeric_value=record.temperature_c,
            unit="°C",
            extraction_state="extracted",
        ))
    if record.time is not None:
        conditions.append(ReviewConditionItem(
            condition_id=f"{record.reaction_id}:cond:time",
            kind="time",
            value=record.time,
            extraction_state="extracted",
        ))
    if record.yield_pct is not None:
        conditions.append(ReviewConditionItem(
            condition_id=f"{record.reaction_id}:cond:yield",
            kind="yield",
            value=str(record.yield_pct),
            numeric_value=record.yield_pct,
            unit="%",
            extraction_state="extracted",
        ))
    for idx, name in enumerate(record.reagents):
        conditions.append(ReviewConditionItem(
            condition_id=f"{record.reaction_id}:cond:reagent:{idx}",
            kind="reagent",
            value=name,
            extraction_state="extracted",
        ))
    for idx, name in enumerate(record.solvents):
        conditions.append(ReviewConditionItem(
            condition_id=f"{record.reaction_id}:cond:solvent:{idx}",
            kind="solvent",
            value=name,
            extraction_state="extracted",
        ))
    return conditions


def _participant_field_path(
    p: ReviewParticipant,
    record: ReactionRecord,
) -> str | None:
    """Compute dot path to this participant's value in the record.

    Returns e.g. ``reactants.0.smiles``, ``reagents.0``, or ``None``
    when the participant_id does not map to a legal record path.
    """
    parts = p.participant_id.split(":")
    if len(parts) < 3:
        return None
    try:
        idx = int(parts[-1]) - 1  # participant_id is 1-based
    except ValueError:
        return None
    if idx < 0:
        return None
    if p.role == "reactant" and idx < len(record.reactants):
        return f"reactants.{idx}.smiles"
    if p.role == "product" and idx < len(record.products):
        return f"products.{idx}.smiles"
    if p.role == "reagent" and idx < len(record.reagents):
        return f"reagents.{idx}"
    if p.role == "solvent" and idx < len(record.solvents):
        return f"solvents.{idx}"
    return None


def _condition_field_path(condition: ReviewConditionItem) -> str | None:
    """Compute dot path to this condition's scalar in the record.

    Maps ``temperature``/``time``/``yield`` to their v1 scalar fields and
    ``reagent``/``solvent`` to indexed list paths.  Returns ``None`` for
    condition kinds without a legal set_value path.
    """
    kind = condition.kind
    if kind == "temperature":
        return "temperature_c"
    if kind == "time":
        return "time"
    if kind == "yield":
        return "yield_pct"
    if kind == "reagent":
        parts = condition.condition_id.split(":")
        try:
            return f"reagents.{int(parts[-1])}"
        except (ValueError, IndexError):
            return None
    if kind == "solvent":
        parts = condition.condition_id.split(":")
        try:
            return f"solvents.{int(parts[-1])}"
        except (ValueError, IndexError):
            return None
    return None


def _analyze_stereo_display(smiles: str | None) -> _StereoDisplay:
    """Run stereo analysis and format for display."""
    if not smiles:
        return _StereoDisplay(
            rdkit_available=False,
            centers=[],
            bonds=[],
            analysis_note="no SMILES provided",
        )
    analysis = analyze_stereo(smiles)
    if analysis is None:
        return _StereoDisplay(
            rdkit_available=False,
            centers=[],
            bonds=[],
            analysis_note="analysis unavailable — SMILES unparseable",
        )
    if not analysis.rdkit_available:
        return _StereoDisplay(
            rdkit_available=False,
            centers=[],
            bonds=[],
            analysis_note="analysis unavailable — RDKit not installed",
        )
    centers = [
        {
            "atom_index": c.atom_index,
            "assignment": c.assignment,
            "specified": c.specified,
        }
        for c in analysis.centers
    ]
    bonds = [
        {
            "bond_index": b.bond_index,
            "begin_atom": b.begin_atom,
            "end_atom": b.end_atom,
            "assignment": b.assignment,
            "specified": b.specified,
        }
        for b in analysis.bonds
    ]
    return _StereoDisplay(
        rdkit_available=True,
        centers=centers,
        bonds=bonds,
        analysis_note=None,
    )


def _render_participant_assets(
    smiles: str | None,
    role: str,
    store: ArtifactStore | None,
) -> tuple[dict[str, Any], str]:
    """Render structure images as hashed asset files.

    Returns ``(images_dict, render_status)``.  When *store* is ``None``
    only render_status is computed (paths are all ``None``).

    Args:
        smiles: SMILES string, or ``None``/blank for missing.
        role: Participant role; stereo/atommap modes only for
            reactant/product.
        store: ArtifactStore for writing asset files.  ``None`` means
            pure view-building with no rendering.

    Returns:
        Tuple of (images dict, render_status string).
    """
    _IMG_SIZE = (420, 280)
    _empty = {"svg": None, "png": None}

    # Auxiliary participants remain condition metadata, never structure assets.
    if role not in ("reactant", "product"):
        return {"normal": dict(_empty), "stereo": None, "atommap": None}, "not_applicable"

    # No SMILES at all → missing_smiles, no images.
    if not smiles or not smiles.strip():
        return {"normal": dict(_empty), "stereo": None, "atommap": None}, "missing_smiles"

    # Store-less path: only compute render_status.
    if store is None:
        canonical = Validator.canonicalize(smiles)
        status = "ok" if canonical else "invalid_smiles"
        return {"normal": dict(_empty), "stereo": None, "atommap": None}, status

    # Determine which modes to render.
    modes: list[str] = ["normal"]
    if role in ("reactant", "product"):
        modes.extend(["stereo", "atommap"])

    images: dict[str, Any] = {}
    overall_status: str = "ok"

    for mode in modes:
        result = render_structure(smiles, size=_IMG_SIZE, mode=mode)  # type: ignore[arg-type]
        if result.status != "ok":
            if mode == "normal":
                # Normal mode failure is the overall status.
                overall_status = result.status
                images["normal"] = dict(_empty)
            else:
                images[mode] = None
            continue

        basename = asset_basename(smiles, mode, _IMG_SIZE)
        if basename is None:
            if mode == "normal":
                overall_status = "invalid_smiles"
                images["normal"] = dict(_empty)
            else:
                images[mode] = None
            continue

        svg_rel = f"review_assets/{basename}.svg"
        png_rel = f"review_assets/{basename}.png"

        # Write SVG (cache-reuse: skip if file already exists).
        svg_path = store.root / svg_rel
        if not svg_path.is_file() and result.svg is not None:
            store.write_bytes(svg_rel, result.svg.encode("utf-8"))
        # Write PNG (cache-reuse: skip if file already exists).
        png_path = store.root / png_rel
        if not png_path.is_file() and result.png is not None:
            store.write_bytes(png_rel, result.png)

        images[mode] = {"svg": svg_rel, "png": png_rel}

    return images, overall_status


def _build_participant_display(
    participant: ReviewParticipant,
    reaction_id: str,
    record: ReactionRecord,
    evidence_map: dict[str, EvidenceRef],
    store: ArtifactStore | None,
    is_legacy: bool,
) -> _ParticipantDisplay:
    """Build display data for one participant."""
    machine_issues = [
        {
            "code": issue.code,
            "severity": issue.severity,
            "target_id": issue.target_id,
            "message": issue.message,
        }
        for issue in record.issues
        if issue.target_id == (participant.label or participant.name or "")
    ]
    images, render_status = _render_participant_assets(
        participant.smiles, participant.role, store
    )
    has_field = bool(participant.field_evidence_ids)
    return _ParticipantDisplay(
        participant_id=participant.participant_id,
        role=participant.role,
        label=participant.label or "",
        name=participant.name or "",
        smiles=participant.smiles or "",
        structure_state=participant.structure_state,
        images=images,
        render_status=render_status,
        stale_image=False,
        machine_issues=machine_issues,
        evidence_ids=list(participant.evidence_ids),
        field_evidence_ids=list(participant.field_evidence_ids),
        has_field_evidence=has_field,
        stereo=_analyze_stereo_display(participant.smiles),
    )


def _build_condition_display(
    condition: ReviewConditionItem,
    is_legacy: bool,
) -> _ConditionDisplay:
    """Build display data for one condition."""
    has_field = bool(condition.field_evidence_ids)
    return _ConditionDisplay(
        condition_id=condition.condition_id,
        kind=condition.kind,
        value=condition.value or "",
        numeric_value=condition.numeric_value,
        unit=condition.unit or "",
        stage_index=condition.stage_index,
        extraction_state=condition.extraction_state,
        evidence_ids=list(condition.evidence_ids),
        field_evidence_ids=list(condition.field_evidence_ids),
        has_field_evidence=has_field,
    )


def _build_gold_display(
    reaction_id: str,
    gold_map: dict[str, GoldComparison],
) -> _GoldDisplay:
    """Build gold comparison display for one reaction."""
    gold = gold_map.get(reaction_id)
    if gold is None:
        return _GoldDisplay(
            reaction_id=reaction_id,
            gold_reaction_id=None,
            alignment="",
            alignment_basis=None,
            participant_results=[],
            has_gold=False,
        )
    return _GoldDisplay(
        reaction_id=reaction_id,
        gold_reaction_id=gold.gold_reaction_id,
        alignment=gold.alignment,
        alignment_basis=gold.alignment_basis,
        participant_results=[
            {
                "participant_id": pr.participant_id,
                "gold_participant_id": pr.gold_participant_id,
                "comparison": pr.comparison,
                "incomparable_reason": pr.incomparable_reason,
            }
            for pr in gold.participant_results
        ],
        has_gold=True,
    )


def _resolve_evidence(
    evidence_ids: list[str],
    evidence_map: dict[str, EvidenceRef],
) -> tuple[list[_EvidenceRefDisplay], list[str]]:
    """Resolve evidence IDs to display entries. Returns (resolved, missing_ids)."""
    resolved: list[_EvidenceRefDisplay] = []
    missing: list[str] = []
    for eid in evidence_ids:
        ref = evidence_map.get(eid)
        if ref is None:
            missing.append(eid)
            continue
        resolved.append(_EvidenceRefDisplay(
            evidence_id=ref.evidence_id,
            kind=ref.kind,
            page=ref.page,
            text=ref.text or "",
            asset_path=ref.asset_path,
        ))
    return resolved, missing


def _build_condition_summary(
    record: ReactionRecord,
    ctx: ReviewContext | None,
) -> str:
    """Build a one-line condition summary string.

    Joins catalyst/ligand/reagent values, solvents, temperature, time,
    and yield with `` · ``.  Returns ``""`` when nothing is known.
    """
    parts: list[str] = []

    if ctx is not None:
        cat_parts: list[str] = []
        for cond in ctx.conditions:
            if cond.kind in ("catalyst", "ligand", "reagent") and cond.value:
                cat_parts.append(cond.value)
        if cat_parts:
            parts.append(" · ".join(cat_parts))
    elif record.reagents:
        parts.append(" · ".join(record.reagents))

    if record.solvents:
        parts.append(" · ".join(record.solvents))
    if record.temperature_c is not None:
        parts.append(f"{record.temperature_c:g} °C")
    if record.time:
        parts.append(record.time)
    if record.yield_pct is not None:
        parts.append(f"收率 {record.yield_pct:g}%")

    return " · ".join(parts)


def _build_gold_side(
    smiles: str | None,
    label: str,
    store: ArtifactStore | None,
    highlight: list[int] | None,
) -> dict[str, Any]:
    """Build one side of a gold-pair comparison (images dict + render_status)."""
    _IMG_SIZE = (360, 260)
    _empty = {"svg": None, "png": None}

    if not smiles or not smiles.strip():
        return {
            "label": label,
            "smiles": smiles or "",
            "images": {"normal": dict(_empty), "diff": None},
            "render_status": "missing_smiles",
        }

    if store is None:
        canonical = Validator.canonicalize(smiles)
        status = "ok" if canonical else "invalid_smiles"
        return {
            "label": label,
            "smiles": smiles,
            "images": {"normal": dict(_empty), "diff": None},
            "render_status": status,
        }

    result = render_structure(smiles, size=_IMG_SIZE, mode="normal")
    if result.status != "ok":
        return {
            "label": label,
            "smiles": smiles,
            "images": {"normal": dict(_empty), "diff": None},
            "render_status": result.status,
        }

    basename = asset_basename(smiles, "normal", _IMG_SIZE)
    if basename is None:
        return {
            "label": label,
            "smiles": smiles,
            "images": {"normal": dict(_empty), "diff": None},
            "render_status": "invalid_smiles",
        }

    normal_images = _write_render_assets(store, result, basename)

    diff_images: dict[str, str | None] | None = None
    if highlight:
        hl_result = render_structure(
            smiles, size=_IMG_SIZE, mode="normal", highlight_atoms=highlight
        )
        if hl_result.status == "ok":
            hl_hash = hashlib.sha256(
                json.dumps(sorted(highlight), separators=(",", ":")).encode()
            ).hexdigest()[:8]
            hl_basename = f"{basename}-hl{hl_hash}"
            diff_images = _write_render_assets(store, hl_result, hl_basename)

    return {
        "label": label,
        "smiles": smiles,
        "images": {"normal": normal_images, "diff": diff_images},
        "render_status": "ok",
    }


def _write_render_assets(
    store: ArtifactStore,
    result: Any,
    basename: str,
) -> dict[str, str | None]:
    """Write SVG and PNG for a render result. Returns path dict."""
    svg_rel = f"review_assets/{basename}.svg"
    png_rel = f"review_assets/{basename}.png"
    svg_path = store.root / svg_rel
    if not svg_path.is_file() and result.svg is not None:
        store.write_bytes(svg_rel, result.svg.encode("utf-8"))
    png_path = store.root / png_rel
    if not png_path.is_file() and result.png is not None:
        store.write_bytes(png_rel, result.png)
    return {"svg": svg_rel, "png": png_rel}


def _build_gold_pairs(
    record: ReactionRecord,
    ctx: ReviewContext | None,
    gold_map: dict[str, GoldComparison],
    gold_records: list[ReactionRecord] | None,
    store: ArtifactStore | None,
) -> list[dict[str, Any]]:
    """Build gold-pair comparison entries for one reaction."""
    gold = gold_map.get(record.reaction_id)
    if gold is None:
        return []

    gold_record_map: dict[str, ReactionRecord] = {}
    if gold_records:
        for gr in gold_records:
            gold_record_map[gr.reaction_id] = gr

    participants_raw = ctx.participants if ctx is not None else _synthesize_participants(record)
    # evaluation._part_id identifies compounds by ``label or name``, so the
    # primary lookup uses the same key; the review-scheme participant_id is
    # kept as a fallback for hand-written comparison files.
    participant_lookup: dict[str, ReviewParticipant] = {}
    for p in participants_raw:
        participant_lookup.setdefault(p.label or p.name or "<unlabelled>", p)
        participant_lookup.setdefault(p.participant_id, p)

    pairs: list[dict[str, Any]] = []
    for pr in gold.participant_results:
        extracted_side: dict[str, Any] | None = None
        ext_part = participant_lookup.get(pr.participant_id)
        if pr.comparison != "missing_extracted" and ext_part is not None:
            extracted_side = _build_gold_side(
                ext_part.smiles, ext_part.label or ext_part.name or pr.participant_id, store, None
            )

        gold_side: dict[str, Any] | None = None
        if (
            pr.comparison != "missing_gold"
            and gold_records
            and gold.gold_reaction_id
            and pr.gold_participant_id
        ):
            gold_rec = gold_record_map.get(gold.gold_reaction_id)
            if gold_rec is not None:
                gold_compound = _find_gold_compound(gold_rec, pr.gold_participant_id)
                if gold_compound is not None:
                    gold_side = _build_gold_side(
                        gold_compound.smiles,
                        gold_compound.label or gold_compound.name or pr.gold_participant_id,
                        store,
                        None,
                    )

        highlight = False
        ext_highlights: list[int] | None = None
        gold_highlights: list[int] | None = None
        if (
            extracted_side is not None
            and gold_side is not None
            and extracted_side.get("smiles")
            and gold_side.get("smiles")
            and pr.comparison != "identical"
        ):
            hl = gold_highlight_atoms(extracted_side["smiles"], gold_side["smiles"])
            if hl is not None and (hl[0] or hl[1]):
                ext_highlights = hl[0]
                gold_highlights = hl[1]
                highlight = True
                if ext_part is not None:
                    extracted_side = _build_gold_side(
                        ext_part.smiles,
                        ext_part.label or ext_part.name or pr.participant_id,
                        store,
                        ext_highlights,
                    )
                gold_compound_2 = None
                if gold_records and gold.gold_reaction_id and pr.gold_participant_id:
                    gold_rec_2 = gold_record_map.get(gold.gold_reaction_id)
                    if gold_rec_2 is not None:
                        gold_compound_2 = _find_gold_compound(
                            gold_rec_2, pr.gold_participant_id
                        )
                if gold_compound_2 is not None:
                    gold_side = _build_gold_side(
                        gold_compound_2.smiles,
                        gold_compound_2.label or gold_compound_2.name
                        or pr.gold_participant_id or "",
                        store,
                        gold_highlights,
                    )

        pairs.append({
            "participant_id": pr.participant_id,
            "gold_participant_id": pr.gold_participant_id,
            "comparison": pr.comparison,
            "incomparable_reason": pr.incomparable_reason,
            "extracted": extracted_side,
            "gold": gold_side,
            "highlight": highlight,
        })

    return pairs


def _find_gold_compound(
    gold_record: ReactionRecord, gold_participant_id: str
) -> CompoundRef | None:
    """Find a compound in a gold record by label or name matching _part_id."""
    for compound in gold_record.reactants + gold_record.products:
        if (compound.label or compound.name or "<unlabelled>") == gold_participant_id:
            return compound
    return None


def build_review_view(
    records: list[ReactionRecord],
    *,
    store: ArtifactStore | None = None,
    contexts: list[ReviewContext] | None = None,
    decisions: list[ReviewDecision] | None = None,
    gold_comparisons: list[GoldComparison] | None = None,
    gold_records: list[ReactionRecord] | None = None,
    evidence: list[EvidenceRef] | None = None,
) -> dict[str, Any]:
    """Build the complete review view model as a plain dict.

    Returns a dict with top-level keys: ``reactions``, ``evidence``,
    ``evidence_missing``, ``has_gold``, ``has_contexts``, ``record_count``.
    """
    # Index inputs
    context_map: dict[str, ReviewContext] = {}
    for ctx in (contexts or []):
        context_map[ctx.reaction_id] = ctx

    decision_map: dict[str, list[ReviewDecision]] = {}
    for dec in (decisions or []):
        decision_map.setdefault(dec.reaction_id, []).append(dec)

    gold_map: dict[str, GoldComparison] = {}
    for gc in (gold_comparisons or []):
        gold_map[gc.reaction_id] = gc

    evidence_map: dict[str, EvidenceRef] = {}
    for ref in (evidence or []):
        evidence_map[ref.evidence_id] = ref

    # Per-record hash map for stale-image detection
    hash_map: dict[str, str] = {}
    for rec in records:
        hash_map[rec.reaction_id] = _record_hash(rec)

    reactions: list[dict[str, Any]] = []
    all_evidence_ids: set[str] = set()
    missing_evidence_ids: set[str] = set()

    for position, record in enumerate(records):
        ctx = context_map.get(record.reaction_id)
        is_legacy = ctx is None

        # Participants
        if ctx is not None:
            participants_raw = ctx.participants
            context_note = ctx.context_schema_note
        else:
            participants_raw = _synthesize_participants(record)
            context_note = "legacy run: reaction-level reference only"

        participants: list[dict[str, Any]] = []
        for p in participants_raw:
            display = _build_participant_display(
                p, record.reaction_id, record, evidence_map, store, is_legacy
            )
            stale = False
            if ctx is not None and ctx.record_hash != hash_map.get(record.reaction_id):
                stale = True
            participants.append({
                "participant_id": display.participant_id,
                "role": display.role,
                "label": display.label,
                "name": display.name,
                "smiles": display.smiles,
                "structure_state": display.structure_state,
                "images": display.images,
                "render_status": display.render_status,
                "stale_image": stale or display.stale_image,
                "machine_issues": display.machine_issues,
                "evidence_ids": display.evidence_ids,
                "field_evidence_ids": display.field_evidence_ids,
                "has_field_evidence": display.has_field_evidence,
                "field_path": _participant_field_path(p, record),
                "stereo": {
                    "rdkit_available": display.stereo.rdkit_available,
                    "centers": display.stereo.centers,
                    "bonds": display.stereo.bonds,
                    "analysis_note": display.stereo.analysis_note,
                },
            })

        # Conditions
        if ctx is not None:
            conditions_raw = ctx.conditions
        else:
            conditions_raw = _synthesize_conditions(record)

        conditions: list[dict[str, Any]] = []
        for cond in conditions_raw:
            cdisplay = _build_condition_display(cond, is_legacy)
            conditions.append({
                "condition_id": cdisplay.condition_id,
                "kind": cdisplay.kind,
                "value": cdisplay.value,
                "numeric_value": cdisplay.numeric_value,
                "unit": cdisplay.unit,
                "stage_index": cdisplay.stage_index,
                "extraction_state": cdisplay.extraction_state,
                "evidence_ids": cdisplay.evidence_ids,
                "field_evidence_ids": cdisplay.field_evidence_ids,
                "has_field_evidence": cdisplay.has_field_evidence,
                "field_path": _condition_field_path(cond),
            })

        # Human status
        rec_decisions = decision_map.get(record.reaction_id, [])
        human_status = aggregate_human_status(
            record.reaction_id, rec_decisions, gold_map
        )

        # Machine status
        has_errors = any(i.severity == "error" for i in record.issues)
        machine_status = "needs_review" if has_errors else "clean"

        # Gold
        gold = _build_gold_display(record.reaction_id, gold_map)

        # Evidence
        reaction_evidence_ids = list(record.evidence_ids)
        if ctx is not None:
            reaction_evidence_ids = list(ctx.reaction_evidence_ids)
        all_evidence_ids.update(reaction_evidence_ids)
        for p in participants_raw:
            all_evidence_ids.update(p.evidence_ids)

        reactions.append({
            "reaction_id": record.reaction_id,
            "record_hash": hash_map[record.reaction_id],
            "review_status": record.review_status,
            "confidence": record.confidence,
            "machine_status": machine_status,
            "human_status": human_status,
            "position": position,
            "participants": participants,
            "structure_participants": [
                p for p in participants if p["role"] in ("reactant", "product")
            ],
            "conditions": conditions,
            "reactants_text": ", ".join(
                c.label or c.name or "?" for c in record.reactants
            ),
            "products_text": ", ".join(
                c.label or c.name or "?" for c in record.products
            ),
            "reagents": list(record.reagents),
            "solvents": list(record.solvents),
            "temperature_c": record.temperature_c,
            "time": record.time or "",
            "yield_pct": record.yield_pct,
            "evidence_ids": reaction_evidence_ids,
            "issues": [
                {
                    "code": i.code,
                    "severity": i.severity,
                    "target_id": i.target_id,
                    "message": i.message,
                    "suggested_value": i.suggested_value,
                }
                for i in record.issues
            ],
            "gold": {
                "reaction_id": gold.reaction_id,
                "gold_reaction_id": gold.gold_reaction_id,
                "alignment": gold.alignment,
                "alignment_basis": gold.alignment_basis,
                "participant_results": gold.participant_results,
                "has_gold": gold.has_gold,
            },
            "condition_summary": _build_condition_summary(record, ctx),
            "gold_pairs": _build_gold_pairs(
                record, ctx, gold_map, gold_records, store
            ),
            "context_note": context_note,
        })

    # Resolve all evidence
    resolved_evidence, missing_ids = _resolve_evidence(
        sorted(all_evidence_ids), evidence_map
    )
    missing_evidence_ids.update(missing_ids)

    return {
        "reactions": reactions,
        "evidence": [
            {
                "evidence_id": e.evidence_id,
                "kind": e.kind,
                "page": e.page,
                "text": e.text,
                "asset_path": e.asset_path,
            }
            for e in resolved_evidence
        ],
        "evidence_missing": sorted(missing_evidence_ids),
        "has_gold": bool(gold_map),
        "has_contexts": bool(context_map),
        "record_count": len(records),
    }


# ---------------------------------------------------------------------------
# Human-state aggregation
# ---------------------------------------------------------------------------

# Required target kinds per reaction (when no gold)
_REQUIRED_BASE: set[str] = {
    "reaction",
    "completeness",
    "condition",
    "participant_identity",
    "participant_structure",
    "stereo",
}


def _build_required_targets(
    reaction_id: str,
    decisions: list[ReviewDecision],
    gold_map: dict[str, GoldComparison] | None,
) -> set[tuple[str, str]]:
    """Build the set of (target_kind, target_id) pairs that must be resolved."""
    required: set[tuple[str, str]] = set()
    # Reaction-level
    required.add(("reaction", reaction_id))
    required.add(("completeness", reaction_id))

    # Conditions and participants from decisions
    for dec in decisions:
        if dec.target_kind == "condition":
            required.add(("condition", dec.target_id))
        elif dec.target_kind in ("participant_identity", "participant_structure"):
            required.add((dec.target_kind, dec.target_id))
        elif dec.target_kind == "stereo":
            required.add(("stereo", dec.target_id))

    # Gold dimension only when gold provided
    if gold_map and reaction_id in gold_map:
        required.add(("gold_alignment", reaction_id))

    return required


def aggregate_human_status(
    reaction_id: str,
    decisions: list[ReviewDecision],
    gold_map: dict[str, GoldComparison] | None = None,
) -> HumanReviewStatus:
    """Aggregate human review status for a reaction.

    Returns one of: unreviewed, in_review, confirmed, pending, rejected.

    Args:
        reaction_id: The reaction to aggregate status for.
        decisions: All ReviewDecision entries for this reaction.
        gold_map: Optional gold comparisons keyed by reaction_id.

    Returns:
        The aggregated HumanReviewStatus.
    """
    if not decisions:
        return "unreviewed"

    # Check for explicit reject
    for dec in decisions:
        if dec.conclusion == "rejected":
            return "rejected"

    required = _build_required_targets(reaction_id, decisions, gold_map)
    if not required:
        return "unreviewed"

    # Index decisions by (target_kind, target_id)
    by_target: dict[tuple[str, str], list[ReviewDecision]] = {}
    for dec in decisions:
        key = (dec.target_kind, dec.target_id)
        by_target.setdefault(key, []).append(dec)

    has_pending = False
    confirmed_count = 0

    for target in required:
        target_decisions = by_target.get(target, [])
        if not target_decisions:
            continue
        # Get latest decision
        latest = target_decisions[-1]
        if latest.conclusion == "confirmed":
            confirmed_count += 1
        elif latest.conclusion == "not_applicable":
            confirmed_count += 1
        elif latest.conclusion == "pending":
            has_pending = True
        elif latest.conclusion == "insufficient_evidence":
            has_pending = True
        elif latest.conclusion == "revised":
            confirmed_count += 1

    if has_pending:
        return "pending"

    if confirmed_count >= len(required):
        return "confirmed"

    # Some decisions but not all confirmed → in_review
    return "in_review"


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def invalidate_stale_decisions(
    decisions: list[ReviewDecision],
    operations: list[ReviewOperation],
    record_hashes: dict[str, str],
) -> set[str]:
    """Determine which decision_ids are invalidated by operations or hash changes.

    Conservative approach: if we cannot determine affected targets, invalidate
    all decisions for that reaction except explicit reject.

    Reject is sticky: machine revalidation MUST NOT flip a human-rejected record
    to accepted.

    Args:
        decisions: All current ReviewDecision entries.
        operations: The ReviewOperation entries being applied.
        record_hashes: Current record hashes keyed by reaction_id.

    Returns:
        Set of invalidated decision_ids.
    """
    invalidated: set[str] = set()
    if not decisions:
        return invalidated

    # Group decisions by reaction
    dec_by_reaction: dict[str, list[ReviewDecision]] = {}
    for dec in decisions:
        dec_by_reaction.setdefault(dec.reaction_id, []).append(dec)

    # Group operations by reaction
    ops_by_reaction: dict[str, list[ReviewOperation]] = {}
    for op in operations:
        ops_by_reaction.setdefault(op.reaction_id, []).append(op)

    # Process each reaction
    all_reactions = set(dec_by_reaction) | set(ops_by_reaction)
    for rid in all_reactions:
        rec_decisions = dec_by_reaction.get(rid, [])
        rec_ops = ops_by_reaction.get(rid, [])
        current_hash = record_hashes.get(rid)

        # Determine affected target kinds from operations
        affected_kinds: set[str] = set()
        for op in rec_ops:
            if op.op == "set_value" and op.target_kind == "participant_structure":
                # SMILES change invalidates structure + stereo for that participant
                affected_kinds.add("participant_structure")
                affected_kinds.add("stereo")
            elif op.op in ("add_participant", "remove_participant", "change_role"):
                affected_kinds.add("completeness")
            elif op.op == "set_value":
                affected_kinds.add(op.target_kind)

        # Check record hash mismatch
        hash_changed = False
        if rec_decisions and current_hash:
            # Any decision with a different record hash means record changed
            for dec in rec_decisions:
                if dec.record_hash != current_hash:
                    hash_changed = True
                    break

        # Invalidate decisions
        for dec in rec_decisions:
            if dec.conclusion == "rejected":
                # Reject is sticky — never invalidate
                continue
            if hash_changed:
                # Conservative: invalidate all except reject
                invalidated.add(dec.decision_id)
                continue
            if dec.target_kind in affected_kinds:
                invalidated.add(dec.decision_id)

    return invalidated


# ---------------------------------------------------------------------------
# Apply — submission
# ---------------------------------------------------------------------------

# Extended allowed roots for submission ops (includes review_status + participant)
_SUBMISSION_ALLOWED_ROOTS: set[str] = {
    "reactants",
    "products",
    "reagents",
    "solvents",
    "temperature_c",
    "time",
    "yield_pct",
    "review_status",
}


def _check_submission_duplicate(
    store: ArtifactStore | None,
    submission_id: str,
) -> None:
    """Reject a submission_id already persisted. Tolerates missing dir."""
    if store is None:
        return
    path = store.root / "review_submissions" / f"{submission_id}.json"
    if path.is_file():
        raise ValueError(f"Duplicate submission_id: {submission_id}")


def _persist_submission(
    store: ArtifactStore | None,
    submission: ReviewSubmission,
) -> None:
    """Persist submission package under review_submissions/."""
    if store is None:
        return
    safe_id = _sanitize_filename(submission.submission_id)
    path = f"review_submissions/{safe_id}.json"
    store.write_json(path, submission.model_dump(mode="json"))


def _apply_operation(
    data: dict[str, Any],
    op: ReviewOperation,
    record: ReactionRecord,
) -> tuple[dict[str, Any], list[ValidationIssue]]:
    """Apply one operation to the record data.

    Returns (modified_data, new_issues).
    """
    new_issues: list[ValidationIssue] = []

    if op.op == "set_value":
        if op.path is None:
            raise ValueError("set_value requires a path")
        _set_path(data, op.path, op.new_value)
    elif op.op == "confirm":
        pass  # Confirmation only — no data change
    elif op.op == "mark_pending":
        pass  # Status tracked via ReviewDecision
    elif op.op == "mark_not_applicable":
        pass  # Status tracked via ReviewDecision
    elif op.op == "reject":
        pass  # Status tracked via ReviewDecision
    elif op.op == "add_participant":
        # Add participant to reactants or products based on target_kind
        if op.new_value and isinstance(op.new_value, dict):
            role = op.new_value.get("role", "reactant")
            compound = CompoundRef(**op.new_value)
            if role == "reactant":
                data.setdefault("reactants", []).append(
                    compound.model_dump(mode="json")
                )
            elif role == "product":
                data.setdefault("products", []).append(
                    compound.model_dump(mode="json")
                )
    elif op.op == "remove_participant":
        # Remove participant by label
        if op.target_id:
            for field_name in ("reactants", "products"):
                compounds = data.get(field_name, [])
                data[field_name] = [
                    c for c in compounds
                    if c.get("label") != op.target_id
                ]
    elif op.op == "change_role":
        pass  # Role change tracked via ReviewDecision

    return data, new_issues


def _create_decision(
    op: ReviewOperation,
    record: ReactionRecord,
    reviewer: str,
    record_hash: str,
) -> ReviewDecision | None:
    """Create a ReviewDecision from an operation, or None for data-only ops."""
    conclusion_map: dict[str, str] = {
        "confirm": "confirmed",
        "mark_pending": "pending",
        "mark_not_applicable": "not_applicable",
        "reject": "rejected",
    }
    conclusion = conclusion_map.get(op.op)
    if conclusion is None:
        return None

    return ReviewDecision(
        decision_id=f"{op.reaction_id}:{op.target_kind}:{op.target_id}:{op.op}",
        reaction_id=op.reaction_id,
        target_kind=op.target_kind,
        target_id=op.target_id,
        conclusion=conclusion,  # type: ignore[arg-type]
        reason=op.reason,
        reviewer=reviewer,
        record_hash=record_hash,
        created_at=utc_now(),
    )


def apply_submission(
    records: list[ReactionRecord],
    submission_path: Path,
    *,
    confirmed_by: str,
    store: ArtifactStore | None = None,
) -> tuple[list[ReactionRecord], list[dict[str, Any]]]:
    """Apply an offline ReviewSubmission package.

    Args:
        records: Current ReactionRecord list.
        submission_path: Path to the ReviewSubmission JSON file.
        confirmed_by: Reviewer identity (must match submission.reviewer).
        store: Optional ArtifactStore for dedup and persistence.

    Returns:
        Tuple of (corrected_records, audit_entries).

    Raises:
        ValueError: On validation failures (mismatch, duplicate, conflict).
    """
    confirmed_by = confirmed_by.strip()
    if not confirmed_by:
        raise ValueError("confirmed_by must be a non-empty string")

    raw = json.loads(submission_path.read_text(encoding="utf-8"))
    submission = ReviewSubmission.model_validate(raw)

    if submission.reviewer != confirmed_by:
        raise ValueError(
            f"Submission reviewer {submission.reviewer!r} "
            f"does not match confirmed_by {confirmed_by!r}"
        )

    # Dedup
    _check_submission_duplicate(store, submission.submission_id)

    # Build record index
    record_map: dict[str, ReactionRecord] = {}
    for rec in records:
        record_map[rec.reaction_id] = rec

    # Version conflict check
    conflicting: list[str] = []
    for rid, expected_hash in submission.base_record_hashes.items():
        rec = record_map.get(rid)
        if rec is None:
            conflicting.append(rid)
        elif _record_hash(rec) != expected_hash:
            conflicting.append(rid)
    if conflicting:
        raise ValueError(
            f"Version conflict for reactions: {sorted(conflicting)}"
        )

    # Build hash map for invalidation
    hash_map: dict[str, str] = {}
    for rec in records:
        hash_map[rec.reaction_id] = _record_hash(rec)

    # Read existing decisions defensively
    existing_decisions = _read_jsonl_defensive(
        store, "review_decisions.jsonl", ReviewDecision
    )

    # Invalidate stale decisions
    invalidated = invalidate_stale_decisions(
        existing_decisions, submission.operations, hash_map
    )

    # Group ops by reaction
    ops_by_reaction: dict[str, list[ReviewOperation]] = {}
    for op in submission.operations:
        ops_by_reaction.setdefault(op.reaction_id, []).append(op)

    result: list[ReactionRecord] = []
    audit_entries: list[dict[str, Any]] = []
    new_decisions: list[ReviewDecision] = []
    paths_by_reaction: dict[str, list[str]] = {}

    for record in records:
        rec_ops = ops_by_reaction.get(record.reaction_id)
        if not rec_ops:
            result.append(record)
            continue

        data = record.model_dump(mode="json")
        record_hash = _record_hash(record)
        all_paths: list[str] = []
        has_confirm_op = False

        for op in rec_ops:
            # Target legality check
            if op.op == "set_value" and op.path:
                root = op.path.split(".")[0]
                if root not in _SUBMISSION_ALLOWED_ROOTS:
                    raise ValueError(f"Operation path not editable: {op.path}")

            data, _ = _apply_operation(data, op, record)
            if op.path:
                all_paths.append(op.path)

            # Track decisions
            decision = _create_decision(op, record, confirmed_by, record_hash)
            if decision is not None:
                new_decisions.append(decision)
                if op.op == "confirm":
                    has_confirm_op = True

        # Revalidate
        corrected_record, issues_added = _revalidate_record(
            ReactionRecord.model_validate(data)
        )

        # Review status logic for submission path:
        # - accepted only when no error issues AND explicit confirm op
        # - rejected is sticky
        if corrected_record.review_status != "rejected":
            has_errors = any(
                i.severity == "error" for i in corrected_record.issues
            )
            if has_errors:
                corrected_record = corrected_record.model_copy(
                    update={"review_status": "needs_review"}
                )
            elif has_confirm_op:
                corrected_record = corrected_record.model_copy(
                    update={"review_status": "accepted"}
                )
            # Otherwise keep existing status

        result.append(corrected_record)
        paths_by_reaction[record.reaction_id] = all_paths
        audit_entries.append({
            "reaction_id": record.reaction_id,
            "confirmed_by": confirmed_by,
            "paths": all_paths,
            "pre_status": record.review_status,
            "post_status": corrected_record.review_status,
            "issues_added": issues_added,
            "confirmed_at": utc_now(),
            "submission_id": submission.submission_id,
            "operations_count": len(rec_ops),
            "invalidated_decisions": len(invalidated),
        })

    # Persist
    _persist_submission(store, submission)
    if store is not None and new_decisions:
        store.append_jsonl("review_decisions.jsonl", new_decisions)

    return result, audit_entries


# ---------------------------------------------------------------------------
# Apply — legacy corrections (byte-compatible with v1)
# ---------------------------------------------------------------------------


def apply_corrections(
    records: list[ReactionRecord],
    corrections_path: Path,
    *,
    confirmed_by: str,
) -> tuple[list[ReactionRecord], list[dict[str, Any]]]:
    """Apply explicit JSON corrections, revalidate records, and emit audit entries."""
    confirmed_by = confirmed_by.strip()
    if not confirmed_by:
        raise ValueError("confirmed_by must be a non-empty string")

    corrections = json.loads(corrections_path.read_text(encoding="utf-8"))
    if not isinstance(corrections, list):
        raise ValueError("Corrections file must contain a JSON array")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in corrections:
        if not isinstance(item, dict) or not item.get("reaction_id") or not item.get("path"):
            raise ValueError("Each correction requires reaction_id, path, and value")
        grouped.setdefault(str(item["reaction_id"]), []).append(item)

    result: list[ReactionRecord] = []
    audit_entries: list[dict[str, Any]] = []
    known = {record.reaction_id for record in records}
    unknown = set(grouped) - known
    if unknown:
        raise ValueError(f"Corrections reference unknown reactions: {sorted(unknown)}")

    for record in records:
        record_corrections = grouped.get(record.reaction_id)
        if not record_corrections:
            result.append(record)
            continue

        data = record.model_dump(mode="json")
        paths: list[str] = []
        for correction in record_corrections:
            _set_path(data, str(correction["path"]), correction.get("value"))
            paths.append(str(correction["path"]))

        corrected_record, issues_added = _revalidate_record(
            ReactionRecord.model_validate(data)
        )
        result.append(corrected_record)
        audit_entries.append(
            {
                "reaction_id": record.reaction_id,
                "confirmed_by": confirmed_by,
                "paths": paths,
                "pre_status": record.review_status,
                "post_status": corrected_record.review_status,
                "issues_added": issues_added,
                "confirmed_at": utc_now(),
            }
        )
    return result, audit_entries


# ---------------------------------------------------------------------------
# Revalidation (shared by both apply paths)
# ---------------------------------------------------------------------------


def _revalidate_record(
    record: ReactionRecord,
) -> tuple[ReactionRecord, int]:
    """Re-canonicalize SMILES and rebuild issues. Returns (record, issues_added)."""
    data = record.model_dump(mode="json")
    issues = [issue for issue in record.issues if issue.code != "R001_INVALID_SMILES"]
    issues_added = 0

    for field_name in ("reactants", "products"):
        compounds = data.get(field_name, [])
        for compound in compounds:
            smiles = compound.get("smiles")
            if not smiles:
                continue
            canonical = Validator.canonicalize(smiles)
            if canonical:
                compound["smiles"] = canonical
                continue

            target_id = compound.get("label") or record.reaction_id
            issue = ValidationIssue(
                code="R001_INVALID_SMILES",
                severity="error",
                target_id=target_id,
                message=f"Invalid SMILES: {smiles}",
            )
            if _has_issue(issues, issue):
                continue
            issues.append(issue)
            issues_added += 1

    data["issues"] = [issue.model_dump(mode="json") for issue in issues]
    data["review_status"] = (
        "needs_review"
        if any(issue.severity == "error" for issue in issues)
        else "accepted"
    )
    return ReactionRecord.model_validate(data), issues_added


def _has_issue(issues: list[ValidationIssue], candidate: ValidationIssue) -> bool:
    candidate_data = candidate.model_dump(mode="json")
    return any(issue.model_dump(mode="json") == candidate_data for issue in issues)


# ---------------------------------------------------------------------------
# Dot-path setter (shared by both apply paths)
# ---------------------------------------------------------------------------


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    """Set a dot-separated path in a nested dict/list structure."""
    allowed_roots = {
        "reactants",
        "products",
        "reagents",
        "solvents",
        "temperature_c",
        "time",
        "yield_pct",
        "review_status",
    }
    parts = path.split(".")
    if not parts or parts[0] not in allowed_roots:
        raise ValueError(f"Correction path is not editable: {path}")
    current: Any = data
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise ValueError(f"Invalid correction path: {path}")
    final = parts[-1]
    if isinstance(current, list):
        current[int(final)] = value
    elif isinstance(current, dict):
        current[final] = value
    else:
        raise ValueError(f"Invalid correction path: {path}")


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def generate_review(
    records: list[ReactionRecord],
    store: ArtifactStore,
    *,
    contexts: list[ReviewContext] | None = None,
    decisions: list[ReviewDecision] | None = None,
    gold_comparisons: list[GoldComparison] | None = None,
    gold_records: list[ReactionRecord] | None = None,
    evidence: list[EvidenceRef] | None = None,
) -> Path:
    """Render the only supported review dashboard into the run directory.

    Args:
        records: ReactionRecord list to display.
        store: ArtifactStore for writing review.html and assets.
        contexts: Optional ReviewContext list.
        decisions: Optional ReviewDecision list.
        gold_comparisons: Optional GoldComparison list.
        gold_records: Optional gold ReactionRecord list for image pairs.
        evidence: Optional EvidenceRef list.

    Returns:
        Path to the generated review.html.
    """
    view_data = build_review_view(
        records,
        store=store,
        contexts=contexts,
        decisions=decisions,
        gold_comparisons=gold_comparisons,
        gold_records=gold_records,
        evidence=evidence,
    )

    template_path = resources.files("chemex_lit.resources").joinpath(
        "templates", "review.html.j2"
    )
    env = Environment(
        autoescape=select_autoescape(["html", "xml"]),
        loader=None,
    )
    template = env.from_string(
        template_path.read_text(encoding="utf-8")
    )
    return store.write_raw("review.html", template.render(view=view_data))
