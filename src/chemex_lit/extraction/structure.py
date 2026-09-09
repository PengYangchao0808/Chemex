"""Multimodal extraction of labelled chemical structures."""

from __future__ import annotations

from pathlib import Path

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import structure_candidates_from_payload
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import ExtractionTask, StructureCandidate


class StructureExtractor:
    """Fulfill structure extraction tasks with the configured vision model."""

    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptRegistry,
        model: ModelSpec,
        workers: int = 1,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.model = model
        self.workers = workers

    def fulfill(self, task: ExtractionTask) -> list[StructureCandidate]:
        """Fulfill one structure task and normalize the LLM payload."""

        payload = self.llm.complete(
            self.model,
            task.instructions,
            [Path(image.path) for image in task.assets.images],
        )
        return structure_candidates_from_payload(payload, task.evidence_ids)
