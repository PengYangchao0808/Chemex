#!/usr/bin/env python3
"""Comprehensive test runner for ChemEx-Lit across all three modes.

Tests:
  1. auto mode: Fully automatic extraction via CLI models.
  2. semi mode: CLI text/table extraction with host-agent structure submission.
  3. agent mode: Full host-agent extraction and adjudication lifecycle.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if (_SRC / "chemex_lit").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from chemex_lit.assembly import Assembler  # noqa: E402
from chemex_lit.chemistry import Validator  # noqa: E402
from chemex_lit.config import AppConfig, load_config  # noqa: E402
from chemex_lit.models import (  # noqa: E402
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
from chemex_lit.pipeline import Pipeline, resume_run, run_pdf, submit_files  # noqa: E402
import chemex_lit.pipeline as pipeline_module  # noqa: E402
from chemex_lit.store import ArtifactStore  # noqa: E402


class MockMinerU:
    """Deterministic document bundle generator for test runs."""

    def convert(self, pdf_path: Path, work_dir: Path) -> DocumentBundle:
        del pdf_path
        image_path = work_dir / "scheme_001.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
        markdown_text = (
            "## Page 1\n\n"
            "Stereoselective [4+3] cycloaddition of chiral oxyallyl cations.\n"
            "Reaction of diene 7 with oxyallyl cation precursor gives cycloadduct 8 in 85% yield.\n\n"
            "| Entry | Reactant | Product | Yield (%) |\n"
            "|---|---|---|---|\n"
            "| 1 | 7 | 8 | 85 |\n"
        )
        evidence = [
            EvidenceRef(
                evidence_id="text-p1",
                kind="text",
                page=1,
                source_path="paper.md",
                text=markdown_text,
            ),
            EvidenceRef(
                evidence_id="table-p1",
                kind="table",
                page=1,
                source_path="paper.md",
                text="| Entry | Reactant | Product | Yield (%) |\n| 1 | 7 | 8 | 85 |",
            ),
            EvidenceRef(
                evidence_id="img-0001",
                kind="image",
                page=1,
                source_path=image_path.as_posix(),
            ),
        ]
        return DocumentBundle(
            document_id="xiong2003",
            markdown=markdown_text,
            images=[image_path.as_posix()],
            evidence=evidence,
        )


class MockPrompts:
    versions = {"text": "1", "table": "1", "structure": "1", "adjudicate": "1"}

    def render(self, name: str, replacements: dict[str, str]) -> str:
        template = f"{name}: <<PAGE_CONTEXT>> <<CONTENT>> <<IMAGE_CONTEXT>> <<RECORD>> <<EVIDENCE>>"
        for token, value in replacements.items():
            template = template.replace(token, value)
        return template


class MockReactionExtractor:
    def __init__(self, source: str) -> None:
        self.source = source

    def fulfill(self, task: ExtractionTask) -> list[ReactionCandidate]:
        return [
            ReactionCandidate(
                candidate_id=f"{self.source}-{task.task_id}",
                source=self.source,
                reactants=[CompoundRef(label="7", name="diene 7", role="reactant")],
                products=[CompoundRef(label="8", name="cycloadduct 8", role="product")],
                yield_pct=85.0,
                evidence_ids=list(task.evidence_ids),
                confidence=0.92,
            )
        ]


class MockStructureExtractor:
    workers = 1

    def fulfill(self, task: ExtractionTask) -> list[StructureCandidate]:
        return [
            StructureCandidate(
                candidate_id=f"structure-7-{task.task_id}",
                compound_label="7",
                smiles="CC=CC=C",
                evidence_ids=list(task.evidence_ids),
                confidence=0.95,
            ),
            StructureCandidate(
                candidate_id=f"structure-8-{task.task_id}",
                compound_label="8",
                smiles="C1CCC2OC(=O)CCC12",
                evidence_ids=list(task.evidence_ids),
                confidence=0.95,
            ),
        ]


class MockAdjudicator:
    def adjudicate(
        self,
        records: list[ReactionRecord],
        document: DocumentBundle,
    ) -> list[ReactionRecord]:
        del document
        adjudicated = []
        for r in records:
            adjudicated.append(r.model_copy(update={"review_status": "accepted"}))
        return adjudicated


def create_test_pipeline(config: AppConfig) -> Pipeline:
    adjudicator = (
        MockAdjudicator()
        if config.pipeline.adjudicate_ambiguous
        else None
    )
    return Pipeline(
        config=config,
        prompts=MockPrompts(),
        mineru=MockMinerU(),
        text_extractor=MockReactionExtractor("text"),
        table_extractor=MockReactionExtractor("table"),
        structure_extractor=MockStructureExtractor(),
        validator=Validator(),
        assembler=Assembler(),
        adjudicator=adjudicator,
    )


def test_mode_auto(config: AppConfig, base_dir: Path) -> dict[str, object]:
    """Test mode 'auto': fully automatic pipeline execution."""
    print("\n" + "=" * 60)
    print(">>> 1. Testing AUTO Mode (Fully Automatic)")
    print("=" * 60)

    pdf = base_dir / "paper.pdf"
    pdf.write_bytes(b"dummy-pdf-content")
    run_dir = base_dir / "run_auto"

    t0 = time.perf_counter()
    summary = run_pdf(config, pdf_path=pdf, output_dir=run_dir, mode="auto")
    elapsed = time.perf_counter() - t0

    store = ArtifactStore(run_dir)
    manifest = store.manifest()
    records = store.read_models("records.jsonl", ReactionRecord) if (run_dir / "records.jsonl").is_file() else []
    provenance = store.read_provenance() if (run_dir / "candidates/provenance.jsonl").is_file() else []

    print(f"[OK] Status: {summary.status}")
    print(f"[OK] Records Extracted: {summary.records_count}")
    print(f"[OK] Manifest Mode: {manifest.get('mode')}")
    print(f"[OK] Stages Completed: {list(manifest.get('stages', {}).keys())}")
    print(f"[OK] Review Dashboard: {(run_dir / 'review.html').is_file()}")
    print(f"[OK] Elapsed: {elapsed:.3f}s")

    assert summary.status == "success", f"Auto mode failed with status: {summary.status}"
    assert summary.records_count > 0, "Auto mode extracted 0 records"
    assert manifest["mode"] == "auto", "Manifest mode mismatch"
    assert manifest["stages"]["extraction"]["status"] == "complete"
    assert manifest["stages"]["assembly"]["status"] == "complete"

    return {
        "mode": "auto",
        "status": summary.status,
        "records": len(records),
        "review_count": summary.review_count,
        "elapsed_s": round(elapsed, 3),
        "provenance_count": len(provenance),
        "html_report": (run_dir / "review.html").is_file(),
    }


def test_mode_semi(config: AppConfig, base_dir: Path) -> dict[str, object]:
    """Test mode 'semi': CLI text/table with host-agent structure submission."""
    print("\n" + "=" * 60)
    print(">>> 2. Testing SEMI Mode (Semi-Automatic: Agent Submits Structures)")
    print("=" * 60)

    pdf = base_dir / "paper.pdf"
    run_dir = base_dir / "run_semi"

    t0 = time.perf_counter()
    # Step 1: Initial Run -> Pauses at structure tasks
    summary = run_pdf(config, pdf_path=pdf, output_dir=run_dir, mode="semi")
    print(f"[Stage 1] Initial run status: {summary.status}")
    print(f"[Stage 1] Awaiting tasks: {len(summary.awaiting)} (Channel: {list(summary.tasks.keys())})")
    assert summary.status == "awaiting_input"
    assert "structure" in summary.tasks

    # Step 2: Agent reads tasks
    store = ArtifactStore(run_dir)
    tasks = store.read_models("tasks/extraction.jsonl", ExtractionTask)
    structure_tasks = [t for t in tasks if t.kind == "structure"]
    print(f"[Stage 2] Host Agent found {len(structure_tasks)} structure task(s)")

    # Step 3: Host Agent prepares and submits structure candidates
    submissions = []
    for task in structure_tasks:
        submissions.append(
            CandidateSubmission(
                task_id=task.task_id,
                producer=SubmissionProducer(
                    kind="host_agent",
                    client_name="opencode",
                    client_version="1.0.0",
                    model="gemini-flash",
                ),
                outputs=[
                    {"compound_label": "7", "smiles": "CC=CC=C", "confidence": 0.98},
                    {"compound_label": "8", "smiles": "C1CCC2OC(=O)CCC12", "confidence": 0.98},
                ],
            ).model_dump(mode="json")
        )

    sub_path = base_dir / "semi_submission.jsonl"
    sub_path.write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in submissions),
        encoding="utf-8",
    )

    submit_res = submit_files(run_dir, [sub_path])
    print(f"[Stage 3] Submission result: status={submit_res['status']}, applied={submit_res['applied']}, awaiting={len(submit_res['awaiting'])}")
    assert submit_res["status"] == "ready"

    # Step 4: Resume
    final_summary = resume_run(config, run_dir)
    elapsed = time.perf_counter() - t0
    manifest = store.manifest()
    records = store.read_models("records.jsonl", ReactionRecord)

    print(f"[Stage 4] Resumed run status: {final_summary.status}")
    print(f"[Stage 4] Final records: {final_summary.records_count}")
    print(f"[Stage 4] Elapsed: {elapsed:.3f}s")

    assert final_summary.status == "success"
    assert len(records) > 0
    assert manifest["mode"] == "semi"

    return {
        "mode": "semi",
        "status": final_summary.status,
        "records": len(records),
        "review_count": final_summary.review_count,
        "elapsed_s": round(elapsed, 3),
        "tasks_fulfilled": submit_res["applied"],
        "html_report": (run_dir / "review.html").is_file(),
    }


def test_mode_agent(config: AppConfig, base_dir: Path) -> dict[str, object]:
    """Test mode 'agent': All generative extraction and adjudication handled by agent."""
    print("\n" + "=" * 60)
    print(">>> 3. Testing AGENT Mode (Full Host-Agent Control & Adjudication)")
    print("=" * 60)

    pdf = base_dir / "paper.pdf"
    run_dir = base_dir / "run_agent"

    t0 = time.perf_counter()
    # Step 1: Run with agent mode & adjudication
    summary = run_pdf(config, pdf_path=pdf, output_dir=run_dir, mode="agent", adjudicate=True)
    print(f"[Stage 1] Initial run status: {summary.status}")
    print(f"[Stage 1] Awaiting tasks across channels: {summary.tasks}")
    assert summary.status == "awaiting_input"

    # Step 2: Fulfill extraction tasks (text, table, structure)
    store = ArtifactStore(run_dir)
    tasks = store.read_models("tasks/extraction.jsonl", ExtractionTask)
    submissions = []
    producer = SubmissionProducer(
        kind="host_agent",
        client_name="opencode",
        client_version="1.0.0",
        model="gemini-flash",
    )

    for task in tasks:
        if task.kind == "text":
            submissions.append(
                CandidateSubmission(
                    task_id=task.task_id,
                    producer=producer,
                    outputs=[
                        {
                            "reactants": [{"label": "7", "name": "diene 7", "role": "reactant"}],
                            "products": [{"label": "8", "name": "cycloadduct 8", "role": "product"}],
                            "yield_pct": 85.0,
                            "temperature_c": 80.0,
                            "confidence": 0.95,
                        }
                    ],
                ).model_dump(mode="json")
            )
        elif task.kind == "table":
            submissions.append(
                CandidateSubmission(
                    task_id=task.task_id,
                    producer=producer,
                    outputs=[
                        {
                            "reactants": [{"label": "7", "name": "diene 7", "role": "reactant"}],
                            "products": [{"label": "8", "name": "cycloadduct 8", "role": "product"}],
                            "yield_pct": 85.0,
                            "confidence": 0.95,
                        }
                    ],
                ).model_dump(mode="json")
            )
        elif task.kind == "structure":
            submissions.append(
                CandidateSubmission(
                    task_id=task.task_id,
                    producer=producer,
                    outputs=[
                        {"compound_label": "7", "smiles": "CC=CC=C", "confidence": 0.98},
                        {"compound_label": "8", "smiles": "C1CCC2OC(=O)CCC12", "confidence": 0.98},
                    ],
                ).model_dump(mode="json")
            )

    cand_file = base_dir / "agent_candidates.jsonl"
    cand_file.write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in submissions),
        encoding="utf-8",
    )
    submit_res = submit_files(run_dir, [cand_file], kind="candidates")
    print(f"[Stage 2] Extraction submitted: status={submit_res['status']}, applied={submit_res['applied']}")
    assert submit_res["status"] == "ready"

    # Step 3: Resume to trigger assembly & adjudication stage
    adj_summary = resume_run(config, run_dir)
    print(f"[Stage 3] Resumed status: {adj_summary.status}")

    # Check if adjudication paused or completed
    if adj_summary.status == "awaiting_input":
        adj_tasks = store.read_models("tasks/adjudication.jsonl", ExtractionTask)
        print(f"[Stage 3] Awaiting {len(adj_tasks)} adjudication task(s)")
        decisions = []
        for at in adj_tasks:
            payload = json.loads(at.assets.text or "{}")
            rec = payload.get("record", {})
            rxn_id = str(rec.get("reaction_id"))
            decisions.append(
                AdjudicationDecision(
                    task_id=at.task_id,
                    reaction_id=rxn_id,
                    decision="accept",
                    rationale="High confidence matching literature precedent and balanced stoichiometry.",
                    producer=producer,
                ).model_dump(mode="json")
            )
        dec_file = base_dir / "agent_decisions.jsonl"
        dec_file.write_text(
            "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in decisions),
            encoding="utf-8",
        )
        dec_res = submit_files(run_dir, [dec_file], kind="adjudications")
        print(f"[Stage 4] Adjudication submitted: status={dec_res['status']}, applied={dec_res['applied']}")
        assert dec_res["status"] == "ready"

        final_summary = resume_run(config, run_dir)
    else:
        final_summary = adj_summary

    elapsed = time.perf_counter() - t0
    manifest = store.manifest()
    records = store.read_models("records.jsonl", ReactionRecord)

    print(f"[Stage 5] Final status: {final_summary.status}")
    print(f"[Stage 5] Final records: {final_summary.records_count}")
    print(f"[Stage 5] Elapsed: {elapsed:.3f}s")

    assert final_summary.status == "success"
    assert len(records) > 0
    assert manifest["mode"] == "agent"

    return {
        "mode": "agent",
        "status": final_summary.status,
        "records": len(records),
        "review_count": final_summary.review_count,
        "elapsed_s": round(elapsed, 3),
        "adjudication_handled": True,
        "html_report": (run_dir / "review.html").is_file(),
    }


def main() -> int:
    print("============================================================")
    print("ChemEx-Lit Three-Mode Comprehensive Execution Test")
    print("============================================================")

    pipeline_module.build_pipeline = create_test_pipeline
    config = load_config()

    with tempfile.TemporaryDirectory(prefix="chemex-test-") as tmp:
        base_dir = Path(tmp)
        auto_res = test_mode_auto(config, base_dir)
        semi_res = test_mode_semi(config, base_dir)
        agent_res = test_mode_agent(config, base_dir)

    print("\n" + "=" * 60)
    print("Execution Summary:")
    print("=" * 60)
    summary_data = [auto_res, semi_res, agent_res]
    print(json.dumps(summary_data, indent=2, ensure_ascii=False))
    print("\nAll 3 modes executed successfully with 100% contract adherence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
