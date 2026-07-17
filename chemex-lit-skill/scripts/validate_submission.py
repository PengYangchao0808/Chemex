#!/usr/bin/env python3
"""Validate a ChemEx-Lit submission JSONL file offline.

Checks each line against the submission-format rules without calling the
CLI. Reports per-line errors and exits with code 1 if any line is invalid.

Usage:
    python validate_submission.py submissions.jsonl
    python validate_submission.py --kind adjudications submissions.jsonl
"""

import argparse
import json
import sys


def _check_no_candidate_id(obj, path: str, errors: list[str]) -> None:
    """Recursively check that *candidate_id* does not appear in *obj*."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            current = f"{path}/{key}" if path else key
            if key == "candidate_id":
                errors.append(
                    f"  Forbidden key 'candidate_id' at {current}"
                )
            _check_no_candidate_id(val, current, errors)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _check_no_candidate_id(item, f"{path}[{idx}]", errors)


def _check_range(
    val, lo: float, hi: float, field: str, line_no: int, errors: list[str],
) -> None:
    """Append an error if *val* is outside [lo, hi]."""
    if not isinstance(val, (int, float)):
        return
    if val < lo or val > hi:
        errors.append(
            f"  Line {line_no}: {field} value {val} is outside "
            f"allowed range [{lo}, {hi}]"
        )


def validate_candidate(line_no: int, obj: dict) -> list[str]:
    """Validate a CandidateSubmission object.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []

    # task_id required
    if "task_id" not in obj:
        errors.append(f"  Line {line_no}: missing 'task_id'")
    elif not isinstance(obj["task_id"], str):
        errors.append(f"  Line {line_no}: 'task_id' must be a string")

    # producer block
    producer = obj.get("producer")
    if not isinstance(producer, dict):
        errors.append(f"  Line {line_no}: missing or invalid 'producer' block")
    else:
        kind = producer.get("kind")
        if kind not in ("human", "host_agent"):
            errors.append(
                f"  Line {line_no}: 'producer.kind' must be 'human' or "
                f"'host_agent', got {kind!r}"
            )
        client = producer.get("client")
        if not isinstance(client, dict):
            errors.append(
                f"  Line {line_no}: 'producer.client' must be an object"
            )
        else:
            if not isinstance(client.get("name"), str):
                errors.append(
                    f"  Line {line_no}: 'producer.client.name' must be "
                    "a string"
                )
            if not isinstance(client.get("version"), str):
                errors.append(
                    f"  Line {line_no}: 'producer.client.version' must be "
                    "a string"
                )

    # outputs
    outputs = obj.get("outputs")
    if not isinstance(outputs, list) or len(outputs) == 0:
        errors.append(
            f"  Line {line_no}: 'outputs' must be a non-empty array"
        )
    else:
        for i, out in enumerate(outputs):
            if not isinstance(out, dict):
                errors.append(
                    f"  Line {line_no}: outputs[{i}] must be an object"
                )
                continue
            # Check compound list fields
            for role_field in ("reactants", "reagents", "solvents", "products"):
                compounds = out.get(role_field)
                if isinstance(compounds, list):
                    for j, comp in enumerate(compounds):
                        if not isinstance(comp, dict):
                            errors.append(
                                f"  Line {line_no}: outputs[{i}]."
                                f"{role_field}[{j}] must be an object"
                            )
                            continue
                        if "label" not in comp:
                            errors.append(
                                f"  Line {line_no}: outputs[{i}]."
                                f"{role_field}[{j}] missing 'label'"
                            )
                        if "smiles" not in comp:
                            errors.append(
                                f"  Line {line_no}: outputs[{i}]."
                                f"{role_field}[{j}] missing 'smiles'"
                            )
                        if "role" not in comp:
                            errors.append(
                                f"  Line {line_no}: outputs[{i}]."
                                f"{role_field}[{j}] missing 'role'"
                            )

            # Scalar fields
            yield_pct = out.get("yield_pct")
            if yield_pct is not None:
                _check_range(
                    yield_pct, 0, 100, f"outputs[{i}].yield_pct",
                    line_no, errors,
                )
            confidence = out.get("confidence")
            if confidence is not None:
                _check_range(
                    confidence, 0.0, 1.0, f"outputs[{i}].confidence",
                    line_no, errors,
                )

    # Forbid candidate_id at any level
    _check_no_candidate_id(obj, "", errors)

    return errors


def validate_adjudication(line_no: int, obj: dict) -> list[str]:
    """Validate an AdjudicationDecision object.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []

    # task_id required
    if "task_id" not in obj:
        errors.append(f"  Line {line_no}: missing 'task_id'")
    elif not isinstance(obj["task_id"], str):
        errors.append(f"  Line {line_no}: 'task_id' must be a string")

    # reaction_id required
    if "reaction_id" not in obj:
        errors.append(f"  Line {line_no}: missing 'reaction_id'")
    elif not isinstance(obj["reaction_id"], str):
        errors.append(f"  Line {line_no}: 'reaction_id' must be a string")

    # decision enum
    decision = obj.get("decision")
    if decision not in ("accept", "keep_review"):
        errors.append(
            f"  Line {line_no}: 'decision' must be 'accept' or "
            f"'keep_review', got {decision!r}"
        )

    # rationale required
    if "rationale" not in obj:
        errors.append(f"  Line {line_no}: missing 'rationale'")
    elif not isinstance(obj["rationale"], str):
        errors.append(f"  Line {line_no}: 'rationale' must be a string")

    # Forbid candidate_id at any level
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
