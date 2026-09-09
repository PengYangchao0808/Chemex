from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from chemex_lit import __version__
from chemex_lit.adjudicator import Adjudicator
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.config import config_fingerprint, load_config
from chemex_lit.errors import ArtifactError, ChemExError
from chemex_lit.extraction import stable_id
from chemex_lit.models import (
    AdjudicationDecision,
    CandidateSubmission,
    CompoundRef,
    DocumentBundle,
    EvidenceRef,
    ExtractionTask,
    ReactionCandidate,
    ReactionRecord,
    RunMode,
    RunRequest,
    StructureCandidate,
    SubmissionProducer,
    ValidationIssue,
)
from chemex_lit.pipeline import Pipeline, apply_decisions, apply_submissions, build_producer_plan
from chemex_lit.store import ArtifactStore, sha256_file


class FakePrompts:
    versions = {"text": "1", "table": "1", "structure": "1", "adjudicate": "1"}

    def render(self, name: str, replacements: dict[str, str]) -> str:
        template = f"{name}: <<PAGE_CONTEXT>> <<CONTENT>> <<IMAGE_CONTEXT>> <<RECORD>> <<EVIDENCE>>"
        for token, value in replacements.items():
            template = template.replace(token, value)
        return template


class ScenarioMinerU:
    def __init__(self, *, include_table: bool = False, include_image: bool = True) -> None:
        self.include_table = include_table
        self.include_image = include_image

    def convert(self, pdf_path: Path, work_dir: Path) -> DocumentBundle:
        image_path = work_dir / "scheme.png"
        if self.include_image:
            image_path.write_bytes(b"image")

        evidence = [
            EvidenceRef(
                evidence_id="text-p1",
                kind="text",
                page=1,
                source_path="paper.md",
                text="reaction",
            )
        ]
        if self.include_table:
            evidence.append(
                EvidenceRef(
                    evidence_id="table-p1",
                    kind="table",
                    page=1,
                    source_path="paper.md",
                    text="| A | B |\n| --- | --- |\n| 7 | 8 |",
                )
            )
        images: list[str] = []
        if self.include_image:
            evidence.append(
                EvidenceRef(
                    evidence_id="img-1",
                    kind="image",
                    page=1,
                    source_path=str(image_path),
                )
            )
            images.append(str(image_path))
        return DocumentBundle(document_id="doc", markdown="reaction", images=images, evidence=evidence)


class FakeReactionExtractor:
    def __init__(self, source: Literal["text", "table"]) -> None:
        self.source: Literal["text", "table"] = source

    def fulfill(self, task: ExtractionTask) -> list[ReactionCandidate]:
        return [
            ReactionCandidate(
                candidate_id=f"{self.source}-{task.task_id}",
                source=self.source,
                reactants=[CompoundRef(label="7", role="reactant")],
                products=[CompoundRef(label="8", role="product")],
                evidence_ids=list(task.evidence_ids),
                confidence=0.9,
            )
        ]

    def extract(self, document: DocumentBundle) -> list[ReactionCandidate]:
        del document
        return []


class FakeStructureExtractor:
    workers = 1

    def fulfill(self, task: ExtractionTask) -> list[StructureCandidate]:
        del task
        return [
            StructureCandidate(candidate_id="s7", compound_label="7", smiles="CC"),
            StructureCandidate(candidate_id="s8", compound_label="8", smiles="CCO"),
        ]

    def extract(self, document: DocumentBundle) -> list[StructureCandidate]:
        del document
        return []


class PassThroughAdjudicator:
    def adjudicate(
        self,
        records: list[ReactionRecord],
        document: DocumentBundle,
    ) -> list[ReactionRecord]:
        del document
        return records


def make_pipeline(mineru: ScenarioMinerU, *, adjudicator: Adjudicator | None = None) -> Pipeline:
    return Pipeline(
        config=load_config(),
        prompts=FakePrompts(),
        mineru=mineru,
        text_extractor=FakeReactionExtractor("text"),
        table_extractor=FakeReactionExtractor("table"),
        structure_extractor=FakeStructureExtractor(),
        validator=Validator(),
        assembler=Assembler(),
        adjudicator=adjudicator,
    )


def initialise_store(
    store: ArtifactStore,
    pdf_path: Path,
    *,
    mode: RunMode,
) -> None:
    config = load_config()
    prompts = FakePrompts()
    plan = build_producer_plan(mode, config, False)
    reasoning_model, _ = config.models.reasoning_spec()
    store.initialise(
        run_id="run-1",
        input_path=pdf_path,
        input_sha256=sha256_file(pdf_path),
        config_dump=config.model_dump(mode="json"),
        config_sha256=config_fingerprint(config),
        version=__version__,
        prompt_versions=prompts.versions,
        models={
            "text": config.models.text.model,
            "vision": config.models.vision.model,
            "reasoning": reasoning_model.model,
        },
        mode=mode,
        producer_plan=plan.model_dump(mode="json"),
    )


def test_pipeline_semi_pause_resume_and_human_provenance(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")

    auto_dir = tmp_path / "auto"
    semi_dir = tmp_path / "semi"
    auto_pipeline = make_pipeline(ScenarioMinerU())
    semi_pipeline = make_pipeline(ScenarioMinerU())

    auto_summary = auto_pipeline.run(RunRequest(pdf_path=pdf, output_dir=auto_dir, mode="auto"))
    assert auto_summary.status == "success"

    summary = semi_pipeline.run(RunRequest(pdf_path=pdf, output_dir=semi_dir, mode="semi"))
    assert summary.status == "awaiting_input"

    store = ArtifactStore(semi_dir)
    tasks = store.read_models("tasks/extraction.jsonl", ExtractionTask)
    assert tasks
    assert [task.kind for task in tasks] == ["structure"]

    state = store.read_json("tasks/state.json")
    submission = CandidateSubmission(
        task_id=tasks[0].task_id,
        producer=SubmissionProducer(kind="human", client_name="tester"),
        outputs=[
            {"compound_label": "7", "smiles": "CC"},
            {"compound_label": "8", "smiles": "CCO"},
        ],
    )
    apply_submissions(store, None, [submission], tasks, state)

    resumed = semi_pipeline.run(
        RunRequest(pdf_path=pdf, output_dir=semi_dir, resume=True, mode="semi")
    )
    assert resumed.status == "success"
    assert (semi_dir / "records.jsonl").is_file()
    assert (semi_dir / "records.jsonl").read_text(encoding="utf-8") == (
        auto_dir / "records.jsonl"
    ).read_text(encoding="utf-8")

    provenance = store.read_provenance()
    human_entries = [entry for entry in provenance if entry.producer_kind == "human"]
    assert human_entries
    assert all(entry.submission_hash for entry in human_entries)


def test_pipeline_agent_mode_pauses_with_text_table_structure_tasks(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    pipeline = make_pipeline(ScenarioMinerU(include_table=True))

    summary = pipeline.run(RunRequest(pdf_path=pdf, output_dir=tmp_path / "agent", mode="agent"))

    assert summary.status == "awaiting_input"
    tasks = ArtifactStore(tmp_path / "agent").read_models("tasks/extraction.jsonl", ExtractionTask)
    assert {task.kind for task in tasks} == {"text", "table", "structure"}
    assert sorted(task.task_id for task in tasks) == summary.awaiting


def test_apply_submissions_validates_rows_and_overrides_evidence(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "semi"
    pipeline = make_pipeline(ScenarioMinerU())

    summary = pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, mode="semi"))
    assert summary.status == "awaiting_input"

    store = ArtifactStore(run_dir)
    tasks = store.read_models("tasks/extraction.jsonl", ExtractionTask)
    state = store.read_json("tasks/state.json")
    task = tasks[0]

    with pytest.raises(ChemExError, match="Unknown task_id"):
        apply_submissions(
            store,
            None,
            [
                CandidateSubmission(
                    task_id="missing",
                    producer=SubmissionProducer(kind="human"),
                    outputs=[{"compound_label": "7", "smiles": "CC"}],
                )
            ],
            tasks,
            state,
        )

    with pytest.raises(ChemExError, match="row 1"):
        apply_submissions(
            store,
            None,
            [
                CandidateSubmission(
                    task_id=task.task_id,
                    producer=SubmissionProducer(kind="human"),
                    outputs=[{"compound_label": "7"}],
                )
            ],
            tasks,
            state,
        )

    raw_output = {"compound_label": "7", "smiles": "CC", "evidence_ids": ["wrong"]}
    apply_submissions(
        store,
        None,
        [
            CandidateSubmission(
                task_id=task.task_id,
                producer=SubmissionProducer(kind="human"),
                outputs=[raw_output],
            )
        ],
        tasks,
        state,
    )

    resumed = pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, resume=True, mode="semi"))
    assert resumed.status == "partial"

    structures = store.read_models("candidates/structures.jsonl", StructureCandidate)
    assert len(structures) == 1
    assert structures[0].evidence_ids == task.evidence_ids
    assert structures[0].candidate_id == stable_id(
        "structure-submission",
        {"task_id": task.task_id, "output": raw_output},
    )


def test_force_resubmission_is_idempotent_without_force_and_invalidates_with_force(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "semi"
    pipeline = make_pipeline(ScenarioMinerU())

    summary = pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, mode="semi"))
    assert summary.status == "awaiting_input"

    store = ArtifactStore(run_dir)
    tasks = store.read_models("tasks/extraction.jsonl", ExtractionTask)
    original = CandidateSubmission(
        task_id=tasks[0].task_id,
        producer=SubmissionProducer(kind="human"),
        outputs=[{"compound_label": "7", "smiles": "CC"}],
    )
    apply_submissions(store, None, [original], tasks, store.read_json("tasks/state.json"))
    pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, resume=True, mode="semi"))

    result = apply_submissions(store, None, [original], tasks, store.read_json("tasks/state.json"))
    assert result.noop_tasks == 1
    assert result.fulfilled_tasks == 0

    changed = CandidateSubmission(
        task_id=tasks[0].task_id,
        producer=SubmissionProducer(kind="human"),
        outputs=[{"compound_label": "7", "smiles": "CCC"}],
    )
    with pytest.raises(ChemExError, match="Conflicting resubmission"):
        apply_submissions(store, None, [changed], tasks, store.read_json("tasks/state.json"))

    apply_submissions(
        store,
        None,
        [changed],
        tasks,
        store.read_json("tasks/state.json"),
        force=True,
    )
    manifest = store.manifest()
    for name in ("extraction", "validation", "assembly", "adjudication", "finalization"):
        assert name not in manifest["stages"]

    audit = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit[-1]["type"] == "supersedes"
    assert tasks[0].task_id in audit[-1]["task_ids"]


def test_adjudication_host_agent_accept_and_keep_review(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    document = DocumentBundle(
        document_id="doc",
        markdown="reaction",
        evidence=[
            EvidenceRef(
                evidence_id="text-p1",
                kind="text",
                page=1,
                source_path="paper.md",
                text="reaction",
            )
        ],
    )
    record = ReactionRecord(
        reaction_id="rxn-1",
        reactants=[CompoundRef(label="7", role="reactant")],
        products=[CompoundRef(label="8", role="product")],
        evidence_ids=["text-p1"],
        confidence=0.6,
        review_status="needs_review",
        issues=[
            ValidationIssue(
                code="A002_STRUCTURE_MISSING",
                severity="warning",
                target_id="7",
                message="No structure was resolved for 7",
            )
        ],
    )
    pipeline = make_pipeline(
        ScenarioMinerU(include_image=False),
        adjudicator=cast(Adjudicator, cast(object, PassThroughAdjudicator())),
    )
    plan = build_producer_plan("agent", load_config(), False)

    accept_dir = tmp_path / "accept"
    accept_store = ArtifactStore(accept_dir)
    initialise_store(accept_store, pdf, mode="agent")

    result = pipeline._adjudication_stage(accept_store, document, [record], plan)
    assert result is None

    tasks = accept_store.read_models("tasks/adjudication.jsonl", ExtractionTask)
    apply_decisions(
        accept_store,
        [
            AdjudicationDecision(
                task_id=tasks[0].task_id,
                reaction_id="rxn-1",
                decision="accept",
                producer=SubmissionProducer(kind="host_agent", client_name="codex"),
            )
        ],
        tasks,
        accept_store.read_json("tasks/state.json"),
    )
    accepted = pipeline._adjudication_stage(accept_store, document, [record], plan)
    assert accepted is not None
    assert accepted[0].review_status == "accepted"

    keep_dir = tmp_path / "keep"
    keep_store = ArtifactStore(keep_dir)
    initialise_store(keep_store, pdf, mode="agent")

    result = pipeline._adjudication_stage(keep_store, document, [record], plan)
    assert result is None
    keep_tasks = keep_store.read_models("tasks/adjudication.jsonl", ExtractionTask)
    apply_decisions(
        keep_store,
        [
            AdjudicationDecision(
                task_id=keep_tasks[0].task_id,
                reaction_id="rxn-1",
                decision="keep_review",
                producer=SubmissionProducer(kind="host_agent"),
            )
        ],
        keep_tasks,
        keep_store.read_json("tasks/state.json"),
    )
    kept = pipeline._adjudication_stage(keep_store, document, [record], plan)
    assert kept is not None
    assert kept[0].review_status == "needs_review"

    with pytest.raises(ValidationError):
        AdjudicationDecision.model_validate(
            {
                "task_id": keep_tasks[0].task_id,
                "reaction_id": "rxn-1",
                "decision": "reject",
                "producer": {"kind": "host_agent"},
            }
        )


def test_resume_rejects_mismatched_mode(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "run"
    pipeline = make_pipeline(ScenarioMinerU())

    pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, mode="auto"))

    with pytest.raises(ArtifactError, match="Run mode changed"):
        pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, resume=True, mode="semi"))


def test_resume_rejects_legacy_alias_manifest(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "run"
    pipeline = make_pipeline(ScenarioMinerU())

    summary = pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, mode="semi"))
    assert summary.status == "awaiting_input"

    store = ArtifactStore(run_dir)
    tasks = store.read_models("tasks/extraction.jsonl", ExtractionTask)
    state = store.read_json("tasks/state.json")
    apply_submissions(
        store,
        None,
        [
            CandidateSubmission(
                task_id=tasks[0].task_id,
                producer=SubmissionProducer(kind="human", client_name="tester"),
                outputs=[
                    {"compound_label": "7", "smiles": "CC"},
                    {"compound_label": "8", "smiles": "CCO"},
                ],
            )
        ],
        tasks,
        state,
    )

    manifest = store.manifest()
    manifest["mode"] = "human-ocsr-agent"
    manifest["producer_plan"]["text"] = {"kind": "host_agent"}
    manifest["producer_plan"]["adjudication"] = {"kind": "host_agent"}
    store.write_json("manifest.json", manifest)

    with pytest.raises(ArtifactError, match="Run mode changed"):
        pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, resume=True, mode="semi"))


def test_producer_plan_maps_channels_to_model_tiers() -> None:
    config = load_config()
    auto = build_producer_plan("auto", config, False)
    assert auto.text.model == config.models.text.model
    assert auto.table.model == config.models.vision.model
    assert auto.structure.model == config.models.vision.model
    reasoning_model, _ = config.models.reasoning_spec()
    assert auto.adjudication.model == reasoning_model.model

    semi = build_producer_plan("semi", config, False)
    assert semi.table.model == config.models.vision.model
    assert semi.structure.kind == "host_agent"
