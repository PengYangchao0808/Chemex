# ChemEx-Lit v1 architecture

## Pipeline

```text
PDF
 └─ MinerUAdapter → DocumentBundle + EvidenceRef
     ├─ TextExtractor      ┐
     ├─ TableExtractor     ├─ ReactionCandidate / StructureCandidate
     └─ StructureExtractor ┘
         └─ Validator → ValidationIssue + canonical SMILES
             └─ Assembler → ReactionRecord
                 ├─ records.jsonl
                 └─ review.jsonl + review.html
```

`Pipeline` coordinates stages but contains no extraction or chemical validation rules. Components
exchange Pydantic models in memory. JSONL is a persistence and external interchange boundary.

## Dependency direction

```text
cli → pipeline → adapters/services → models
                          store ─────→ models
```

`models.py` imports no business modules. Experimental code may depend on the core; the core must
never depend on experimental code.

## Artifact contract

```text
<run-dir>/
├── manifest.json
├── document.json
├── evidence.jsonl
├── candidates/
│   ├── reactions.jsonl
│   └── structures.jsonl
├── validation/
│   ├── outcome.json
│   └── issues.jsonl
├── records.jsonl
├── review.jsonl
├── review.html
├── review_assets/
└── raw/mineru/
```

Each stage stores an input hash in `manifest.json`. Resume skips a stage only when both the hash
and its declared output match. Changed PDF, prompt, configuration, or manual structures invalidate
the appropriate downstream stage.

## Safety rules

- ZIP extraction rejects paths outside the run directory.
- Artifact paths cannot escape the run directory.
- Artifact replacement is atomic.
- Model fallback is never silent.
- Invalid SMILES remain visible with validation issues.
- The optional adjudicator can accept warning-only records but cannot alter chemical fields.

## Public compatibility

Version 1.x preserves the CLI, configuration schema, artifact names, and `ReactionRecord` schema
unless a documented deprecation has been issued. Prompt and remote model versions are recorded in
each manifest because they affect reproducibility even when the JSON schema is unchanged.
