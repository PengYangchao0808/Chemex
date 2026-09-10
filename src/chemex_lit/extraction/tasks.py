"""Task builders for extraction and adjudication stages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chemex_lit.models import (
    DocumentBundle,
    ExtractionTask,
    ReactionRecord,
    TaskAssets,
    TaskImageAsset,
)
from chemex_lit.store import sha256_text


_PREFIXES = {
    "text": "tx",
    "table": "tt",
    "structure": "st",
    "adjudication": "ad",
}


def task_id(kind: str, key: str) -> str:
    """Return a stable task identifier for one channel-specific key."""

    prefix = _PREFIXES[kind]
    return f"{prefix}-{sha256_text(f'{kind}|{key}')[:8]}"


def _instruction_version(prompts: Any, name: str) -> str:
    versions = getattr(prompts, "versions", {})
    if isinstance(versions, dict) and name in versions:
        return str(versions[name])
    return ""


def build_text_tasks(
    document: DocumentBundle,
    prompts: Any,
    max_text_chars: int,
) -> list[ExtractionTask]:
    """Build text extraction tasks from inline text evidence."""

    tasks: list[ExtractionTask] = []
    for evidence in document.evidence:
        if evidence.kind != "text" or not evidence.text:
            continue
        content = evidence.text[:max_text_chars]
        page_context = _page_context(evidence.page)
        tasks.append(
            ExtractionTask(
                task_id=task_id("text", evidence.evidence_id),
                kind="text",
                instruction_version=_instruction_version(prompts, "text"),
                instructions=prompts.render(
                    "text",
                    {
                        "<<PAGE_CONTEXT>>": page_context,
                        "<<CONTENT>>": content,
                    },
                ),
                output_schema_version="ReactionCandidate@1",
                evidence_ids=[evidence.evidence_id],
                input_artifacts=["document.json"],
                assets=TaskAssets(text=content),
                status="awaiting",
            )
        )
    return tasks


def build_table_tasks(document: DocumentBundle, prompts: Any) -> list[ExtractionTask]:
    """Build table extraction tasks from structured table evidence."""

    tasks: list[ExtractionTask] = []
    for evidence in document.evidence:
        if evidence.kind != "table" or (not evidence.text and not evidence.asset_path):
            continue
        page_context = _page_context(evidence.page)
        images: list[TaskImageAsset] = []
        if evidence.asset_path:
            images.append(
                TaskImageAsset(
                    path=Path(evidence.asset_path).as_posix(),
                    evidence_id=evidence.evidence_id,
                    context=page_context,
                )
            )
        tasks.append(
            ExtractionTask(
                task_id=task_id("table", evidence.evidence_id),
                kind="table",
                instruction_version=_instruction_version(prompts, "table"),
                instructions=prompts.render(
                    "table",
                    {
                        "<<PAGE_CONTEXT>>": page_context,
                        "<<CONTENT>>": evidence.text or "",
                    },
                ),
                output_schema_version="ReactionCandidate@1",
                evidence_ids=[evidence.evidence_id],
                input_artifacts=["document.json"],
                assets=TaskAssets(text=evidence.text, images=images),
                status="awaiting",
            )
        )
    return tasks


def build_structure_tasks(document: DocumentBundle, prompts: Any) -> list[ExtractionTask]:
    """Build structure extraction tasks for non-table image assets."""

    image_evidence = {
        Path(evidence.source_path).resolve(): evidence
        for evidence in document.evidence
        if evidence.kind == "image"
    }
    table_assets = {
        Path(evidence.asset_path).resolve()
        for evidence in document.evidence
        if evidence.kind == "table" and evidence.asset_path is not None
    }
    tasks: list[ExtractionTask] = []
    for raw_path in document.images:
        path = Path(raw_path)
        if not path.is_file() or path.resolve() in table_assets:
            continue
        evidence = image_evidence.get(path.resolve())
        evidence_id = evidence.evidence_id if evidence is not None else None
        page = evidence.page if evidence is not None else None
        context = f"{path.name}; page {page}" if page is not None else path.name
        tasks.append(
            ExtractionTask(
                task_id=task_id("structure", str(path.resolve())),
                kind="structure",
                instruction_version=_instruction_version(prompts, "structure"),
                instructions=prompts.render(
                    "structure",
                    {"<<IMAGE_CONTEXT>>": context},
                ),
                output_schema_version="StructureCandidate@1",
                evidence_ids=[evidence_id] if evidence_id else [],
                input_artifacts=["document.json"],
                assets=TaskAssets(
                    images=[
                        TaskImageAsset(
                            path=path.resolve().as_posix(),
                            evidence_id=evidence_id,
                            context=context,
                        )
                    ]
                ),
                status="awaiting",
            )
        )
    return tasks


def build_adjudication_tasks(
    records: list[ReactionRecord],
    document: DocumentBundle,
    prompts: Any,
) -> list[ExtractionTask]:
    """Build adjudication tasks for warning-only review records."""

    evidence_index = {item.evidence_id: item for item in document.evidence}
    tasks: list[ExtractionTask] = []
    for record in records:
        if record.review_status != "needs_review" or any(
            issue.severity == "error" for issue in record.issues
        ):
            continue
        selected = [
            evidence_index[evidence_id]
            for evidence_id in record.evidence_ids
            if evidence_id in evidence_index
        ]
        record_payload = record.model_dump(mode="json")
        evidence_payload = [item.model_dump(mode="json") for item in selected]
        task_payload = json.dumps(
            {"record": record_payload, "evidence": evidence_payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        tasks.append(
            ExtractionTask(
                task_id=task_id("adjudication", record.reaction_id),
                kind="adjudication",
                instruction_version=_instruction_version(prompts, "adjudicate"),
                instructions=prompts.render(
                    "adjudicate",
                    {
                        "<<RECORD>>": json.dumps(record_payload, ensure_ascii=False),
                        "<<EVIDENCE>>": json.dumps(evidence_payload, ensure_ascii=False),
                    },
                ),
                output_schema_version="AdjudicationDecision@1",
                evidence_ids=list(record.evidence_ids),
                input_artifacts=["assembly/records.jsonl", "document.json"],
                assets=TaskAssets(text=task_payload),
                status="awaiting",
            )
        )
    return tasks


def _page_context(page: int | None) -> str:
    return f"page {page}" if page is not None else "document"
