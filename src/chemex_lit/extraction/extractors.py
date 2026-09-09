"""Fulfill-only extractors for the text, table, and structure channels."""

from __future__ import annotations

from pathlib import Path

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import reaction_candidates_from_payload, structure_candidates_from_payload
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import ExtractionTask, ReactionCandidate, StructureCandidate


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
