"""Single HTML review surface and field-level correction application."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import Template

from chemex_lit.chemistry.render import render_smiles
from chemex_lit.models import ReactionRecord


def generate_review(records: list[ReactionRecord], output: Path) -> Path:
    """Render the only supported review dashboard."""
    assets = output.parent / "review_assets"
    assets.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for record in records:
        structures: list[dict[str, str]] = []
        for index, compound in enumerate(record.reactants + record.products):
            image = ""
            if compound.smiles:
                target = assets / f"{record.reaction_id}-{index}.png"
                if render_smiles(compound.smiles, target):
                    image = target.relative_to(output.parent).as_posix()
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
    output.write_text(template.render(rows=rows), encoding="utf-8")
    return output


def apply_corrections(
    records: list[ReactionRecord],
    corrections_path: Path,
) -> list[ReactionRecord]:
    """Apply explicit JSON corrections and revalidate each modified record."""
    corrections = json.loads(corrections_path.read_text(encoding="utf-8"))
    if not isinstance(corrections, list):
        raise ValueError("Corrections file must contain a JSON array")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in corrections:
        if not isinstance(item, dict) or not item.get("reaction_id") or not item.get("path"):
            raise ValueError("Each correction requires reaction_id, path, and value")
        grouped.setdefault(str(item["reaction_id"]), []).append(item)

    result: list[ReactionRecord] = []
    known = {record.reaction_id for record in records}
    unknown = set(grouped) - known
    if unknown:
        raise ValueError(f"Corrections reference unknown reactions: {sorted(unknown)}")

    for record in records:
        data = record.model_dump(mode="json")
        for correction in grouped.get(record.reaction_id, []):
            _set_path(data, str(correction["path"]), correction.get("value"))
        if record.reaction_id in grouped:
            data["review_status"] = "accepted"
        result.append(ReactionRecord.model_validate(data))
    return result


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
