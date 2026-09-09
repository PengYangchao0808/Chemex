"""Tests for the user credential store and the auth CLI surface."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

import chemex_lit.cli as cli_module
from chemex_lit.cli import main
from chemex_lit.credentials import (
    StoredCredential,
    auth_store_path,
    check_credential_status,
    credential_status,
    credential_value,
    expires_in_days,
    load_auth_store,
    remove_credential,
    resolve_credential,
    set_credential,
)
from chemex_lit.errors import ConfigurationError


def _write_expiring_entry(name: str, days: int) -> None:
    moment = datetime.now(timezone.utc) + timedelta(days=days)
    set_credential(name, "stored-secret", expires_at=moment.isoformat(), store_path=auth_store_path())


def test_env_wins_over_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROBE_KEY", "from-env")
    set_credential("PROBE_KEY", "from-store", store_path=auth_store_path())

    resolution = resolve_credential("PROBE_KEY")

    assert resolution is not None
    assert resolution.value == "from-env"
    assert resolution.source == "env"


def test_store_fallback_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROBE_KEY", raising=False)
    set_credential("PROBE_KEY", "from-store", store_path=auth_store_path())

    resolution = resolve_credential("PROBE_KEY")

    assert resolution is not None
    assert resolution.value == "from-store"
    assert resolution.source == "auth-store"


def test_missing_everywhere_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    assert resolve_credential("ABSENT_KEY") is None
    assert credential_status("ABSENT_KEY") == (False, None, None)
    with pytest.raises(ConfigurationError, match="auth set ABSENT_KEY"):
        credential_value("ABSENT_KEY", what="probe")


def test_set_roundtrip_permissions_and_atomicity() -> None:
    set_credential("A_KEY", "value-a")
    set_credential("B_KEY", "value-b")

    store = load_auth_store()
    assert set(store.credentials) == {"A_KEY", "B_KEY"}
    path = auth_store_path()
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".auth-*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_remove_credential() -> None:
    set_credential("GONE_KEY", "value")

    assert remove_credential("GONE_KEY") is True
    assert remove_credential("GONE_KEY") is False
    assert resolve_credential("GONE_KEY") is None


@pytest.mark.parametrize("bad_name", ["", "has space", "dash-key", "9start"])
def test_invalid_name_rejected(bad_name: str) -> None:
    with pytest.raises(ConfigurationError, match="Invalid credential name"):
        set_credential(bad_name, "value")


def test_blank_value_rejected() -> None:
    with pytest.raises(ConfigurationError, match="must not be empty"):
        set_credential("EMPTY_KEY", "   ")


def test_expiry_parsing_and_status() -> None:
    set_credential("OK_KEY", "v", expires_at="2030-01-01", store_path=auth_store_path())
    entry = load_auth_store().credentials["OK_KEY"]
    assert entry.expires_at is not None
    assert expires_in_days(entry) > 300


def test_expired_store_entry_is_reported_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLD_KEY", raising=False)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    set_credential("OLD_KEY", "v", expires_at=past.isoformat(), store_path=auth_store_path())

    present, source, days = check_credential_status("OLD_KEY")
    assert present is False
    assert source == "auth-store"
    assert days is not None and days < 0
    assert credential_status("OLD_KEY")[0] is True


def test_expiring_soon_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOON_KEY", raising=False)
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    set_credential("SOON_KEY", "v", expires_at=soon.isoformat(), store_path=auth_store_path())

    present, _source, days = check_credential_status("SOON_KEY")

    assert present is True
    assert days is not None and 0 <= days < 7


def test_unparseable_expiry_treated_as_no_expiry() -> None:
    entry = StoredCredential(value="v", updated_at="now", expires_at="not-a-date")
    assert expires_in_days(entry) is None


def test_corrupt_store_raises_configuration_error(tmp_path: Path) -> None:
    bad = tmp_path / "auth.json"
    bad.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Invalid credential store"):
        load_auth_store(bad)


def test_store_env_override_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "nested" / "custom-auth.json"
    monkeypatch.setenv("CHEMEX_AUTH_STORE", str(custom))

    set_credential("CUSTOM_KEY", "value")

    assert custom.is_file()
    assert resolve_credential("CUSTOM_KEY") is not None


# ---- CLI surface ----


def test_auth_set_list_remove_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "s3cret-value")

    set_result = CliRunner().invoke(main, ["auth", "set", "CLI_KEY"])
    assert set_result.exit_code == 0, set_result.output
    assert "CLI_KEY" in set_result.output

    list_result = CliRunner().invoke(main, ["auth", "list", "--json"])
    assert list_result.exit_code == 0
    rows = json.loads(list_result.output)
    assert rows == [
        {
            "name": "CLI_KEY",
            "masked": "s3cret-value"[:5] + "…" + "s3cret-value"[-2:],
            "status": "ok",
            "updated_at": rows[0]["updated_at"],
            "expires_at": None,
            "expires_in_days": None,
        }
    ]
    assert "s3cret-value" not in list_result.output.replace(rows[0]["masked"], "")

    human = CliRunner().invoke(main, ["auth", "list"])
    assert "ok " in human.output or human.output.startswith("ok")
    assert "s3cret-value" not in human.output

    remove_result = CliRunner().invoke(main, ["auth", "remove", "CLI_KEY"])
    assert remove_result.exit_code == 0
    assert CliRunner().invoke(main, ["auth", "remove", "CLI_KEY"]).exit_code == 1


def test_auth_set_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(main, ["auth", "set", "STDIN_KEY", "--stdin"], input="piped-secret\n")

    assert result.exit_code == 0, result.output
    resolution = resolve_credential("STDIN_KEY")
    assert resolution is not None
    assert resolution.value == "piped-secret"
    assert resolution.source == "auth-store"
    assert os.environ.get("STDIN_KEY") is None


def test_auth_test_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    set_credential("PROBE_MINERU", "token")
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    probes: list[str] = []

    def fake_probe(name: str, config: object) -> tuple[bool, str]:
        del config
        probes.append(name)
        return name != "BAD", f"HTTP {200 if name != 'BAD' else 401}"

    monkeypatch.setattr(cli_module, "_probe_credential", fake_probe)

    good = CliRunner().invoke(main, ["auth", "test", "PROBE_MINERU"])
    assert good.exit_code == 0 and probes == ["PROBE_MINERU"]

    set_credential("BAD", "token")
    bad = CliRunner().invoke(main, ["auth", "test", "BAD"])
    assert bad.exit_code == 2


def test_check_json_reports_credential_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from chemex_lit import profiles

    monkeypatch.setattr(profiles, "default_models_path", lambda: tmp_path / "missing.yaml")
    monkeypatch.setenv("MINERU_API_KEY", "mineru-env")
    result = CliRunner().invoke(main, ["check", "--mode", "agent", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    key_items = [item for item in payload["checks"] if item["name"] == "MinerU key"]
    assert key_items == [
        {"name": "MinerU key", "status": "ok", "source": "env", "expires_in_days": None}
    ]


def test_check_json_auth_store_source_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    from chemex_lit import profiles
    from pathlib import Path as _Path

    monkeypatch.setattr(profiles, "default_models_path", lambda: _Path("/nonexistent/models.yaml"))
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    set_credential("MINERU_API_KEY", "stored-token", store_path=auth_store_path())

    result = CliRunner().invoke(main, ["check", "--mode", "agent", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    item = next(item for item in payload["checks"] if item["name"] == "MinerU key")
    assert item["status"] == "ok"
    assert item["source"] == "auth-store"
    assert item["expires_in_days"] is None
