"""Tests for task-bounded contract models and store helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from chemex_lit.models import (
    AdjudicationDecision,
    CandidateSubmission,
    ExtractionTask,
    ProducerPlan,
    ProducerSpec,
    ProvenanceEntry,
    RunRequest,
    RunSummary,
    SubmissionProducer,
    TaskAssets,
    TaskImageAsset,
)
from chemex_lit.store import ArtifactStore


@pytest.mark.parametrize(
    ("model", "payload", "extra_field"),
    [
        (ProducerSpec, {"kind": "cli_model", "model": "m", "provider": "p"}, "unexpected"),
        (
            ProducerPlan,
            {
                "text": {"kind": "cli_model", "model": "text-model"},
                "table": {"kind": "cli_model", "model": "vision-model"},
                "structure": {"kind": "human"},
                "adjudication": {"kind": "host_agent", "provider": "host"},
            },
            "unexpected",
        ),
        (
            TaskImageAsset,
            {"path": "raw/image.png", "evidence_id": "ev-1", "context": "Figure 1"},
            "unexpected",
        ),
        (
            TaskAssets,
            {
                "text": "inline text",
                "images": [{"path": "raw/image.png", "evidence_id": "ev-1"}],
            },
            "unexpected",
        ),
        (
            ExtractionTask,
            {
                "task_id": "st-0001",
                "kind": "text",
                "instruction_version": "chemex-instructions@1",
                "instructions": "extract reactions",
                "output_schema_version": "ReactionCandidate@1",
                "evidence_ids": ["ev-1"],
                "input_artifacts": ["document.json"],
                "assets": {"text": "body", "images": []},
                "status": "awaiting",
            },
            "unexpected",
        ),
        (
            SubmissionProducer,
            {"kind": "human", "client_name": "codex", "client_version": "1.0"},
            "unexpected",
        ),
        (
            CandidateSubmission,
            {
                "task_id": "st-0001",
                "producer": {"kind": "host_agent", "client_name": "codex", "client_version": "1.0"},
                "outputs": [{"source": "text", "reactants": [], "products": []}],
            },
            "unexpected",
        ),
        (
            AdjudicationDecision,
            {
                "task_id": "ad-0001",
                "reaction_id": "rxn-1",
                "decision": "accept",
                "rationale": "evidence is sufficient",
                "producer": {"kind": "human", "client_name": "review-ui"},
            },
            "unexpected",
        ),
        (
            ProvenanceEntry,
            {
                "candidate_id": "cand-1",
                "task_id": "st-0001",
                "channel": "structure",
                "producer_kind": "human",
                "provider": "review-ui",
                "model": "n/a",
                "prompt_version": "structure@3",
                "instruction_version": "chemex-instructions@1",
                "input_hash": "sha256:input",
                "submission_hash": "sha256:submission",
                "client_name": "codex",
                "client_version": "1.0",
                "created_at": "2026-07-17T00:00:00Z",
            },
            "unexpected",
        ),
    ],
)
def test_new_models_validate_and_forbid_extra(
    model: type[Any], payload: dict[str, Any], extra_field: str
) -> None:
    instance = model.model_validate(payload)
    assert instance.model_dump(mode="json", exclude_none=True) == payload

    with pytest.raises(ValidationError):
        model.model_validate({**payload, extra_field: True})


def test_candidate_submission_rejects_candidate_id_in_outputs() -> None:
    with pytest.raises(ValidationError, match="candidate_id"):
        CandidateSubmission.model_validate(
            {
                "task_id": "st-0001",
                "producer": {"kind": "human"},
                "outputs": [{"candidate_id": "cand-1", "smiles": "CCO"}],
            }
        )


def test_run_request_defaults_to_auto_mode() -> None:
    request = RunRequest.model_validate(
        {"pdf_path": "paper.pdf", "output_dir": "outputs/run-1", "resume": False}
    )

    assert request.mode == "auto"
    assert request.pdf_path == Path("paper.pdf")


def test_run_summary_accepts_all_status_values() -> None:
    statuses = [
        "running",
        "awaiting_input",
        "ready",
        "success",
        "completed_empty",
        "partial",
        "failed",
        "cancelled",
    ]

    for status in statuses:
        summary = RunSummary.model_validate(
            {
                "run_id": "run-1",
                "status": status,
                "records_count": 0,
                "review_count": 0,
                "output_dir": "outputs/run-1",
            }
        )
        assert summary.status == status


def test_producer_plan_channel_returns_configured_spec() -> None:
    plan = ProducerPlan.model_validate(
        {
            "text": {"kind": "cli_model", "model": "text-model"},
            "table": {"kind": "cli_model", "model": "vision-model"},
            "structure": {"kind": "human", "provider": "review-ui"},
            "adjudication": {"kind": "host_agent", "provider": "host"},
        }
    )

    spec = plan.channel("structure")

    assert spec == ProducerSpec(kind="human", provider="review-ui")


def test_provenance_round_trip_and_missing_file_returns_empty(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    missing = ArtifactStore(tmp_path / "missing")
    entries = [
        ProvenanceEntry(
            candidate_id="cand-1",
            task_id="st-0001",
            channel="text",
            producer_kind="cli_model",
            provider="https://api.example.test/v1",
            model="text-model",
            prompt_version="text@1",
            instruction_version="chemex-instructions@1",
            input_hash="sha256:input-1",
            submission_hash="sha256:submission-1",
            created_at="2026-07-17T00:00:00Z",
        ),
        ProvenanceEntry(
            candidate_id="cand-2",
            task_id="st-0002",
            channel="structure",
            producer_kind="human",
            client_name="codex",
            client_version="1.0",
            created_at="2026-07-17T00:00:01Z",
        ),
    ]

    store.write_provenance(entries)

    assert store.read_provenance() == entries
    assert missing.read_provenance() == []


def test_append_jsonl_creates_then_appends(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    relative = "candidates/submissions.jsonl"

    store.append_jsonl(relative, [{"task_id": "st-0001", "value": 1}])
    first_lines = (tmp_path / "run" / relative).read_text(encoding="utf-8").splitlines()
    assert len(first_lines) == 1

    store.append_jsonl(relative, [{"task_id": "st-0002", "value": 2}])
    all_lines = (tmp_path / "run" / relative).read_text(encoding="utf-8").splitlines()

    assert len(all_lines) == 2
    assert [json.loads(line) for line in all_lines] == [
        {"task_id": "st-0001", "value": 1},
        {"task_id": "st-0002", "value": 2},
    ]
