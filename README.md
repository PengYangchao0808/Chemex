# ChemEx-Lit

ChemEx-Lit extracts traceable chemical reaction records from literature PDFs.

The v1 core has one pipeline:

```text
PDF → MinerU → text/table/structure candidates → validation → assembly → review
```

Every reaction record links back to text, table, or image evidence. Ambiguous and invalid
records are retained in the review queue instead of being silently repaired.

## Requirements

- Python 3.11 or newer
- MinerU cloud API key
- OpenAI-compatible text and vision model endpoints

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
```

## Usage

```bash
chemex-lit check
chemex-lit run paper.pdf
chemex-lit status outputs/paper-<hash>
chemex-lit review outputs/paper-<hash>
chemex-lit evaluate outputs/paper-<hash> --gold benchmark.jsonl
```

Human-supplied structures use the same downstream pipeline:

```bash
chemex-lit run paper.pdf --structures manual_structures.jsonl
chemex-lit resume outputs/paper-<hash> --structures manual_structures.jsonl
```

## Stable public contract

ChemEx-Lit v1 treats these as public API:

- the `chemex-lit` command surface;
- the configuration schema;
- `records.jsonl` with `schema_version: "1.0"`;
- the run artifact layout documented in [docs/architecture.md](docs/architecture.md).

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
