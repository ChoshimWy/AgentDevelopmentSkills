#!/usr/bin/env python3
"""Compare the frozen iOSAgentSkills route baseline with Core plans."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dumps, load  # noqa: E402
from agent_workflow.compatibility import compare_apple_routes  # noqa: E402
from agent_workflow.registry import ManifestRegistry  # noqa: E402


def main() -> int:
    baseline = load(ROOT / "tests" / "fixtures" / "compatibility" / "apple-dual-route-v1.json")
    registry = ManifestRegistry.from_directory(ROOT / "platforms")
    report = compare_apple_routes(baseline, repository_fixtures=ROOT / "tests" / "fixtures", registry=registry)
    print(dumps(report), end="")
    return 0 if report["status"] == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
