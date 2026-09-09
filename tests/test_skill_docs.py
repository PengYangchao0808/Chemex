"""Doc-contract tests: skill docs must stay in lockstep with the code contract.

These tests parse the Markdown sources that host agents read and enforce the
P0 CLI contract: exactly three canonical modes, flat SubmissionProducer, the
six-command surface with --json outputs, and forward-slash protocol paths.
Any doc drift fails CI instead of silently misleading agents.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from chemex_lit.models import (
    AdjudicationDecision,
    CandidateSubmission,
    RunSummary,
    SubmissionProducer,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "chemex-lit-skill"
SKILL_MD = SKILL_DIR / "SKILL.md"
CLI_CONTRACT_MD = SKILL_DIR / "references" / "cli-contract.md"
SUBMISSION_FORMAT_MD = SKILL_DIR / "references" / "submission-format.md"
README_MD = REPO_ROOT / "README.md"

DEPRECATED_ALIASES = ("human-ocsr-agent", "auto-agent")
NESTED_CLIENT = re.compile(r'"client"\s*:\s*\{')
COMMANDS_WITH_JSON = ("check", "run", "status", "submit", "resume")
REVIEW_COMMAND = "review"
RUN_SUMMARY_KEYS = set(RunSummary.model_fields)
CHECK_ENVELOPE_KEYS = {"ok", "mode", "profile", "checks"}

_FENCE = re.compile(r"```(?:json|jsonl)\s*\n(.*?)```", re.DOTALL)


def _docs() -> dict[str, str]:
    return {
        "SKILL.md": SKILL_MD.read_text(encoding="utf-8"),
        "cli-contract.md": CLI_CONTRACT_MD.read_text(encoding="utf-8"),
        "submission-format.md": SUBMISSION_FORMAT_MD.read_text(encoding="utf-8"),
        "README.md": README_MD.read_text(encoding="utf-8"),
    }


def _fenced_json_blocks(text: str) -> list[Any]:
    payloads: list[Any] = []
    for block in _FENCE.findall(text):
        lines = [line for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue
        try:
            payloads.append(json.loads(block))
            continue
        except json.JSONDecodeError:
            pass
        for line in lines:
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return payloads


def _assert_no_nested_client(name: str, text: str) -> None:
    assert not NESTED_CLIENT.search(text), (
        f"{name}: nested producer.client {{name, version}} is forbidden; "
        "use flat client_name/client_version"
    )


def _assert_no_deprecated_aliases(name: str, text: str) -> None:
    offenders = [
        line.strip()
        for line in text.splitlines()
        if any(alias in line for alias in DEPRECATED_ALIASES)
    ]
    assert not offenders, (
        f"{name}: v1 docs must not mention removed mode aliases, found: {offenders}"
    )


def _validate_submission_like(name: str, payload: dict[str, Any]) -> None:
    if "candidate_id" in payload:
        return
    if "producer" not in payload:
        return
    if "outputs" in payload:
        CandidateSubmission.model_validate(payload)
    elif "decision" in payload:
        AdjudicationDecision.model_validate(payload)
    else:
        SubmissionProducer.model_validate(payload["producer"])


def _validate_payloads(name: str, payloads: list[Any]) -> None:
    for payload in payloads:
        assert "\\" not in json.dumps(payload, ensure_ascii=False), (
            f"{name}: protocol JSON must not contain backslash path separators"
        )
        if not isinstance(payload, dict):
            continue
        _validate_submission_like(name, payload)
        if CHECK_ENVELOPE_KEYS.issubset(payload):
            assert set(payload) == CHECK_ENVELOPE_KEYS, (
                f"{name}: check envelope must have exactly {sorted(CHECK_ENVELOPE_KEYS)}"
            )
            for item in payload["checks"]:
                assert {"name", "status"} <= set(item) <= {"name", "status", "source", "expires_in_days"}
                assert item["status"] in {"ok", "missing", "expiring"}
        if {"run_id", "awaiting", "tasks"}.issubset(payload):
            assert set(payload) == RUN_SUMMARY_KEYS, (
                f"{name}: status envelope keys {sorted(payload)} must equal the "
                f"RunSummary contract {sorted(RUN_SUMMARY_KEYS)}"
            )


def test_skill_md_contract() -> None:
    text = _docs()["SKILL.md"]
    _assert_no_nested_client("SKILL.md", text)
    _assert_no_deprecated_aliases("SKILL.md", text)
    for command in (*COMMANDS_WITH_JSON, REVIEW_COMMAND):
        assert re.search(rf"chemex_lit\.cli\s+{command}\b|chemex-lit\s+{command}\b", text), (
            f"SKILL.md must document the '{command}' command"
        )
    for command in COMMANDS_WITH_JSON:
        pattern = rf"{command}\b[^`\n]*--json|--json[^`\n]*{command}\b"
        assert re.search(pattern, text), f"SKILL.md must show '{command} --json'"
    assert "python -m chemex_lit.cli" in text, (
        "SKILL.md must document the python -m fallback"
    )
    assert "tasks/extraction.jsonl" in text
    _validate_payloads("SKILL.md", _fenced_json_blocks(text))


def test_cli_contract_md() -> None:
    text = _docs()["cli-contract.md"]
    _assert_no_nested_client("cli-contract.md", text)
    _assert_no_deprecated_aliases("cli-contract.md", text)
    for command in (*COMMANDS_WITH_JSON, REVIEW_COMMAND):
        assert re.search(rf"chemex_lit\.cli\s+{command}\b|chemex-lit\s+{command}\b", text), (
            f"cli-contract.md must document the '{command}' command"
        )
    for command in COMMANDS_WITH_JSON:
        pattern = rf"{command}\b[^`\n]*--json|--json[^`\n]*{command}\b"
        assert re.search(pattern, text), f"cli-contract.md must show '{command} --json'"
    assert "tasks/extraction.jsonl" in text
    _validate_payloads("cli-contract.md", _fenced_json_blocks(text))


def test_submission_format_md() -> None:
    text = _docs()["submission-format.md"]
    _assert_no_nested_client("submission-format.md", text)
    _assert_no_deprecated_aliases("submission-format.md", text)
    payloads = [
        payload
        for payload in _fenced_json_blocks(text)
        if isinstance(payload, dict) and "producer" in payload
    ]
    assert payloads, "submission-format.md must contain submission JSON examples"
    _validate_payloads("submission-format.md", payloads)


def test_readme_modes() -> None:
    text = _docs()["README.md"]
    _assert_no_deprecated_aliases("README.md", text)
    assert "tasks/extraction.jsonl" not in text or "\\" not in text.split(
        "tasks/extraction.jsonl"
    )[0][-200:]


@pytest.mark.parametrize(
    "path",
    [SKILL_MD, CLI_CONTRACT_MD, SUBMISSION_FORMAT_MD, README_MD],
    ids=["SKILL.md", "cli-contract.md", "submission-format.md", "README.md"],
)
def test_doc_files_exist(path: Path) -> None:
    assert path.is_file(), f"required doc missing: {path}"
