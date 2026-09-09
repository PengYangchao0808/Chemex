from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import pytest

from chemex_lit import profiles
from chemex_lit.cli import main


def _write_profiles_file(path: Path) -> None:
    _ = path.write_text(
        """
version: 1
default_profile: demo
profiles:
  alpha:
    text:
      base_url: https://alpha-text.example/v1
      model: alpha-text
      api_key_env: ALPHA_TEXT_KEY
    vision:
      base_url: https://alpha-vision.example/v1
      model: alpha-vision
      api_key_env: ALPHA_VISION_KEY
  demo:
    text:
      base_url: https://demo-text.example/v1
      model: demo-text
      api_key_env: TEXT_KEY
    vision:
      base_url: https://demo-vision.example/v1
      model: demo-vision
      api_key_env: VISION_KEY
    reasoning:
      base_url: https://demo-reasoning.example/v1
      model: demo-reasoning
      api_key_env: REASONING_KEY
""".strip(),
        encoding="utf-8",
    )


def test_models_list_with_user_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    models_path = tmp_path / "models.yaml"
    monkeypatch.setattr(profiles, "default_models_path", lambda: models_path)
    _write_profiles_file(models_path)

    result = CliRunner().invoke(main, ["models", "list"])

    assert result.exit_code == 0
    assert result.output.splitlines() == ["alpha", "demo *"]


def test_models_list_falls_back_to_packaged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models_path = tmp_path / "missing-models.yaml"
    monkeypatch.setattr(profiles, "default_models_path", lambda: models_path)

    result = CliRunner().invoke(main, ["models", "list"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == f"# (no user models.yaml at {models_path}; showing packaged samples)"
    assert lines[1:] == ["deepseek-glm", "openai"]


def test_models_show_known_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    models_path = tmp_path / "models.yaml"
    monkeypatch.setattr(profiles, "default_models_path", lambda: models_path)
    _write_profiles_file(models_path)

    result = CliRunner().invoke(main, ["models", "show", "demo"])

    assert result.exit_code == 0
    assert '"text": {' in result.output
    assert '"model": "demo-text"' in result.output


def test_models_show_unknown_profile_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models_path = tmp_path / "missing-models.yaml"
    monkeypatch.setattr(profiles, "default_models_path", lambda: models_path)

    result = CliRunner().invoke(main, ["models", "show", "bogus"])

    assert result.exit_code != 0
    assert "Unknown profile: bogus" in result.output
    assert "Available:" in result.output


def test_models_check_reports_missing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    models_path = tmp_path / "models.yaml"
    monkeypatch.setattr(profiles, "default_models_path", lambda: models_path)
    _ = models_path.write_text(
        """
version: 1
profiles:
  missing:
    text:
      base_url: https://missing-text.example/v1
      model: missing-text
      api_key_env: BOGUS_KEY
    vision:
      base_url: https://missing-vision.example/v1
      model: missing-vision
      api_key_env: BOGUS_KEY
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("BOGUS_KEY", raising=False)

    result = CliRunner().invoke(main, ["models", "check", "missing"])

    assert result.exit_code == 2
    assert "MISSING BOGUS_KEY" in result.output


def test_global_profile_flag_threads_to_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models_path = tmp_path / "models.yaml"
    monkeypatch.setattr(profiles, "default_models_path", lambda: models_path)
    _write_profiles_file(models_path)
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    monkeypatch.setenv("TEXT_KEY", "test-text")
    monkeypatch.setenv("VISION_KEY", "test-vision")

    result = CliRunner().invoke(main, ["--profile", "demo", "check", "--show-config"])

    assert '"profile": "demo"' in result.output
    assert '"models_source": "profile:demo"' in result.output


def test_explicit_models_path_is_used_by_global_profile_flag(tmp_path: Path) -> None:
    models_path = tmp_path / "models.yaml"
    _write_profiles_file(models_path)

    result = CliRunner().invoke(
        main,
        ["--models-path", str(models_path), "--profile", "demo", "check", "--mode", "agent"],
        env={"MINERU_API_KEY": "test-mineru"},
    )

    assert result.exit_code == 0, result.output
    assert "Profile: demo" in result.output
    assert "Source:  profile:demo" in result.output


def test_run_help_lists_all_supported_modes() -> None:
    result = CliRunner().invoke(main, ["run", "--help"])

    assert result.exit_code == 0
    for mode in ("auto", "semi", "agent", "human-ocsr-agent", "auto-agent"):
        assert mode in result.output


def test_check_command_prints_profile_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models_path = tmp_path / "missing-models.yaml"
    monkeypatch.setattr(profiles, "default_models_path", lambda: models_path)

    result = CliRunner().invoke(main, ["check"])

    assert "Profile: (none)" in result.output
    assert "Source:  default" in result.output
