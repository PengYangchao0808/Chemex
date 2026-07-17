from __future__ import annotations

import json
from pathlib import Path

from chemex_lit.extraction import (
    load_external_structures,
    reaction_candidates_from_payload,
    structure_candidates_from_payload,
)
from chemex_lit.extraction.table import _markdown_tables
from chemex_lit.models import EvidenceRef


def test_reaction_payload_normalization() -> None:
    rows = reaction_candidates_from_payload(
        {
            "reactions": [
                {
                    "reactants": ["7"],
                    "products": [{"label": "8"}],
                    "reagents": "Pd(OAc)2; PPh3",
                    "solvent": "THF",
                    "temperature": "25 °C",
                    "yield": "83%",
                    "confidence": 0.9,
                }
            ]
        },
        "text",
        ["text-p1"],
    )
    assert len(rows) == 1
    assert rows[0].yield_pct == 83
    assert rows[0].reactants[0].role == "reactant"
    assert rows[0].reagents == ["Pd(OAc)2", "PPh3"]


def test_structure_payload_normalization() -> None:
    rows = structure_candidates_from_payload(
        {"structures": [{"label": "8", "smiles": "CCO", "confidence": 0.8}]},
        ["image-1"],
    )
    assert rows[0].compound_label == "8"
    assert rows[0].smiles == "CCO"


def test_external_structure_short_form(tmp_path: Path) -> None:
    path = tmp_path / "manual.jsonl"
    path.write_text(json.dumps({"label": "8", "smiles": "CCO"}) + "\n", encoding="utf-8")
    assert load_external_structures(path)[0].compound_label == "8"


def test_markdown_table_detection() -> None:
    markdown = "before\n| Entry | Yield |\n|---|---|\n| 1 | 80 |\nafter"
    assert len(_markdown_tables(markdown)) == 1


def test_evidence_ref_without_asset_path_still_parses() -> None:
    evidence = EvidenceRef.model_validate(
        {
            "evidence_id": "layout-0001",
            "kind": "table",
            "source_path": "paper_content_list.json",
            "text": "row",
        }
    )

    assert evidence.asset_path is None
