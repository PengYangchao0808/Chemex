from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

import chemex_lit.store as store_module
from chemex_lit.errors import ArtifactError
from chemex_lit.models import (
    GoldComparison,
    GoldSource,
    ReactionRecord,
    ReviewContext,
    ReviewDecision,
    ReviewParticipant,
    ReviewSubmission,
    StructureCandidate,
)
from chemex_lit.store import ArtifactStore, record_content_hash, sha256_file, sha256_text


def _initialise_store(
    store: ArtifactStore,
    input_file: Path,
    config_dump: Mapping[str, object],
    config_sha256: str | None = None,
) -> None:
    payload = dict(config_dump)
    store.initialise(
        run_id="r",
        input_path=input_file,
        input_sha256="a",
        config_dump=payload,
        config_sha256=config_sha256 or _config_sha256(config_dump),
        version="1",
        prompt_versions={},
        models={},
    )


def _config_sha256(config_dump: Mapping[str, object]) -> str:
    return sha256_text(json.dumps(config_dump, sort_keys=True, ensure_ascii=False))


def test_hash_helpers(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    _ = path.write_text("abc", encoding="utf-8")
    assert sha256_file(path) == sha256_text("abc")


def test_atomic_jsonl_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    rows = [StructureCandidate(candidate_id="s1", compound_label="1", smiles="CC")]
    _ = store.write_jsonl("candidates/structures.jsonl", rows)
    assert store.read_models("candidates/structures.jsonl", StructureCandidate) == rows


def test_stage_cache_requires_matching_hash_and_output(tmp_path: Path) -> None:
    input_file = tmp_path / "paper.pdf"
    _ = input_file.write_bytes(b"pdf")
    store = ArtifactStore(tmp_path / "run")
    _initialise_store(store, input_file, {"models": {"text": {"model": "a"}}}, config_sha256="b")
    _ = store.write_json("document.json", {"ok": True})
    store.mark_stage("document", status="complete", input_hash="a", output="document.json")
    assert store.stage_complete("document", "a", "document.json")
    assert not store.stage_complete("document", "different", "document.json")


def test_artifact_path_cannot_escape_run(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError):
        _ = ArtifactStore(tmp_path / "run").write_json("../outside.json", {})


def test_initialise_stores_config_and_allows_identical_resume(tmp_path: Path) -> None:
    input_file = tmp_path / "paper.pdf"
    _ = input_file.write_bytes(b"pdf")
    config_dump = {"models": {"text": {"model": "alpha"}}, "mineru": {"backend": "cloud"}}
    store = ArtifactStore(tmp_path / "run")

    _initialise_store(store, input_file, config_dump)
    manifest = store.manifest()

    assert manifest["config"] == config_dump
    _initialise_store(store, input_file, config_dump)


def test_initialise_raises_with_dotted_paths_for_changed_config(tmp_path: Path) -> None:
    input_file = tmp_path / "paper.pdf"
    _ = input_file.write_bytes(b"pdf")
    store = ArtifactStore(tmp_path / "run")
    _initialise_store(
        store,
        input_file,
        {
            "mineru": {"backend": "cloud"},
            "models": {"text": {"model": "alpha"}, "vision": {"model": "vision-a"}},
        },
    )

    with pytest.raises(ArtifactError, match="Configuration changed since run creation") as exc_info:
        _initialise_store(
            store,
            input_file,
            {
                "mineru": {"backend": "local"},
                "models": {"text": {"model": "beta"}, "vision": {"model": "vision-a"}},
            },
        )

    message = str(exc_info.value)
    assert "mineru.backend" in message
    assert "models.text.model" in message


def test_initialise_rejects_legacy_manifest_without_config(tmp_path: Path) -> None:
    input_file = tmp_path / "paper.pdf"
    _ = input_file.write_bytes(b"pdf")
    config_dump = {"models": {"text": {"model": "alpha"}}}
    store = ArtifactStore(tmp_path / "run")
    _ = store.write_json(
        "manifest.json",
        {
            "run_id": "r",
            "created_at": "2026-01-01T00:00:00+00:00",
            "chemex_version": "1",
            "schema_version": "1.0",
            "input_path": str(input_file.resolve()),
            "input_sha256": "a",
            "config_sha256": _config_sha256(config_dump),
            "prompt_versions": {},
            "models": {},
            "status": "running",
            "stages": {},
        },
    )

    with pytest.raises(ArtifactError, match="predates v1 config tracking"):
        _initialise_store(store, input_file, config_dump)


def test_config_diff_reports_sorted_dotted_paths() -> None:
    diff = cast(Callable[[object, object], list[str]], getattr(store_module, "_config_diff"))
    old = {
        "alpha": 1,
        "nested": {"dict": {"value": "same"}, "list": ["a", "b"], "type": {"leaf": 1}},
    }
    new = {
        "alpha": 2,
        "nested": {"dict": {"value": "changed"}, "list": ["a", "c"], "type": ["leaf", 1]},
    }

    assert diff(old, new) == [
        "alpha",
        "nested.dict.value",
        "nested.list",
        "nested.type",
    ]


def test_record_content_hash_is_deterministic(tmp_path: Path) -> None:
    record = ReactionRecord(
        reaction_id="rxn-1",
        reactants=[],
        products=[],
        confidence=0.5,
        review_status="accepted",
    )
    first = record_content_hash(record)
    second = record_content_hash(record)
    assert first == second
    assert len(first) == 64


def test_record_content_hash_differs_for_different_records() -> None:
    a = ReactionRecord(reaction_id="a", confidence=0.5, review_status="accepted")
    b = ReactionRecord(reaction_id="b", confidence=0.5, review_status="accepted")
    assert record_content_hash(a) != record_content_hash(b)


def _make_context(reaction_id: str = "rxn-1") -> ReviewContext:
    return ReviewContext(
        reaction_id=reaction_id,
        record_hash="abc123",
        participants=[
            ReviewParticipant(
                participant_id=f"{reaction_id}:reactant:1",
                role="reactant",
                label="1",
                smiles="CC",
                structure_state="resolved",
            ),
        ],
        reaction_evidence_ids=["e1"],
    )


def test_write_read_review_contexts_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    contexts = [_make_context("rxn-1"), _make_context("rxn-2")]
    _ = store.write_review_contexts(contexts)
    loaded = store.read_review_contexts()
    assert len(loaded) == 2
    assert loaded[0].reaction_id == "rxn-1"
    assert loaded[1].reaction_id == "rxn-2"
    assert loaded[0].participants[0].participant_id == "rxn-1:reactant:1"


def test_read_review_contexts_returns_empty_when_absent(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    assert store.read_review_contexts() == []


def _make_decision(reaction_id: str = "rxn-1", conclusion: str = "confirmed") -> ReviewDecision:
    return ReviewDecision(
        decision_id=f"d-{reaction_id}",
        reaction_id=reaction_id,
        target_kind="reaction",
        target_id=reaction_id,
        conclusion=conclusion,
        reviewer="tester",
        record_hash="abc",
        created_at="2026-01-01T00:00:00Z",
    )


def test_append_read_review_decisions_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    _ = store.append_review_decisions([_make_decision("rxn-1")])
    _ = store.append_review_decisions([_make_decision("rxn-2")])
    loaded = store.read_review_decisions()
    assert len(loaded) == 2
    assert loaded[0].reaction_id == "rxn-1"
    assert loaded[1].reaction_id == "rxn-2"


def test_read_review_decisions_returns_empty_when_absent(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    assert store.read_review_decisions() == []


def _make_gold_comparison(reaction_id: str = "rxn-1") -> GoldComparison:
    return GoldComparison(
        reaction_id=reaction_id,
        alignment="mapped",
        gold_source=GoldSource(
            file_name="gold.jsonl",
            file_hash="abc",
            entry_count=1,
            gold_schema_version="1.0",
        ),
    )


def test_write_read_gold_comparisons_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    comparisons = [_make_gold_comparison("rxn-1"), _make_gold_comparison("rxn-2")]
    _ = store.write_gold_comparisons(comparisons)
    loaded = store.read_gold_comparisons()
    assert len(loaded) == 2
    assert loaded[0].reaction_id == "rxn-1"
    assert loaded[1].reaction_id == "rxn-2"


def test_read_gold_comparisons_returns_empty_when_absent(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    assert store.read_gold_comparisons() == []


def _make_submission(submission_id: str = "sub-1") -> ReviewSubmission:
    return ReviewSubmission(
        submission_id=submission_id,
        reviewer="tester",
        created_at="2026-01-01T00:00:00Z",
    )


def test_persist_review_submission_writes_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    submission = _make_submission("sub-1")
    path = store.persist_review_submission(submission)
    assert path.is_file()
    loaded = ReviewSubmission.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.submission_id == "sub-1"


def test_persist_review_submission_noop_on_identical_content(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    submission = _make_submission("sub-1")
    path1 = store.persist_review_submission(submission)
    path2 = store.persist_review_submission(submission)
    assert path1 == path2


def test_persist_review_submission_raises_on_content_mismatch(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    store.persist_review_submission(_make_submission("sub-1"))
    different = ReviewSubmission(
        submission_id="sub-1",
        reviewer="other-reviewer",
        created_at="2026-06-01T00:00:00Z",
    )
    with pytest.raises(ArtifactError, match="already exists"):
        store.persist_review_submission(different)
