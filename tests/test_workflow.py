from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from click.testing import CliRunner
import pytest

from chemex_lit.adjudicator import Adjudicator
import chemex_lit.pipeline as pipeline_module
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.cli import main
from chemex_lit.config import AppConfig, load_config
from chemex_lit.errors import ArtifactError, ChemExError
from chemex_lit.models import (
    AdjudicationDecision,
    CandidateSubmission,
    CompoundRef,
    DocumentBundle,
    EvidenceRef,
    ExtractionTask,
    ReactionCandidate,
    ReactionRecord,
    StructureCandidate,
    SubmissionProducer,
)
from chemex_lit.pipeline import (
    Pipeline,
    build_pipeline,
    resume_run,
    run_pdf,
    run_status,
    submit_files,
)
from chemex_lit.store import ArtifactStore


class FakePrompts:
    versions = {"text": "1", "table": "1", "structure": "1", "adjudicate": "1"}

    def render(self, name: str, replacements: dict[str, str]) -> str:
        template = f"{name}: <<PAGE_CONTEXT>> <<CONTENT>> <<IMAGE_CONTEXT>> <<RECORD>> <<EVIDENCE>>"
        for token, value in replacements.items():
            template = template.replace(token, value)
        return template


class ScenarioMinerU:
    def convert(self, pdf_path: Path, work_dir: Path) -> DocumentBundle:
        del pdf_path
        image_path = work_dir / "scheme.png"
        image_path.write_bytes(b"image")
        return DocumentBundle(
            document_id="doc",
            markdown="reaction",
            images=[str(image_path)],
            evidence=[
                EvidenceRef(
                    evidence_id="text-p1",
                    kind="text",
                    page=1,
                    source_path="paper.md",
                    text="reaction",
                ),
                EvidenceRef(
                    evidence_id="img-1",
                    kind="image",
                    page=1,
                    source_path=str(image_path),
                ),
            ],
        )


class TextExtractor:
    def fulfill(self, task: ExtractionTask) -> list[ReactionCandidate]:
        return [
            ReactionCandidate(
                candidate_id=f"text-{task.task_id}",
                source="text",
                reactants=[CompoundRef(label="7", role="reactant")],
                products=[CompoundRef(label="8", role="product")],
                evidence_ids=list(task.evidence_ids),
                confidence=0.9,
            )
        ]


class TableExtractor:
    def fulfill(self, task: ExtractionTask) -> list[ReactionCandidate]:
        del task
        return []


class StructureExtractor:
    workers = 1

    def fulfill(self, task: ExtractionTask) -> list[StructureCandidate]:
        del task
        return [
            StructureCandidate(candidate_id="s7", compound_label="7", smiles="CC"),
            StructureCandidate(candidate_id="s8", compound_label="8", smiles="CCO"),
        ]


class PassThroughAdjudicator:
    def adjudicate(
        self,
        records: list[ReactionRecord],
        document: DocumentBundle,
    ) -> list[ReactionRecord]:
        del document
        return records


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(config: AppConfig) -> Pipeline:
        adjudicator = (
            cast(Adjudicator, cast(object, PassThroughAdjudicator()))
            if config.pipeline.adjudicate_ambiguous
            else None
        )
        return Pipeline(
            config=config,
            prompts=FakePrompts(),
            mineru=ScenarioMinerU(),
            text_extractor=TextExtractor(),
            table_extractor=TableExtractor(),
            structure_extractor=StructureExtractor(),
            validator=Validator(),
            assembler=Assembler(),
            adjudicator=adjudicator,
        )

    monkeypatch.setattr(pipeline_module, "build_pipeline", factory)


def _config() -> AppConfig:
    return load_config()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def _structure_submission(task: ExtractionTask, smiles7: str = "CC", smiles8: str = "CCO") -> dict[str, object]:
    return CandidateSubmission(
        task_id=task.task_id,
        producer=SubmissionProducer(kind="human", client_name="tester"),
        outputs=[
            {"compound_label": "7", "smiles": smiles7},
            {"compound_label": "8", "smiles": smiles8},
        ],
    ).model_dump(mode="json")


def test_build_pipeline_wires_table_extractor_to_vision_model() -> None:
    config = load_config()

    pipeline = build_pipeline(config)

    assert pipeline.table_extractor.model == config.models.vision


def test_workflow_run_semi_returns_awaiting_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")

    summary = run_pdf(_config(), pdf_path=pdf, output_dir=tmp_path / "semi", mode="semi")

    assert summary.status == "awaiting_input"
    assert summary.awaiting


def test_workflow_submit_then_resume_matches_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    auto_dir = tmp_path / "auto"
    semi_dir = tmp_path / "semi"

    auto = run_pdf(_config(), pdf_path=pdf, output_dir=auto_dir, mode="auto")
    assert auto.status == "success"

    paused = run_pdf(_config(), pdf_path=pdf, output_dir=semi_dir, mode="semi")
    assert paused.status == "awaiting_input"

    store = ArtifactStore(semi_dir)
    task = store.read_models("tasks/extraction.jsonl", ExtractionTask)[0]
    submission_file = _write_jsonl(
        tmp_path / "structures.jsonl",
        [_structure_submission(task)],
    )

    submitted = submit_files(semi_dir, [submission_file])
    assert submitted["status"] == "ready"
    assert submitted["awaiting"] == []

    resumed = resume_run(_config(), semi_dir)
    assert resumed.status == "success"
    assert (semi_dir / "records.jsonl").read_text(encoding="utf-8") == (
        auto_dir / "records.jsonl"
    ).read_text(encoding="utf-8")


def test_workflow_resubmit_identical_conflicting_and_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "semi"

    paused = run_pdf(_config(), pdf_path=pdf, output_dir=run_dir, mode="semi")
    assert paused.status == "awaiting_input"

    task = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)[0]
    first_file = _write_jsonl(tmp_path / "structures-1.jsonl", [_structure_submission(task)])
    _ = submit_files(run_dir, [first_file])
    _ = resume_run(_config(), run_dir)

    duplicate = submit_files(run_dir, [first_file])
    assert duplicate["applied"] == 0
    assert duplicate["files"][0]["status"] == "duplicate"

    changed_file = _write_jsonl(
        tmp_path / "structures-2.jsonl",
        [_structure_submission(task, smiles7="CCC", smiles8="CCO")],
    )
    with pytest.raises(ChemExError, match="Conflicting resubmission"):
        submit_files(run_dir, [changed_file])

    forced = submit_files(run_dir, [changed_file], force=True)
    assert forced["status"] == "ready"

    manifest = ArtifactStore(run_dir).manifest()
    for stage_name in ("extraction", "validation", "assembly", "adjudication", "finalization"):
        assert stage_name not in manifest["stages"]

    audit_entries = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_entries[-1]["type"] == "supersedes"


def test_workflow_status_cancel_and_resume_after_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "semi"

    paused = run_pdf(_config(), pdf_path=pdf, output_dir=run_dir, mode="semi")
    assert paused.status == "awaiting_input"

    status = run_status(run_dir)
    assert status.tasks["structure"].awaiting == 1
    assert status.tasks["structure"].awaiting_task_ids

    cancelled_result = CliRunner().invoke(main, ["cancel", str(run_dir)])
    assert cancelled_result.exit_code == 0
    cancelled = run_status(run_dir)
    assert cancelled.status == "cancelled"

    with pytest.raises(ChemExError, match="Cancelled runs cannot be resumed"):
        resume_run(_config(), run_dir)

    success_dir = tmp_path / "success"
    success = run_pdf(_config(), pdf_path=pdf, output_dir=success_dir, mode="auto")
    assert success.status == "success"
    rejected = CliRunner().invoke(main, ["cancel", str(success_dir)])
    assert rejected.exit_code != 0
    assert "terminal status" in str(rejected.exception)


def test_workflow_resume_rejects_manifest_with_bad_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "run"
    summary = run_pdf(_config(), pdf_path=pdf, output_dir=run_dir, mode="auto")
    assert summary.status == "success"

    store = ArtifactStore(run_dir)
    manifest = store.manifest()
    manifest["mode"] = "turbo"
    store.write_json("manifest.json", manifest)

    with pytest.raises(ArtifactError, match="invalid mode"):
        resume_run(_config(), run_dir)

    manifest.pop("mode")
    store.write_json("manifest.json", manifest)

    with pytest.raises(ArtifactError, match="missing a valid mode"):
        resume_run(_config(), run_dir)


def test_workflow_submit_adjudications_then_resume_accepts_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "agent"

    paused = run_pdf(
        _config(),
        pdf_path=pdf,
        output_dir=run_dir,
        mode="agent",
        adjudicate=True,
    )
    assert paused.status == "awaiting_input"

    extraction_tasks = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)
    candidate_rows: list[dict[str, object]] = []
    for task in extraction_tasks:
        if task.kind == "text":
            candidate_rows.append(
                CandidateSubmission(
                    task_id=task.task_id,
                    producer=SubmissionProducer(kind="host_agent", client_name="codex"),
                    outputs=[
                        {
                            "reactants": [{"label": "7"}],
                            "products": [{"label": "8"}],
                            "confidence": 0.9,
                        }
                    ],
                ).model_dump(mode="json")
            )
        elif task.kind == "structure":
            candidate_rows.append(
                CandidateSubmission(
                    task_id=task.task_id,
                    producer=SubmissionProducer(kind="host_agent", client_name="codex"),
                    outputs=[{"compound_label": "7", "smiles": "CC"}],
                ).model_dump(mode="json")
            )

    candidates_file = _write_jsonl(tmp_path / "candidates.jsonl", candidate_rows)
    submitted = submit_files(run_dir, [candidates_file], kind="candidates")
    assert submitted["status"] == "ready"

    adjudication_pause = resume_run(_config(), run_dir)
    assert adjudication_pause.status == "awaiting_input"

    adjudication_task = ArtifactStore(run_dir).read_models("tasks/adjudication.jsonl", ExtractionTask)[0]
    payload = json.loads(adjudication_task.assets.text or "{}")
    assert isinstance(payload, dict)
    record = payload.get("record")
    assert isinstance(record, dict)
    reaction_id = str(record["reaction_id"])
    decisions_file = _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            AdjudicationDecision(
                task_id=adjudication_task.task_id,
                reaction_id=reaction_id,
                decision="accept",
                producer=SubmissionProducer(kind="host_agent", client_name="codex"),
            ).model_dump(mode="json")
        ],
    )

    decisions = submit_files(run_dir, [decisions_file], kind="adjudications")
    assert decisions["status"] == "ready"

    finished = resume_run(_config(), run_dir)
    assert finished.status == "success"
    records = ArtifactStore(run_dir).read_models("records.jsonl", ReactionRecord)
    assert records[0].review_status == "accepted"
