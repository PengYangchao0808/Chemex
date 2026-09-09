"""The single production pipeline."""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import rdkit

from chemex_lit import __version__
from chemex_lit.adjudicator import Adjudicator
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import ValidationOutcome, Validator
from chemex_lit.config import AppConfig, ModelSpec, config_fingerprint
from chemex_lit.errors import ArtifactError, ChemExError, utc_now
from chemex_lit.extraction import (
    load_external_structures,
    reaction_candidates_from_payload,
    stable_id,
    structure_candidates_from_payload,
)
from chemex_lit.extraction.tasks import (
    build_adjudication_tasks,
    build_structure_tasks,
    build_table_tasks,
    build_text_tasks,
    task_id,
)
from chemex_lit.models import (
    AdjudicationDecision,
    CandidateSubmission,
    Channel,
    DocumentBundle,
    ExtractionTask,
    ProducerPlan,
    ProducerSpec,
    ProvenanceEntry,
    ReactionCandidate,
    ReactionRecord,
    RunMode,
    RunRequest,
    RunSummary,
    StructureCandidate,
    SubmissionProducer,
    TaskAssets,
    TaskChannelStatus,
    ValidationIssue,
)
from chemex_lit.review import generate_review
from chemex_lit.store import ArtifactStore, sha256_file, sha256_text

_TASK_STATE_PATH = "tasks/state.json"
_EXTRACTION_TASKS_PATH = "tasks/extraction.jsonl"
_ADJUDICATION_TASKS_PATH = "tasks/adjudication.jsonl"
_ASSEMBLY_OUTPUT = "assembly/records.jsonl"
_FINAL_RECORDS_OUTPUT = "records.jsonl"
_PROVENANCE_OUTPUT = "candidates/provenance.jsonl"


@dataclass(frozen=True)
class ApplyResult:
    """Counts returned after accepting external task submissions."""

    fulfilled_tasks: int
    output_count: int
    noop_tasks: int


def build_producer_plan(
    mode: RunMode,
    config: AppConfig,
    has_external_structures: bool,
) -> ProducerPlan:
    """Build the per-run producer plan declared in the manifest.

    Channel ownership by mode:

    - ``auto``: every channel is fulfilled by CLI-configured models.
    - ``semi``: CLI models cover text/table/adjudication; structures are
      externalized to the host agent.
    - ``agent``: every channel is externalized to the host agent.
    """

    del has_external_structures
    reasoning_model, _ = config.models.reasoning_spec()
    if mode == "auto":
        return ProducerPlan(
            text=_cli_spec(config.models.text),
            table=_cli_spec(config.models.vision),
            structure=_cli_spec(config.models.vision),
            adjudication=_cli_spec(reasoning_model),
        )
    if mode == "semi":
        return ProducerPlan(
            text=_cli_spec(config.models.text),
            table=_cli_spec(config.models.vision),
            structure=_host_spec(config, "structure"),
            adjudication=_cli_spec(reasoning_model),
        )
    return ProducerPlan(
        text=_host_spec(config, "text"),
        table=_host_spec(config, "table"),
        structure=_host_spec(config, "structure"),
        adjudication=_host_spec(config, "adjudication"),
    )


def _host_spec(config: AppConfig, channel: Channel) -> ProducerSpec:
    """Build a host producer declaration from the active profile policy."""

    route = config.host_routes.get(channel)
    if route is None:
        return ProducerSpec(kind="host_agent")
    return ProducerSpec(
        kind="host_agent",
        model=route.model,
        policy=route.policy,
        fallbacks=list(route.fallbacks) or None,
    )


def apply_submissions(
    store: ArtifactStore,
    document_or_none: DocumentBundle | None,
    submissions: list[CandidateSubmission],
    tasks: list[ExtractionTask],
    state: dict[str, Any],
    *,
    force: bool = False,
) -> ApplyResult:
    """Validate and persist external extraction submissions."""

    del document_or_none
    task_entries = _task_entries(state)
    tasks_by_id = {task.task_id: task for task in tasks}
    unknown_task_ids = sorted(
        {submission.task_id for submission in submissions if submission.task_id not in tasks_by_id}
    )
    if unknown_task_ids:
        raise ChemExError(f"Unknown task_id(s): {unknown_task_ids}")

    fulfilled_tasks = 0
    output_count = 0
    noop_tasks = 0
    superseded_task_ids: set[str] = set()

    for submission in submissions:
        task = tasks_by_id[submission.task_id]
        if task.kind not in {"text", "table", "structure"}:
            raise ChemExError(f"Task {task.task_id} is not an extraction task")
        if not submission.outputs:
            raise ChemExError(f"Submission for task {task.task_id} must include at least one output")

        submission_hash = _submission_hash(submission.model_dump(mode="json"))
        entry = task_entries.setdefault(task.task_id, {"kind": task.kind, "status": "awaiting"})
        current_status = str(entry.get("status", "awaiting"))
        current_hash = entry.get("submission_hash")
        if current_status == "fulfilled":
            if current_hash == submission_hash:
                noop_tasks += 1
                continue
            if not force:
                raise ChemExError(f"Conflicting resubmission for task_id(s): {[task.task_id]}")
            superseded_task_ids.add(task.task_id)

        parsed = _parse_candidate_outputs(task, submission.outputs)
        entry.update(
            {
                "kind": task.kind,
                "status": "fulfilled",
                "submission_hash": submission_hash,
                "producer": submission.producer.model_dump(mode="json"),
                "outputs": submission.outputs,
                "updated_at": utc_now(),
            }
        )
        fulfilled_tasks += 1
        output_count += len(parsed)

    if superseded_task_ids:
        apply_force_invalidation(store, superseded_task_ids)
    store.write_json(_TASK_STATE_PATH, state)
    _set_submit_status(store, state)
    return ApplyResult(
        fulfilled_tasks=fulfilled_tasks,
        output_count=output_count,
        noop_tasks=noop_tasks,
    )


def apply_force_invalidation(
    store: ArtifactStore,
    task_ids: set[str] | list[str] | tuple[str, ...] | None = None,
) -> None:
    """Invalidate downstream stages and append a supersedes audit entry."""

    if task_ids is None:
        task_ids = sorted(_task_entries(_read_task_state(store)))
    store.invalidate_stages(["extraction", "validation", "assembly", "adjudication", "finalization"])
    store.append_jsonl(
        "audit.jsonl",
        [
            {
                "type": "supersedes",
                "task_ids": sorted(set(task_ids)),
                "at": utc_now(),
            }
        ],
    )


def apply_decisions(
    store: ArtifactStore,
    decisions: list[AdjudicationDecision],
    tasks: list[ExtractionTask],
    state: dict[str, Any],
) -> ApplyResult:
    """Validate and persist external adjudication decisions."""

    task_entries = _task_entries(state)
    tasks_by_id = {task.task_id: task for task in tasks}
    unknown_task_ids = sorted(
        {decision.task_id for decision in decisions if decision.task_id not in tasks_by_id}
    )
    if unknown_task_ids:
        raise ChemExError(f"Unknown task_id(s): {unknown_task_ids}")

    fulfilled_tasks = 0
    noop_tasks = 0
    for decision in decisions:
        task = tasks_by_id[decision.task_id]
        if task.kind != "adjudication":
            raise ChemExError(f"Task {task.task_id} is not an adjudication task")
        expected_reaction_id = _adjudication_reaction_id(task)
        if decision.reaction_id != expected_reaction_id:
            raise ChemExError(
                f"Decision for task {task.task_id} targets {decision.reaction_id}, expected "
                f"{expected_reaction_id}"
            )

        submission_hash = _submission_hash(decision.model_dump(mode="json"))
        entry = task_entries.setdefault(task.task_id, {"kind": task.kind, "status": "awaiting"})
        current_status = str(entry.get("status", "awaiting"))
        current_hash = entry.get("submission_hash")
        if current_status == "fulfilled":
            if current_hash == submission_hash:
                noop_tasks += 1
                continue
            raise ChemExError(f"Conflicting resubmission for task_id(s): {[task.task_id]}")

        entry.update(
            {
                "kind": task.kind,
                "status": "fulfilled",
                "submission_hash": submission_hash,
                "producer": decision.producer.model_dump(mode="json"),
                "decision": {
                    "reaction_id": decision.reaction_id,
                    "decision": decision.decision,
                    "rationale": decision.rationale,
                },
                "updated_at": utc_now(),
            }
        )
        fulfilled_tasks += 1

    store.write_json(_TASK_STATE_PATH, state)
    _set_submit_status(store, state)
    return ApplyResult(fulfilled_tasks=fulfilled_tasks, output_count=fulfilled_tasks, noop_tasks=noop_tasks)


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
            source_hash = sha256_file(request.pdf_path)
        elif request.resume and existing:
            source_hash = str(existing["input_sha256"])
        else:
            raise FileNotFoundError(f"PDF not found: {request.pdf_path}")

        run_id = str(existing["run_id"]) if existing else f"{request.pdf_path.stem}-{source_hash[:8]}"
        plan = build_producer_plan(request.mode, self.config, request.external_structures is not None)
        reasoning_model, _ = self.config.models.reasoning_spec()
        store.initialise(
            run_id=run_id,
            input_path=request.pdf_path,
            input_sha256=source_hash,
            config_dump=self.config.model_dump(mode="json"),
            config_sha256=config_fingerprint(self.config),
            version=__version__,
            prompt_versions=self.prompts.versions,
            models={
                "text": self.config.models.text.model,
                "vision": self.config.models.vision.model,
                "reasoning": reasoning_model.model,
            },
            mode=request.mode,
            producer_plan=plan.model_dump(mode="json"),
            profile=self.config.profile,
            models_source=self.config.models_source,
        )

        try:
            store.set_status("running")
            document = self._document_stage(store, request, source_hash)
            extraction = self._extraction_stage(store, request, document, plan)
            if extraction is None:
                return self._summary(store, "awaiting_input")
            reactions, structures = extraction
            validation = self._validation_stage(store, reactions, structures)
            records = self._assembly_stage(store, reactions, structures, validation)
            records = self._adjudication_stage(store, document, records, plan)
            if records is None:
                return self._summary(store, "awaiting_input")
            status = self._finalization_stage(store, records)
            return self._summary(store, status)
        except Exception:
            store.finish("failed")
            raise

    def _document_stage(
        self,
        store: ArtifactStore,
        request: RunRequest,
        source_hash: str,
    ) -> DocumentBundle:
        output = "document.json"
        input_hash = sha256_text(
            json.dumps(
                {
                    "source_hash": source_hash,
                    "mineru": self.config.mineru.model_dump(mode="json"),
                    "chemex_version": __version__,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
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
        plan: ProducerPlan,
    ) -> tuple[list[ReactionCandidate], list[StructureCandidate]] | None:
        state = _read_task_state(store)
        text_tasks = self._text_tasks(document)
        table_tasks = self._table_tasks(document)
        structure_tasks = build_structure_tasks(document, self.prompts)
        extraction_tasks = text_tasks + table_tasks + structure_tasks
        input_hash = self._extraction_input_hash(
            document=document,
            plan=plan,
            tasks=extraction_tasks,
            state=state,
            external_structures=request.external_structures,
        )
        reaction_output = "candidates/reactions.jsonl"
        structure_output = "candidates/structures.jsonl"
        if store.stage_complete("extraction", input_hash, reaction_output) and (
            store.root / structure_output
        ).is_file():
            return (
                store.read_models(reaction_output, ReactionCandidate),
                store.read_models(structure_output, StructureCandidate),
            )

        in_process_reactions: dict[str, list[ReactionCandidate]] = {}
        in_process_structures: dict[str, list[StructureCandidate]] = {}
        with ThreadPoolExecutor(max_workers=self.config.pipeline.extraction_workers) as executor:
            reaction_futures: dict[Future[list[ReactionCandidate]], ExtractionTask] = {}
            if plan.text.kind == "cli_model":
                for task in text_tasks:
                    reaction_futures[executor.submit(self.text_extractor.fulfill, task)] = task
            if plan.table.kind == "cli_model":
                for task in table_tasks:
                    reaction_futures[executor.submit(self.table_extractor.fulfill, task)] = task

            structure_future: Future[dict[str, list[StructureCandidate]]] | None = None
            if plan.structure.kind == "cli_model":
                structure_future = executor.submit(self._fulfill_structure_tasks, structure_tasks)

            for future, task in reaction_futures.items():
                in_process_reactions[task.task_id] = future.result()
            if structure_future is not None:
                in_process_structures = structure_future.result()

        external_tasks: list[ExtractionTask] = []
        if plan.text.kind != "cli_model":
            external_tasks.extend(text_tasks)
        if plan.table.kind != "cli_model":
            external_tasks.extend(table_tasks)
        if plan.structure.kind != "cli_model":
            external_tasks.extend(structure_tasks)

        if request.external_structures:
            shortcut = _external_structure_submissions(request.external_structures, structure_tasks)
            if shortcut:
                apply_submissions(store, document, shortcut, structure_tasks, state)
                state = _read_task_state(store)

        submitted_reactions: dict[str, list[ReactionCandidate]] = {}
        submitted_structures: dict[str, list[StructureCandidate]] = {}
        awaiting_tasks: list[ExtractionTask] = []
        for task in external_tasks:
            entry = _task_entries(state).setdefault(task.task_id, {"kind": task.kind, "status": "awaiting"})
            if str(entry.get("status", "awaiting")) != "fulfilled":
                awaiting_tasks.append(task)
                continue
            try:
                parsed = _parse_candidate_outputs(task, _entry_outputs(entry, task.task_id))
            except ChemExError as exc:
                raise ArtifactError(f"Invalid submission stored for task {task.task_id}: {exc}") from exc
            if task.kind == "structure":
                submitted_structures[task.task_id] = [item for item in parsed if isinstance(item, StructureCandidate)]
            else:
                submitted_reactions[task.task_id] = [item for item in parsed if isinstance(item, ReactionCandidate)]

        if awaiting_tasks:
            store.write_jsonl(_EXTRACTION_TASKS_PATH, _tasks_with_status(external_tasks, state))
            store.write_json(_TASK_STATE_PATH, state)
            store.mark_stage(
                "extraction",
                status="awaiting",
                input_hash=input_hash,
                output=_EXTRACTION_TASKS_PATH,
                detail=f"awaiting {len(awaiting_tasks)} extraction task(s)",
            )
            store.set_status("awaiting_input")
            return None

        reactions = _deduplicate_candidates(
            [
                candidate
                for group in list(in_process_reactions.values()) + list(submitted_reactions.values())
                for candidate in group
            ]
        )
        structures = _deduplicate_candidates(
            [
                candidate
                for group in list(in_process_structures.values()) + list(submitted_structures.values())
                for candidate in group
            ]
        )
        store.write_jsonl(reaction_output, reactions)
        store.write_jsonl(structure_output, structures)
        store.write_provenance(
            self._extraction_provenance(
                plan=plan,
                input_hash=input_hash,
                text_tasks=text_tasks,
                table_tasks=table_tasks,
                structure_tasks=structure_tasks,
                state=state,
                reaction_results={**in_process_reactions, **submitted_reactions},
                structure_results={**in_process_structures, **submitted_structures},
            )
        )
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
                {
                    "reactions": [item.model_dump(mode="json") for item in reactions],
                    "structures": [item.model_dump(mode="json") for item in structures],
                    "rdkit": rdkit.__version__,
                    "chemex_version": __version__,
                },
                sort_keys=True,
                ensure_ascii=False,
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
                    "chemex_version": __version__,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        if store.stage_complete("assembly", input_hash, _ASSEMBLY_OUTPUT):
            return store.read_models(_ASSEMBLY_OUTPUT, ReactionRecord)

        records = self.assembler.assemble(reactions, structures, validation)
        store.write_jsonl(_ASSEMBLY_OUTPUT, records)
        store.mark_stage(
            "assembly",
            status="complete",
            input_hash=input_hash,
            output=_ASSEMBLY_OUTPUT,
            detail=f"{len(records)} records",
        )
        return records

    def _adjudication_stage(
        self,
        store: ArtifactStore,
        document: DocumentBundle,
        records: list[ReactionRecord],
        plan: ProducerPlan,
    ) -> list[ReactionRecord] | None:
        if self.adjudicator is None:
            store.write_jsonl(_FINAL_RECORDS_OUTPUT, records)
            store.mark_stage(
                "adjudication",
                status="skipped",
                input_hash="",
                output=_FINAL_RECORDS_OUTPUT,
                detail="adjudication disabled",
            )
            return records

        state = _read_task_state(store)
        tasks = build_adjudication_tasks(records, document, self.prompts)
        input_hash = self._adjudication_input_hash(records, plan, tasks, state)
        if store.stage_complete("adjudication", input_hash, _FINAL_RECORDS_OUTPUT):
            return store.read_models(_FINAL_RECORDS_OUTPUT, ReactionRecord)

        if plan.adjudication.kind == "cli_model":
            result = self.adjudicator.adjudicate(records, document)
            store.write_jsonl(_FINAL_RECORDS_OUTPUT, result)
            store.append_jsonl(
                _PROVENANCE_OUTPUT,
                self._adjudication_cli_provenance(tasks, input_hash),
            )
            store.mark_stage(
                "adjudication",
                status="complete",
                input_hash=input_hash,
                output=_FINAL_RECORDS_OUTPUT,
                detail=f"{len(result)} records",
            )
            return result

        if not tasks:
            store.write_jsonl(_FINAL_RECORDS_OUTPUT, records)
            store.mark_stage(
                "adjudication",
                status="skipped",
                input_hash=input_hash,
                output=_FINAL_RECORDS_OUTPUT,
                detail="no eligible records",
            )
            return records

        awaiting_tasks: list[ExtractionTask] = []
        decisions: dict[str, AdjudicationDecision] = {}
        for task in tasks:
            entry = _task_entries(state).setdefault(task.task_id, {"kind": task.kind, "status": "awaiting"})
            if str(entry.get("status", "awaiting")) != "fulfilled":
                awaiting_tasks.append(task)
                continue
            decisions[task.task_id] = _decision_from_state(task, entry)

        if awaiting_tasks:
            store.write_jsonl(_ADJUDICATION_TASKS_PATH, _tasks_with_status(tasks, state))
            store.write_json(_TASK_STATE_PATH, state)
            store.mark_stage(
                "adjudication",
                status="awaiting",
                input_hash=input_hash,
                output=_ADJUDICATION_TASKS_PATH,
                detail=f"awaiting {len(awaiting_tasks)} adjudication task(s)",
            )
            store.set_status("awaiting_input")
            return None

        result = records
        for task in tasks:
            decision = decisions[task.task_id]
            if decision.decision != "accept":
                continue
            result = [
                item.model_copy(update={"review_status": "accepted"})
                if item.reaction_id == decision.reaction_id
                else item
                for item in result
            ]

        store.write_jsonl(_FINAL_RECORDS_OUTPUT, result)
        store.append_jsonl(
            _PROVENANCE_OUTPUT,
            self._adjudication_external_provenance(tasks, state, input_hash),
        )
        store.mark_stage(
            "adjudication",
            status="complete",
            input_hash=input_hash,
            output=_FINAL_RECORDS_OUTPUT,
            detail=f"{len(result)} records",
        )
        return result

    def _finalization_stage(
        self,
        store: ArtifactStore,
        records: list[ReactionRecord],
    ) -> Literal["success", "completed_empty", "partial", "failed"]:
        store.write_jsonl(_FINAL_RECORDS_OUTPUT, records)
        store.write_jsonl(
            "review.jsonl",
            [item for item in records if item.review_status == "needs_review"],
        )
        generate_review(records, store)
        status = self._status(records)
        store.mark_stage(
            "finalization",
            status="complete",
            input_hash="",
            output="review.html",
            detail=status,
        )
        store.finish(status)
        return status

    def _text_tasks(self, document: DocumentBundle) -> list[ExtractionTask]:
        tasks = build_text_tasks(document, self.prompts, self.config.pipeline.max_text_chars)
        if tasks or not document.markdown.strip():
            return tasks
        content = document.markdown[: self.config.pipeline.max_text_chars]
        return [
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
                status="awaiting",
            )
        ]

    def _table_tasks(self, document: DocumentBundle) -> list[ExtractionTask]:
        tasks = build_table_tasks(document, self.prompts)
        if tasks:
            return tasks
        return [
            ExtractionTask(
                task_id=task_id("table", f"markdown:{index}"),
                kind="table",
                instruction_version=str(self.prompts.versions["table"]),
                instructions=self.prompts.render(
                    "table",
                    {"<<PAGE_CONTEXT>>": "markdown table", "<<CONTENT>>": table},
                ),
                output_schema_version="ReactionCandidate@1",
                input_artifacts=["document.json"],
                assets=TaskAssets(text=table),
                status="awaiting",
            )
            for index, table in enumerate(_markdown_tables(document.markdown), 1)
        ]

    def _fulfill_structure_tasks(
        self,
        tasks: list[ExtractionTask],
    ) -> dict[str, list[StructureCandidate]]:
        if getattr(self.structure_extractor, "workers", 1) == 1:
            return {task.task_id: self.structure_extractor.fulfill(task) for task in tasks}

        with ThreadPoolExecutor(max_workers=self.structure_extractor.workers) as executor:
            futures = {executor.submit(self.structure_extractor.fulfill, task): task for task in tasks}
            return {task.task_id: future.result() for future, task in futures.items()}

    def _extraction_input_hash(
        self,
        *,
        document: DocumentBundle,
        plan: ProducerPlan,
        tasks: list[ExtractionTask],
        state: dict[str, Any],
        external_structures: Path | None,
    ) -> str:
        submission_hashes = {
            task.task_id: entry["submission_hash"]
            for task in tasks
            if (entry := _task_entries(state).get(task.task_id)) and entry.get("submission_hash")
        }
        payload = {
            "document": document.model_dump(mode="json"),
            "prompt_versions": self.prompts.versions,
            "models": {
                "text": self.config.models.text.model_dump(mode="json")
                if plan.text.kind == "cli_model"
                else None,
                "table": self.config.models.vision.model_dump(mode="json")
                if plan.table.kind == "cli_model"
                else None,
                "structure": self.config.models.vision.model_dump(mode="json")
                if plan.structure.kind == "cli_model"
                else None,
            },
            "producer_plan": plan.model_dump(mode="json"),
            "external_structures_hash": sha256_file(external_structures)
            if external_structures
            else "",
            "submission_hashes": submission_hashes,
        }
        return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    def _adjudication_input_hash(
        self,
        records: list[ReactionRecord],
        plan: ProducerPlan,
        tasks: list[ExtractionTask],
        state: dict[str, Any],
    ) -> str:
        submission_hashes = {
            task.task_id: entry["submission_hash"]
            for task in tasks
            if (entry := _task_entries(state).get(task.task_id)) and entry.get("submission_hash")
        }
        reasoning_model, _ = self.config.models.reasoning_spec()
        payload = {
            "records": [item.model_dump(mode="json") for item in records],
            "prompt_version": _prompt_version(self.prompts, "adjudication"),
            "producer_plan": plan.adjudication.model_dump(mode="json"),
            "model": reasoning_model.model_dump(mode="json")
            if plan.adjudication.kind == "cli_model"
            else None,
            "submission_hashes": submission_hashes,
            "chemex_version": __version__,
        }
        return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    def _extraction_provenance(
        self,
        *,
        plan: ProducerPlan,
        input_hash: str,
        text_tasks: list[ExtractionTask],
        table_tasks: list[ExtractionTask],
        structure_tasks: list[ExtractionTask],
        state: dict[str, Any],
        reaction_results: dict[str, list[ReactionCandidate]],
        structure_results: dict[str, list[StructureCandidate]],
    ) -> list[ProvenanceEntry]:
        entries: list[ProvenanceEntry] = []
        for task in text_tasks:
            entries.extend(
                self._task_provenance(
                    task=task,
                    producer=plan.text,
                    model=self.config.models.text,
                    input_hash=input_hash,
                    candidates=reaction_results.get(task.task_id, []),
                    state=state,
                )
            )
        for task in table_tasks:
            entries.extend(
                self._task_provenance(
                    task=task,
                    producer=plan.table,
                    model=self.config.models.vision,
                    input_hash=input_hash,
                    candidates=reaction_results.get(task.task_id, []),
                    state=state,
                )
            )
        for task in structure_tasks:
            entries.extend(
                self._task_provenance(
                    task=task,
                    producer=plan.structure,
                    model=self.config.models.vision,
                    input_hash=input_hash,
                    candidates=structure_results.get(task.task_id, []),
                    state=state,
                )
            )
        return entries

    def _task_provenance(
        self,
        *,
        task: ExtractionTask,
        producer: ProducerSpec,
        model: ModelSpec,
        input_hash: str,
        candidates: list[ReactionCandidate] | list[StructureCandidate],
        state: dict[str, Any],
    ) -> list[ProvenanceEntry]:
        result: list[ProvenanceEntry] = []
        entry = _task_entries(state).get(task.task_id, {})
        submission_producer = None
        if entry.get("producer"):
            submission_producer = SubmissionProducer.model_validate(entry["producer"])
        producer_kind = (
            submission_producer.kind if submission_producer is not None else producer.kind
        )
        for candidate in candidates:
            result.append(
                ProvenanceEntry(
                    candidate_id=candidate.candidate_id,
                    task_id=task.task_id,
                    channel=task.kind,
                    producer_kind=producer_kind,
                    provider=model.base_url if producer_kind == "cli_model" else None,
                    model=(
                        model.model
                        if producer_kind == "cli_model"
                        else submission_producer.model
                        if submission_producer and submission_producer.model
                        else producer.model
                    ),
                    policy=(
                        submission_producer.policy
                        if submission_producer and submission_producer.policy
                        else producer.policy
                    ),
                    attempt=submission_producer.attempt if submission_producer else None,
                    prompt_version=_prompt_version(self.prompts, task.kind),
                    instruction_version=task.instruction_version,
                    input_hash=input_hash,
                    submission_hash=str(entry.get("submission_hash"))
                    if submission_producer is not None and entry.get("submission_hash")
                    else None,
                    client_name=submission_producer.client_name if submission_producer else None,
                    client_version=submission_producer.client_version if submission_producer else None,
                    created_at=utc_now(),
                )
            )
        return result

    def _adjudication_cli_provenance(
        self,
        tasks: list[ExtractionTask],
        input_hash: str,
    ) -> list[ProvenanceEntry]:
        reasoning_model, _ = self.config.models.reasoning_spec()
        return [
            ProvenanceEntry(
                candidate_id=_adjudication_reaction_id(task),
                task_id=task.task_id,
                channel="adjudication",
                producer_kind="cli_model",
                provider=reasoning_model.base_url,
                model=reasoning_model.model,
                prompt_version=_prompt_version(self.prompts, "adjudication"),
                instruction_version=task.instruction_version,
                input_hash=input_hash,
                created_at=utc_now(),
            )
            for task in tasks
        ]

    def _adjudication_external_provenance(
        self,
        tasks: list[ExtractionTask],
        state: dict[str, Any],
        input_hash: str,
    ) -> list[ProvenanceEntry]:
        entries: list[ProvenanceEntry] = []
        for task in tasks:
            entry = _task_entries(state)[task.task_id]
            producer = SubmissionProducer.model_validate(entry["producer"])
            entries.append(
                ProvenanceEntry(
                    candidate_id=_adjudication_reaction_id(task),
                    task_id=task.task_id,
                    channel="adjudication",
                    producer_kind=producer.kind,
                    prompt_version=_prompt_version(self.prompts, "adjudication"),
                    instruction_version=task.instruction_version,
                    input_hash=input_hash,
                    submission_hash=str(entry["submission_hash"]),
                    client_name=producer.client_name,
                    client_version=producer.client_version,
                    model=producer.model,
                    policy=producer.policy,
                    attempt=producer.attempt,
                    created_at=utc_now(),
                )
            )
        return entries

    def _summary(
        self,
        store: ArtifactStore,
        status: Literal[
            "running",
            "awaiting_input",
            "ready",
            "success",
            "completed_empty",
            "partial",
            "failed",
            "cancelled",
        ],
    ) -> RunSummary:
        manifest = store.manifest()
        state = _read_task_state(store)
        awaiting = awaiting_task_ids(state)
        records_count = 0
        review_count = 0
        if status != "awaiting_input" and (store.root / _FINAL_RECORDS_OUTPUT).is_file():
            records = store.read_models(_FINAL_RECORDS_OUTPUT, ReactionRecord)
            records_count = len(records)
            review_count = sum(item.review_status == "needs_review" for item in records)
        return RunSummary(
            run_id=str(manifest["run_id"]),
            status=status,
            records_count=records_count,
            review_count=review_count,
            run_dir=store.root.as_posix(),
            stages={
                name: str(data.get("status", "unknown"))
                for name, data in manifest.get("stages", {}).items()
            },
            awaiting=awaiting,
            tasks=task_channel_counts(state),
        )

    @staticmethod
    def _status(
        records: list[ReactionRecord],
    ) -> Literal["success", "completed_empty", "partial", "failed"]:
        if not records:
            return "completed_empty"
        if any(item.review_status == "needs_review" for item in records):
            return "partial"
        return "success"


def _cli_spec(model: ModelSpec) -> ProducerSpec:
    return ProducerSpec(kind="cli_model", model=model.model, provider=model.base_url)


def _read_task_state(store: ArtifactStore) -> dict[str, Any]:
    path = store.root / _TASK_STATE_PATH
    if not path.is_file():
        return {"tasks": {}}
    data = store.read_json(_TASK_STATE_PATH)
    if not isinstance(data, dict):
        raise ArtifactError("Task state must be a JSON object")
    if "tasks" not in data:
        data["tasks"] = {}
    if not isinstance(data["tasks"], dict):
        raise ArtifactError("Task state 'tasks' entry must be a JSON object")
    return data


def _task_entries(state: dict[str, Any]) -> dict[str, Any]:
    tasks = state.setdefault("tasks", {})
    if not isinstance(tasks, dict):
        raise ArtifactError("Task state 'tasks' entry must be a JSON object")
    return tasks


def _set_submit_status(store: ArtifactStore, state: dict[str, Any]) -> None:
    store.set_status("awaiting_input" if awaiting_task_ids(state) else "ready")


def awaiting_task_ids(state: dict[str, Any]) -> list[str]:
    """Return sorted IDs of all tasks still awaiting a submission."""
    return sorted(
        task_id
        for task_id, entry in _task_entries(state).items()
        if isinstance(entry, dict) and str(entry.get("status", "awaiting")) == "awaiting"
    )


_MAX_LISTED_TASK_IDS = 50


def task_channel_counts(
    state: dict[str, Any],
    *,
    max_listed_ids: int = _MAX_LISTED_TASK_IDS,
) -> dict[str, TaskChannelStatus]:
    """Summarize awaiting/fulfilled counts per external task kind.

    Shared by the pipeline summary and the application service so the
    machine-readable envelope stays identical across commands.
    """

    counts: dict[str, TaskChannelStatus] = {}
    for entry_id, entry in _task_entries(state).items():
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "unknown"))
        status = str(entry.get("status", "awaiting"))
        bucket = counts.setdefault(kind, TaskChannelStatus())
        if status == "fulfilled":
            bucket.fulfilled += 1
            continue
        bucket.awaiting += 1
        if len(bucket.awaiting_task_ids) < max_listed_ids:
            bucket.awaiting_task_ids.append(entry_id)
    return counts


def _submission_hash(payload: dict[str, Any]) -> str:
    return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _parse_candidate_outputs(
    task: ExtractionTask,
    outputs: list[dict[str, Any]],
) -> list[ReactionCandidate | StructureCandidate]:
    errors: list[str] = []
    parsed: list[ReactionCandidate | StructureCandidate] = []
    for index, output in enumerate(outputs, 1):
        try:
            parsed.append(_parse_candidate_output(task, output))
        except Exception as exc:
            errors.append(f"row {index}: {exc}")
    if errors:
        raise ChemExError(f"Invalid {task.kind} submission rows for task {task.task_id}: {errors}")
    return parsed


def _parse_candidate_output(
    task: ExtractionTask,
    output: dict[str, Any],
) -> ReactionCandidate | StructureCandidate:
    if not isinstance(output, dict):
        raise ValueError("output must be a JSON object")
    candidate_id = stable_id(
        f"{task.kind}-submission",
        {"task_id": task.task_id, "output": output},
    )
    evidence_ids = list(task.evidence_ids)
    candidate: ReactionCandidate | StructureCandidate
    if task.kind in {"text", "table"}:
        source: Literal["text", "table"] = "text" if task.kind == "text" else "table"
        reactions = reaction_candidates_from_payload([output], source, evidence_ids)
        if len(reactions) != 1:
            raise ValueError("submission row must normalize to exactly one reaction candidate")
        candidate = reactions[0]
    else:
        structures = structure_candidates_from_payload([output], evidence_ids)
        if not structures:
            raise ValueError("smiles must not be empty")
        candidate = structures[0]
    return candidate.model_copy(update={"candidate_id": candidate_id, "evidence_ids": evidence_ids})


def _entry_outputs(entry: dict[str, Any], task_id_value: str) -> list[dict[str, Any]]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ChemExError(f"Task {task_id_value} has no persisted outputs")
    for output in outputs:
        if not isinstance(output, dict):
            raise ChemExError(f"Task {task_id_value} persisted a non-object output")
    return outputs


def _tasks_with_status(tasks: list[ExtractionTask], state: dict[str, Any]) -> list[ExtractionTask]:
    entries = _task_entries(state)
    result: list[ExtractionTask] = []
    for task in tasks:
        status = str(entries.get(task.task_id, {}).get("status", task.status))
        result.append(task.model_copy(update={"status": status}))
    return result


def _deduplicate_candidates(items: list[Any]) -> list[Any]:
    return list({item.candidate_id: item for item in items}.values())


def _external_structure_submissions(
    path: Path,
    tasks: list[ExtractionTask],
) -> list[CandidateSubmission]:
    manual = load_external_structures(path)
    if not manual or not tasks:
        return []
    grouped: dict[str, list[StructureCandidate]] = {task.task_id: [] for task in tasks}
    if len(tasks) == 1:
        grouped[tasks[0].task_id] = manual
    else:
        task_evidence = {task.task_id: set(task.evidence_ids) for task in tasks if task.evidence_ids}
        unmatched: list[StructureCandidate] = []
        for candidate in manual:
            matches = [
                task_id_value
                for task_id_value, evidence_ids in task_evidence.items()
                if evidence_ids.intersection(candidate.evidence_ids)
            ]
            if len(matches) == 1:
                grouped[matches[0]].append(candidate)
            else:
                unmatched.append(candidate)
        if unmatched:
            raise ChemExError(
                "External structures shortcut is ambiguous for multiple structure tasks; "
                "use task-bound submissions instead"
            )
    return [
        CandidateSubmission(
            task_id=task_id_value,
            producer=SubmissionProducer(kind="human"),
            outputs=[candidate.model_dump(mode="json", exclude={"candidate_id"}) for candidate in candidates],
        )
        for task_id_value, candidates in grouped.items()
        if candidates
    ]


def _adjudication_reaction_id(task: ExtractionTask) -> str:
    if task.assets.text is None:
        raise ArtifactError(f"Adjudication task {task.task_id} is missing record payload")
    try:
        payload = json.loads(task.assets.text)
        record = payload["record"]
        return str(record["reaction_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(f"Adjudication task {task.task_id} has invalid payload") from exc


def _decision_from_state(task: ExtractionTask, entry: dict[str, Any]) -> AdjudicationDecision:
    decision = entry.get("decision")
    if not isinstance(decision, dict):
        raise ArtifactError(f"Adjudication task {task.task_id} is missing persisted decision data")
    producer = entry.get("producer")
    if not isinstance(producer, dict):
        raise ArtifactError(f"Adjudication task {task.task_id} is missing persisted producer data")
    return AdjudicationDecision.model_validate(
        {
            "task_id": task.task_id,
            "reaction_id": decision.get("reaction_id"),
            "decision": decision.get("decision"),
            "rationale": decision.get("rationale"),
            "producer": producer,
        }
    )


def _prompt_version(prompts: Any, channel: Channel) -> str | None:
    name = "adjudicate" if channel == "adjudication" else channel
    version = prompts.versions.get(name)
    return str(version) if version is not None else None


def _markdown_tables(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    tables: list[str] = []
    current: list[str] = []
    for line in lines + [""]:
        if line.count("|") >= 2:
            current.append(line)
            continue
        if len(current) >= 2 and any("---" in item for item in current[:3]):
            tables.append("\n".join(current))
        current = []
    return tables


