from __future__ import annotations

from pathlib import Path

import pytest

from chemex_lit.errors import ArtifactError
from chemex_lit.models import StructureCandidate
from chemex_lit.store import ArtifactStore, sha256_file, sha256_text


def test_hash_helpers(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("abc", encoding="utf-8")
    assert sha256_file(path) == sha256_text("abc")


def test_atomic_jsonl_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    rows = [StructureCandidate(candidate_id="s1", compound_label="1", smiles="CC")]
    store.write_jsonl("candidates/structures.jsonl", rows)
    assert store.read_models("candidates/structures.jsonl", StructureCandidate) == rows


def test_stage_cache_requires_matching_hash_and_output(tmp_path: Path) -> None:
    input_file = tmp_path / "paper.pdf"
    input_file.write_bytes(b"pdf")
    store = ArtifactStore(tmp_path / "run")
    store.initialise(
        run_id="r",
        input_path=input_file,
        input_sha256="a",
        config_sha256="b",
        version="1",
        prompt_versions={},
        models={},
    )
    store.write_json("document.json", {"ok": True})
    store.mark_stage("document", status="complete", input_hash="a", output="document.json")
    assert store.stage_complete("document", "a", "document.json")
    assert not store.stage_complete("document", "different", "document.json")


def test_artifact_path_cannot_escape_run(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path / "run").write_json("../outside.json", {})
