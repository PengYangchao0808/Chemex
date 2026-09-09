"""Single HTML review surface and field-level correction application."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import Template

from chemex_lit.chemistry.render import render_smiles
from chemex_lit.chemistry.validate import Validator
from chemex_lit.errors import utc_now
from chemex_lit.models import ReactionRecord, ValidationIssue
from chemex_lit.store import ArtifactStore


def generate_review(records: list[ReactionRecord], store: ArtifactStore) -> Path:
    """Render the only supported review dashboard into the run directory."""
    rows: list[dict[str, Any]] = []
    for record in records:
        structures: list[dict[str, str]] = []
        for index, compound in enumerate(record.reactants + record.products):
            image = ""
            if compound.smiles:
                relative = f"review_assets/{record.reaction_id}-{index}.png"
                png = render_smiles(compound.smiles)
                if png is not None:
                    store.write_bytes(relative, png)
                    image = relative
            structures.append(
                {
                    "label": compound.label or compound.name or "unlabelled",
                    "role": compound.role,
                    "smiles": compound.smiles or "",
                    "image": image,
                }
            )
        rows.append({"record": record, "structures": structures})

    template_path = resources.files("chemex_lit.resources").joinpath(
        "templates", "review.html.j2"
    )
    template = Template(template_path.read_text(encoding="utf-8"))
    return store.write_raw("review.html", template.render(rows=rows))


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

        corrected_record, issues_added = _revalidate_record(ReactionRecord.model_validate(data))
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


def _revalidate_record(record: ReactionRecord) -> tuple[ReactionRecord, int]:
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
        "needs_review" if any(issue.severity == "error" for issue in issues) else "accepted"
    )
    return ReactionRecord.model_validate(data), issues_added


def _has_issue(issues: list[ValidationIssue], candidate: ValidationIssue) -> bool:
    candidate_data = candidate.model_dump(mode="json")
    return any(issue.model_dump(mode="json") == candidate_data for issue in issues)


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
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
