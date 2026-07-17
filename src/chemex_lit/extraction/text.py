"""Reaction extraction from narrative text."""

from __future__ import annotations

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import reaction_candidates_from_payload
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import DocumentBundle, ReactionCandidate


class TextExtractor:
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

    def extract(self, document: DocumentBundle) -> list[ReactionCandidate]:
        chunks = [item for item in document.evidence if item.kind == "text" and item.text]
        if not chunks and document.markdown.strip():
            return self._extract_chunk(document.markdown[: self.max_chars], "document", [])

        candidates: list[ReactionCandidate] = []
        for item in chunks:
            text = item.text or ""
            for offset in range(0, len(text), self.max_chars):
                page = f"page {item.page}" if item.page is not None else "unknown page"
                candidates.extend(
                    self._extract_chunk(
                        text[offset : offset + self.max_chars],
                        page,
                        [item.evidence_id],
                    )
                )
        return _deduplicate(candidates)

    def _extract_chunk(
        self,
        text: str,
        context: str,
        evidence_ids: list[str],
    ) -> list[ReactionCandidate]:
        if not text.strip():
            return []
        prompt = self.prompts.render(
            "text",
            {"<<PAGE_CONTEXT>>": context, "<<CONTENT>>": text},
        )
        payload = self.llm.complete(self.model, prompt)
        return reaction_candidates_from_payload(payload, "text", evidence_ids)


def _deduplicate(rows: list[ReactionCandidate]) -> list[ReactionCandidate]:
    return list({row.candidate_id: row for row in rows}.values())
