"""Reaction extraction from narrative text."""

from __future__ import annotations

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import reaction_candidates_from_payload
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import ExtractionTask, ReactionCandidate


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
