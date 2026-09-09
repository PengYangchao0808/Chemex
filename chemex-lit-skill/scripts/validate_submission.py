#!/usr/bin/env python3
"""Validate a ChemEx-Lit submission JSONL file offline.

Checks each line against the submission-format rules without calling the
CLI. The schema mirrors chemex_lit.models exactly: flat producer fields
(client_name/client_version, no nested client object), task-bounded
outputs, and no candidate_id anywhere.

Usage:
    python validate_submission.py submissions.jsonl
    python validate_submission.py --kind adjudications submissions.jsonl
"""

import argparse
import json
import sys

_PRODUCER_FIELDS = {"kind", "client_name", "client_version", "model", "policy", "attempt"}
_OPTIONAL_STR_PRODUCER_FIELDS = ("client_name", "client_version", "model", "policy")
_LABEL_KEYS = ("compound_label", "label", "compound_id")
_COMPOUND_LIST_FIELDS = ("reactants", "products")
_STRING_LIST_FIELDS = ("reagents", "solvents")


def _check_no_candidate_id(obj, path: str, errors: list[str]) -> None:
    """Recursively check that *candidate_id* does not appear in *obj*."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            current = f"{path}/{key}" if path else key
            if key == "candidate_id":
                errors.append(f"  Forbidden key 'candidate_id' at {current}")
            _check_no_candidate_id(val, current, errors)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _check_no_candidate_id(item, f"{path}[{idx}]", errors)


def _check_range(
    val, lo: float, hi: float, field: str, line_no: int, errors: list[str],
) -> None:
    """Append an error if *val* is outside [lo, hi]."""
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return
    if val < lo or val > hi:
        errors.append(
            f"  Line {line_no}: {field} value {val} is outside "
            f"allowed range [{lo}, {hi}]"
        )


def _validate_producer(line_no: int, obj: dict, errors: list[str]) -> None:
    """Validate the flat producer block against SubmissionProducer."""
    producer = obj.get("producer")
    if not isinstance(producer, dict):
        errors.append(f"  Line {line_no}: missing or invalid 'producer' block")
        return
    kind = producer.get("kind")
    if kind not in ("human", "host_agent"):
        errors.append(
            f"  Line {line_no}: 'producer.kind' must be 'human' or "
            f"'host_agent', got {kind!r}"
        )
    unknown = sorted(set(producer) - _PRODUCER_FIELDS)
    if unknown:
        errors.append(
            f"  Line {line_no}: unknown producer fields {unknown}; the schema "
            "is flat: kind/client_name/client_version/model/policy/attempt"
        )
    for field in _OPTIONAL_STR_PRODUCER_FIELDS:
        value = producer.get(field)
        if value is not None and not isinstance(value, str):
            errors.append(f"  Line {line_no}: 'producer.{field}' must be a string")
    attempt = producer.get("attempt")
    if attempt is not None and (
        not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
    ):
        errors.append(
            f"  Line {line_no}: 'producer.attempt' must be an integer >= 1"
        )


def _validate_compound(
    comp, line_no: int, path: str, errors: list[str],
) -> None:
    if not isinstance(comp, dict):
        errors.append(f"  Line {line_no}: {path} must be an object")
        return
    for field in ("label", "smiles", "role"):
        if field not in comp:
            errors.append(f"  Line {line_no}: {path} missing '{field}'")


def _validate_output(
    out, index: int, line_no: int, errors: list[str],
) -> None:
    path = f"outputs[{index}]"
    if not isinstance(out, dict):
        errors.append(f"  Line {line_no}: {path} must be an object")
        return
    is_reaction = any(field in out for field in ("reactants", "products"))
    if is_reaction:
        for role_field in _COMPOUND_LIST_FIELDS:
            compounds = out.get(role_field)
            if compounds is None:
                continue
            if not isinstance(compounds, list):
                errors.append(f"  Line {line_no}: {path}.{role_field} must be an array")
                continue
            for j, comp in enumerate(compounds):
                _validate_compound(comp, line_no, f"{path}.{role_field}[{j}]", errors)
        for string_field in _STRING_LIST_FIELDS:
            values = out.get(string_field)
            if values is None:
                continue
            if not isinstance(values, list):
                errors.append(f"  Line {line_no}: {path}.{string_field} must be an array")
                continue
            for j, value in enumerate(values):
                if not isinstance(value, str):
                    errors.append(
                        f"  Line {line_no}: {path}.{string_field}[{j}] must be a string"
                    )
        yield_pct = out.get("yield_pct", out.get("yield"))
        _check_range(yield_pct, 0, 100, f"{path}.yield_pct", line_no, errors)
    else:
        if not any(key in out for key in _LABEL_KEYS):
            errors.append(
                f"  Line {line_no}: {path} missing a label key "
                f"(one of {list(_LABEL_KEYS)})"
            )
        smiles = out.get("smiles", out.get("canonical_smiles"))
        if not isinstance(smiles, str) or not smiles.strip():
            errors.append(f"  Line {line_no}: {path}.smiles must be a non-empty string")
    confidence = out.get("confidence")
    _check_range(confidence, 0.0, 1.0, f"{path}.confidence", line_no, errors)


def validate_candidate(line_no: int, obj: dict) -> list[str]:
    """Validate a CandidateSubmission object.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []

    if "task_id" not in obj:
        errors.append(f"  Line {line_no}: missing 'task_id'")
    elif not isinstance(obj["task_id"], str):
        errors.append(f"  Line {line_no}: 'task_id' must be a string")

    _validate_producer(line_no, obj, errors)

    outputs = obj.get("outputs")
    if not isinstance(outputs, list) or len(outputs) == 0:
        errors.append(f"  Line {line_no}: 'outputs' must be a non-empty array")
    else:
        for i, out in enumerate(outputs):
            _validate_output(out, i, line_no, errors)

    _check_no_candidate_id(obj, "", errors)
    return errors


def validate_adjudication(line_no: int, obj: dict) -> list[str]:
    """Validate an AdjudicationDecision object.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []

    if "task_id" not in obj:
        errors.append(f"  Line {line_no}: missing 'task_id'")
    elif not isinstance(obj["task_id"], str):
        errors.append(f"  Line {line_no}: 'task_id' must be a string")

    if "reaction_id" not in obj:
        errors.append(f"  Line {line_no}: missing 'reaction_id'")
    elif not isinstance(obj["reaction_id"], str):
        errors.append(f"  Line {line_no}: 'reaction_id' must be a string")

    decision = obj.get("decision")
    if decision not in ("accept", "keep_review"):
        errors.append(
            f"  Line {line_no}: 'decision' must be 'accept' or "
            f"'keep_review', got {decision!r}"
        )

    rationale = obj.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        errors.append(f"  Line {line_no}: 'rationale' must be a string")

    _validate_producer(line_no, obj, errors)
    _check_no_candidate_id(obj, "", errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a ChemEx-Lit submission JSONL file.",
    )
    parser.add_argument(
        "file",
        type=str,
        help="Path to the JSONL submission file",
    )
    parser.add_argument(
        "--kind",
        choices=["candidates", "adjudications"],
        default="candidates",
        help="Type of submission (default: candidates)",
    )
    args = parser.parse_args()

    validator = (
        validate_adjudication
        if args.kind == "adjudications"
        else validate_candidate
    )

    all_errors: list[str] = []
    total_lines = 0

    with open(args.file, "r", encoding="utf-8") as fh:
        for raw_line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            total_lines += 1
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                all_errors.append(
                    f"  Line {raw_line_no}: invalid JSON - {exc}"
                )
                continue

            if not isinstance(obj, dict):
                all_errors.append(
                    f"  Line {raw_line_no}: entry must be a JSON object"
                )
                continue

            errors = validator(raw_line_no, obj)
            all_errors.extend(errors)

    if not total_lines:
        print("ERROR: file is empty or contains no non-blank lines")
        return 1

    if all_errors:
        print(f"ERROR: {len(all_errors)} issue(s) found in {args.file}:")
        for msg in all_errors:
            print(msg)
        return 1

    kind_label = args.kind.replace("_", " ").title()
    print(f"OK: {total_lines} {kind_label} submission(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
