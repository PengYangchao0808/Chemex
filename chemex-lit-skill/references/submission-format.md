# Submission format

Submissions are newline-delimited JSON (JSONL) files. Each line is an
independent submission for one task. The schema below is the single
authoritative schema; the Pydantic models in `chemex_lit.models`, the
offline validator `scripts/validate_submission.py`, and the CLI submit
command all enforce exactly this shape.

## CandidateSubmission

Used for extraction tasks (`submit --kind candidates`, the default).

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

### Envelope fields

| Field | Required | Type | Rules |
| --- | --- | --- | --- |
| `task_id` | yes | string | Must match a task_id from tasks/extraction.jsonl. |
| `producer.kind` | yes | string | Must be `"human"` or `"host_agent"`. |
| `producer.client_name` | no | string | Name of the fulfiller, e.g. `"codex"`. |
| `producer.client_version` | no | string | Version of the fulfiller client. |
| `producer.model` | no | string | Host model used, e.g. `"gpt-5"`. |
| `producer.policy` | no | string | Routing policy label from the task, if any. |
| `producer.attempt` | no | integer | Retry counter, >= 1. |
| `outputs` | yes | array | Non-empty array of output objects. |

No other producer fields exist. Unknown fields are rejected; the
producer identity is flat (`client_name`/`client_version`), never a
nested client object.

### Structure task outputs

| Field | Required | Type | Rules |
| --- | --- | --- | --- |
| `compound_label` | yes* | string | Compound label from the task (preferred key). `label` or `compound_id` are also parsed. |
| `smiles` | yes | string | Non-empty SMILES string. |
| `confidence` | no | number | 0.0-1.0. |
| `evidence_ids` | no | array | Must reference evidence belonging to the task. |

*Exactly one label key (`compound_label`, `label`, or `compound_id`)
should be present; the parser tries them in that order.

### Reaction task outputs (text/table)

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

| Field | Required | Type | Rules |
| --- | --- | --- | --- |
| `outputs[].reactants` | no | array | Compound objects (see below). |
| `outputs[].products` | no | array | Compound objects. |
| `outputs[].reagents` | no | array of strings | Reagent names, e.g. `["DMDO"]`. |
| `outputs[].solvents` | no | array of strings | Solvent names, e.g. `["CH2Cl2"]`. |
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
- **Human submissions** (semi manual structures) use
  `"producer": {"kind": "human", "client_name": "reviewer"}`.

## AdjudicationDecision

Used for adjudication tasks (`submit --kind adjudications`). The
`producer` envelope is the same object as above.

```json
{
  "task_id": "adj-0001",
  "reaction_id": "rec-001",
  "decision": "accept",
  "rationale": "All compounds present, yields sum correctly.",
  "producer": {
    "kind": "host_agent",
    "client_name": "codex",
    "client_version": "1.0.0"
  }
}
```

| Field | Required | Type | Rules |
| --- | --- | --- | --- |
| `task_id` | yes | string | Must match a task_id from tasks/adjudication.jsonl. |
| `reaction_id` | yes | string | References an assembled reaction. |
| `decision` | yes | string | Must be `"accept"` or `"keep_review"`. No other values. |
| `rationale` | no | string | Free-text explanation of the decision. |
| `producer` | yes | object | Same producer schema as CandidateSubmission. |

## Batching

- Submit no more than 20 tasks per submission file. If more tasks
  exist, split across multiple files and submit separately.
- Partial submissions are allowed. Remaining tasks stay `awaiting`.
- Resubmitting the same file is idempotent if the output content is
  identical (candidate_id is content-addressed). Changed output
  resubmission requires `--force`, which agents must never use.
- After a partial submission, run `status --json` and use the
  `awaiting` list to see which task IDs remain.
