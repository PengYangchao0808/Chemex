from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

import chemex_lit.cli as cli_module
from chemex_lit.cli import main
from chemex_lit.config import AppConfig
from chemex_lit.models import RunSummary, TaskChannelStatus
from chemex_lit.store import ArtifactStore


def _fake_run_pdf(
    config: AppConfig,
    *,
    pdf_path: Path,
    output_dir: Path | None = None,
    mode: str = "auto",
    external_structures: Path | None = None,
    adjudicate: bool = False,
) -> RunSummary:
    del config, pdf_path, external_structures, adjudicate
    if mode == "semi":
        return RunSummary(
            run_id="run-1",
            status="awaiting_input",
            records_count=0,
            review_count=0,
            run_dir="outputs/run-1",
            stages={"extraction": "awaiting"},
            awaiting=["st-123", "st-456"],
            tasks={"structure": TaskChannelStatus(awaiting=2)},
        )
    return RunSummary(
        run_id="run-1",
        status="success",
        records_count=0,
        review_count=0,
        run_dir="outputs/run-1",
    )


def _fake_run_status(run_dir: Path) -> RunSummary:
    return RunSummary(
        run_id="run-1",
        status="cancelled",
        records_count=0,
        review_count=0,
        run_dir=str(run_dir),
        stages={"extraction": "awaiting"},
        tasks={"structure": TaskChannelStatus(awaiting=1, awaiting_task_ids=["st-123"])},
    )


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "chemex-lit" in result.output


def test_help_exposes_compact_command_surface() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in (
        "run",
        "resume",
        "submit",
        "status",
        "cancel",
        "review",
        "review-apply",
        "evaluate",
        "check",
    ):
        assert command in result.output


def test_run_mode_semi_prints_awaiting_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "paper.pdf"
    _ = pdf.write_bytes(b"pdf")
    monkeypatch.setattr(cli_module, "run_pdf", _fake_run_pdf)

    result = CliRunner().invoke(main, ["run", str(pdf), "--mode", "semi"])

    assert result.exit_code == 0
    assert "Awaiting tasks: 2" in result.output
    assert "tasks/extraction.jsonl" in result.output


def test_submit_requires_existing_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = CliRunner().invoke(main, ["submit", str(run_dir), str(tmp_path / "missing.jsonl")])

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cancel_prints_resulting_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    ArtifactStore(run_dir).write_json(
        "manifest.json",
        {"run_id": "run-1", "status": "awaiting_input", "stages": {}},
    )
    monkeypatch.setattr(cli_module, "run_status", _fake_run_status)

    result = CliRunner().invoke(main, ["cancel", str(run_dir)])

    assert result.exit_code == 0
    assert "Status: cancelled" in result.output


def test_cancel_rejects_terminal_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    ArtifactStore(run_dir).write_json(
        "manifest.json",
        {"run_id": "run-1", "status": "success", "stages": {}},
    )

    result = CliRunner().invoke(main, ["cancel", str(run_dir)])

    assert result.exit_code != 0
    assert isinstance(result.exception, Exception)
    assert "terminal status" in str(result.exception)


def test_review_apply_requires_confirmed_by(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    corrections = tmp_path / "corrections.json"
    _ = corrections.write_text(json.dumps([]), encoding="utf-8")

    result = CliRunner().invoke(main, ["review-apply", str(run_dir), str(corrections)])

    assert result.exit_code != 0
    assert "--confirmed-by" in result.output
