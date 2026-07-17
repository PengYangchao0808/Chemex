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

## Mode selection

Three modes control who fulfills each extraction channel.

| Mode | Text | Table | Structure | Adjudication | When to pick |
| --- | --- | --- | --- | --- | --- |
| auto | CLI model | CLI vision model | CLI vision model | CLI reasoning model | CI, unattended batch, reproducible runs, or when all API keys are set |
| semi | CLI model | CLI vision model | You (human or agent) submit structures | CLI reasoning model | You want to supply or verify structures yourself |
| agent | You submit all | You submit all | You submit all | You submit decisions | No CLI model keys, or you want full generative control |

In auto mode the CLI needs all four API keys (`MINERU_API_KEY`,
`CHEMEX_TEXT_API_KEY`, `CHEMEX_VISION_API_KEY`, `CHEMEX_REASONING_API_KEY`).
In semi mode `CHEMEX_REASONING_API_KEY` is optional if you handle
adjudication. In agent mode only `MINERU_API_KEY` is required.

## Golden workflow

The workflow is a cycle: run, check status, read tasks, fulfill, submit,
resume, review.

### 1. Run

```
chemex-lit run paper.pdf --mode <auto|semi|agent>
```

Optional flags:

- `--structures file.jsonl` Supply structure candidates up front (semi/auto).
- `--adjudicate` Run the adjudication step.
- `--json` Emit structured output (status, run directory path).

The CLI prints a run directory path, for example
`outputs/paper-a1b2c3d4/`.

### 2. Check status

```
chemex-lit status outputs/paper-a1b2c3d4/
```

The status tells you where the pipeline is:

- `running` Core is processing.
- `awaiting_input` The pipeline is paused, waiting for external task
  submissions. This happens in semi mode (structure tasks) and agent mode
  (all tasks).
- `ready` All external tasks submitted; next resume will continue.
- `success` All records extracted and finalized.
- `completed_empty` Pipeline finished with zero records.
- `partial` Some tasks failed or were skipped.
- `failed` A non-recoverable error occurred.
- `cancelled` Explicitly cancelled.

### 3. Read task files

When status is `awaiting_input`, look inside the run directory:

```
outputs/paper-a1b2c3d4/tasks/extraction.jsonl
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
    "client": {"name": "codex", "version": "0.2.0"}
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
chemex-lit submit outputs/paper-a1b2c3d4/ my_submission.jsonl
```

The CLI validates each line, computes `candidate_id` values, writes
provenance, and marks tasks as fulfilled. When all tasks for the current
stage are fulfilled the status changes to `ready`.

### 6. Resume

```
chemex-lit resume outputs/paper-a1b2c3d4/
```

Resume advances the pipeline to the next stage. In semi mode this runs
adjudication. In agent mode the next set of tasks (adjudication) becomes
available. Repeat the fulfill-submit-resume cycle until the run reaches
a terminal status.

### 7. Review

```
chemex-lit review outputs/paper-a1b2c3d4/
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
  status. If tasks still show `awaiting`, produce more submissions.
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

- [modes.md](references/modes.md) Mode comparison, state machine,
  awaiting_input semantics.
- [contracts.md](references/contracts.md) Full CLI contract, artifact
  layout, environment variables.
- [submission-format.md](references/submission-format.md) Schema reference
  for task submissions.
- [review-policy.md](references/review-policy.md) How to handle the
  review queue, corrections, and adjudication.
