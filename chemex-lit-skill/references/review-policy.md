# Review policy

## Review queue

After assembly the pipeline produces a review queue. Access it with:

```
chemex-lit review <run_dir>
```

Output is `review.html` (human-readable) and `review.jsonl`
(machine-readable). Each review entry has one of these statuses:

| Status | Meaning |
| --- | --- |
| `needs_review` | Record has issues that prevent acceptance. |
| `accepted` | Record passed all validation and adjudication. |
| `corrected` | Record was modified by review-apply and revalidated. |

Records marked `needs_review` include an `issues` array with severity:

| Severity | Meaning |
| --- | --- |
| `error` | Chemistry validation failure (invalid SMILES, unbalanced reaction). Must be fixed before the record can be accepted. |
| `warning` | Suspicious but not invalid (unusual yield, unexpected reagent). May be acceptable. |

## Corrections

To suggest a correction, create a corrections JSON file:

```json
[
  {
    "reaction_id": "rec-001",
    "path": "/reactants/0/smiles",
    "value": "CCO"
  },
  {
    "reaction_id": "rec-001",
    "path": "/yield_pct",
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

1. Schema validation of the correction file.
2. Each correction is applied to its target record.
3. Affected records go through RDKit revalidation. This step cannot be
   skipped.
4. Assembly re-runs on corrected records to recalculate issues.
5. The new review status appears. Records that still have errors stay
   `needs_review`. They are not automatically accepted.
6. The result is written to `records.corrected.jsonl` with a full audit
   trail: who confirmed, when, what changed, pre and post validation
   state.

### Important

- Corrections are suggestions. The Core revalidates and may keep the
  record as `needs_review` if chemistry issues persist.
- `records.jsonl` is never overwritten. Corrections live in
  `records.corrected.jsonl`.
- A record that still has errors after correction cannot be accepted.
  Only a human can override this.

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

1. Run `chemex-lit review <run_dir>` and inspect the output.
2. If no records need review, the job is done.
3. If records need review, present the issues to the human user with
   suggested corrections. Do not apply corrections autonomously.
4. Once the human provides corrections and confirms with their name,
   run `review-apply` and check the result.
