from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from chemex_lit.cli import main
from chemex_lit.config import load_config
from chemex_lit.errors import ChemExError
from chemex_lit.models import (
    CompoundRef,
    DocumentBundle,
    EvidenceRef,
    ExtractionTask,
    ReactionCandidate,
    RunRequest,
    StructureCandidate,
    SubmissionProducer,
    normalize_mode,
)
from chemex_lit.pipeline import Pipeline, build_producer_plan
from chemex_lit.assembly import Assembler
from chemex_lit.chemistry import Validator
from chemex_lit.store import ArtifactStore


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
        image = work_dir / "scheme.png"
        image.write_bytes(b"image")
        return DocumentBundle(
            document_id="doc",
            markdown="reaction",
            images=[str(image)],
            evidence=[
                EvidenceRef(
                    evidence_id="ev-text",
                    kind="text",
                    page=1,
                    source_path="paper.md",
                    text="reaction",
                ),
                EvidenceRef(
                    evidence_id="ev-image",
                    kind="image",
                    page=1,
                    source_path=str(image),
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
            )
        ]


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


def _pipeline(config) -> Pipeline:
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


def _host_profile_yaml(path: Path) -> Path:
    payload = {
        "version": 1,
        "profiles": {
            "codex-diverse": {
                "text": {
                    "base_url": "https://text.example/v1",
                    "model": "unused-text",
                    "api_key_env": "UNUSED_TEXT_KEY",
                },
                "vision": {
                    "base_url": "https://vision.example/v1",
                    "model": "unused-vision",
                    "api_key_env": "UNUSED_VISION_KEY",
                },
                "host_routes": {
                    "text": {
                        "policy": "codex-text",
                        "model": "gpt-text",
                        "fallbacks": ["gpt-fast"],
                    },
                    "table": {"policy": "codex-vision", "model": "gpt-vision"},
                    "structure": {"policy": "codex-structure", "model": "gpt-vision"},
                    "adjudication": {
                        "policy": "codex-reasoning",
                        "model": "gpt-reasoning",
                    },
                },
            }
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_normalize_mode_accepts_canonical_and_rejects_removed_aliases() -> None:
    assert normalize_mode("auto") == "auto"
    assert normalize_mode("semi") == "semi"
    assert normalize_mode("agent") == "agent"
    with pytest.raises(ChemExError, match="Unknown mode"):
        normalize_mode("human-ocsr-agent")
    with pytest.raises(ChemExError, match="Unknown mode"):
        normalize_mode("auto-agent")
    with pytest.raises(ChemExError, match="Unknown mode"):
        normalize_mode("turbo")


def test_build_producer_plan_covers_all_modes() -> None:
    config = load_config()

    auto_plan = build_producer_plan("auto", config, False)
    assert {auto_plan.channel(channel).kind for channel in
            ("text", "table", "structure", "adjudication")} == {"cli_model"}

    semi_plan = build_producer_plan("semi", config, False)
    assert semi_plan.text.kind == "cli_model"
    assert semi_plan.table.kind == "cli_model"
    assert semi_plan.structure.kind == "host_agent"
    assert semi_plan.adjudication.kind == "cli_model"

    agent_plan = build_producer_plan("agent", config, False)
    assert {agent_plan.channel(channel).kind for channel in
            ("text", "table", "structure", "adjudication")} == {"host_agent"}


def test_agent_plan_persists_host_policy(tmp_path: Path) -> None:
    config = load_config(models_path=_host_profile_yaml(tmp_path / "models.yaml"), profile="codex-diverse")

    plan = build_producer_plan("agent", config, False)

    assert plan.text.policy == "codex-text"
    assert plan.text.model == "gpt-text"
    assert plan.text.fallbacks == ["gpt-fast"]
    assert plan.adjudication.policy == "codex-reasoning"


def test_semi_structure_route_uses_host_profile(tmp_path: Path) -> None:
    config = load_config(models_path=_host_profile_yaml(tmp_path / "models.yaml"), profile="codex-diverse")

    plan = build_producer_plan("semi", config, False)

    assert plan.structure.kind == "host_agent"
    assert plan.structure.policy == "codex-structure"
    assert plan.text.kind == "cli_model"
    assert plan.adjudication.kind == "cli_model"


def test_agent_mode_pauses_for_all_generative_tasks(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    config = load_config(models_path=_host_profile_yaml(tmp_path / "models.yaml"), profile="codex-diverse")

    run_dir = tmp_path / "agent"
    summary = _pipeline(config).run(
        RunRequest(pdf_path=pdf, output_dir=run_dir, mode="agent")
    )

    assert summary.status == "awaiting_input"
    tasks = ArtifactStore(run_dir).read_models("tasks/extraction.jsonl", ExtractionTask)
    assert {task.kind for task in tasks} == {"text", "structure"}
    manifest = ArtifactStore(run_dir).manifest()
    assert manifest["mode"] == "agent"
    plan = manifest["producer_plan"]
    assert plan["structure"]["kind"] == "host_agent"
    assert plan["structure"]["policy"] == "codex-structure"


@pytest.mark.parametrize(("raw_mode", "needs_cli_keys"), [
    ("agent", False),
    ("semi", True),
])
def test_check_reports_canonical_mode(
    raw_mode: str,
    needs_cli_keys: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from chemex_lit import profiles

    monkeypatch.setattr(profiles, "default_models_path", lambda: tmp_path / "missing.yaml")
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    for name in ("CHEMEX_TEXT_API_KEY", "CHEMEX_VISION_API_KEY", "CHEMEX_REASONING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    if needs_cli_keys:
        monkeypatch.setenv("CHEMEX_TEXT_API_KEY", "text")
        monkeypatch.setenv("CHEMEX_VISION_API_KEY", "vision")

    result = CliRunner().invoke(main, ["check", "--mode", raw_mode])

    assert result.exit_code == 0, result.output
    assert f"Mode:    {raw_mode}" in result.output


def test_check_rejects_removed_alias_modes() -> None:
    result = CliRunner().invoke(main, ["check", "--mode", "auto-agent"])

    assert result.exit_code != 0


def test_check_agent_json_omits_cli_model_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    from chemex_lit import profiles

    monkeypatch.setattr(profiles, "default_models_path", lambda: tmp_path / "missing.yaml")
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    for name in ("CHEMEX_TEXT_API_KEY", "CHEMEX_VISION_API_KEY", "CHEMEX_REASONING_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    result = CliRunner().invoke(main, ["check", "--mode", "agent", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["mode"] == "agent"
    assert payload["profile"] is None
    assert {item["name"] for item in payload["checks"]} == {"RDKit", "MinerU key"}
    assert all(item["status"] == "ok" for item in payload["checks"])


def test_submission_producer_records_host_model_and_policy() -> None:
    producer = SubmissionProducer(
        kind="host_agent",
        client_name="codex",
        model="gpt-5",
        policy="codex-reasoning",
        attempt=2,
    )

    assert producer.model == "gpt-5"
    assert producer.policy == "codex-reasoning"
    assert producer.attempt == 2
