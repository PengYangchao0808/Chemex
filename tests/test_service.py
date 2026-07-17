from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from chemex_lit.adjudicator import Adjudicator
from chemex_lit.application import ChemExService
import chemex_lit.application.service as service_module
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.config import AppConfig, load_config
from chemex_lit.errors import ChemExError
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
from chemex_lit.pipeline import Pipeline
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

    def extract(self, document: DocumentBundle) -> list[ReactionCandidate]:
        del document
        return []


class TableExtractor:
    def fulfill(self, task: ExtractionTask) -> list[ReactionCandidate]:
        del task
        return []

    def extract(self, document: DocumentBundle) -> list[ReactionCandidate]:
        del document
        return []


class StructureExtractor:
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

    monkeypatch.setattr(service_module, "_build_pipeline", factory)


def _service() -> ChemExService:
    return ChemExService(load_config())


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


def test_service_run_semi_returns_awaiting_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")

    summary = _service().run(pdf_path=pdf, output_dir=tmp_path / "semi", mode="semi")

    assert summary.status == "awaiting_input"
    assert summary.awaiting


def test_service_submit_then_resume_matches_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    auto_dir = tmp_path / "auto"
    semi_dir = tmp_path / "semi"
    service = _service()

    auto = service.run(pdf_path=pdf, output_dir=auto_dir, mode="auto")
    assert auto.status == "success"

    paused = service.run(pdf_path=pdf, output_dir=semi_dir, mode="semi")
    assert paused.status == "awaiting_input"

    store = ArtifactStore(semi_dir)
    task = store.read_models("tasks/extraction.jsonl", ExtractionTask)[0]
    submission_file = _write_jsonl(
        tmp_path / "structures.jsonl",
        [_structure_submission(task)],
    )

    submitted = service.submit(semi_dir, [submission_file])
    assert submitted["status"] == "ready"
    assert submitted["awaiting"] == []

    resumed = service.resume(semi_dir)
    assert resumed.status == "success"
    assert (semi_dir / "records.jsonl").read_text(encoding="utf-8") == (
        auto_dir / "records.jsonl"
    ).read_text(encoding="utf-8")


def test_service_resubmit_identical_conflicting_and_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "semi"
    service = _service()

    paused = service.run(pdf_path=pdf, output_dir=run_dir, mode="semi")
    assert paused.status == "awaiting_input"

    task = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)[0]
    first_file = _write_jsonl(tmp_path / "structures-1.jsonl", [_structure_submission(task)])
    _ = service.submit(run_dir, [first_file])
    _ = service.resume(run_dir)

    duplicate = service.submit(run_dir, [first_file])
    assert duplicate["applied"] == 0
    assert duplicate["files"][0]["status"] == "duplicate"

    changed_file = _write_jsonl(
        tmp_path / "structures-2.jsonl",
        [_structure_submission(task, smiles7="CCC", smiles8="CCO")],
    )
    with pytest.raises(ChemExError, match="Conflicting resubmission"):
        service.submit(run_dir, [changed_file])

    forced = service.submit(run_dir, [changed_file], force=True)
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


def test_service_status_cancel_and_resume_after_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "semi"
    service = _service()

    paused = service.run(pdf_path=pdf, output_dir=run_dir, mode="semi")
    assert paused.status == "awaiting_input"

    status = service.status(run_dir)
    assert status["tasks"]["structure"]["awaiting"] == 1
    assert status["tasks"]["structure"]["awaiting_task_ids"]

    service.cancel(run_dir)
    cancelled = service.status(run_dir)
    assert cancelled["status"] == "cancelled"

    with pytest.raises(ChemExError, match="Cancelled runs cannot be resumed"):
        service.resume(run_dir)

    success_dir = tmp_path / "success"
    success = service.run(pdf_path=pdf, output_dir=success_dir, mode="auto")
    assert success.status == "success"
    with pytest.raises(ChemExError, match="terminal status"):
        service.cancel(success_dir)


def test_service_submit_adjudications_then_resume_accepts_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "agent"
    service = _service()

    paused = service.run(
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
    submitted = service.submit(run_dir, [candidates_file], kind="candidates")
    assert submitted["status"] == "ready"

    adjudication_pause = service.resume(run_dir)
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

    decisions = service.submit(run_dir, [decisions_file], kind="adjudications")
    assert decisions["status"] == "ready"

    finished = service.resume(run_dir)
    assert finished.status == "success"
    records = ArtifactStore(run_dir).read_models("records.jsonl", ReactionRecord)
    assert records[0].review_status == "accepted"
