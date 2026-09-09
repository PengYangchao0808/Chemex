#!/usr/bin/env python3
"""Host-agnostic ChemEx-Lit smoke test: no network, no real models.

Runs the same flow every host agent (Codex, OpenCode, Antigravity) uses,
fully in-process with mocked MinerU/LLM adapters:

    PDF -> document stage -> extraction task -> submission -> submit
        -> resume -> validation -> assembly -> review

Usage:
    python scripts/smoke-cli.py

Exits 0 when every step holds; any failure prints the broken step and
exits 1. Safe to run anywhere: it works inside a temporary directory and
never touches the repository or calls external services.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if (_SRC / "chemex_lit").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from chemex_lit.application import ChemExService  # noqa: E402
import chemex_lit.application.service as service_module  # noqa: E402
from chemex_lit.assembly import Assembler  # noqa: E402
from chemex_lit.chemistry import Validator  # noqa: E402
from chemex_lit.config import AppConfig, load_config  # noqa: E402
from chemex_lit.models import (  # noqa: E402
    CompoundRef,
    DocumentBundle,
    EvidenceRef,
    ExtractionTask,
    ReactionCandidate,
    ReactionRecord,
    RunSummary,
    StructureCandidate,
)
from chemex_lit.pipeline import Pipeline  # noqa: E402
from chemex_lit.store import ArtifactStore  # noqa: E402

RUN_SUMMARY_KEYS = {
    "run_id",
    "status",
    "records_count",
    "review_count",
    "run_dir",
    "stages",
    "awaiting",
    "tasks",
}


class FakePrompts:
    versions = {"text": "1", "table": "1", "structure": "1", "adjudicate": "1"}

    def render(self, name: str, replacements: dict[str, str]) -> str:
        template = f"{name}: <<PAGE_CONTEXT>> <<CONTENT>> <<IMAGE_CONTEXT>> <<RECORD>> <<EVIDENCE>>"
        for token, value in replacements.items():
            template = template.replace(token, value)
        return template


class FakeMinerU:
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


class FakeReactionExtractor:
    def __init__(self, source: str) -> None:
        self.source = source

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
        return [
            StructureCandidate(
                candidate_id=f"structure-{task.task_id}",
                compound_label="7",
                smiles="CC",
                evidence_ids=list(task.evidence_ids),
            )
        ]

    def extract(self, document: DocumentBundle) -> list[StructureCandidate]:
        del document
        return []


def _mock_pipeline(config: AppConfig) -> Pipeline:
    return Pipeline(
        config=config,
        prompts=FakePrompts(),
        mineru=FakeMinerU(),
        text_extractor=FakeReactionExtractor("text"),
        table_extractor=FakeReactionExtractor("table"),
        structure_extractor=FakeStructureExtractor(),
        validator=Validator(),
        assembler=Assembler(),
    )


def _fail(step: str, detail: str) -> int:
    print(f"[FAIL] {step}: {detail}")
    return 1


def _check_envelope(step: str, summary: RunSummary) -> int | None:
    payload = summary.model_dump(mode="json")
    if set(payload) != RUN_SUMMARY_KEYS:
        return _fail(step, f"envelope keys {sorted(payload)} != contract")
    if "\\" in json.dumps(payload):
        return _fail(step, "envelope contains a backslash path")
    print(f"[OK]   {step}: status={summary.status} awaiting={len(summary.awaiting)}")
    return None


def _load_validator():
    path = REPO_ROOT / "chemex-lit-skill" / "scripts" / "validate_submission.py"
    spec = importlib.util.spec_from_file_location("validate_submission", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"validator not found: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    service_module._build_pipeline = _mock_pipeline
    service = ChemExService(load_config())

    with tempfile.TemporaryDirectory(prefix="chemex-smoke-") as tmp:
        tmp_path = Path(tmp)
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"pdf")
        run_dir = tmp_path / "outputs" / "paper-smoke"

        summary = service.run(pdf_path=pdf, output_dir=run_dir, mode="semi")
        if failed := _check_envelope("run --mode semi", summary):
            return failed
        if summary.status != "awaiting_input":
            return _fail("run --mode semi", f"expected awaiting_input, got {summary.status}")
        if set(summary.tasks) != {"structure"}:
            return _fail("run --mode semi", f"expected structure tasks, got {sorted(summary.tasks)}")

        store = ArtifactStore(run_dir)
        tasks = store.read_models("tasks/extraction.jsonl", ExtractionTask)
        if not tasks or {task.kind for task in tasks} != {"structure"}:
            return _fail("read tasks/extraction.jsonl", "expected structure tasks only")
        if any("\\" in image.path for task in tasks for image in task.assets.images):
            return _fail("read tasks/extraction.jsonl", "backslash in asset path")
        print(f"[OK]   read tasks/extraction.jsonl: {len(tasks)} structure task(s)")

        rows = [
            {
                "task_id": task.task_id,
                "producer": {
                    "kind": "host_agent",
                    "client_name": "smoke",
                    "client_version": "1.0.0",
                },
                "outputs": [
                    {"compound_label": "7", "smiles": "CC", "confidence": 0.9},
                    {"compound_label": "8", "smiles": "CCO", "confidence": 0.9},
                ],
            }
            for task in tasks
        ]
        submission = tmp_path / "submission.jsonl"
        submission.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        validator = _load_validator()
        for line in submission.read_text(encoding="utf-8").splitlines():
            errors = validator.validate_candidate(0, json.loads(line))
            if errors:
                return _fail("offline validation", "; ".join(errors))
        print("[OK]   offline validation: submission.jsonl matches the contract")

        result = service.submit(run_dir, [submission])
        if result["status"] != "ready" or result["awaiting"]:
            return _fail(
                "submit",
                f"expected ready with no awaiting tasks, got {result['status']}",
            )
        print("[OK]   submit: status=ready")

        final = service.resume(run_dir)
        if failed := _check_envelope("resume", final):
            return failed
        if final.status != "success":
            return _fail("resume", f"expected success, got {final.status}")

        records = store.read_models("records.jsonl", ReactionRecord)
        if not records:
            return _fail("validation/assembly", "records.jsonl is empty")
        if not (run_dir / "review.html").is_file():
            return _fail("review", "review.html missing")
        provenance = store.read_provenance()
        if not provenance:
            return _fail("provenance", "provenance sidecar is empty")
        manifest = store.manifest()
        if manifest["mode"] != "semi":
            return _fail("manifest", f"mode drifted: {manifest['mode']}")
        print(
            f"[OK]   records={len(records)} review={final.review_count} "
            f"provenance={len(provenance)} manifest.mode={manifest['mode']}"
        )

    print("\nSmoke OK: the agent flow run -> submit -> resume -> review is intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
