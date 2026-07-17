"""Reaction extraction from markdown tables and structured table evidence."""

from __future__ import annotations

import re
from pathlib import Path

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import reaction_candidates_from_payload
from chemex_lit.extraction.tasks import build_table_tasks, task_id
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import DocumentBundle, ExtractionTask, ReactionCandidate, TaskAssets


class TableExtractor:
    """Fulfill table extraction tasks with the configured vision model."""

    def __init__(self, llm: LLMClient, prompts: PromptRegistry, model: ModelSpec) -> None:
        self.llm = llm
        self.prompts = prompts
        self.model = model

    def fulfill(self, task: ExtractionTask) -> list[ReactionCandidate]:
        """Fulfill one table task and normalize the LLM payload."""

        image_paths = [Path(image.path) for image in task.assets.images if Path(image.path).is_file()]
        payload = self.llm.complete(self.model, task.instructions, image_paths)
        return reaction_candidates_from_payload(payload, "table", task.evidence_ids)

    def extract(self, document: DocumentBundle) -> list[ReactionCandidate]:
        tasks = build_table_tasks(document, self.prompts)
        if not tasks:
            tasks = [
                ExtractionTask(
                    task_id=task_id("table", f"markdown:{index}"),
                    kind="table",
                    instruction_version=str(self.prompts.versions["table"]),
                    instructions=self.prompts.render(
                        "table",
                        {"<<PAGE_CONTEXT>>": "markdown table", "<<CONTENT>>": table},
                    ),
                    output_schema_version="ReactionCandidate@1",
                    input_artifacts=["document.json"],
                    assets=TaskAssets(text=table),
                )
                for index, table in enumerate(_markdown_tables(document.markdown), 1)
            ]

        result: list[ReactionCandidate] = []
        for task in tasks:
            result.extend(self.fulfill(task))
        return list({row.candidate_id: row for row in result}.values())


def _markdown_tables(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    tables: list[str] = []
    current: list[str] = []
    for line in lines + [""]:
        if line.count("|") >= 2:
            current.append(line)
            continue
        if len(current) >= 2 and any(re.search(r"---+", item) for item in current[:3]):
            tables.append("\n".join(current))
        current = []
    return tables
