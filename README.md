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

Set credentials through environment variables **or** the user credential
store. Resolution order: process environment first (CI-friendly), then
`auth.json`; the store never lives inside a repository.

```powershell
$env:MINERU_API_KEY="..."
$env:DEEPSEEK_API_KEY="..."
$env:GLM_API_KEY="..."
$env:OPENAI_API_KEY="..."
```

Or store them once via the `auth` commands (file is created with mode 0600
under the OS config directory):

```bash
chemex-lit auth set MINERU_API_KEY          # hidden prompt (or --stdin)
chemex-lit auth set MINERU_API_KEY --expires 2026-10-03
chemex-lit auth list                        # masked values, expiry status
chemex-lit auth test MINERU_API_KEY         # live probe; exit 2 on rejection
chemex-lit auth remove MINERU_API_KEY
```

These variable *names* are referenced by `api_key_env` entries in your `models.yaml`; only the values live in the environment or `auth.json`.

## Model profiles

Model endpoints (text/vision/reasoning) are managed as named profiles in `~/.config/chemex-lit/models.yaml`. API keys remain in environment variables; the YAML stores only the variable *names*.

```yaml
version: 1
default_profile: deepseek-glm

profiles:
  deepseek-glm:
    text:
      base_url: https://api.deepseek.com/v1
      model: deepseek-v4-flash
      api_key_env: DEEPSEEK_API_KEY
    vision:
      base_url: https://open.bigmodel.cn/api/paas/v4
      model: glm-4v-plus
      api_key_env: GLM_API_KEY
    reasoning:
      base_url: https://api.deepseek.com/v1
      model: deepseek-v4-pro
      api_key_env: DEEPSEEK_API_KEY

  openai:
    text:
      base_url: https://api.openai.com/v1
      model: gpt-4o
      api_key_env: OPENAI_API_KEY
    vision:
      base_url: https://api.openai.com/v1
      model: gpt-4o
      api_key_env: OPENAI_API_KEY
    reasoning:
      base_url: https://api.openai.com/v1
      model: gpt-4o
      api_key_env: OPENAI_API_KEY

  codex-diverse:
    # host_routes are routing metadata for the host agent; they contain no secrets.
    host_routes:
      text:
        policy: codex-text
        model: gpt-5
        fallbacks: [gpt-5-mini]
      table:
        policy: codex-table
        model: gpt-5-mini
      structure:
        policy: gemini-web-ocsr
        model: human-assisted
      adjudication:
        policy: codex-review
        model: gpt-5
```

On Linux the file lives at `~/.config/chemex-lit/models.yaml`. On macOS use `~/Library/Application Support/chemex-lit/models.yaml`. On Windows use `%LOCALAPPDATA%\chemex-lit\models.yaml`.

Optional fields (`timeout`, `max_tokens`, `temperature`, `retries`, `response_format`, `extra_payload`) may be omitted; they inherit from the packaged `default_config.yaml`.

## Usage

`--profile` selects a named profile from `models.yaml`. `--models-path` can point to an
explicit profile file. Without either, ChemEx-Lit uses `default_profile` from the user file
or the packaged defaults.

```bash
chemex-lit check
chemex-lit check --mode agent
chemex-lit --models-path configs/chemex-models.yaml --profile deepseek-glm check --show-config
chemex-lit --profile deepseek-glm \
  run paper.pdf --mode auto \
  --output-dir outputs/bench/deepseek-glm
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

Modes: `auto` is unattended and suitable for CI or reproducible batch runs.
`semi` pauses at `awaiting_input` so structure tasks can be submitted by a
host agent (or supplied up front with `--structures`). `agent` externalizes
all generative tasks to a host agent. See
[docs/adr/002-layered-architecture.md](docs/adr/002-layered-architecture.md).

With a profile such as `codex-diverse`, run `--mode agent` to emit host tasks
carrying the selected `policy`, `model`, and optional `fallbacks`. The CLI
does not call those host models itself; OpenCode/Codex fulfills the task and
submits the resulting JSONL. The `structure` route may point at any host
model or at human-assisted OCSR performed outside the CLI.

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

### Model management commands

List all defined profiles:

```bash
chemex-lit models list
```

Show one profile's non-secret model configuration:

```bash
chemex-lit models show deepseek-glm
```

Check whether the profile's referenced credential variables are present:

```bash
chemex-lit models check deepseek-glm
```

### Benchmarking multiple profiles

Run the same PDF against several profiles in a loop:

```bash
for profile in deepseek-glm openai; do
  chemex-lit --profile "$profile" \
    run paper.pdf --mode auto \
    --output-dir "outputs/benchmark/$profile"
done
```

Each run records its profile name, effective models, and a configuration fingerprint in `manifest.json`, so swapped-model mistakes cannot silently reuse stale results.

If `CHEMEX_*_MODEL` environment variables are set during a profile run, ChemEx-Lit emits a warning -- these remain functional as a debug escape hatch but break profile reproducibility.

## Stable public contract

ChemEx-Lit v1 treats these as public API:

- the `chemex-lit` command surface (`run`, `resume`, `submit`, `status`, `cancel`, `review`,
  `review-apply`, `evaluate`, `check`, `models`);
- the configuration schema;
- `records.jsonl` with `schema_version: "1.0"`;
- the run artifact layout documented in [docs/architecture.md](docs/architecture.md), including
  task files and provenance sidecars that support pause/resume without changing the record schema;
- the `profile` and `models_source` keys in `manifest.json` plus the existing `config_sha256` fingerprint.

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
