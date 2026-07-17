# ChemEx-Lit

ChemEx-Lit extracts traceable chemical reaction records from literature PDFs.

The v1 core has one pipeline:

```text
PDF → document → extraction → validation → assembly → adjudication → finalization
```

Every reaction record links back to text, table, or image evidence. Ambiguous and invalid
records are retained in the review queue instead of being silently repaired.

## Requirements

- Python 3.11 or newer
- MinerU cloud API key
- OpenAI-compatible text and vision model endpoints
- Optional OpenAI-compatible reasoning model endpoint for adjudication

## Installation

```bash
python -m pip install -e ".[dev]"
chemex-lit --version
```

Set credentials through environment variables only:

```powershell
$env:MINERU_API_KEY="..."
$env:CHEMEX_TEXT_API_KEY="..."
$env:CHEMEX_VISION_API_KEY="..."
$env:CHEMEX_REASONING_API_KEY="..."  # optional; falls back to text tier with a warning
```

## Usage

```bash
chemex-lit check
chemex-lit run paper.pdf --mode auto
chemex-lit submit outputs/paper-<hash> submissions.jsonl
chemex-lit resume outputs/paper-<hash>
chemex-lit status outputs/paper-<hash>
chemex-lit cancel outputs/paper-<hash>
chemex-lit review outputs/paper-<hash>
chemex-lit review-apply outputs/paper-<hash> corrections.json --confirmed-by "Reviewer One"
chemex-lit evaluate outputs/paper-<hash> --gold benchmark.jsonl
```

`review-apply` requires `--confirmed-by` and revalidates chemistry before writing
`records.corrected.jsonl`.

Modes: `auto` is unattended and suitable for CI or reproducible batch runs. `semi` pauses at
`awaiting_input` so external structure tasks can be submitted. `agent` externalizes extraction and
adjudication tasks to a host agent and is used via `chemex-lit-skill/`. See
[docs/adr/002-layered-architecture.md](docs/adr/002-layered-architecture.md).

Human-supplied structures use the same downstream pipeline:

```bash
chemex-lit run paper.pdf --mode semi
chemex-lit submit outputs/paper-<hash> manual_structures.jsonl
chemex-lit resume outputs/paper-<hash>
```

Run-start shortcut:

```bash
chemex-lit run paper.pdf --mode semi --structures manual_structures.jsonl
```

## Stable public contract

ChemEx-Lit v1 treats these as public API:

- the `chemex-lit` command surface (`run`, `resume`, `submit`, `status`, `cancel`, `review`,
  `review-apply`, `evaluate`, `check`);
- the configuration schema;
- `records.jsonl` with `schema_version: "1.0"`;
- the run artifact layout documented in [docs/architecture.md](docs/architecture.md), including
  task files and provenance sidecars that support pause/resume without changing the record schema.

Python modules other than `chemex_lit.models` are implementation details.

## Development

```bash
python -m pytest
python -m ruff check src tests
python -m pyright src/chemex_lit
python -m build
python -m twine check dist/*
```

No real MinerU or LLM calls are made by the test suite.

## License

MIT
