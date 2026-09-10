from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner
import pytest

import chemex_lit.cli as cli_module
from chemex_lit.cli import main
from chemex_lit.config import AppConfig
from chemex_lit.errors import utc_now
from chemex_lit.models import (
    CompoundRef,
    ReactionRecord,
    RunSummary,
    TaskChannelStatus,
)
from chemex_lit.review import _record_hash
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


# ---------------------------------------------------------------------------
# Helpers for review / review-apply tests
# ---------------------------------------------------------------------------


def _sample_record() -> ReactionRecord:
    return ReactionRecord(
        reaction_id="r1",
        reactants=[CompoundRef(label="7", smiles="CC", role="reactant")],
        products=[CompoundRef(label="8", smiles="CCO", role="product")],
        confidence=0.9,
        review_status="accepted",
    )


def _write_run_dir(tmp_path: Path, rec: ReactionRecord | None = None) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    r = rec or _sample_record()
    (run_dir / "records.jsonl").write_text(
        json.dumps(r.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    return run_dir


def _write_gold_file(tmp_path: Path) -> Path:
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps(_sample_record().model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )
    return gold


def _write_submission_file(
    tmp_path: Path,
    *,
    reviewer: str = "Reviewer",
    submission_id: str = "sub-001",
    base_hashes: dict[str, str] | None = None,
) -> Path:
    rec = _sample_record()
    sub: dict[str, Any] = {
        "schema_version": "1.0",
        "submission_id": submission_id,
        "base_record_hashes": base_hashes or {"r1": _record_hash(rec)},
        "operations": [
            {
                "reaction_id": "r1",
                "target_kind": "reaction",
                "target_id": "r1",
                "op": "confirm",
            }
        ],
        "reviewer": reviewer,
        "created_at": utc_now(),
    }
    path = tmp_path / "submission.json"
    path.write_text(json.dumps(sub), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# review command tests
# ---------------------------------------------------------------------------


def test_review_prints_html_path(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    result = CliRunner().invoke(main, ["review", str(run_dir)])
    assert result.exit_code == 0
    assert "review.html" in result.output


def test_review_json_envelope_keys(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    result = CliRunner().invoke(main, ["review", str(run_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert set(payload) == {
        "run_dir",
        "review_html",
        "records_count",
        "human_status_counts",
        "gold",
    }
    assert payload["records_count"] == 1
    assert payload["run_dir"].replace("\\", "/") == run_dir.resolve().as_posix()
    assert payload["gold"] is None


def test_review_json_human_status_counts(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    result = CliRunner().invoke(main, ["review", str(run_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    counts = payload["human_status_counts"]
    assert isinstance(counts, dict)
    assert counts.get("unreviewed", 0) >= 1


def test_review_with_gold_option(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    gold = _write_gold_file(tmp_path)
    result = CliRunner().invoke(main, ["review", str(run_dir), "--gold", str(gold), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["gold"] is not None
    assert payload["gold"]["file_name"] == "gold.jsonl"
    assert payload["gold"]["entry_count"] == 1
    assert len(payload["gold"]["file_hash"]) == 64


def test_review_with_invalid_gold_exits_nonzero(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    bad_gold = tmp_path / "bad.jsonl"
    bad_gold.write_text("not json at all\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["review", str(run_dir), "--gold", str(bad_gold)])
    assert result.exit_code != 0


def test_review_loads_existing_gold_comparison(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    gold = _write_gold_file(tmp_path)
    CliRunner().invoke(
        main, ["review", str(run_dir), "--gold", str(gold)]
    )
    assert (run_dir / "gold_comparison.jsonl").is_file()
    result = CliRunner().invoke(main, ["review", str(run_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["gold"] is not None


# ---------------------------------------------------------------------------
# review-apply command tests
# ---------------------------------------------------------------------------


def test_review_apply_legacy_corrections_writes_corrected_and_review(
    tmp_path: Path,
) -> None:
    run_dir = _write_run_dir(tmp_path)
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "r1", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(corrections), "--confirmed-by", "Reviewer"],
    )

    assert result.exit_code == 0
    assert (run_dir / "records.corrected.jsonl").is_file()
    assert (run_dir / "audit.jsonl").is_file()
    assert (run_dir / "review.html").is_file()
    assert "corrected records" in result.output
    assert "audit log" in result.output
    assert "review dashboard" in result.output
    assert "original records preserved" in result.output
    assert "corrections entry" in result.output


def test_review_apply_submission_auto_detect(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path)
    submission = _write_submission_file(tmp_path)

    result = CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(submission), "--confirmed-by", "Reviewer"],
    )

    assert result.exit_code == 0
    assert (run_dir / "records.corrected.jsonl").is_file()
    assert (run_dir / "audit.jsonl").is_file()
    assert (run_dir / "review.html").is_file()
    assert "submission entry" in result.output


def test_review_apply_submission_version_conflict_exits_nonzero(
    tmp_path: Path,
) -> None:
    run_dir = _write_run_dir(tmp_path)
    submission = _write_submission_file(
        tmp_path,
        base_hashes={"r1": "wrong_hash"},
    )

    result = CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(submission), "--confirmed-by", "Reviewer"],
    )

    assert result.exit_code != 0
    assert "Version conflict" in result.output


def test_review_apply_legacy_unknown_reaction_exits_nonzero(
    tmp_path: Path,
) -> None:
    run_dir = _write_run_dir(tmp_path)
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps([{"reaction_id": "missing", "path": "yield_pct", "value": 81}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["review-apply", str(run_dir), str(corrections), "--confirmed-by", "Reviewer"],
    )

    assert result.exit_code != 0
    assert "unknown reactions" in result.output
