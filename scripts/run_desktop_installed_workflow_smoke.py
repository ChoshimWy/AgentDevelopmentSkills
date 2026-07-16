#!/usr/bin/env python3
"""Exercise the installed Desktop workflow contract without target execution."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dumps, load  # noqa: E402
from agent_workflow.discovery import DiscoveryEngine  # noqa: E402
from agent_workflow.installation import build_install_bundle, install_bundle  # noqa: E402
from agent_workflow.planning import PlanCompiler  # noqa: E402
from agent_workflow.policy import PolicyResolver  # noqa: E402
from agent_workflow.registry import ManifestRegistry  # noqa: E402
from agent_workflow.reporting import delivery_report  # noqa: E402
from agent_workflow.runtime import FakeAdapterExecutor  # noqa: E402


def exercise(target: Path) -> dict:
    lock = load(target / ".agent-skills" / "install-lock.json")
    registry = ManifestRegistry.from_directory(target / ".agent-skills" / "packages")
    fixture = ROOT / "tests" / "fixtures" / "desktop-tauri"
    profile = DiscoveryEngine(registry).discover(fixture)
    policy = PolicyResolver().resolve(profile, "执行 Desktop 回归测试", explicit_platforms=["desktop"])
    plan = PlanCompiler(registry).compile(profile, policy)
    if plan["status"] != "ready":
        raise RuntimeError("installed Desktop workflow did not produce a ready plan")
    missing_targets: list[str] = []
    for node in plan["nodes"]:
        binding = node.get("binding")
        if not isinstance(binding, dict) or binding["kind"] == "tool":
            continue
        if binding["kind"] == "skill":
            path = target / "skills" / binding["name"] / "SKILL.md"
        else:
            package = next(
                item for item in lock["selected_packages"]
                if item["id"] == node["provider"].removesuffix("-agent-skills")
                or item["id"] == node["provider"]
            )
            path = target / ".agent-skills" / "packages" / package["id"] / binding["name"]
        if not path.is_file():
            missing_targets.append(binding["name"])
    if missing_targets:
        raise RuntimeError("installed Desktop plan references missing targets: " + ", ".join(sorted(set(missing_targets))))
    ledger = FakeAdapterExecutor().run(plan)
    report = delivery_report(plan, ledger)
    if ledger["final_status"] != "completed" or report["status"] != "completed":
        raise RuntimeError("installed Desktop workflow contract did not complete")
    return {
        "installed": {
            "selected_packages": [item["id"] for item in lock["selected_packages"]],
            "selected_platforms": lock["selected_platforms"],
            "status": lock["status"],
        },
        "platform": "desktop",
        "schema_version": "1.0",
        "status": "passed",
        "workflow": {
            "final_status": ledger["final_status"],
            "plan_status": plan["status"],
            "review_status": "passed",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-root", type=Path)
    arguments = parser.parse_args()
    if arguments.target_root is not None:
        report = exercise(arguments.target_root.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="desktop-installed-workflow-") as directory:
            target = Path(directory) / "codex"
            install_bundle(build_install_bundle(ROOT / "platforms", platforms=["desktop"]), target)
            report = exercise(target)
    sys.stdout.write(dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
