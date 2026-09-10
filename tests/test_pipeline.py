from __future__ import annotations

from pathlib import Path
from typing import Literal

from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.config import load_config
from chemex_lit.models import (
    CompoundRef,
    DocumentBundle,
    EvidenceRef,
    ReactionCandidate,
    ReactionRecord,
    RunRequest,
    StructureCandidate,
)
from chemex_lit.pipeline import Pipeline
from chemex_lit.store import ArtifactStore, record_content_hash


class FakePrompts:
    versions = {"text": "1", "table": "1", "structure": "1", "adjudicate": "1"}

    def render(self, name: str, replacements: dict[str, str]) -> str:
        template = f"{name}: <<PAGE_CONTEXT>> <<CONTENT>> <<IMAGE_CONTEXT>> <<RECORD>> <<EVIDENCE>>"
        for token, value in replacements.items():
            template = template.replace(token, value)
        return template


class FakeMinerU:
    calls = 0

    def convert(self, pdf_path: Path, work_dir: Path) -> DocumentBundle:
        del pdf_path
        self.calls += 1
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


class ReactionExtractor:
    def __init__(self, source: Literal["text", "table"]) -> None:
        self.source: Literal["text", "table"] = source

    def fulfill(self, task: object) -> list[ReactionCandidate]:
        return [
            ReactionCandidate(
                candidate_id=self.source,
                source=self.source,
                reactants=[CompoundRef(label="7", role="reactant")],
                products=[CompoundRef(label="8", role="product")],
                evidence_ids=["text-p1"],
                confidence=0.9,
            )
        ]


class StructureExtractor:
    def fulfill(self, task: object) -> list[StructureCandidate]:
        return [
            StructureCandidate(candidate_id="s7", compound_label="7", smiles="CC"),
            StructureCandidate(candidate_id="s8", compound_label="8", smiles="CCO"),
        ]


def make_pipeline(mineru: FakeMinerU) -> Pipeline:
    return Pipeline(
        config=load_config(),
        prompts=FakePrompts(),
        mineru=mineru,
        text_extractor=ReactionExtractor("text"),
        table_extractor=ReactionExtractor("table"),
        structure_extractor=StructureExtractor(),
        validator=Validator(),
        assembler=Assembler(),
    )


def test_pipeline_writes_minimal_artifact_contract(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "run"
    summary = make_pipeline(FakeMinerU()).run(RunRequest(pdf_path=pdf, output_dir=run_dir))
    assert summary.status == "success"
    assert summary.records_count == 1
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "records.jsonl").is_file()
    assert (run_dir / "review.html").is_file()


def test_pipeline_resume_uses_cached_document(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "run"
    mineru = FakeMinerU()
    pipeline = make_pipeline(mineru)
    pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir))
    pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir, resume=True))
    assert mineru.calls == 1


def test_empty_pipeline_is_not_success(tmp_path: Path) -> None:
    class EmptyExtractor:
        def fulfill(self, task: object) -> list[object]:
            return []

        def extract(self, document: DocumentBundle) -> list[object]:
            return []

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    pipeline = Pipeline(
        config=load_config(),
        prompts=FakePrompts(),
        mineru=FakeMinerU(),
        text_extractor=EmptyExtractor(),
        table_extractor=EmptyExtractor(),
        structure_extractor=EmptyExtractor(),
        validator=Validator(),
        assembler=Assembler(),
    )
    summary = pipeline.run(RunRequest(pdf_path=pdf, output_dir=tmp_path / "empty"))
    assert summary.status == "completed_empty"


def test_pipeline_emits_review_context_with_stable_ids(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "run"
    make_pipeline(FakeMinerU()).run(RunRequest(pdf_path=pdf, output_dir=run_dir))

    assert (run_dir / "review_context.jsonl").is_file()

    store = ArtifactStore(run_dir)
    records = store.read_models("records.jsonl", ReactionRecord)
    contexts = store.read_review_contexts()

    assert len(contexts) == len(records)

    record_by_id = {r.reaction_id: r for r in records}
    for ctx in contexts:
        record = record_by_id[ctx.reaction_id]
        assert ctx.record_hash == record_content_hash(record)

        assert ctx.participants[0].participant_id == f"{ctx.reaction_id}:reactant:1"
        assert ctx.participants[0].role == "reactant"
        assert ctx.participants[0].structure_state == "resolved"
        assert ctx.participants[1].participant_id == f"{ctx.reaction_id}:product:1"
        assert ctx.participants[1].role == "product"
        assert ctx.participants[1].structure_state == "resolved"

        reagent_parts = [p for p in ctx.participants if p.role == "reagent"]
        for p in reagent_parts:
            assert p.structure_state == "not_attempted"


def test_pipeline_emits_review_context_with_reagent_participants(tmp_path: Path) -> None:
    class ReagentExtractor:
        def fulfill(self, task: object) -> list[ReactionCandidate]:
            return [
                ReactionCandidate(
                    candidate_id="text",
                    source="text",
                    reactants=[CompoundRef(label="1", role="reactant")],
                    products=[CompoundRef(label="2", role="product")],
                    reagents=["Pd(OAc)2", "K2CO3"],
                    solvents=["DMF"],
                    evidence_ids=["text-p1"],
                    confidence=0.9,
                )
            ]

    class ReagentStructureExtractor:
        def fulfill(self, task: object) -> list[StructureCandidate]:
            return [
                StructureCandidate(candidate_id="s1", compound_label="1", smiles="CC"),
                StructureCandidate(candidate_id="s2", compound_label="2", smiles="CCO"),
            ]

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = tmp_path / "run"
    pipeline = Pipeline(
        config=load_config(),
        prompts=FakePrompts(),
        mineru=FakeMinerU(),
        text_extractor=ReagentExtractor(),
        table_extractor=ReagentExtractor(),
        structure_extractor=ReagentStructureExtractor(),
        validator=Validator(),
        assembler=Assembler(),
    )
    pipeline.run(RunRequest(pdf_path=pdf, output_dir=run_dir))

    store = ArtifactStore(run_dir)
    contexts = store.read_review_contexts()
    assert len(contexts) == 1

    ctx = contexts[0]
    reagent_parts = [p for p in ctx.participants if p.role == "reagent"]
    solvent_parts = [p for p in ctx.participants if p.role == "solvent"]
    assert len(reagent_parts) == 2
    assert reagent_parts[0].name == "Pd(OAc)2"
    assert reagent_parts[0].participant_id == f"{ctx.reaction_id}:reagent:1"
    assert reagent_parts[0].structure_state == "not_attempted"
    assert reagent_parts[1].name == "K2CO3"
    assert reagent_parts[1].participant_id == f"{ctx.reaction_id}:reagent:2"
    assert len(solvent_parts) == 1
    assert solvent_parts[0].name == "DMF"
    assert solvent_parts[0].participant_id == f"{ctx.reaction_id}:solvent:1"

    reagent_conditions = [c for c in ctx.conditions if c.kind == "reagent"]
    solvent_conditions = [c for c in ctx.conditions if c.kind == "solvent"]
    assert len(reagent_conditions) == 2
    assert reagent_conditions[0].value == "Pd(OAc)2"
    assert len(solvent_conditions) == 1
    assert solvent_conditions[0].value == "DMF"
