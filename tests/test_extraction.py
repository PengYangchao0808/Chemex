from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from chemex_lit.config import load_config
from chemex_lit.extraction import (
    load_external_structures,
    reaction_candidates_from_payload,
    structure_candidates_from_payload,
)
from chemex_lit.extraction.table import TableExtractor
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import EvidenceRef, ExtractionTask, TaskAssets, TaskImageAsset
from chemex_lit.pipeline import _markdown_tables


class RecordingLLM:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, list[Path]]] = []

    def complete(self, model: Any, prompt: str, images: list[Path] | tuple[Path, ...] = ()) -> Any:
        del model
        self.calls.append((prompt, list(images)))
        return self.payload


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


def test_table_extractor_fulfill_passes_existing_image_assets(tmp_path: Path) -> None:
    image_path = tmp_path / "table.png"
    image_path.write_bytes(b"image")
    llm = RecordingLLM({"reactions": [{"reactants": ["7"], "products": ["8"]}]})
    task = ExtractionTask(
        task_id="tt-1",
        kind="table",
        instruction_version="1",
        instructions="read the table",
        output_schema_version="ReactionCandidate@1",
        evidence_ids=["table-1"],
        assets=TaskAssets(images=[TaskImageAsset(path=str(image_path), evidence_id="table-1")]),
    )

    rows = TableExtractor(
        cast(LLMClient, cast(object, llm)),
        PromptRegistry(),
        load_config().models.vision,
    ).fulfill(task)

    assert len(rows) == 1
    assert llm.calls == [("read the table", [image_path])]


def test_table_extractor_fulfill_omits_images_when_assets_are_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.png"
    llm = RecordingLLM({"reactions": [{"reactants": ["7"], "products": ["8"]}]})
    task = ExtractionTask(
        task_id="tt-2",
        kind="table",
        instruction_version="1",
        instructions="read the table",
        output_schema_version="ReactionCandidate@1",
        evidence_ids=["table-2"],
        assets=TaskAssets(
            text="| 7 | 8 |",
            images=[TaskImageAsset(path=str(missing_path), evidence_id="table-2")],
        ),
    )

    rows = TableExtractor(
        cast(LLMClient, cast(object, llm)),
        PromptRegistry(),
        load_config().models.vision,
    ).fulfill(task)

    assert len(rows) == 1
    assert llm.calls == [("read the table", [])]
