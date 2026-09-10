"""Atomic artifact persistence and hash-based stage resumption."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TypeVar

from pydantic import BaseModel

from chemex_lit.errors import ArtifactError
from chemex_lit.models import (
    GoldComparison,
    ProvenanceEntry,
    ReactionRecord,
    ReviewContext,
    ReviewDecision,
    ReviewSubmission,
)

T = TypeVar("T", bound=BaseModel)


def _config_diff(old: Any, new: Any, prefix: str = "") -> list[str]:
    if type(old) is not type(new):
        return [prefix or "<root>"]

    if isinstance(old, dict):
        differences: list[str] = []
        keys = sorted(set(old) | set(new))
        for key in keys:
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in old or key not in new:
                differences.append(path)
                continue
            differences.extend(_config_diff(old[key], new[key], path))
        return differences

    if isinstance(old, list):
        return [] if old == new else [prefix or "<root>"]

    return [] if old == new else [prefix or "<root>"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_content_hash(record: ReactionRecord) -> str:
    """Return the SHA-256 digest of a serialised ReactionRecord."""

    return sha256_text(record.model_dump_json())


REVIEW_CONTEXT_PATH = "review_context.jsonl"
REVIEW_DECISIONS_PATH = "review_decisions.jsonl"
GOLD_COMPARISON_PATH = "gold_comparison.jsonl"
REVIEW_SUBMISSIONS_DIR = "review_submissions"


class ArtifactStore:
    """Owns every persistent file produced by one pipeline run."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "candidates").mkdir(exist_ok=True)
        (self.root / "validation").mkdir(exist_ok=True)
        (self.root / "raw").mkdir(exist_ok=True)
        self.manifest_path = self.root / "manifest.json"

    def initialise(
        self,
        *,
        run_id: str,
        input_path: Path,
        input_sha256: str,
        config_dump: dict[str, Any],
        config_sha256: str,
        version: str,
        prompt_versions: dict[str, str],
        models: dict[str, str],
        mode: str = "auto",
        producer_plan: dict[str, Any] | None = None,
        profile: str | None = None,
        models_source: str = "default",
    ) -> None:
        plan_dump = producer_plan or {}
        if self.manifest_path.exists():
            manifest = self.manifest()
            stored_mode = manifest.get("mode")
            if isinstance(stored_mode, str) and stored_mode != mode:
                raise ArtifactError(
                    "Run mode changed since run creation: "
                    f"stored={stored_mode} requested={mode}. "
                    "Mode changes require a fresh run directory."
                )
            stored_plan = manifest.get("producer_plan")
            if stored_plan not in (None, plan_dump):
                raise ArtifactError(
                    "Producer plan changed since run creation. Producer changes "
                    "require a fresh run directory."
                )
            if manifest.get("profile") not in (None, profile):
                raise ArtifactError(
                    "Profile changed since run creation: "
                    f"stored={manifest.get('profile')!r} requested={profile!r}. "
                    "Profile changes require a fresh run directory."
                )
            stored_config = manifest.get("config")
            if not isinstance(stored_config, dict):
                raise ArtifactError(
                    "Run manifest predates v1 config tracking and cannot be resumed; "
                    "start a fresh run with the same input instead."
                )
            differences = _config_diff(stored_config, config_dump)
            if differences:
                changed = ", ".join(differences)
                raise ArtifactError(
                    "Configuration changed since run creation: "
                    f"{changed}. Changed config requires a fresh run directory."
                )
            return
        self.write_json(
            "manifest.json",
            {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "chemex_version": version,
                "schema_version": "1.0",
                "input_path": input_path.resolve().as_posix(),
                "input_sha256": input_sha256,
                "config": config_dump,
                "config_sha256": config_sha256,
                "prompt_versions": prompt_versions,
                "models": models,
                "mode": mode,
                "producer_plan": plan_dump,
                "profile": profile,
                "models_source": models_source,
                "status": "running",
                "stages": {},
            },
        )

    def manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            raise ArtifactError(f"Run manifest not found: {self.manifest_path}")
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ArtifactError(f"Invalid run manifest: {exc}") from exc

    def stage_complete(self, name: str, input_hash: str, output: str) -> bool:
        if not self.manifest_path.exists() or not (self.root / output).is_file():
            return False
        stage = self.manifest().get("stages", {}).get(name, {})
        return stage.get("status") == "complete" and stage.get("input_hash") == input_hash

    def mark_stage(
        self,
        name: str,
        *,
        status: str,
        input_hash: str,
        output: str,
        detail: str = "",
    ) -> None:
        manifest = self.manifest()
        manifest.setdefault("stages", {})[name] = {
            "status": status,
            "input_hash": input_hash,
            "output": output,
            "detail": detail,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.write_json("manifest.json", manifest)

    def finish(self, status: str) -> None:
        manifest = self.manifest()
        manifest["status"] = status
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.write_json("manifest.json", manifest)

    def set_status(self, status: str) -> None:
        """Update the manifest status without finalizing the run."""

        manifest = self.manifest()
        manifest["status"] = status
        self.write_json("manifest.json", manifest)

    def invalidate_stages(self, names: Iterable[str]) -> None:
        """Remove stored stage metadata so downstream stages recompute on resume."""

        manifest = self.manifest()
        stages = manifest.setdefault("stages", {})
        for name in names:
            stages.pop(name, None)
        self.write_json("manifest.json", manifest)

    def write_json(self, relative: str, data: Any) -> Path:
        path = self._path(relative)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        self._atomic_write(path, payload + "\n")
        return path

    def write_raw(self, relative: str, text: str) -> Path:
        """Write raw text (HTML, Markdown, ...) atomically inside the run directory."""

        path = self._path(relative)
        self._atomic_write(path, text)
        return path

    def write_bytes(self, relative: str, data: bytes) -> Path:
        """Write binary content (PNG, ...) atomically inside the run directory."""

        path = self._path(relative)
        self._atomic_write(path, data)
        return path

    def read_json(self, relative: str) -> Any:
        path = self._path(relative)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"Cannot read JSON artifact {path}: {exc}") from exc

    def write_jsonl(self, relative: str, rows: Iterable[BaseModel | dict[str, Any]]) -> Path:
        path = self._path(relative)
        lines: list[str] = []
        for row in rows:
            data = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
            lines.append(json.dumps(data, ensure_ascii=False))
        self._atomic_write(path, "\n".join(lines) + ("\n" if lines else ""))
        return path

    def append_jsonl(self, relative: str, rows: Iterable[BaseModel | dict[str, Any]]) -> Path:
        """Appends JSONL rows by atomically rewriting old and new content together."""

        path = self._path(relative)
        lines: list[str] = []
        if path.is_file():
            lines.extend(path.read_text(encoding="utf-8").splitlines())
        for row in rows:
            data = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
            lines.append(json.dumps(data, ensure_ascii=False))
        self._atomic_write(path, "\n".join(lines) + ("\n" if lines else ""))
        return path

    def write_provenance(self, entries: Iterable[ProvenanceEntry | dict[str, Any]]) -> Path:
        """Writes candidate provenance sidecar entries."""

        return self.write_jsonl("candidates/provenance.jsonl", entries)

    def read_provenance(self) -> list[ProvenanceEntry]:
        """Reads candidate provenance sidecar entries if present."""

        path = self._path("candidates/provenance.jsonl")
        if not path.is_file():
            return []
        return self.read_models("candidates/provenance.jsonl", ProvenanceEntry)

    def write_review_contexts(self, contexts: Iterable[ReviewContext]) -> Path:
        """Write review context sidecar entries."""

        return self.write_jsonl(REVIEW_CONTEXT_PATH, contexts)

    def read_review_contexts(self) -> list[ReviewContext]:
        """Read review context sidecar entries if present."""

        path = self._path(REVIEW_CONTEXT_PATH)
        if not path.is_file():
            return []
        return self.read_models(REVIEW_CONTEXT_PATH, ReviewContext)

    def append_review_decisions(self, decisions: Iterable[ReviewDecision]) -> Path:
        """Append review decision entries to the decisions sidecar."""

        return self.append_jsonl(REVIEW_DECISIONS_PATH, decisions)

    def read_review_decisions(self) -> list[ReviewDecision]:
        """Read review decision entries, returning an empty list when absent."""

        path = self._path(REVIEW_DECISIONS_PATH)
        if not path.is_file():
            return []
        return self.read_models(REVIEW_DECISIONS_PATH, ReviewDecision)

    def write_gold_comparisons(self, comparisons: Iterable[GoldComparison]) -> Path:
        """Write gold comparison sidecar entries."""

        return self.write_jsonl(GOLD_COMPARISON_PATH, comparisons)

    def read_gold_comparisons(self) -> list[GoldComparison]:
        """Read gold comparison sidecar entries, returning an empty list when absent."""

        path = self._path(GOLD_COMPARISON_PATH)
        if not path.is_file():
            return []
        return self.read_models(GOLD_COMPARISON_PATH, GoldComparison)

    def persist_review_submission(self, submission: ReviewSubmission) -> Path:
        """Persist a review submission package.

        Writes ``review_submissions/{submission_id}.json``.  If a file with
        the same name already exists and its content matches *submission* the
        call is a no-op.  If the existing content differs an
        :class:`ArtifactError` is raised so callers can handle deduplication.
        """

        relative = f"{REVIEW_SUBMISSIONS_DIR}/{submission.submission_id}.json"
        path = self._path(relative)
        new_content = submission.model_dump_json(indent=2)
        if path.is_file():
            existing = path.read_text(encoding="utf-8").rstrip("\n")
            if existing == new_content:
                return path
            raise ArtifactError(
                f"Review submission {submission.submission_id!r} already exists "
                "with different content"
            )
        self._atomic_write(path, new_content + "\n")
        return path

    def read_models(self, relative: str, model: type[T]) -> list[T]:
        path = self._path(relative)
        if not path.is_file():
            raise ArtifactError(f"Artifact not found: {path}")
        result: list[T] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                result.append(model.model_validate_json(line))
            except Exception as exc:
                raise ArtifactError(f"Invalid {path.name} line {number}: {exc}") from exc
        return result

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactError(f"Artifact path escapes run directory: {relative}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _atomic_write(path: Path, content: str | bytes) -> None:
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            if isinstance(content, bytes):
                temp.write_bytes(content)
            else:
                temp.write_text(content, encoding="utf-8")
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()


def atomic_write_text(path: Path, text: str) -> Path:
    """Atomically write text to a user-selected path outside any run directory.

    Run artifacts always go through :class:`ArtifactStore`; this helper exists
    for explicit user-directed exports (for example ``--output``) so the same
    temp-then-replace discipline still applies.
    """

    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return path
