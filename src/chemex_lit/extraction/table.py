"""Reaction extraction from markdown tables and structured table evidence."""

from __future__ import annotations

import re

from chemex_lit.config import ModelSpec
from chemex_lit.extraction import reaction_candidates_from_payload
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import DocumentBundle, ReactionCandidate


class TableExtractor:
    def __init__(self, llm: LLMClient, prompts: PromptRegistry, model: ModelSpec) -> None:
        self.llm = llm
        self.prompts = prompts
        self.model = model

    def extract(self, document: DocumentBundle) -> list[ReactionCandidate]:
        inputs: list[tuple[str, str, list[str]]] = []
        for evidence in document.evidence:
            if evidence.kind == "table" and evidence.text:
                context = f"page {evidence.page}" if evidence.page is not None else "table"
                inputs.append((evidence.text, context, [evidence.evidence_id]))

        if not inputs:
            inputs = [
                (table, "markdown table", [])
                for table in _markdown_tables(document.markdown)
            ]

        result: list[ReactionCandidate] = []
        for content, context, evidence_ids in inputs:
            prompt = self.prompts.render(
                "table",
                {"<<PAGE_CONTEXT>>": context, "<<CONTENT>>": content},
            )
            payload = self.llm.complete(self.model, prompt)
            result.extend(reaction_candidates_from_payload(payload, "table", evidence_ids))
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
