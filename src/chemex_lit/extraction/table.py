"""Reaction extraction from markdown tables and structured table evidence."""

from __future__ import annotations

from pathlib import Path

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import reaction_candidates_from_payload
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import ExtractionTask, ReactionCandidate


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
