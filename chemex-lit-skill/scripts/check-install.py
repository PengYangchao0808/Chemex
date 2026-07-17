#!/usr/bin/env python3
"""Check that chemex-lit is installed and operational.

Runs ``chemex-lit --version`` and ``chemex-lit check``, then prints a
friendly report. Exits with code 0 on success, 1 if the CLI is not found,
and 2 if it is installed but the check command reports a problem.
"""

import subprocess
import sys


def run_cmd(cmd: list[str], label: str, timeout: int = 30) -> tuple[int, str]:
    """Run *cmd* and return (returncode, combined stdout+stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return result.returncode, output.strip()
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def main() -> int:
    print("=== ChemEx-Lit installation check ===\n")

    # Step 1: check --version
    rc, out = run_cmd(["chemex-lit", "--version"], "version")
    if rc == 127:
        print("[FAIL] chemex-lit not found on PATH.")
        print("       Install with:  pip install chemex-lit")
        return 1
    if rc != 0:
        print(f"[FAIL] chemex-lit --version returned exit code {rc}")
        print(f"       Output:\n{out}")
        return 2

    print(f"[OK]   chemex-lit is installed.")
    print(f"       Version output: {out}\n")

    # Step 2: check configuration
    rc, out = run_cmd(["chemex-lit", "check"], "check")
    if rc == 0:
        print("[OK]   chemex-lit check passed.")
        if out:
            print(f"       {out}")
    elif rc == 3:
        print("[WARN] chemex-lit check found configuration issues.")
        print(f"       Output:\n{out}")
    else:
        print(f"[FAIL] chemex-lit check returned exit code {rc}")
        print(f"       Output:\n{out}")
        return 2

    print("\n=== Check complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
