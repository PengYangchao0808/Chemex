# CLI contracts

## Command surface

```
chemex-lit run <pdf> [--mode auto|semi|agent] [--structures jsonl]
              [--adjudicate] [--json]
chemex-lit submit <run_dir> <submission.jsonl>...
              [--kind candidates|adjudications] [--force] [--resume]
chemex-lit resume <run_dir>
chemex-lit status <run_dir> [--json]
chemex-lit cancel <run_dir>
chemex-lit review <run_dir>
chemex-lit review-apply <run_dir> <corrections.json>
              --confirmed-by "<name>"
chemex-lit evaluate <run_dir> --gold <benchmark.jsonl>
chemex-lit check [--show-config]
```

## Run statuses

```
running | awaiting_input | ready | success | completed_empty
| partial | failed | cancelled
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Command completed successfully. |
| 1 | Validation error in arguments or input files. |
| 2 | Runtime error (pipeline failure, network error). |
| 3 | Configuration error (missing key, invalid config). |
| 4 | Run not found or in wrong state for the command. |

## Run artifact layout

Every run creates a directory named `outputs/<paper-slug>-<hash>/`.

```
<run_dir>/
├── manifest.json            Run metadata, producer_plan, config_sha256
├── document.json            Parsed document (MinerU output)
├── evidence.jsonl           Evidence references with asset paths
├── tasks/
│   ├── extraction.jsonl     Extraction tasks (text/table/structure)
│   ├── adjudication.jsonl   Adjudication tasks (when applicable)
│   └── instructions/        Task instructions by version
├── candidates/
│   ├── text.jsonl           Text extraction candidates
│   ├── table.jsonl          Table extraction candidates
│   ├── structure.jsonl      Structure extraction candidates
│   └── provenance.jsonl     Provenance sidecar for every candidate
├── validation/
│   ├── issues.jsonl         Validation issues per candidate
│   └── valid.jsonl          Validated candidates
├── records.jsonl            Final records (schema_version: "1.0")
├── review.html              Human-readable review page
├── review.jsonl             Machine-readable review queue
├── records.corrected.jsonl  Corrected records (after review-apply)
├── evaluation.json          Evaluation metrics (after evaluate)
└── provenance/
    └── submissions/         Submission files accepted and persisted
```

### Manifest

The manifest (`manifest.json`) records configuration, timestamps, and
producer assignments:

```json
{
  "run_id": "a1b2c3d4",
  "config_sha256": "abc...",
  "pipeline_version": "1.0.0a1",
  "producer_plan": {
    "text": {"kind": "cli_model", "model": "deepseek-v4-flash"},
    "table": {"kind": "cli_model", "model": "glm-4v-plus"},
    "structure": {"kind": "host_agent"},
    "adjudication": {"kind": "host_agent"}
  },
  "created_at": "2026-07-17T12:00:00Z",
  "status": "awaiting_input",
  "modes": {"run": "agent", "actual_channels": {...}}
}
```

### Provenance

The `provenance.jsonl` sidecar traces every candidate back to its
origins:

```json
{
  "candidate_id": "cand-a1b2c3",
  "task_id": "st-0001",
  "channel": "structure",
  "producer_kind": "host_agent",
  "provider": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "prompt_version": "structure@3",
  "instruction_version": "chemex-instructions@1",
  "input_hash": "sha256:...",
  "submission_hash": "sha256:...",
  "client": {"name": "codex", "version": "0.2.0"},
  "created_at": "2026-07-17T12:05:00Z"
}
```

The provenance chain answers: what was submitted, by whom, with what
model, based on what input.

## Environment variables

| Variable | Required? | Purpose |
| --- | --- | --- |
| `MINERU_API_KEY` | Always | MinerU cloud document parsing. |
| `CHEMEX_TEXT_API_KEY` | Auto/semi text | Text extraction model endpoint. |
| `CHEMEX_VISION_API_KEY` | Auto/semi vision | Vision model for table and structure images. |
| `CHEMEX_REASONING_API_KEY` | Auto/semi adjudication | Reasoning model for adjudication. Optional: falls back to text model with warning. |

Advanced overrides (optional):

| Variable | Purpose |
| --- | --- |
| `CHEMEX_TEXT_BASE_URL` | Override text model base URL. |
| `CHEMEX_VISION_BASE_URL` | Override vision model base URL. |
| `CHEMEX_REASONING_BASE_URL` | Override reasoning model base URL. |

## Config

The CLI reads configuration from the packaged defaults (merged at build
time). Use `chemex-lit check --show-config` to display the effective
configuration. Environment variables override config values at runtime.
