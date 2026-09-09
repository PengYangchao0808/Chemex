# Changelog

## Unreleased

## 0.0.0 - 2026-09-09

First team development baseline for collaborative GitHub development. The
package release version is independent of the internal "v1 core" naming and
the `records.jsonl` `schema_version: "1.0"` data contract; pre-1.0 versions
may still break the public surface with documented CHANGELOG entries.

### Changed

- Consolidated the 26-module v1 core to 20 structural units. The workflow
  layer in `chemex_lit.application` was dissolved into public pipeline
  functions (`run_pdf`, `resume_run`, `submit_files`, `run_status`); the
  CLI is a thin shell over them and `cancel` writes terminal state through
  `ArtifactStore` inline.
- Flattened single-purpose packages into modules: `llm/`, `chemistry/`,
  and `evaluation/` are now `llm.py`, `chemistry.py`, and `evaluation.py`;
  the text/table/structure extractors live in `extraction/extractors.py`.
  Submodule import paths such as `chemex_lit.chemistry.validate` no longer
  exist; import the top-level module instead. Python modules other than
  `chemex_lit.models` are implementation details.
- Dropped the deprecated `--mode` aliases `human-ocsr-agent` and
  `auto-agent`; only `auto`, `semi`, and `agent` are accepted, and
  `normalize_mode` rejects anything else.
- Deterministic helpers now persist through the artifact store: review
  generation takes the `ArtifactStore`, `render_smiles` returns PNG bytes,
  and evaluation metrics accept an optional store.

### Migration notes

- Run directories created before this change are not resumable. Manifests
  that record an alias mode or lack the `config` dict (pre-P0A metadata)
  are rejected with an explicit error that names the cause; start a fresh
  run with the same input instead.

## 1.0.0a1 - 2026-07-17

### Changed

- Replaced the pre-v1 89-module package with a 26-file `src`-layout core.
- Renamed the distribution, import package, and CLI to avoid the existing PyPI `chemex` project.
- Replaced automatic and semi-manual orchestrators with one pipeline and external structure input.
- Replaced backend routing with one explicit OpenAI-compatible client.
- Replaced G1-G4 and repair subsystems with one deterministic validation report.
- Replaced Oracle record generation with deterministic assembly and optional read-only adjudication.
- Replaced multiple review outputs with one HTML dashboard and correction format.

### Added

- Stable `ReactionRecord` schema version 1.0.
- Atomic artifact store with input-hash resume behavior.
- Packaged prompts, default configuration, and review template.
- Python 3.11+ test, lint, type, build, and package checks.
