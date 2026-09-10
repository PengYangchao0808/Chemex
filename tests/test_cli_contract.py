"""P0 cross-platform CLI contract smoke tests.

Locks the six-command agent surface: three canonical modes, alias
normalization into the manifest, stable JSON envelopes, the submission
schema, and forward-slash protocol paths. All external services are
mocked; no network access happens here.
"""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from typing import Any, cast

from click.testing import CliRunner
import pytest

from chemex_lit.adjudicator import Adjudicator
import chemex_lit.pipeline as pipeline_module
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.cli import main
from chemex_lit.config import AppConfig
from chemex_lit.models import (
    CompoundRef,
    DocumentBundle,
    EvidenceRef,
    ExtractionTask,
    ReactionCandidate,
    ReactionRecord,
    RunSummary,
    StructureCandidate,
)
from chemex_lit.pipeline import Pipeline
from chemex_lit.store import ArtifactStore

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        evidence: list[EvidenceRef] = [
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
        return DocumentBundle(
            document_id="doc", markdown="reaction", images=images, evidence=evidence
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


class EmptyExtractor:
    def fulfill(self, task: ExtractionTask) -> list[object]:
        del task
        return []

    def extract(self, document: DocumentBundle) -> list[object]:
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


def _pipeline_factory(mineru: ScenarioMinerU, *, empty: bool = False):
    def factory(config: AppConfig) -> Pipeline:
        adjudicator = (
            cast(Adjudicator, cast(object, PassThroughAdjudicator()))
            if config.pipeline.adjudicate_ambiguous
            else None
        )
        text = EmptyExtractor() if empty else FakeReactionExtractor("text")
        table = EmptyExtractor() if empty else FakeReactionExtractor("table")
        structure = EmptyExtractor() if empty else FakeStructureExtractor()
        return Pipeline(
            config=config,
            prompts=FakePrompts(),
            mineru=mineru,
            text_extractor=text,
            table_extractor=table,
            structure_extractor=structure,
            validator=Validator(),
            assembler=Assembler(),
            adjudicator=adjudicator,
        )

    return factory


@pytest.fixture()
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from chemex_lit import profiles

    monkeypatch.setattr(profiles, "default_models_path", lambda: tmp_path / "missing.yaml")
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    monkeypatch.setenv("CHEMEX_TEXT_API_KEY", "text")
    monkeypatch.setenv("CHEMEX_VISION_API_KEY", "vision")
    monkeypatch.setenv("CHEMEX_REASONING_API_KEY", "reasoning")
    return tmp_path


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(main, list(args))


def _parse_json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def _patch(monkeypatch: pytest.MonkeyPatch, mineru: ScenarioMinerU, *, empty: bool = False) -> None:
    monkeypatch.setattr(
        pipeline_module, "build_pipeline", _pipeline_factory(mineru, empty=empty)
    )


def _write_submission(run_dir: Path, tasks: list[ExtractionTask]) -> Path:
    rows = [
        {
            "task_id": task.task_id,
            "producer": {
                "kind": "host_agent",
                "client_name": "codex",
                "client_version": "1.0.0",
                "model": "gpt-5",
                "policy": "chemex-lit-default",
            },
            "outputs": [
                {"compound_label": "7", "smiles": "CC", "confidence": 0.9},
                {"compound_label": "8", "smiles": "CCO", "confidence": 0.9},
            ],
        }
        for task in tasks
        if task.kind == "structure"
    ]
    path = run_dir.parent / f"submission-{run_dir.name}.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _validator():
    spec = importlib.util.spec_from_file_location(
        "validate_submission",
        REPO_ROOT / "chemex-lit-skill" / "scripts" / "validate_submission.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 1. --help succeeds and exposes the agent command surface.
def test_help_succeeds() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for command in ("run", "status", "submit", "resume", "check", "review"):
        assert command in result.output


# 2-4. check --mode {auto,semi,agent} --json returns the stable envelope.
@pytest.mark.parametrize("mode", ["auto", "semi", "agent"])
def test_check_json_envelope_for_all_modes(mode: str, cli_env: Path) -> None:
    del cli_env
    result = _invoke("check", "--mode", mode, "--json")
    assert result.exit_code == 0, result.output
    payload = _parse_json(result)
    assert set(payload) == {"ok", "mode", "profile", "checks"}
    assert payload["ok"] is True
    assert payload["mode"] == mode
    assert payload["profile"] is None
    expected_names = {"RDKit", "MinerU key"}
    if mode in {"auto", "semi"}:
        expected_names |= {"Text model key", "Vision model key"}
    assert {item["name"] for item in payload["checks"]} == expected_names
    for item in payload["checks"]:
        if item["name"] == "RDKit":
            assert set(item) == {"name", "status"}
        else:
            assert set(item) == {"name", "status", "source", "expires_in_days"}
            assert item["source"] == "env"
            assert item["expires_in_days"] is None
        assert item["status"] in {"ok", "missing", "expiring"}


# 5-6. All three modes create a run manifest; manifest mode is canonical only.
@pytest.mark.parametrize("mode", ["auto", "semi", "agent"])
def test_run_creates_manifest_with_canonical_mode(mode: str, cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ScenarioMinerU(include_table=True))
    pdf = cli_env / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = cli_env / "outputs" / mode

    result = _invoke("run", str(pdf), "--mode", mode, "--output-dir", str(run_dir), "--json")

    assert result.exit_code == 0, result.output
    manifest = ArtifactStore(run_dir).manifest()
    assert manifest["mode"] == mode


# 6b. Removed alias input is rejected before anything is persisted.
@pytest.mark.parametrize("alias", ["auto-agent", "human-ocsr-agent"])
def test_removed_alias_input_is_rejected(alias: str, cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ScenarioMinerU(include_table=True))
    pdf = cli_env / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = cli_env / "outputs" / alias

    result = _invoke("run", str(pdf), "--mode", alias, "--output-dir", str(run_dir), "--json")

    assert result.exit_code != 0
    assert not run_dir.exists()


# 7. semi produces structure external tasks only.
def test_semi_only_externalizes_structure(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ScenarioMinerU(include_table=True))
    pdf = cli_env / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = cli_env / "outputs" / "semi"

    result = _invoke("run", str(pdf), "--mode", "semi", "--output-dir", str(run_dir), "--json")

    assert result.exit_code == 0, result.output
    summary = RunSummary.model_validate(_parse_json(result))
    assert summary.status == "awaiting_input"
    tasks = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)
    assert tasks
    assert {task.kind for task in tasks} == {"structure"}

    status = _parse_json(_invoke("status", str(run_dir), "--json"))
    assert status["status"] == "awaiting_input"
    assert status["awaiting"] == sorted(task.task_id for task in tasks)
    assert set(status["tasks"]) == {"structure"}


# 8. agent externalizes text, table, and structure.
def test_agent_externalizes_all_generative_tasks(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ScenarioMinerU(include_table=True))
    pdf = cli_env / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = cli_env / "outputs" / "agent"

    result = _invoke("run", str(pdf), "--mode", "agent", "--output-dir", str(run_dir), "--json")

    assert result.exit_code == 0, result.output
    tasks = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)
    assert {task.kind for task in tasks} == {"text", "table", "structure"}


# 9-11. Submission JSONL passes the offline validator; submit -> ready; resume advances.
def test_submit_validate_ready_resume_cycle(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ScenarioMinerU(include_table=True))
    pdf = cli_env / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = cli_env / "outputs" / "cycle"

    paused = _parse_json(
        _invoke("run", str(pdf), "--mode", "semi", "--output-dir", str(run_dir), "--json")
    )
    assert paused["status"] == "awaiting_input"

    tasks = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)
    submission = _write_submission(run_dir, tasks)

    validator = _validator()
    for line in submission.read_text(encoding="utf-8").splitlines():
        assert validator.validate_candidate(0, json.loads(line)) == []

    submitted = _parse_json(_invoke("submit", str(run_dir), str(submission), "--json"))
    assert submitted["status"] == "ready"
    assert submitted["awaiting"] == []

    status = _parse_json(_invoke("status", str(run_dir), "--json"))
    assert status["status"] == "ready"

    resumed = _parse_json(_invoke("resume", str(run_dir), "--json"))
    assert resumed["status"] == "success"
    assert resumed["records_count"] >= 1


# 12. Empty extraction is completed_empty with exit code 4.
def test_empty_run_is_completed_empty(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ScenarioMinerU(), empty=True)
    pdf = cli_env / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = cli_env / "outputs" / "empty"

    result = _invoke("run", str(pdf), "--mode", "auto", "--output-dir", str(run_dir), "--json")

    assert result.exit_code == 4
    payload = _parse_json(result)
    assert payload["status"] == "completed_empty"


# 13. Protocol paths never contain backslashes; Path() round-trip opens artifacts.
def test_protocol_paths_are_forward_slash_and_reopenable(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, ScenarioMinerU(include_table=True))
    pdf = cli_env / "paper.pdf"
    pdf.write_bytes(b"pdf")
    run_dir = cli_env / "outputs" / "paths"

    summary_payload = _parse_json(
        _invoke("run", str(pdf), "--mode", "semi", "--output-dir", str(run_dir), "--json")
    )
    assert "\\" not in json.dumps(summary_payload, ensure_ascii=False)
    manifest = ArtifactStore(run_dir).manifest()
    assert "\\" not in json.dumps(manifest, ensure_ascii=False)
    assert Path(manifest["input_path"]).is_file()

    run_summary = RunSummary.model_validate(summary_payload)
    task_file = Path(run_summary.run_dir) / "tasks/extraction.jsonl"
    assert task_file.is_file()
    tasks = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)
    for task in tasks:
        for image in task.assets.images:
            assert "\\" not in image.path
            assert (Path(run_summary.run_dir) / image.path).is_file()
