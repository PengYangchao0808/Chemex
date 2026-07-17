"""Multimodal extraction of labelled chemical structures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import structure_candidates_from_payload
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import DocumentBundle, StructureCandidate


class StructureExtractor:
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

    def extract(self, document: DocumentBundle) -> list[StructureCandidate]:
        image_evidence = {
            Path(item.source_path).resolve(): item
            for item in document.evidence
            if item.kind == "image"
        }
        images = [Path(path) for path in document.images if Path(path).is_file()]
        if self.workers == 1:
            rows = [self._extract_image(path, image_evidence.get(path.resolve())) for path in images]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                rows = list(
                    executor.map(
                        lambda path: self._extract_image(path, image_evidence.get(path.resolve())),
                        images,
                    )
                )
        flattened = [candidate for group in rows for candidate in group]
        return list({candidate.candidate_id: candidate for candidate in flattened}.values())

    def _extract_image(self, path: Path, evidence: object | None) -> list[StructureCandidate]:
        evidence_id = getattr(evidence, "evidence_id", None)
        page = getattr(evidence, "page", None)
        context = f"{path.name}; page {page}" if page is not None else path.name
        prompt = self.prompts.render("structure", {"<<IMAGE_CONTEXT>>": context})
        payload = self.llm.complete(self.model, prompt, [path])
        return structure_candidates_from_payload(
            payload,
            [evidence_id] if evidence_id else [],
        )
