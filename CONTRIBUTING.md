# Contributing

Keep the v1 core small. New features should first demonstrate that they cannot be expressed as an
existing extractor, validation rule, assembly rule, or external input.

Before opening a pull request, run:

```bash
python -m pytest
python -m ruff check src tests
python -m pyright src/chemex_lit
python -m build
python -m twine check dist/*
```

Do not commit credentials, paper PDFs, generated outputs, or real API calls in tests.
