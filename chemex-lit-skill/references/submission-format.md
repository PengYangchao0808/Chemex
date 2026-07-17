# Submission format

Submissions are newline-delimited JSON (JSONL) files. Each line is an
independent submission for one task.

## CandidateSubmission

Used for extraction tasks (`--kind candidates`, the default).

```json
{
  "task_id": "st-0001",
  "producer": {
    "kind": "host_agent",
    "client": {"name": "codex", "version": "0.2.0"}
  },
  "outputs": [
    {
      "reactants": [
        {"label": "7", "name": "ethanol", "smiles": "CCO", "role": "reactant"}
      ],
      "reagents": [],
      "solvents": [],
      "products": [
        {"label": "8", "name": "acetaldehyde", "smiles": "CC=O", "role": "product"}
      ],
      "temperature_c": 78,
      "time": "2 h",
      "yield_pct": 85,
      "confidence": 0.9
    }
  ]
}
```

### Fields

| Field | Required | Type | Rules |
| --- | --- | --- | --- |
| `task_id` | yes | string | Must match a task_id from tasks/extraction.jsonl. |
| `producer.kind` | yes | string | Must be `"human"` or `"host_agent"`. |
| `producer.client` | yes | object | Contains `name` (string) and `version` (string). |
| `outputs` | yes | array | Non-empty array of output objects. |
| `outputs[].reactants` | no | array | List of compound objects (see below). |
| `outputs[].reagents` | no | array | List of compound objects. |
| `outputs[].solvents` | no | array | List of compound objects. |
| `outputs[].products` | no | array | List of compound objects. |
| `outputs[].temperature_c` | no | number | Reaction temperature in Celsius. |
| `outputs[].time` | no | string | Reaction time (free form, e.g. "2 h"). |
| `outputs[].yield_pct` | no | number | Yield percentage, 0-100. |
| `outputs[].confidence` | no | number | Confidence score, 0.0-1.0. |

### Compound object

```json
{"label": "7", "name": "ethanol", "smiles": "CCO", "role": "reactant"}
```

| Field | Required | Type | Rules |
| --- | --- | --- | --- |
| `label` | yes | string | Compound label from the source (e.g. "7", "2a"). |
| `name` | no | string | Chemical name. |
| `smiles` | yes | string | SMILES string. |
| `role` | yes | string | One of: `reactant`, `reagent`, `solvent`, `product`. |

### Strict rules

- **No `candidate_id` anywhere.** The Core computes candidate IDs. The
  presence of a `candidate_id` key at any nesting level is a validation
  error.
- **`evidence_ids` is optional.** If omitted, Core associates the
  submission with the task's default evidence. If provided, must match
  evidence that exists and belongs to the referenced task.
- **Outputs must be valid JSON and parseable.** The Core validates
  output schema and chemistry after submission.

## AdjudicationDecision

Used for adjudication tasks (`--kind adjudications`).

```json
{
  "task_id": "adj-0001",
  "reaction_id": "rec-001",
  "decision": "accept",
  "rationale": "All compounds present, yields sum correctly."
}
```

### Fields

| Field | Required | Type | Rules |
| --- | --- | --- | --- |
| `task_id` | yes | string | Must match a task_id from tasks/adjudication.jsonl. |
| `reaction_id` | yes | string | References a record in records.jsonl or candidates/. |
| `decision` | yes | string | Must be `"accept"` or `"keep_review"`. No other values. |
| `rationale` | yes | string | Free-text explanation of the decision. |

## Batching

- Submit no more than 20 tasks per submission file. If more tasks
  exist, split across multiple files and submit separately.
- Partial submissions are allowed. Remaining tasks stay `awaiting`.
- Submit the same file twice is idempotent if the output content is
  identical (candidate_id is content-addressed). Change the output and
  resubmit requires `--force`.
- To resume after a partial submission, run `status` and look at the
  task files for which task_ids remain `awaiting`. Produce a new
  submission for those task_ids.
