"""Deterministic comparison between frozen Apple legacy routing and Core plans."""

from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any

from ..canonical_json import sha256
from ..adapters import build_adapter_request
from ..discovery import DiscoveryEngine
from ..planning import PlanCompiler
from ..policy import PolicyResolver
from ..registry import ManifestRegistry
from ..runtime import RecordedAdapterExecutor


def compare_apple_routes(
    baseline: dict[str, Any],
    *,
    repository_fixtures: str | Path,
    registry: ManifestRegistry,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for case in baseline["cases"]:
        profile = DiscoveryEngine(registry).discover(Path(repository_fixtures) / case["repository_fixture"])
        policy = PolicyResolver().resolve(profile, case["task"], explicit_platforms=["apple"])
        plan = PlanCompiler(registry).compile(profile, policy)
        actual_capabilities = [node["capability"] for node in plan["nodes"]]
        differences: list[dict[str, str]] = []
        _compare(differences, "task_type", case["expected_task_type"], policy["task"]["type"])
        _compare_capability_extension(
            differences,
            case["expected_capabilities"],
            case.get("allowed_additional_capabilities", []),
            actual_capabilities,
        )
        _compare(differences, "roles", case["legacy_workflow"]["roles"], plan["workflow"]["roles"])
        _compare(differences, "checkpoints", case["legacy_workflow"]["checkpoints"], plan["workflow"]["checkpoints"])
        _compare(
            differences,
            "independent_review",
            case["legacy_workflow"]["independent_review"],
            plan["workflow"]["independent_review"],
        )
        cases.append(
            {
                "case_id": case["id"],
                "differences": differences,
                "plan_fingerprint": plan["fingerprint"],
                "status": "matched" if not differences and plan["status"] == "ready" else "different",
            }
        )
    content = {
        "baseline_sha256": sha256(baseline),
        "cases": cases,
        "provider_registry_sha256": registry.digest(),
        "schema_version": "1.0",
    }
    return {"status": "matched" if all(case["status"] == "matched" for case in cases) else "different", **content}


def run_apple_dual_route_smoke(
    baseline: dict[str, Any],
    *,
    repository_fixtures: str | Path,
    registry: ManifestRegistry,
    disabled_registry: ManifestRegistry,
) -> dict[str, Any]:
    """Execute a no-side-effect Core Adapter smoke against frozen legacy evidence."""

    fixture = Path(repository_fixtures) / baseline["repository_fixture"]
    profile = DiscoveryEngine(registry).discover(fixture)
    policy = PolicyResolver().resolve(profile, baseline["task"], explicit_platforms=["apple"])
    plan = PlanCompiler(registry).compile(profile, policy)
    context = {
        "actors": {"implementation_actor": "legacy-main", "reviewer_actor": "core-reviewer"},
        "checkpoints": {"CP0": "completed", "CP1": "completed", "CP2": "completed", "CP3": "in_progress"},
        "target_modules": profile.get("target_modules", []),
        "task": policy["task"],
        "user_constraints": ["no-write", "no-xcode", "no-network"],
    }
    results: dict[str, dict[str, Any]] = {}
    for node in plan["nodes"]:
        if node.get("provider") == "core":
            continue
        invocation_id = f"dual-route-smoke-{node['id']}-1"
        request = build_adapter_request(
            plan, node["id"], context=context, invocation_id=invocation_id
        )
        is_review = node["capability"].startswith("review.")
        evidence = {
            "artifact_ids": [],
            "data": (
                {
                    "blocking_issues": [],
                    "implementation_actor": "legacy-main",
                    "reviewer_actor": "core-reviewer",
                }
                if is_review
                else {"project_facts": baseline["project_facts"], "validation_level": "none"}
            ),
            "kind": "review" if is_review else "diagnostic",
            "status": "passed" if is_review else "completed",
            "summary": "no-side-effect dual-route smoke evidence",
        }
        results[node["id"]] = {
            "artifacts": [],
            "binding": request["binding"],
            "capability": request["capability"],
            "cleanup": [],
            "evidence": [evidence],
            "failure_attribution": {"category": "none", "summary": "no failure"},
            "invocation_id": request["invocation_id"],
            "node_id": request["node_id"],
            "plan_fingerprint": request["plan_fingerprint"],
            "provider": request["provider"],
            "request_id": request["request_id"],
            "schema_version": "1.0",
            "status": "completed",
        }
    ledger = RecordedAdapterExecutor(results, context=context).run(plan)

    disabled_profile = DiscoveryEngine(disabled_registry).discover(fixture)
    disabled_policy = PolicyResolver().resolve(
        disabled_profile, baseline["task"], explicit_platforms=["apple"]
    )
    disabled_plan = PlanCompiler(disabled_registry).compile(disabled_profile, disabled_policy)

    actual_capabilities = [node["capability"] for node in plan["nodes"]]
    actual_file_hashes = {
        relative_path: hashlib.sha256((fixture / relative_path).read_bytes()).hexdigest()
        for relative_path in baseline["project_facts"]["file_sha256"]
    }
    checks = {
        "capabilities_match": _capability_extension_matches(
            baseline["expected_core_capabilities"],
            baseline.get("allowed_additional_capabilities", []),
            actual_capabilities,
        ),
        "checkpoints_match": plan["workflow"]["checkpoints"] == baseline["checkpoints"],
        "core_completed": ledger["final_status"] == "completed",
        "core_disabled_is_blocked": disabled_plan["status"] == "blocked",
        "legacy_completed_without_core": (
            baseline["legacy"]["final_status"] == "completed"
            and baseline["legacy"]["core_cli_used"] is False
        ),
        "legacy_evidence_has_provenance": bool(
            baseline["legacy"].get("execution_evidence", {}).get("evidence_artifact")
            and baseline["legacy"]["execution_evidence"].get("route")
        ),
        "legacy_fixture_facts_match": actual_file_hashes == baseline["project_facts"]["file_sha256"],
        "roles_match": plan["workflow"]["roles"] == baseline["legacy"]["logical_roles"],
        "side_effects_are_none": (
            all(not node.get("side_effects") for node in plan["nodes"])
            and not any(baseline["legacy"]["side_effects"].values())
        ),
        "task_type_matches": policy["task"]["type"] == baseline["legacy"]["task_type"],
        "validation_level_matches": baseline["legacy"]["validation_level"] == "none",
    }
    content = {
        "baseline_sha256": sha256(baseline),
        "checks": checks,
        "core": {
            "evidence_command": "PYTHONPATH=src python3 scripts/run_apple_dual_route_smoke.py",
            "evidence_kinds": sorted({item["kind"] for item in ledger["evidence"]}),
            "final_status": ledger["final_status"],
            "plan_fingerprint": plan["fingerprint"],
            "structured_evidence_count": len(ledger["evidence"]),
        },
        "fallback": {
            "disabled_core_plan_status": disabled_plan["status"],
            "legacy_independent_status": baseline["legacy"]["final_status"],
        },
        "legacy": baseline["legacy"],
        "schema_version": "1.0",
    }
    return {"status": "matched" if all(checks.values()) else "different", **content}


def _compare(differences: list[dict[str, str]], field: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        differences.append(
            {
                "actual": repr(actual),
                "expected": repr(expected),
                "field": field,
                "reason_code": "LEGACY_CORE_CONTRACT_MISMATCH",
            }
        )


def _capability_extension_matches(
    legacy: list[str], allowed_additional: list[str], actual: list[str]
) -> bool:
    """Allow audited P2C nodes without weakening the frozen legacy route contract."""

    legacy_set = set(legacy)
    filtered_legacy = [capability for capability in actual if capability in legacy_set]
    additional = [capability for capability in actual if capability not in legacy_set]
    return filtered_legacy == legacy and additional == allowed_additional


def _compare_capability_extension(
    differences: list[dict[str, str]],
    legacy: list[str],
    allowed_additional: list[str],
    actual: list[str],
) -> None:
    if _capability_extension_matches(legacy, allowed_additional, actual):
        return
    differences.append(
        {
            "actual": repr(actual),
            "expected": repr({"legacy": legacy, "allowed_additional": allowed_additional}),
            "field": "capabilities",
            "reason_code": "LEGACY_CORE_CONTRACT_MISMATCH",
        }
    )
