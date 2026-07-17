"""The single production pipeline."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from chemex_lit import __version__
from chemex_lit.adjudicator import Adjudicator
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import ValidationOutcome, Validator
from chemex_lit.config import AppConfig, config_fingerprint
from chemex_lit.extraction import load_external_structures
from chemex_lit.models import (
    DocumentBundle,
    ReactionCandidate,
    ReactionRecord,
    RunRequest,
    RunSummary,
    StructureCandidate,
    ValidationIssue,
)
from chemex_lit.review import generate_review
from chemex_lit.store import ArtifactStore, sha256_file, sha256_text


class Pipeline:
    """Coordinate explicit stages; all chemistry remains in injected components."""

    def __init__(
        self,
        *,
        config: AppConfig,
        prompts: Any,
        mineru: Any,
        text_extractor: Any,
        table_extractor: Any,
        structure_extractor: Any,
        validator: Validator,
        assembler: Assembler,
        adjudicator: Adjudicator | None = None,
    ) -> None:
        self.config = config
        self.prompts = prompts
        self.mineru = mineru
        self.text_extractor = text_extractor
        self.table_extractor = table_extractor
        self.structure_extractor = structure_extractor
        self.validator = validator
        self.assembler = assembler
        self.adjudicator = adjudicator

    def run(self, request: RunRequest) -> RunSummary:
        store = ArtifactStore(request.output_dir)
        existing = store.manifest() if store.manifest_path.exists() else None
        if request.pdf_path.is_file():
            input_hash = sha256_file(request.pdf_path)
        elif request.resume and existing:
            input_hash = str(existing["input_sha256"])
        else:
            raise FileNotFoundError(f"PDF not found: {request.pdf_path}")

        run_id = str(existing["run_id"]) if existing else f"{request.pdf_path.stem}-{input_hash[:8]}"
        store.initialise(
            run_id=run_id,
            input_path=request.pdf_path,
            input_sha256=input_hash,
            config_sha256=config_fingerprint(self.config),
            version=__version__,
            prompt_versions=self.prompts.versions,
            models={
                "text": self.config.models.text.model,
                "vision": self.config.models.vision.model,
            },
        )

        try:
            document = self._document_stage(store, request, input_hash)
            reactions, structures = self._extraction_stage(store, request, document)
            validation = self._validation_stage(store, reactions, structures)
            records = self._assembly_stage(store, document, reactions, structures, validation)
            generate_review(records, store.root / "review.html")
            status = self._status(records)
            store.finish(status)
            review_count = sum(item.review_status == "needs_review" for item in records)
            return RunSummary(
                run_id=run_id,
                status=status,
                records_count=len(records),
                review_count=review_count,
                output_dir=str(store.root),
                stages={
                    name: str(data.get("status", "unknown"))
                    for name, data in store.manifest().get("stages", {}).items()
                },
            )
        except Exception:
            store.finish("failed")
            raise

    def _document_stage(
        self,
        store: ArtifactStore,
        request: RunRequest,
        input_hash: str,
    ) -> DocumentBundle:
        output = "document.json"
        if store.stage_complete("document", input_hash, output):
            return DocumentBundle.model_validate(store.read_json(output))
        document = self.mineru.convert(request.pdf_path, store.root / "raw")
        store.write_json(output, document.model_dump(mode="json"))
        store.write_jsonl("evidence.jsonl", document.evidence)
        store.mark_stage("document", status="complete", input_hash=input_hash, output=output)
        return document

    def _extraction_stage(
        self,
        store: ArtifactStore,
        request: RunRequest,
        document: DocumentBundle,
    ) -> tuple[list[ReactionCandidate], list[StructureCandidate]]:
        external_hash = ""
        if request.external_structures:
            external_hash = sha256_file(request.external_structures)
        input_hash = sha256_text(
            json.dumps(document.model_dump(mode="json"), sort_keys=True)
            + json.dumps(self.prompts.versions, sort_keys=True)
            + external_hash
        )
        reaction_output = "candidates/reactions.jsonl"
        structure_output = "candidates/structures.jsonl"
        cached = store.stage_complete("extraction", input_hash, reaction_output)
        if cached and (store.root / structure_output).is_file():
            return (
                store.read_models(reaction_output, ReactionCandidate),
                store.read_models(structure_output, StructureCandidate),
            )

        with ThreadPoolExecutor(max_workers=self.config.pipeline.extraction_workers) as executor:
            text_future = executor.submit(self.text_extractor.extract, document)
            table_future = executor.submit(self.table_extractor.extract, document)
            structure_future = executor.submit(self.structure_extractor.extract, document)
            reactions = text_future.result() + table_future.result()
            structures = structure_future.result()

        if request.external_structures:
            manual = load_external_structures(request.external_structures)
            manual_labels = {
                item.compound_label.strip().lower()
                for item in manual
                if item.compound_label
            }
            structures = [
                item
                for item in structures
                if not item.compound_label or item.compound_label.strip().lower() not in manual_labels
            ] + manual

        reactions = list({item.candidate_id: item for item in reactions}.values())
        structures = list({item.candidate_id: item for item in structures}.values())
        store.write_jsonl(reaction_output, reactions)
        store.write_jsonl(structure_output, structures)
        store.mark_stage(
            "extraction",
            status="complete",
            input_hash=input_hash,
            output=reaction_output,
            detail=f"{len(reactions)} reactions, {len(structures)} structures",
        )
        return reactions, structures

    def _validation_stage(
        self,
        store: ArtifactStore,
        reactions: list[ReactionCandidate],
        structures: list[StructureCandidate],
    ) -> ValidationOutcome:
        input_hash = sha256_text(
            json.dumps(
                [item.model_dump(mode="json") for item in reactions + structures],
                sort_keys=True,
            )
        )
        output = "validation/outcome.json"
        if store.stage_complete("validation", input_hash, output):
            data = store.read_json(output)
            return ValidationOutcome(
                issues=[ValidationIssue.model_validate(item) for item in data["issues"]],
                canonical_smiles=data["canonical_smiles"],
            )
        outcome = self.validator.validate(reactions, structures)
        store.write_json(
            output,
            {
                "issues": [item.model_dump(mode="json") for item in outcome.issues],
                "canonical_smiles": outcome.canonical_smiles,
            },
        )
        store.write_jsonl("validation/issues.jsonl", outcome.issues)
        store.mark_stage(
            "validation",
            status="complete",
            input_hash=input_hash,
            output=output,
            detail=f"{len(outcome.issues)} issues",
        )
        return outcome

    def _assembly_stage(
        self,
        store: ArtifactStore,
        document: DocumentBundle,
        reactions: list[ReactionCandidate],
        structures: list[StructureCandidate],
        validation: ValidationOutcome,
    ) -> list[ReactionRecord]:
        input_hash = sha256_text(
            json.dumps(
                {
                    "reactions": [item.model_dump(mode="json") for item in reactions],
                    "structures": [item.model_dump(mode="json") for item in structures],
                    "issues": [item.model_dump(mode="json") for item in validation.issues],
                    "adjudicate": bool(self.adjudicator),
                },
                sort_keys=True,
            )
        )
        output = "records.jsonl"
        if store.stage_complete("assembly", input_hash, output):
            return store.read_models(output, ReactionRecord)
        records = self.assembler.assemble(reactions, structures, validation)
        if self.adjudicator:
            records = self.adjudicator.adjudicate(records, document)
        store.write_jsonl(output, records)
        store.write_jsonl(
            "review.jsonl",
            [item for item in records if item.review_status == "needs_review"],
        )
        store.mark_stage(
            "assembly",
            status="complete",
            input_hash=input_hash,
            output=output,
            detail=f"{len(records)} records",
        )
        return records

    @staticmethod
    def _status(
        records: list[ReactionRecord],
    ) -> Literal["success", "completed_empty", "partial", "failed"]:
        if not records:
            return "completed_empty"
        if any(item.review_status == "needs_review" for item in records):
            return "partial"
        return "success"
