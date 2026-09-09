# ChemEx-Lit v1 architecture

## Pipeline

```text
PDF
 └─ document (MinerUAdapter) → DocumentBundle + EvidenceRef
     └─ extraction (task-bounded)
         ├─ text tasks      ┐
         ├─ table tasks     ├─ ReactionCandidate / StructureCandidate
         └─ structure tasks ┘
             fulfilled by cli_model in-process or external human/host_agent
     └─ validation (Validator) → ValidationIssue + canonical SMILES
     └─ assembly (Assembler) → assembly/records.jsonl
     └─ adjudication (optional)
         ├─ cli reasoning model, or
         └─ external accept/keep_review decisions
     └─ finalization → records.jsonl + review.jsonl + review.html
```

`Pipeline` coordinates stages but contains no extraction or chemical validation rules. Extraction
is task-bounded: Core emits `ExtractionTask`, fulfills it in-process with `cli_model`, or accepts
external `CandidateSubmission`. In-process extraction uses the text tier for narrative text and the
vision tier for tables and structures. Adjudication is optional and may run in-process with the
reasoning tier or from external `AdjudicationDecision` payloads. Components exchange Pydantic
models in memory. JSONL is a persistence and external interchange boundary.

## Dependency direction

```text
cli → pipeline → adapters/services → models
                 store ────────────→ models
```

The CLI is a thin shell: each command maps one-to-one onto a pipeline workflow function
(`run_pdf`, `resume_run`, `submit_files`, `run_status`); `cancel` writes the terminal state
through `ArtifactStore` directly. `models.py` imports no business modules. Experimental code may
depend on the core; the core must never depend on experimental code.

## Artifact contract

```text
<run-dir>/
├── manifest.json
├── document.json
├── evidence.jsonl
├── audit.jsonl
├── tasks/
│   ├── extraction.jsonl
│   ├── adjudication.jsonl
│   └── state.json
├── candidates/
│   ├── reactions.jsonl
│   ├── structures.jsonl
│   └── provenance.jsonl
├── validation/
│   ├── outcome.json
│   └── issues.jsonl
├── assembly/
│   └── records.jsonl
├── records.jsonl
├── review.jsonl
├── review.html
├── review_assets/
└── raw/mineru/
```

Mode-dependent files appear only when a run uses them. `assembly/records.jsonl` is the
pre-adjudication record set. `records.jsonl` is the final public output. `tasks/state.json` is the
shared resume state for extraction and adjudication tasks.

Each stage stores an input hash in `manifest.json`. Fingerprints cover non-secret model specs,
relevant `producer_plan` slices, prompt and instruction versions, submission hashes, and tool
versions (`rdkit.__version__` for validation). `manifest.json` stores run status, mode,
`producer_plan`, the effective config dump, and the config hash. Resume skips a stage only when
both the hash and its declared output match. Changed PDF, model, prompt, producer, submission, or
tool version invalidate the appropriate downstream stage. Config or mode drift is rejected with an
explicit error instead of silently reusing old artifacts.

## Safety rules

- ZIP extraction rejects paths outside the run directory.
- Artifact paths cannot escape the run directory.
- Artifact replacement is atomic.
- Model fallback is never silent.
- Submissions are task-bounded and Core assigns `candidate_id` values.
- Invalid SMILES remain visible with validation issues.
- Corrections require a human confirmer and are revalidated before acceptance changes.
- Adjudication decisions are `accept` / `keep_review` only and cannot alter chemical fields.
- Cancelled runs refuse resume.

## Public compatibility

Version 1.x preserves the CLI, configuration schema, artifact names, and `ReactionRecord` schema
unless a documented deprecation has been issued. Prompt, model, producer, and submission metadata
are recorded in the manifest and sidecars because they affect reproducibility even when the JSON
schema is unchanged.
