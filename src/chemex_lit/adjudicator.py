"""Optional evidence-bound adjudication for low-confidence records."""

from __future__ import annotations

import json

from chemex_lit.config import ModelSpec
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.models import DocumentBundle, ReactionRecord


class Adjudicator:
    """May accept a warning-only record, but may never rewrite chemistry."""

    def __init__(self, llm: LLMClient, prompts: PromptRegistry, model: ModelSpec) -> None:
        self.llm = llm
        self.prompts = prompts
        self.model = model

    def adjudicate(
        self,
        records: list[ReactionRecord],
        document: DocumentBundle,
    ) -> list[ReactionRecord]:
        evidence = {item.evidence_id: item for item in document.evidence}
        result: list[ReactionRecord] = []
        for record in records:
            if record.review_status != "needs_review" or any(
                issue.severity == "error" for issue in record.issues
            ):
                result.append(record)
                continue
            selected = [evidence[item] for item in record.evidence_ids if item in evidence]
            prompt = self.prompts.render(
                "adjudicate",
                {
                    "<<RECORD>>": json.dumps(record.model_dump(mode="json"), ensure_ascii=False),
                    "<<EVIDENCE>>": json.dumps(
                        [item.model_dump(mode="json") for item in selected],
                        ensure_ascii=False,
                    ),
                },
            )
            decision = self.llm.complete(self.model, prompt)
            if isinstance(decision, dict) and decision.get("accepted") is True:
                record = record.model_copy(update={"review_status": "accepted"})
            result.append(record)
        return result
