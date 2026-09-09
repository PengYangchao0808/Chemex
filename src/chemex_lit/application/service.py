"""Thin application service over the single production pipeline."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal, TypeVar, cast, get_args

from pydantic import BaseModel, ValidationError

from chemex_lit.adjudicator import Adjudicator
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.config import AppConfig
from chemex_lit.errors import ArtifactError, ChemExError, utc_now
from chemex_lit.extraction.structure import StructureExtractor
from chemex_lit.extraction.table import TableExtractor
from chemex_lit.extraction.text import TextExtractor
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.mineru import MinerUAdapter
from chemex_lit.models import (
    AdjudicationDecision,
    CandidateSubmission,
    ExtractionTask,
    ReactionRecord,
    RunMode,
    RunRequest,
    RunStatus,
    RunSummary,
    normalize_mode,
)
from chemex_lit.pipeline import (
    Pipeline,
    _task_entries,
    apply_decisions,
    apply_force_invalidation,
    apply_submissions,
    awaiting_task_ids,
    task_channel_counts,
)
from chemex_lit.store import ArtifactStore, sha256_file

logger = logging.getLogger(__name__)

_T = TypeVar("_T", bound=BaseModel)


class ChemExService:
    """Application service that owns command-level workflow concerns."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        pdf_path: Path,
        output_dir: Path | None = None,
        mode: RunMode = "auto",
        external_structures: Path | None = None,
        adjudicate: bool = False,
    ) -> RunSummary:
        """Run the extraction pipeline from a PDF entry point."""

        config = self._config(adjudicate=adjudicate)
        run_dir = output_dir or _default_run_dir(config, pdf_path)
        pipeline = _build_pipeline(config)
        return pipeline.run(
            RunRequest(
                pdf_path=pdf_path,
                output_dir=run_dir,
                external_structures=external_structures,
                resume=False,
                mode=mode,
            )
        )

    def resume(self, run_dir: Path) -> RunSummary:
        """Resume a persisted run using its manifest metadata."""

        store = ArtifactStore(run_dir)
        manifest = store.manifest()
        status = str(manifest.get("status", ""))
        if status == "cancelled":
            raise ChemExError("Cancelled runs cannot be resumed")
        mode = _manifest_mode(manifest)
        if "producer_plan" not in manifest:
            raise ArtifactError("Run manifest is missing producer_plan; resume requires a P1 manifest")

        config = self._config(adjudicate=_manifest_adjudicate_flag(manifest))
        pipeline = _build_pipeline(config)
        return pipeline.run(
            RunRequest(
                pdf_path=Path(str(manifest["input_path"])),
                output_dir=run_dir,
                resume=True,
                mode=mode,
            )
        )

    def submit(
        self,
        run_dir: Path,
        files: list[Path],
        *,
        kind: Literal["candidates", "adjudications"] = "candidates",
        force: bool = False,
    ) -> dict[str, Any]:
        """Validate and persist external task submissions for a run."""

        if not files:
            raise ChemExError("At least one submission file is required")

        store = ArtifactStore(run_dir)
        manifest = store.manifest()
        status = str(manifest.get("status", ""))
        if status == "cancelled":
            raise ChemExError("Cancelled runs cannot accept submissions")

        state = _read_state(store)
        task_file = "tasks/extraction.jsonl" if kind == "candidates" else "tasks/adjudication.jsonl"
        if not (store.root / task_file).is_file():
            raise ArtifactError(f"Task file not found for {kind}: {store.root / task_file}")
        tasks = store.read_models(task_file, ExtractionTask)

        file_index = _submission_file_index(state)
        bucket = file_index.setdefault(kind, {})
        parsed_files: list[tuple[Path, str, list[CandidateSubmission] | list[AdjudicationDecision]]] = []
        file_summaries: list[dict[str, Any]] = []
        duplicate_count = 0

        for path in files:
            file_hash = sha256_file(path)
            if file_hash in bucket:
                file_summaries.append(
                    {
                        "file": str(path),
                        "sha256": file_hash,
                        "status": "duplicate",
                    }
                )
                duplicate_count += 1
                continue
            if kind == "candidates":
                parsed = _read_jsonl_models(path, CandidateSubmission)
            else:
                parsed = _read_jsonl_models(path, AdjudicationDecision)
            parsed_files.append((path, file_hash, parsed))

        applied_count = 0
        if parsed_files:
            if kind == "candidates":
                submissions = [
                    item
                    for _, _, parsed in parsed_files
                    for item in parsed
                    if isinstance(item, CandidateSubmission)
                ]
                result = apply_submissions(store, None, submissions, tasks, state, force=force)
            else:
                decisions = [
                    item
                    for _, _, parsed in parsed_files
                    for item in parsed
                    if isinstance(item, AdjudicationDecision)
                ]
                if force:
                    apply_force_invalidation(store)
                    _reset_fulfilled_task_entries(state, decisions)
                result = apply_decisions(store, decisions, tasks, state)
            applied_count = result.output_count

            refreshed_state = _read_state(store)
            bucket = _submission_file_index(refreshed_state).setdefault(kind, {})
            submitted_at = utc_now()
            for path, file_hash, _ in parsed_files:
                bucket[file_hash] = {
                    "file_name": path.name,
                    "file_path": path.resolve().as_posix(),
                    "sha256": file_hash,
                    "submitted_at": submitted_at,
                }
                file_summaries.append(
                    {
                        "file": str(path),
                        "sha256": file_hash,
                        "status": "applied",
                    }
                )
            store.write_json("tasks/state.json", refreshed_state)
            state = refreshed_state
        else:
            store.write_json("tasks/state.json", state)

        awaiting = _awaiting_task_ids(tasks, state)
        store.set_status("awaiting_input" if awaiting else "ready")
        return {
            "status": "awaiting_input" if awaiting else "ready",
            "applied": applied_count,
            "awaiting": awaiting,
            "files": file_summaries,
            "duplicates": duplicate_count,
        }

    def status(self, run_dir: Path) -> RunSummary:
        """Return the machine-readable run summary shared by all commands."""

        store = ArtifactStore(run_dir)
        manifest = store.manifest()
        state = _read_state(store)
        records_count = 0
        review_count = 0
        if (store.root / "records.jsonl").is_file():
            records = store.read_models("records.jsonl", ReactionRecord)
            records_count = len(records)
            review_count = sum(item.review_status == "needs_review" for item in records)
        stages = {
            name: str(data.get("status", "unknown"))
            for name, data in manifest.get("stages", {}).items()
            if isinstance(data, dict)
        }
        raw_status = manifest.get("status", "running")
        if raw_status not in get_args(RunStatus):
            raise ArtifactError(f"Run manifest has an invalid status {raw_status!r}")
        return RunSummary(
            run_id=str(manifest.get("run_id", "")),
            status=cast(RunStatus, raw_status),
            records_count=records_count,
            review_count=review_count,
            run_dir=store.root.as_posix(),
            stages=stages,
            awaiting=awaiting_task_ids(state),
            tasks=task_channel_counts(state),
        )

    def cancel(self, run_dir: Path) -> None:
        """Cancel an in-flight run."""

        store = ArtifactStore(run_dir)
        manifest = store.manifest()
        status = str(manifest.get("status", ""))
        if status not in {"running", "awaiting_input", "ready"}:
            raise ChemExError(f"Run cannot be cancelled from terminal status: {status}")
        store.finish("cancelled")

    def _config(self, *, adjudicate: bool) -> AppConfig:
        if not adjudicate:
            return self.config
        return self.config.model_copy(
            update={
                "pipeline": self.config.pipeline.model_copy(update={"adjudicate_ambiguous": True})
            }
        )


def _build_pipeline(config: AppConfig) -> Pipeline:
    """Construct the production pipeline without a service locator."""

    llm = LLMClient()
    prompts = PromptRegistry()
    reasoning_model, used_reasoning_fallback = config.models.reasoning_spec()
    if config.pipeline.adjudicate_ambiguous and used_reasoning_fallback:
        logger.warning(
            "Reasoning model tier not configured; falling back to text model %s",
            reasoning_model.model,
        )
    adjudicator = (
        Adjudicator(llm, prompts, reasoning_model)
        if config.pipeline.adjudicate_ambiguous
        else None
    )
    return Pipeline(
        config=config,
        prompts=prompts,
        mineru=MinerUAdapter(config.mineru),
        text_extractor=TextExtractor(
            llm,
            prompts,
            config.models.text,
            config.pipeline.max_text_chars,
        ),
        table_extractor=TableExtractor(llm, prompts, config.models.vision),
        structure_extractor=StructureExtractor(
            llm,
            prompts,
            config.models.vision,
            config.pipeline.image_workers,
        ),
        validator=Validator(),
        assembler=Assembler(),
        adjudicator=adjudicator,
    )


def _default_run_dir(config: AppConfig, pdf_path: Path) -> Path:
    """Return the default run directory for one PDF."""

    return config.output_dir / f"{pdf_path.stem}-{sha256_file(pdf_path)[:8]}"


def _read_jsonl_models(path: Path, model: type[_T]) -> list[_T]:
    """Read one JSONL file into validated Pydantic models."""

    result: list[_T] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChemExError(f"Invalid JSON in {path.name}:{number}: {exc}") from exc
        try:
            result.append(model.model_validate(payload))
        except ValidationError as exc:
            raise ChemExError(f"Invalid {model.__name__} in {path.name}:{number}: {exc}") from exc
    if not result:
        raise ChemExError(f"Submission file is empty: {path}")
    return result


def _manifest_mode(manifest: dict[str, Any]) -> RunMode:
    """Extract and normalize the run mode from a manifest.

    Legacy manifests may still carry deprecated alias values; they are
    converted to the canonical mode so resume keeps working across the
    v1 mode convergence.
    """

    raw = manifest.get("mode")
    if not isinstance(raw, str):
        raise ArtifactError("Run manifest is missing a valid mode")
    try:
        return normalize_mode(raw)
    except ChemExError as exc:
        raise ArtifactError(
            f"Run manifest has an invalid mode {raw!r}; expected auto, semi, or agent"
            " (legacy aliases human-ocsr-agent and auto-agent are accepted)"
        ) from exc


def _manifest_adjudicate_flag(manifest: dict[str, Any]) -> bool:
    """Return the persisted adjudication flag from manifest config when available."""

    config = manifest.get("config")
    if not isinstance(config, dict):
        return False
    pipeline = config.get("pipeline")
    if not isinstance(pipeline, dict):
        return False
    return bool(pipeline.get("adjudicate_ambiguous", False))


def _read_state(store: ArtifactStore) -> dict[str, Any]:
    """Read persisted task state or return an empty default."""

    path = store.root / "tasks/state.json"
    if not path.is_file():
        return {"tasks": {}, "submission_files": {}}
    state = store.read_json("tasks/state.json")
    if not isinstance(state, dict):
        raise ArtifactError("Task state must be a JSON object")
    state.setdefault("tasks", {})
    state.setdefault("submission_files", {})
    return state


def _submission_file_index(state: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    files = state.setdefault("submission_files", {})
    if not isinstance(files, dict):
        raise ArtifactError("Task state submission_files must be a JSON object")
    return files


def _awaiting_task_ids(tasks: list[ExtractionTask], state: dict[str, Any]) -> list[str]:
    entries = _task_entries(state)
    return sorted(
        task.task_id
        for task in tasks
        if str(entries.get(task.task_id, {}).get("status", "awaiting")) == "awaiting"
    )


def _reset_fulfilled_task_entries(
    state: dict[str, Any],
    decisions: list[AdjudicationDecision],
) -> None:
    """Reset fulfilled decision tasks so force resubmission can replace them."""

    entries = _task_entries(state)
    for decision in decisions:
        entry = entries.get(decision.task_id)
        if not isinstance(entry, dict):
            continue
        entry["status"] = "awaiting"
        entry.pop("submission_hash", None)
        entry.pop("decision", None)
        entry.pop("producer", None)
