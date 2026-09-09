# CLI contract

The single command surface every host agent calls. Prefer `chemex-lit`;
if the console script is not on PATH, fall back to
`python -m chemex_lit.cli` with identical arguments. Always add `--json`
on machine-facing commands and parse the JSON, never the human text.

## Modes

Exactly three canonical modes exist. Every manifest stores one of them.

| Mode | Channels fulfilled by CLI models | Channels you fulfill |
| --- | --- | --- |
| `auto` | text, table, structure, adjudication | none |
| `semi` | text, table, adjudication | structure |
| `agent` | none | text, table, structure, adjudication |

| Deprecated alias | Canonical replacement |
| --- | --- |
| `human-ocsr-agent` (deprecated alias) | `semi` |
| `auto-agent` (deprecated alias) | `agent` |

`--mode` still accepts the alias values; they are normalized before the
run starts and only the canonical value is persisted.

## Six standard commands

```text
chemex-lit check --mode agent --json
chemex-lit run paper.pdf --mode auto --json
chemex-lit status <run_dir> --json
chemex-lit submit <run_dir> submission.jsonl --json
chemex-lit resume <run_dir> --json
chemex-lit review <run_dir>
```

Auxiliary commands (human-facing, not part of the agent loop):
`cancel <run_dir> [--json]`, `review-apply <run_dir> corrections.json
--confirmed-by "<human name>"`, `evaluate <run_dir> --gold benchmark.jsonl`,
`models list|show|check`, and the credential commands below.

## Credential store

Credentials resolve **environment first, then `auth.json`**; the store is
`auth.json` under the OS config directory (never inside a repository,
always mode 0600). `models.yaml` only references credential *names*
(`api_key_env`), never values.

```text
chemex-lit auth set NAME            # hidden prompt; --stdin to read from a pipe
chemex-lit auth set NAME --expires 2026-10-03
chemex-lit auth list [--json]       # masked values with expiry status
chemex-lit auth remove NAME
chemex-lit auth test NAME           # live probe against the endpoint; exit 2 on rejection
```

Set `CHEMEX_AUTH_STORE` to relocate the store file (used by tests and
multi-profile setups). Prefer `auth test` after refreshing a token: it
surfaces an expired credential immediately instead of failing mid-run.

`run` flags: `--output-dir DIR`, `--mode MODE`, `--structures FILE.jsonl`,
`--adjudicate`, `--json`.

`submit` flags: `--kind candidates|adjudications`, `--resume` (resume
immediately when ready), `--json`. Never use `--force`.

## check output

```json
{
  "ok": true,
  "mode": "agent",
  "profile": null,
  "checks": [
    {"name": "RDKit", "status": "ok"},
    {"name": "MinerU key", "status": "ok", "source": "env", "expires_in_days": null}
  ]
}
```

Each check item has `name` and `status`. RDKit items carry exactly those
two keys. Credential items additionally carry `source` (`env` or
`auth-store`) and `expires_in_days` (`null` when unknown). `status` is:

- `ok` — credential present and usable.
- `expiring` — still usable but expires in fewer than 7 days.
- `missing` — absent, or a stored credential whose `expires_at` has passed.

In `auto` and `semi` the CLI also checks the text and vision model keys; in
`agent` mode only RDKit and the MinerU key are required. A non-zero exit
code (2) means at least one check is `missing`.

## run / status / resume output

All three commands emit the same envelope (`cancel --json` too, and
`submit --json --resume` embeds it as `resume`):

```json
{
  "run_id": "paper-a1b2c3d4",
  "status": "awaiting_input",
  "records_count": 0,
  "review_count": 0,
  "run_dir": "/work/extraction/outputs/paper-a1b2c3d4",
  "stages": {"document": "complete", "extraction": "awaiting"},
  "awaiting": ["st-0001", "st-0002"],
  "tasks": {
    "structure": {"awaiting": 2, "fulfilled": 0, "awaiting_task_ids": ["st-0001", "st-0002"]}
  }
}
```

`awaiting` lists every task ID still waiting for a submission. `run_dir`
is an absolute path with forward slashes on every platform; join relative
artifact paths with the local path library (`Path(run_dir) / "tasks/extraction.jsonl"`).

## submit output

```json
{
  "status": "ready",
  "applied": 2,
  "awaiting": [],
  "files": [{"file": "submission.jsonl", "sha256": "abcdef1234567890", "status": "applied"}],
  "duplicates": 0
}
```

`status` becomes `ready` when no tasks remain `awaiting`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Unhandled runtime error. |
| 2 | Usage error, or `check` found missing requirements. |
| 4 | `run` finished as `completed_empty` (zero records; normal outcome). |

## Path rules

Every path inside manifests, protocol files, and JSON output uses
forward slashes, including on Windows. Relative paths such as
`tasks/extraction.jsonl` or `raw/page-003-image-001.png` are relative to
`run_dir`. Convert to a local path only at the moment you open the file
(`Path(run_dir) / value`). Never write backslash paths into submissions
or protocol files.

## Run statuses

`running | awaiting_input | ready | success | completed_empty | partial | failed | cancelled`

## Run artifact layout

```text
<run_dir>/
├── manifest.json            Run metadata, mode, producer_plan, config_sha256
├── document.json            Parsed document (MinerU output)
├── evidence.jsonl           Evidence references with asset paths
├── tasks/
│   ├── extraction.jsonl     Extraction tasks (text/table/structure)
│   ├── adjudication.jsonl   Adjudication tasks (when applicable)
│   └── state.json           Task fulfillment state
├── candidates/
│   ├── reactions.jsonl      Reaction candidates
│   ├── structures.jsonl     Structure candidates
│   └── provenance.jsonl     Provenance sidecar for every candidate
├── validation/              Validation issues and validated candidates
├── records.jsonl            Final records (schema_version: "1.0")
├── review.html              Human-readable review page
├── records.corrected.jsonl  Corrected records (after review-apply)
└── provenance/submissions/  Accepted submission files
```

## Task file

Read `<run_dir>/tasks/extraction.jsonl` when status is `awaiting_input`.
Each line is one task:

```json
{
  "task_id": "st-0001",
  "kind": "structure",
  "instruction_version": "chemex-instructions@1",
  "output_schema_version": "ReactionCandidate@1",
  "evidence_ids": ["ev-041"],
  "input_artifacts": ["document.json"],
  "assets": {
    "text": "...",
    "images": [{"evidence_id": "ev-041", "path": "raw/table_003.png"}]
  },
  "status": "awaiting"
}
```

## Manifest

```json
{
  "run_id": "paper-a1b2c3d4",
  "chemex_version": "1.0.0",
  "schema_version": "1.0",
  "mode": "agent",
  "profile": "codex-diverse",
  "models_source": "profile:codex-diverse",
  "producer_plan": {
    "text": {"kind": "host_agent", "policy": "codex-text", "model": "gpt-5"},
    "table": {"kind": "host_agent"},
    "structure": {"kind": "host_agent", "policy": "codex-structure"},
    "adjudication": {"kind": "host_agent", "policy": "codex-reasoning"}
  },
  "created_at": "2026-09-03T12:00:00Z",
  "status": "awaiting_input"
}
```

## Provenance

The `candidates/provenance.jsonl` sidecar traces every candidate back to
its origins using flat client fields:

```json
{
  "candidate_id": "cand-a1b2c3",
  "task_id": "st-0001",
  "channel": "structure",
  "producer_kind": "host_agent",
  "provider": null,
  "model": "gpt-5",
  "policy": "codex-structure",
  "attempt": 1,
  "prompt_version": "structure@3",
  "instruction_version": "chemex-instructions@1",
  "input_hash": "sha256:...",
  "submission_hash": "sha256:...",
  "client_name": "codex",
  "client_version": "1.0.0",
  "created_at": "2026-09-03T12:05:00Z"
}
```

## Environment variables

| Variable | Required? | Purpose |
| --- | --- | --- |
| `MINERU_API_KEY` | Always | MinerU cloud document parsing. |
| `CHEMEX_TEXT_API_KEY` | auto/semi | Text extraction model endpoint. |
| `CHEMEX_VISION_API_KEY` | auto/semi | Vision model for table and structure images. |
| `CHEMEX_REASONING_API_KEY` | auto/semi with `--adjudicate` | Reasoning model for adjudication; falls back to the text model with a warning. |
| Host credentials | agent mode | Managed by the host application, not ChemEx-Lit. |

Advanced overrides (optional): `CHEMEX_TEXT_BASE_URL`,
`CHEMEX_VISION_BASE_URL`, `CHEMEX_REASONING_BASE_URL`.

## Config

The CLI reads configuration from packaged defaults, an optional user
`models.yaml` profile file, and environment variables. Use
`chemex-lit check --show-config` to display the effective configuration.
