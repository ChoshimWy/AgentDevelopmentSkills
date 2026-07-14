#!/usr/bin/env python3
"""Run the deterministic Phase 1 conformance gates."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    commands = [
        [sys.executable, "scripts/validate_schemas.py"],
        [sys.executable, "scripts/validate_manifests.py"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    print("PASS Phase 1 conformance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
