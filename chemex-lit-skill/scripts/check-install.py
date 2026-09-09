#!/usr/bin/env python3
"""Check that chemex-lit is installed and operational.

Tries the ``chemex-lit`` console script first and falls back to
``python -m chemex_lit.cli`` when the script is not on PATH. Runs
``--version`` and ``check --mode agent --json``, then prints a friendly
report. Exits with code 0 on success, 1 if the CLI is not found, and 2
if it is installed but the check command reports a problem.
"""

import json
import subprocess
import sys


def cli_invocations(args: list[str]) -> list[list[str]]:
    """Return candidate command lines: console script, then module fallback."""

    return [
        ["chemex-lit", *args],
        [sys.executable, "-m", "chemex_lit.cli", *args],
    ]


def run_first_available(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run *args* via the first working invocation.

    Returns (returncode, combined output, invocation name). Return code 127
    means no invocation could be found.
    """

    last_label = "chemex-lit"
    for cmd in cli_invocations(args):
        last_label = cmd[0] if len(cmd) == len(args) + 1 else "python -m chemex_lit.cli"
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s", last_label
        return result.returncode, (result.stdout + result.stderr).strip(), last_label
    return 127, "", last_label


def main() -> int:
    print("=== ChemEx-Lit installation check ===\n")

    rc, out, used = run_first_available(["--version"])
    if rc == 127:
        print("[FAIL] chemex-lit not found (tried console script and python -m).")
        print("       Install with:  pip install -e .")
        return 1
    if rc != 0:
        print(f"[FAIL] {used} --version returned exit code {rc}")
        print(f"       Output:\n{out}")
        return 2

    print(f"[OK]   chemex-lit is installed via {used}.")
    print(f"       Version output: {out}\n")

    rc, out, _ = run_first_available(["check", "--mode", "agent", "--json"])
    if rc == 0:
        print("[OK]   chemex-lit check passed.")
        try:
            payload = json.loads(out)
            failed = [item["name"] for item in payload.get("checks", []) if item["status"] != "ok"]
            if failed:
                print(f"       Checks with issues: {failed}")
            else:
                print("       All checks ok.")
        except json.JSONDecodeError:
            if out:
                print(f"       {out}")
    elif rc == 2:
        print("[WARN] chemex-lit check found missing requirements.")
        print(f"       Output:\n{out}")
    else:
        print(f"[FAIL] chemex-lit check returned exit code {rc}")
        print(f"       Output:\n{out}")
        return 2

    print("\n=== Check complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
