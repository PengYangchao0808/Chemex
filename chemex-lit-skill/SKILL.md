---
name: chemex-lit
description: Extract traceable chemical reaction records from literature PDFs using auto, semi, or agent mode through the chemex-lit CLI.
---

# ChemEx-Lit Agent Skill

## When to use this skill

Use ChemEx-Lit when a task asks you to extract chemical reactions from a PDF
and produce structured records with evidence provenance. The skill orchestrates
the `chemex-lit` CLI. It does not contain extraction prompts, chemistry logic,
or LLM calls. The CLI and its Core library own all chemistry.

## Calling the CLI

Prefer the installed console script. If it is not on PATH, fall back to the
Python module; both accept identical arguments.

```text
chemex-lit <command> ...
python -m chemex_lit.cli <command> ...
```

Always pass `--json` on machine-facing commands and parse the JSON output.
Never parse human-readable text such as `Awaiting tasks: 2`; depend only on
JSON fields (`status`, `awaiting`, `run_dir`, `tasks`).

## Mode selection

Three modes control who fulfills each extraction channel.

| Mode | Text | Table | Structure | Adjudication | When to pick |
| --- | --- | --- | --- | --- | --- |
| auto | CLI model | CLI vision model | CLI vision model | CLI reasoning model | CI, unattended batch, reproducible runs, or when all API keys are set |
| semi | CLI model | CLI vision model | Host agent submits structures | CLI reasoning model | You want to supply or verify structures yourself |
| agent | Host agent | Host agent | Host agent | Host agent | Full host-agent control |

Credential requirements by mode:

- `auto`: `MINERU_API_KEY`, `CHEMEX_TEXT_API_KEY`, `CHEMEX_VISION_API_KEY`,
  and (for `--adjudicate`) `CHEMEX_REASONING_API_KEY`.
- `semi`: the same keys as `auto`; structures are submitted by the host agent.
- `agent`: only `MINERU_API_KEY`. Host credentials are managed by the host
  application.

Credentials resolve from the process environment first, then from the user
auth store. Hosts may store them with `chemex-lit auth set NAME --stdin`
(and verify with `chemex-lit auth test NAME`) instead of shell exports;
see the cli-contract reference.

Verify with `check --json` before starting.

```text
chemex-lit check --mode agent --json
```

```json
{
  "ok": true,
  "mode": "agent",
  "profile": null,
  "checks": [
    {"name": "RDKit", "status": "ok"},
    {"name": "MinerU key", "status": "ok"}
  ]
}
```

## Golden workflow

The workflow is a cycle: run, check status, read tasks, fulfill, submit,
resume, review.

### 1. Run

```
chemex-lit run paper.pdf --mode auto --json
```

Optional flags:

- `--structures file.jsonl` Supply structure candidates up front.
- `--adjudicate` Run the adjudication step.
- `--json` Emit the run summary as JSON.

The JSON summary contains the run directory:

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

Exit code 4 means the run finished as `completed_empty` (zero records found);
that is a normal outcome, not an error.

### 2. Check status

```
chemex-lit status /work/extraction/outputs/paper-a1b2c3d4 --json
```

The `status` field tells you where the pipeline is:

- `running` Core is processing.
- `awaiting_input` The pipeline is paused, waiting for external task
  submissions (semi: structure tasks; agent: all generative tasks).
- `ready` All external tasks submitted; next resume will continue.
- `success` All records extracted and finalized.
- `completed_empty` Pipeline finished with zero records.
- `partial` Some records need review.
- `failed` A non-recoverable error occurred.
- `cancelled` Explicitly cancelled.

The same JSON shape as the run summary is returned; `awaiting` lists the
exact task IDs that still need submissions.

### 3. Read task files

When status is `awaiting_input`, read the task file inside the run
directory. Paths in protocol files and JSON always use forward slashes:

```
<run_dir>/tasks/extraction.jsonl
```

Each line is an `ExtractionTask`:

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

Asset and image paths are relative to `run_dir`; join them with the local
path library (for example `Path(run_dir) / asset_path`) before reading.

The task tells you exactly what to produce: which evidence to use, the
expected output schema, any inline text, and image paths for vision tasks.
The `instructions` field (provided by Core as `assets.text`) contains the
prompt the CLI would have used. Present it to the host as-is. Do not
rewrite extraction prompts.

### 4. Fulfill tasks

For each `awaiting` task, produce the outputs described in the
submission-format reference. For structure tasks, extract compound
structures from the referenced images or text. For adjudication tasks,
decide `accept` or `keep_review` for each candidate with a rationale.

Collect your outputs into a submission JSONL file. Each line is a
`CandidateSubmission`:

```json
{
  "task_id": "st-0001",
  "producer": {
    "kind": "host_agent",
    "client_name": "codex",
    "client_version": "1.0.0",
    "model": "gpt-5",
    "policy": "chemex-lit-default"
  },
  "outputs": [
    {"compound_label": "7", "smiles": "CCO", "confidence": 0.9}
  ]
}
```

Reaction outputs from text or table tasks use the same envelope:

```json
{
  "task_id": "txt-0001",
  "producer": {
    "kind": "host_agent",
    "client_name": "codex",
    "client_version": "1.0.0"
  },
  "outputs": [
    {
      "reactants": [{"label": "7", "name": "ethanol", "smiles": "CCO", "role": "reactant"}],
      "reagents": [],
      "solvents": [],
      "products": [{"label": "8", "name": "acetaldehyde", "smiles": "CC=O", "role": "product"}],
      "temperature_c": 78,
      "time": "2 h",
      "yield_pct": 85,
      "confidence": 0.9
    }
  ]
}
```

Batch in groups of 20 or fewer tasks per submission file. Partial
submissions are allowed. Unsubmitted tasks remain `awaiting`.

### 5. Submit

```
chemex-lit submit /work/extraction/outputs/paper-a1b2c3d4 my_submission.jsonl --json
```

The CLI validates each line, computes `candidate_id` values, writes
provenance, and marks tasks as fulfilled. When all tasks for the current
stage are fulfilled the returned status changes to `ready`.

### 6. Resume

```
chemex-lit resume /work/extraction/outputs/paper-a1b2c3d4 --json
```

Resume advances the pipeline to the next stage. Repeat the
fulfill-submit-resume cycle until the run reaches a terminal status
(`success`, `completed_empty`, `partial`, or `failed`).

### 7. Review

```
chemex-lit review /work/extraction/outputs/paper-a1b2c3d4
```

Open the review output to see which records need attention (marked
`needs_review` with issues). If corrections are needed, see the
review-policy reference.

## Hard rules

These rules must never be broken. They exist because the Core owns
chemistry and the CLI owns persistence.

- **Never edit `records.jsonl` directly.** The file is produced by the
  Core pipeline. Write to it and the run is invalid.
- **Never invent a `candidate_id`.** The Core computes it from
  `task_id` + output content hash. Submissions must not contain a
  `candidate_id` key anywhere.
- **Never use `--force`.** It creates a `supersedes` audit record and
  invalidates all downstream stages. A skill may not decide this.
- **Never supply `--confirmed-by` yourself.** That field is the name of
  the human who confirmed a correction. The host agent must ask a human.
- **Evidence IDs must come from the task.** Use the `evidence_ids` the
  Core provides. Do not invent or reuse evidence from other runs.
- **Fill every task or report which remain.** After submission, check
  status. If `awaiting` is non-empty, produce more submissions.
- **Adjudication decisions are `accept` or `keep_review` only.**
  There is no `reject` in the API. If a record warrants rejection, that
  is a human review action outside the skill scope.
- **Submissions must be task-bounded.** Each `CandidateSubmission`
  references exactly one `task_id`. One task per line.
- **Do not copy extraction prompts into this skill.** The Core ships
  instructions inside the task assets. Present them verbatim.
- **Do not include chemistry heuristics.** No SMILES validation, no
  reaction balancing, no yield computation. The Core owns all chemistry.

## Reference documents

- [cli-contract.md](references/cli-contract.md) Full CLI contract: commands,
  JSON shapes, path rules, environment variables, artifact layout.
- [submission-format.md](references/submission-format.md) Schema reference
  for task submissions.
- [review-policy.md](references/review-policy.md) How to handle the
  review queue, corrections, and adjudication.
