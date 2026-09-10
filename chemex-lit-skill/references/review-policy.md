# Review policy

## Status model

ChemEx-Lit separates machine validation from human review:

**Machine status** (`review_status` on `ReactionRecord`) has exactly three
values:

| Status | Meaning |
| --- | --- |
| `needs_review` | Record has issues that prevent acceptance. |
| `accepted` | Record passed all validation and adjudication. |
| `rejected` | Record was explicitly rejected. Sticky — machine revalidation cannot flip it. |

There is no `corrected` review status. A record modified by `review-apply`
is revalidated and receives `accepted` (if clean) or `needs_review` (if
issues remain).

**Human review status** is a sidecar aggregated from `ReviewDecision`
entries. It has five values:

| Status | Meaning |
| --- | --- |
| `unreviewed` | No human decisions recorded. |
| `in_review` | Some decisions exist but not all required targets are resolved. |
| `confirmed` | All required targets confirmed or marked not-applicable. |
| `pending` | At least one target is marked pending or has insufficient evidence. |
| `rejected` | A human explicitly rejected the record. |

`revised` is an event (an operation kind in `ReviewOperation`), **not** a
status.

Records marked `needs_review` include an `issues` array with severity:

| Severity | Meaning |
| --- | --- |
| `error` | Chemistry validation failure (invalid SMILES, unbalanced reaction). Must be fixed before the record can be accepted. |
| `warning` | Suspicious but not invalid (unusual yield, unexpected reagent). May be acceptable. |

## Review queue

After assembly the pipeline produces a review queue. Access it with:

```
chemex-lit review <run_dir>
```

When `--gold` is given, gold comparisons are computed and persisted:

```
chemex-lit review <run_dir> --gold benchmark.jsonl --json
```

The `--json` flag emits a machine-readable envelope:

```json
{
  "run_dir": "/work/extraction/outputs/paper-a1b2c3d4",
  "review_html": "/work/extraction/outputs/paper-a1b2c3d4/review.html",
  "records_count": 12,
  "human_status_counts": {"unreviewed": 10, "confirmed": 2},
  "gold": null
}
```

When `--gold` is provided, the `gold` key contains `file_name`,
`file_hash`, and `entry_count`. When `--gold` is absent but
`gold_comparison.jsonl` already exists from a previous run, it is
reloaded for reproducibility.

## Corrections (legacy)

To suggest a correction, create a corrections JSON file. Paths use
dot notation matching the record structure:

```json
[
  {
    "reaction_id": "rec-001",
    "path": "reactants.0.smiles",
    "value": "CCO"
  },
  {
    "reaction_id": "rec-001",
    "path": "yield_pct",
    "value": 92
  }
]
```

Apply with:

```
chemex-lit review-apply <run_dir> corrections.json --confirmed-by "Jane Smith"
```

The `--confirmed-by` flag records who confirmed the corrections. It is
mandatory. A host agent must never supply this value itself. The field
must be a real human name.

### What happens on review-apply

1. Schema validation of the correction file (or submission package).
2. Each correction is applied to its target record using dot-path
   assignment.
3. Affected records go through RDKit SMILES revalidation and
   canonicalization. This step cannot be skipped.
4. `records.corrected.jsonl` is written alongside a full audit trail:
   who confirmed, when, what changed, pre and post validation state.
5. `review.html` is regenerated to reflect the current effective
   revision.
6. The original `records.jsonl` is never overwritten.

## ReviewSubmission package

`review-apply` auto-detects the package format. A JSON object with
`submission_id` and `operations` keys is treated as a `ReviewSubmission`.
A JSON array of `{reaction_id, path, value}` objects uses the legacy
corrections path.

### Package format

```json
{
  "schema_version": "1.0",
  "submission_id": "sub-001",
  "base_record_hashes": {
    "rec-001": "a1b2c3d4e5f6"
  },
  "operations": [
    {
      "reaction_id": "rec-001",
      "target_kind": "participant_structure",
      "target_id": "7",
      "op": "set_value",
      "path": "reactants.0.smiles",
      "old_value": "CCO",
      "new_value": "CC=O",
      "reason": "Incorrect structure assignment",
      "evidence_ids": ["ev-041"]
    }
  ],
  "reviewer": "Jane Smith",
  "created_at": "2026-09-10T12:00:00Z"
}
```

`operations[].op` values: `set_value`, `confirm`, `mark_pending`,
`mark_not_applicable`, `reject`, `add_participant`,
`remove_participant`, `change_role`.

`set_value` paths use dot notation and must target an allowed root:
`reactants`, `products`, `reagents`, `solvents`, `temperature_c`,
`time`, `yield_pct`, `review_status`.

### Apply semantics

- **Version conflict**: if `base_record_hashes` contains a hash that
  does not match the current record, `review-apply` raises an error
  listing the conflicting `reaction_id` values. The package must be
  refreshed against current records.
- **Dedup**: a `submission_id` already persisted under
  `review_submissions/` is rejected as a duplicate.
- **Reviewer identity**: `submission.reviewer` must equal
  `--confirmed-by`. Mismatch is an error.
- **Gold is optional**: when no `--gold` is given, the
  `gold_alignment` target kind is not required for confirmation.
- **`records.jsonl` is never overwritten.** Corrected records live in
  `records.corrected.jsonl`.

### Invalidation rules

When operations affect a record, existing human decisions may be
invalidated:

- Structure edit (`set_value` on `participant_structure`) invalidates
  `participant_structure` and `stereo` confirmations for that
  participant.
- Membership or role change (`add_participant`, `remove_participant`,
  `change_role`) invalidates `completeness` confirmations.
- Record hash mismatch invalidates all decisions for that reaction
  except explicit rejects.
- **Reject is sticky**: machine revalidation must not flip a
  human-rejected record to accepted.

## Adjudication outcomes

When acting as adjudicator (agent mode), you provide decisions through
submissions. The allowed outcomes are:

| Decision | Meaning |
| --- | --- |
| `accept` | The candidate is chemically valid and sufficiently supported by evidence. |
| `keep_review` | The candidate has issues that warrant a human review. Equivalent to a warning-only recommendation. |

There is no `reject` decision in the submission API. The reasoning:
final rejection of a record that passed validation but fails
adjudication is a human action, managed outside this tool. If you
believe a record must be rejected, use `keep_review` with a strong
rationale so the human reviewer has the full context.

## What the skill does with review output

After a run reaches `success` or `partial`, the skill should:

1. Run `chemex-lit review <run_dir> [--gold benchmark.jsonl] --json`
   and inspect the output.
2. If no records need review, the job is done.
3. If records need review, present the issues to the human user with
   suggested corrections. Do not apply corrections autonomously.
4. Once the human provides corrections and confirms with their name,
   run `review-apply` and check the result.
