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

T = TypeVar("T", bound=BaseModel)


def _config_fingerprint(config_dump: dict[str, Any]) -> str:
    payload = json.dumps(config_dump, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    ) -> None:
        if self.manifest_path.exists():
            manifest = self.manifest()
            stored_config = manifest.get("config")
            if isinstance(stored_config, dict):
                differences = _config_diff(stored_config, config_dump)
                if differences:
                    changed = ", ".join(differences)
                    raise ArtifactError(
                        "Configuration changed since run creation: "
                        f"{changed}. Changed config requires a fresh run directory."
                    )
                return

            if manifest.get("config_sha256") != _config_fingerprint(config_dump):
                raise ArtifactError(
                    "Configuration changed since run creation. Legacy run metadata cannot "
                    "list changed keys; use a fresh run directory."
                )
            return
        self.write_json(
            "manifest.json",
            {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "chemex_version": version,
                "schema_version": "1.0",
                "input_path": str(input_path.resolve()),
                "input_sha256": input_sha256,
                "config": config_dump,
                "config_sha256": config_sha256,
                "prompt_versions": prompt_versions,
                "models": models,
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

    def write_json(self, relative: str, data: Any) -> Path:
        path = self._path(relative)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        self._atomic_write(path, payload + "\n")
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
    def _atomic_write(path: Path, content: str) -> None:
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(content, encoding="utf-8")
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
