#!/usr/bin/env python3
"""Run the no-side-effect legacy/Core Apple Adapter smoke and fallback gate."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dumps, load  # noqa: E402
from agent_workflow.compatibility import run_apple_dual_route_smoke  # noqa: E402
from agent_workflow.registry import ManifestRegistry  # noqa: E402


def main() -> int:
    registry = ManifestRegistry.from_directory(ROOT / "platforms")
    disabled = ManifestRegistry.from_directory(
        ROOT / "platforms", disabled_providers=["ios-agent-skills"]
    )
    baseline = load(ROOT / "tests" / "fixtures" / "compatibility" / "apple-dual-route-smoke-v1.json")
    report = run_apple_dual_route_smoke(
        baseline,
        repository_fixtures=ROOT / "tests" / "fixtures",
        registry=registry,
        disabled_registry=disabled,
    )
    print(dumps(report), end="")
    return 0 if report["status"] == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
