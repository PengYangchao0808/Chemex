"""Reaction extraction from narrative text."""

from __future__ import annotations

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import reaction_candidates_from_payload
from chemex_lit.extraction.tasks import build_text_tasks, task_id
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import DocumentBundle, ExtractionTask, ReactionCandidate, TaskAssets


class TextExtractor:
    """Fulfill text extraction tasks with the configured text model."""

    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptRegistry,
        model: ModelSpec,
        max_chars: int = 50000,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.model = model
        self.max_chars = max_chars

    def fulfill(self, task: ExtractionTask) -> list[ReactionCandidate]:
        """Fulfill one text task and normalize the LLM payload."""

        payload = self.llm.complete(self.model, task.instructions)
        return reaction_candidates_from_payload(payload, "text", task.evidence_ids)

    def extract(self, document: DocumentBundle) -> list[ReactionCandidate]:
        tasks = build_text_tasks(document, self.prompts, self.max_chars)
        if not tasks and document.markdown.strip():
            content = document.markdown[: self.max_chars]
            tasks = [
                ExtractionTask(
                    task_id=task_id("text", document.document_id),
                    kind="text",
                    instruction_version=str(self.prompts.versions["text"]),
                    instructions=self.prompts.render(
                        "text",
                        {"<<PAGE_CONTEXT>>": "document", "<<CONTENT>>": content},
                    ),
                    output_schema_version="ReactionCandidate@1",
                    input_artifacts=["document.json"],
                    assets=TaskAssets(text=content),
                )
            ]

        candidates: list[ReactionCandidate] = []
        for task in tasks:
            candidates.extend(self.fulfill(task))
        return _deduplicate(candidates)


def _deduplicate(rows: list[ReactionCandidate]) -> list[ReactionCandidate]:
    return list({row.candidate_id: row for row in rows}.values())
