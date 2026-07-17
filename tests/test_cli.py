from __future__ import annotations

from click.testing import CliRunner

from chemex_lit.cli import main


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "chemex-lit" in result.output


def test_help_exposes_compact_command_surface() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "resume", "status", "review", "review-apply", "evaluate", "check"):
        assert command in result.output
