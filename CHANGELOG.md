# Changelog

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
