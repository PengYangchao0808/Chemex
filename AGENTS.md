# ChemEx-Lit project instructions

ChemEx-Lit is a Python 3.11+ `src`-layout package for extracting traceable reaction records
from literature PDFs.

## Production structure

```text
src/chemex_lit/
├── __init__.py     # Package version only
├── cli.py          # Click thin shell over the pipeline workflow API
├── pipeline.py     # The only production pipeline and workflow API
├── models.py       # Stable v1 data contract
├── store.py        # Atomic artifacts and resume state
├── config.py       # Effective configuration and its fingerprint
├── profiles.py     # Model profile discovery and resolution
├── credentials.py  # Environment-first credential resolution
├── errors.py       # Shared exception types and utc_now
├── mineru.py       # MinerU cloud adapter
├── assembly.py     # Deterministic candidate merge
├── adjudicator.py  # Optional evidence-bound acceptance only
├── review.py       # The only review generator/applier
├── extraction/     # Task planning, payload normalization, fulfill-only extractors
├── llm.py          # One OpenAI-compatible client and prompt registry
├── chemistry.py    # Deterministic validation and rendering
├── evaluation.py   # Deterministic release-gate metrics
└── resources/      # Packaged config, prompts, and template
```

## Invariants

- There is one `Pipeline`; do not add a second automatic/manual orchestrator.
- Manual structures enter as `StructureCandidate` JSONL.
- All public records validate through `chemex_lit.models`.
- Extractors select evidence, call an LLM, and return candidates only.
- Validators report issues; they never perform generative repair.
- The assembler is deterministic. The adjudicator cannot rewrite chemistry.
- Runtime resources use `importlib.resources`; never rely on repository-root paths.
- Credentials resolve from the process environment first, then the user
  credential store (`auth.json`, managed by `chemex-lit auth`); they never
  live in repository files or `models.yaml`.
- Every persisted write goes through `ArtifactStore`.
- Empty extraction is `completed_empty`, never `success`.

## Style and verification

- Line length 100; Ruff target `py311`.
- Google-style docstrings for public APIs.
- Use `httpx`, Pydantic v2, and UTF-8.
- Mock all external services in tests.
- Required checks: pytest, Ruff, Pyright, build, and Twine check.

## Legacy

Pre-v1 code is retained locally under ignored `legacy/` only because the original directory had
no Git history. It is not packaged, tested, or part of the supported architecture.
