from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

import chemex_lit.cli as cli_module
from chemex_lit.cli import main
from chemex_lit.models import RunSummary


class FakeService:
    cancelled: list[Path] = []

    def __init__(self, config: object) -> None:
        del config

    def run(
        self,
        *,
        pdf_path: Path,
        output_dir: Path | None = None,
        mode: str = "auto",
        external_structures: Path | None = None,
        adjudicate: bool = False,
    ) -> RunSummary:
        del pdf_path, external_structures, adjudicate
        return RunSummary(
            run_id="run-1",
            status="awaiting_input" if mode == "semi" else "success",
            records_count=0,
            review_count=0,
            output_dir=str(output_dir or Path("outputs/run-1")),
            stages={"extraction": "awaiting"} if mode == "semi" else {},
            awaiting=["st-123", "st-456"] if mode == "semi" else [],
        )

    def resume(self, run_dir: Path) -> RunSummary:
        return RunSummary(
            run_id="run-1",
            status="success",
            records_count=1,
            review_count=0,
            output_dir=str(run_dir),
            stages={"finalization": "complete"},
        )

    def submit(
        self,
        run_dir: Path,
        files: list[Path],
        *,
        kind: str = "candidates",
        force: bool = False,
    ) -> dict[str, object]:
        del run_dir, kind, force
        return {
            "status": "ready",
            "applied": len(files),
            "awaiting": [],
            "files": [
                {"file": str(path), "sha256": "abcdef123456", "status": "applied"}
                for path in files
            ],
        }

    def status(self, run_dir: Path) -> dict[str, object]:
        return {
            "run_id": "run-1",
            "status": "cancelled" if run_dir in self.cancelled else "awaiting_input",
            "stages": {"extraction": {"status": "awaiting", "detail": "awaiting 1 task(s)"}},
            "tasks": {
                "structure": {
                    "awaiting": 1,
                    "fulfilled": 0,
                    "awaiting_task_ids": ["st-123"],
                }
            },
        }

    def cancel(self, run_dir: Path) -> None:
        self.cancelled.append(run_dir)


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
    monkeypatch.setattr(cli_module, "ChemExService", FakeService)

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
    run_dir.mkdir()
    monkeypatch.setattr(cli_module, "ChemExService", FakeService)

    result = CliRunner().invoke(main, ["cancel", str(run_dir)])

    assert result.exit_code == 0
    assert "Status: cancelled" in result.output


def test_review_apply_requires_confirmed_by(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    corrections = tmp_path / "corrections.json"
    _ = corrections.write_text(json.dumps([]), encoding="utf-8")

    result = CliRunner().invoke(main, ["review-apply", str(run_dir), str(corrections)])

    assert result.exit_code != 0
    assert "--confirmed-by" in result.output
