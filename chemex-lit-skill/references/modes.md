# Modes

## Channel fulfillment by mode

| Channel | Auto | Semi | Agent |
| --- | --- | --- | --- |
| PDF parsing | MinerU cloud service | MinerU cloud service | MinerU cloud service |
| Text extraction | CLI model (models.text) | CLI model (models.text) | Host agent |
| Table extraction | CLI vision model (models.vision) | CLI vision model (models.vision) | Host agent (vision) |
| Structure extraction | CLI vision model (models.vision) | Human or agent submit | Host agent (vision) |
| Validation / assembly | Deterministic (RDKit) | Deterministic (RDKit) | Deterministic (RDKit) |
| Adjudication | CLI reasoning model (models.reasoning) | CLI reasoning model (models.reasoning) | Host agent |
| Finalization | Template + metrics | Template + metrics | Template + metrics |

The producer plan for each mode is written into `manifest.json` at run
start and participates in stage fingerprints.

### Auto (default)

All generative channels are fulfilled by CLI-configured models. No
external submissions needed. The run progresses without pausing unless
a task fails validation.

Use auto for: CI pipelines, batch processing, reproducible extraction
runs, or any scenario where you have API keys for all four model slots.

### Semi

The CLI handles text and table extraction. Structure tasks are handed
to an external submitter (human or host agent). The pipeline pauses after
text and table extraction, producing structure tasks in
`tasks/extraction.jsonl`. After submission the CLI resumes and runs
adjudication with its reasoning model.

Semi is the default when you pass `--structures` or when
`CHEMEX_REASONING_API_KEY` is set without `CHEMEX_VISION_API_KEY` for
structure tasks.

### Agent

The CLI only handles PDF parsing (MinerU), validation, assembly, and
finalization. All generative channels are fulfilled by the host agent.
The pipeline pauses at every stage boundary, producing task files.
The host agent fulfills each task set and submits. After the final
submission set, the CLI runs validation, assembly, and finalization.

Use agent mode when:
- You do not have CLI model API keys (only MinerU key needed).
- You want full control over extraction and adjudication prompts.
- You are orchestrating from an agent host with its own model access.

## State machine

```
                   submit (validate + persist)
running ──► awaiting_input ──────────────────► ready ──► running
           (tasks created)       │                        ▲
                                 │                        │
                                 └── submit + --resume ───┘
                                         (explicit combo)

running ──► success / completed_empty / partial
running ──► failed
running ──► cancelled (explicit, never automatic)
```

Transitions:
- `running` to `awaiting_input`: Core created task files and needs
  external fulfillment.
- `awaiting_input` to `ready`: `submit` validates and persists
  submissions. The pipeline does not advance.
- `ready` to `running`: `resume` advances the pipeline to the next
  stage.
- `awaiting_input` direct to `running`: `submit --resume` offered as
  combined convenience. Prefer explicit two-step for clarity.
- `running` to `success|completed_empty|partial`: Terminal success
  states.
- `running` to `failed`: Non-recoverable error.
- `running` to `cancelled`: Explicit `chemex-lit cancel`. Never
  automatic.

### What awaiting_input means

The pipeline has reached a stage boundary where external task
fulfillment is required. The run directory contains:

- `tasks/extraction.jsonl` One or more ExtractionTask entries with
  status `awaiting`.
- `tasks/adjudication.jsonl` (if in agent mode) Adjudication tasks
  with status `awaiting`.

The run is paused and will not advance until:
1. All required tasks are submitted (`submit`), AND
2. `resume` is called.

You can inspect which tasks remain by re-running `status` and looking
at the task file. Partial submissions are valid. The status changes to
`ready` only when all tasks are fulfilled.

### Resume semantics

`resume` does not resubmit. It checks that all required tasks are
fulfilled, then advances the pipeline to the next processing stage.
If fingerprint matching finds valid cached artifacts from a prior
stage, it reuses them.

`resume` fails if:
- Any required task remains `awaiting`.
- The configuration has changed since the run was created (manifest
  checksum mismatch).

### Cancel semantics

`cancel` sets the run status to `cancelled`. This is a terminal state.
Cancelled runs cannot be resumed. Cancel is explicit. A run never
transitions to cancelled due to timeout or inactivity.
