#!/usr/bin/env python3
"""Exercise the installed Apple/iOS workflow contract end to end."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_workflow.adapters import build_adapter_request  # noqa: E402
from agent_workflow.canonical_json import dumps, load  # noqa: E402
from agent_workflow.discovery import DiscoveryEngine  # noqa: E402
from agent_workflow.installation import build_install_bundle, install_bundle  # noqa: E402
from agent_workflow.planning import PlanCompiler  # noqa: E402
from agent_workflow.policy import PolicyResolver  # noqa: E402
from agent_workflow.registry import ManifestRegistry  # noqa: E402
from agent_workflow.reporting import delivery_report  # noqa: E402
from agent_workflow.runtime import RecordedAdapterExecutor  # noqa: E402


DEFERRED_PLATFORMS = ("android", "backend", "web")


def _result_for_node(
    plan: dict[str, Any],
    node: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    node_id = node["id"]
    request = build_adapter_request(
        plan,
        node_id,
        context=context,
        invocation_id=f"ios-installed-smoke-{node_id}-1",
    )
    capability = node["capability"]
    if capability.startswith("verification."):
        kind = "validation"
        data: dict[str, Any] = {"level": "affected-tests", "tests": 1}
        if capability.endswith(".auto"):
            data = {
                "level": "lint",
                "executed_validation": [{
                    "kind": "installed-contract-smoke",
                    "status": "passed",
                }],
            }
    elif capability.startswith("review."):
        kind = "review"
        data = {
            "blocking_issues": [],
            "implementation_actor": "ios-smoke-builder",
            "reviewer_actor": "ios-smoke-reviewer",
        }
    elif capability.startswith("implementation."):
        kind = "delivery"
        data = {"changed_files": ["Fixture.swift"]}
    elif capability.startswith("reporting."):
        kind = "delivery"
        data = {"acceptance_matrix": []}
    else:
        kind = "diagnostic"
        data = {"checkpoint": "CP0", "scope": "apple"}
    return {
        "schema_version": "1.0",
        "request_id": request["request_id"],
        "invocation_id": request["invocation_id"],
        "plan_fingerprint": request["plan_fingerprint"],
        "node_id": request["node_id"],
        "capability": request["capability"],
        "provider": request["provider"],
        "binding": request["binding"],
        "status": "completed",
        "failure_attribution": {"category": "none", "summary": "未发现失败"},
        "cleanup": [],
        "evidence": [{
            "kind": kind,
            "status": "passed" if kind in {"review", "validation"} else "completed",
            "summary": f"{kind} structured evidence",
            "data": data,
            "artifact_ids": [],
        }],
        "artifacts": [],
    }


def _exercise_installed_target(
    target: Path,
    *,
    install_status: str,
    deferred_status: dict[str, str],
) -> dict[str, Any]:
    fixture = ROOT / "tests" / "fixtures" / "apple-app"
    lock = load(target / ".agent-skills" / "install-lock.json")
    installed_registry = ManifestRegistry.from_directory(target / ".agent-skills" / "packages")
    profile = DiscoveryEngine(installed_registry).discover(fixture)
    policy = PolicyResolver().resolve(profile, "实现 iOS 功能并补充测试", explicit_platforms=["apple"])
    plan = PlanCompiler(installed_registry).compile(profile, policy)
    if plan["status"] != "ready":
        raise RuntimeError("installed Apple workflow did not produce a ready plan")

    missing_skills = sorted({
        node["binding"]["name"]
        for node in plan["nodes"]
        if node["binding"]["kind"] == "skill"
        and not (target / "skills" / node["binding"]["name"] / "SKILL.md").is_file()
    })
    if missing_skills:
        raise RuntimeError(f"installed plan references missing skills: {', '.join(missing_skills)}")

    context = {
        "task": policy["task"],
        "target_modules": profile["target_modules"],
        "user_constraints": ["最窄验证", "独立 reviewer"],
        "checkpoints": {
            "CP0": "completed",
            "CP1": "in_progress",
            "CP2": "pending",
            "CP3": "pending",
        },
        "actors": {
            "implementation_actor": "ios-smoke-builder",
            "reviewer_actor": "ios-smoke-reviewer",
        },
    }
    results = {
        node["id"]: _result_for_node(plan, node, context)
        for node in plan["nodes"]
        if node["binding"]["kind"] != "tool"
    }
    ledger = RecordedAdapterExecutor(results, context=context).run(plan)
    report = delivery_report(plan, ledger)
    if ledger["final_status"] != "completed" or report["status"] != "completed":
        raise RuntimeError("installed Apple workflow did not complete")

    return {
        "schema_version": "1.0",
        "status": "passed",
        "platform": "apple",
        "fixture": "tests/fixtures/apple-app",
        "installed": {
            "status": install_status,
            "lock_status": lock["status"],
            "selected_platforms": lock["selected_platforms"],
            "selected_runtime_configs": lock["selected_runtime_configs"],
            "selected_packages": [item["id"] for item in lock["selected_packages"]],
            "skill_count": len(lock["skills"]),
        },
        "workflow": {
            "detected_platforms": profile["platforms"],
            "selected_platforms": policy["selected_platforms"],
            "plan_status": plan["status"],
            "plan_fingerprint": plan["fingerprint"],
            "node_count": len(plan["nodes"]),
            "capabilities": [node["capability"] for node in plan["nodes"]],
            "final_status": ledger["final_status"],
            "validation_mode": report["validation"]["mode"],
            "review_status": report["review"]["status"],
        },
        "deferred_platforms": deferred_status,
        "execution_boundary": (
            "Validates installed routing and structured provider evidence; "
            "does not invoke Xcode or modify a business repository."
        ),
    }


def run_smoke(target_root: Path | None = None) -> dict[str, Any]:
    manifests = ROOT / "platforms"
    source_registry = ManifestRegistry.from_directory(manifests)
    deferred_status = {
        platform: source_registry.by_id(platform).value["implementation_status"]
        for platform in DEFERRED_PLATFORMS
    }
    if set(deferred_status.values()) != {"bootstrap-only"}:
        raise RuntimeError("deferred platforms must remain bootstrap-only")

    if target_root is not None:
        target = target_root.expanduser().resolve()
        return _exercise_installed_target(
            target,
            install_status="existing",
            deferred_status=deferred_status,
        )

    with tempfile.TemporaryDirectory(prefix="ios-installed-workflow-") as directory:
        target = Path(directory) / "codex"
        bundle = build_install_bundle(manifests, platforms=["apple"])
        install_result = install_bundle(bundle, target)
        return _exercise_installed_target(
            target,
            install_status=install_result["status"],
            deferred_status=deferred_status,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-root")
    args = parser.parse_args()
    print(dumps(run_smoke(Path(args.target_root) if args.target_root else None)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
