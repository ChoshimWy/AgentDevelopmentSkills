#!/usr/bin/env python3
"""Build deterministic Phase 4 PRD, Bug and Release workflow goldens."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent_workflow.canonical_json import dump, sha256  # noqa: E402
from agent_workflow.discovery import DiscoveryEngine  # noqa: E402
from agent_workflow.models import NodeStatus  # noqa: E402
from agent_workflow.planning import PlanCompiler  # noqa: E402
from agent_workflow.policy import PolicyResolver  # noqa: E402
from agent_workflow.qa import (  # noqa: E402
    aggregate_workflow_results,
    compile_bug_workflow,
    compile_prd_workflow,
    compile_release_workflow,
    qa_fingerprint,
)
from agent_workflow.registry import ManifestRegistry  # noqa: E402
from agent_workflow.runtime import NodeStateMachine, RunLedger  # noqa: E402
from platforms.desktop.scripts.desktop_discovery import build_environment_profile  # noqa: E402


OUTCOMES = ("passed", "failed", "blocked", "partial", "cancelled")
FIXTURE_REPOSITORY_ROOT = "/agent-workflow-fixtures/desktop-tauri"


def build(output_root: Path) -> dict:
    output_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    compilers = {
        "bug": (compile_bug_workflow, _bug_request),
        "prd": (compile_prd_workflow, _prd_request),
        "release": (compile_release_workflow, _release_request),
    }
    for workflow_kind, (compiler, request_factory) in compilers.items():
        directory = output_root / workflow_kind
        directory.mkdir(parents=True, exist_ok=True)
        for outcome in OUTCOMES:
            request = request_factory(outcome)
            compiled = compiler(request)
            artifact = aggregate_workflow_results(compiled, _fixture_execution(compiled, outcome, request))
            path = directory / f"{outcome}.json"
            dump(artifact, path)
            entries.append({
                "artifact_fingerprint": artifact["fingerprint"],
                "path": path.relative_to(output_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    index = {"entries": entries, "schema_version": "1.0"}
    dump(index, output_root / "golden-index.json")
    return index


def _base(workflow_kind: str, outcome: str) -> dict:
    environment = build_environment_profile(_desktop_environment_facts())
    return {
        "environments": [{
            "attributes": {"arch": "arm64", "dpi": 2, "input": "keyboard-mouse", "os": "macOS"},
            "fingerprint": environment["fingerprint"],
            "id": "desktop-macos-arm64-retina",
            "platform": "desktop",
        }],
        "objective": f"Exercise the Phase 4 {workflow_kind} QA workflow.",
        "requested_level": "targeted",
        "risks": [{
            "categories": ["compatibility", "functional"],
            "id": "RISK-DESKTOP-COMPATIBILITY",
            "impact": 4,
            "likelihood": 3,
            "requirement_refs": [],
            "title": "Desktop environment changes can alter behavior",
        }],
        "schema_version": "1.0",
        "scope": {"excluded": ["production-deployment"], "included": ["desktop-orchestration"]},
        "verification_level": "affected-tests",
        "workflow_id": f"phase4-{workflow_kind}-{outcome}",
    }


def _prd_request(outcome: str) -> dict:
    return {
        **_base("prd", outcome),
        "requirements": [{
            "acceptance_criteria": ["The workspace remains usable across supported desktop environments."],
            "id": "REQ-DESKTOP-WORKSPACE",
            "title": "Open the desktop workspace",
        }],
    }


def _bug_request(outcome: str) -> dict:
    return {
        **_base("bug", outcome),
        "defect": {
            "actual": "The window clips after moving between displays.",
            "expected": "The window reflows without clipping.",
            "id": "BUG-DPI-TRANSITION",
            "owner": "desktop-ui",
            "priority": "p1",
            "reproduction_steps": ["Move the window from a 1x display to a 2x display."],
            "severity": "major",
            "title": "Window clips after DPI transition",
        },
    }


def _release_request(outcome: str) -> dict:
    return {
        **_base("release", outcome),
        "changes": [{
            "acceptance_criteria": "The window reflows without clipping.",
            "id": "CHANGE-DPI-LAYOUT",
            "title": "DPI-aware window layout",
        }],
        "known_issues": [] if outcome == "passed" else ["legacy installer upgrade path is not automated"],
    }


def _fixture_execution(compiled: dict, outcome: str, request: dict) -> dict:
    """Build synthetic artifacts only for committed conformance fixtures."""

    workflow_id = compiled["workflow_id"]
    cases = compiled["test_cases"]
    plan = compiled["plan"]
    environment_fingerprint = plan["environments"][0]["fingerprint"]
    defect_id = f"DEF-{workflow_id}"
    blockers = ["desktop-provider-unavailable"] if outcome == "blocked" else []
    results: list[dict] = []
    for index, case in enumerate(cases):
        status = _fixture_result_status(outcome, index)
        value = {
            "attempt_id": f"attempt-{workflow_id}-{index + 1:02d}",
            "blockers": blockers if status in {"blocked", "cancelled"} else [],
            "case_id": case["case_id"],
            "defect_refs": [defect_id] if status == "failed" else [],
            "environment_fingerprint": environment_fingerprint,
            "evidence_refs": [_evidence(f"{workflow_id}-result-{index + 1}", "test-report")] if status in {"passed", "failed"} else [],
            "plan_fingerprint": plan["fingerprint"],
            "result_id": f"TR-{workflow_id}-{index + 1:02d}",
            "schema_version": "1.0",
            "status": status,
            "test_data_fingerprint": qa_fingerprint(case["test_data"]),
        }
        value["fingerprint"] = qa_fingerprint(value)
        results.append(value)

    defects: list[dict] = []
    if compiled["workflow_kind"] == "bug" or outcome == "failed":
        source = request.get("defect") or {
            "actual": "Observed behavior violates the expected result.",
            "expected": "All scoped acceptance criteria pass.",
            "owner": "qa-owner",
            "priority": "p1",
            "reproduction_steps": ["Run the failed QA case."],
            "severity": "major",
            "title": f"{compiled['workflow_kind']} QA failure",
        }
        bug_workflow = compiled["workflow_kind"] == "bug"
        defect_status = "closed" if bug_workflow and outcome == "passed" else "blocked" if bug_workflow and outcome == "blocked" else "open"
        reproduction_rate = "not-reproduced" if defect_status == "blocked" else "always"
        defect_blockers = blockers if defect_status == "blocked" else ["collect-additional-regression-evidence"] if bug_workflow and outcome == "partial" else []
        defect = {
            "attribution": {"category": "unknown" if defect_status == "blocked" else "code", "component": None, "confidence": 0 if defect_status == "blocked" else 0.8},
            "blockers": defect_blockers,
            "defect_id": defect_id,
            "environment_fingerprint": environment_fingerprint,
            "evidence_refs": [_evidence(f"{workflow_id}-defect", "log")],
            "fix_verification_result_refs": [results[0]["result_id"]] if defect_status == "closed" else [],
            "impact": {"regression_risk": "high", "scope": plan["scope"]["included"]},
            "owner": source["owner"],
            "priority": source["priority"],
            "regression_case_refs": [case["case_id"] for case in cases] if defect_status == "closed" else [],
            "reproduction": {
                "actual": "Not observed in the frozen environment." if reproduction_rate == "not-reproduced" else source["actual"],
                "expected": source["expected"],
                "rate": reproduction_rate,
                "steps": source["reproduction_steps"],
            },
            "schema_version": "1.0",
            "severity": source["severity"],
            "status": defect_status,
            "title": source["title"],
        }
        defect["fingerprint"] = qa_fingerprint(defect)
        defects.append(defect)

    regression = None
    if compiled["workflow_kind"] == "bug":
        regression_status = "current" if outcome == "passed" else "stale" if outcome in {"failed", "partial"} else "blocked"
        regression = {
            "case_refs": [case["case_id"] for case in cases],
            "defect_refs": [defect_id],
            "environment_fingerprints": [environment_fingerprint],
            "schema_version": "1.0",
            "set_id": f"REG-{workflow_id}",
            "source_fingerprints": [defects[0]["fingerprint"]],
            "stale_reasons": [] if regression_status == "current" else ["fix-verification-failed" if outcome == "failed" else "regression-evidence-incomplete"],
            "status": regression_status,
        }
        regression["fingerprint"] = qa_fingerprint(regression)

    verification_status = "passed" if outcome in {"passed", "failed", "blocked"} else outcome
    verification = {
        "evidence_refs": [_evidence(f"{workflow_id}-verification", "test-report")] if verification_status == "passed" else [],
        "level": plan["verification"]["level"],
        "status": verification_status,
    }
    workflow_plan = _fixture_workflow_plan()
    run_ledger = _fixture_run_ledger(workflow_plan, f"run-{workflow_id}", verification_status)
    return {
        "blockers": blockers,
        "declared_gaps": ["coverage-incomplete"] if outcome == "partial" else [],
        "defect_reports": defects,
        "evaluated_on": "2026-07-16",
        "qa_evidence_refs": [_evidence(f"{workflow_id}-qa", "structured-report")],
        "regression_set": regression,
        "residual_risks": [],
        "schema_version": "1.0",
        "test_results": results,
        "verification": verification,
        "workflow_plan": workflow_plan,
        "run_ledger": run_ledger,
        "waivers": [{
            "expires_on": "2026-08-15",
            "id": f"WAIVER-{workflow_id}",
            "owner": "release-owner",
            "reason": "Accept explicitly disclosed partial release coverage.",
        }] if compiled["workflow_kind"] == "release" and outcome == "partial" else [],
    }


def _fixture_result_status(outcome: str, index: int) -> str:
    if outcome == "passed":
        return "passed"
    if outcome == "failed":
        return "failed" if index == 0 else "passed"
    if outcome == "blocked":
        return "blocked" if index == 0 else "skipped"
    if outcome == "partial":
        return "passed" if index == 0 else "skipped"
    return "cancelled" if index == 0 else "skipped"


def _evidence(seed: str, kind: str) -> dict[str, str]:
    return {"kind": kind, "sha256": sha256({"fixture_seed": seed}), "uri": f"artifact://qa-fixture/{seed}"}


def _desktop_environment_facts() -> dict:
    import json

    return json.loads((ROOT / "tests" / "fixtures" / "desktop-environment.json").read_text(encoding="utf-8"))


def _fixture_workflow_plan() -> dict:
    registry = ManifestRegistry.from_directory(ROOT / "platforms")
    profile = _canonical_fixture_profile(
        DiscoveryEngine(registry).discover(ROOT / "tests" / "fixtures" / "desktop-tauri")
    )
    policy = PolicyResolver().resolve(
        profile,
        "执行 Desktop Bug 回归测试",
        explicit_platforms=["desktop"],
    )
    return PlanCompiler(registry).compile(profile, policy)


def _canonical_fixture_profile(profile: dict) -> dict:
    # Discovery intentionally records absolute paths for a real repository.  Golden
    # fixtures, however, must remain identical after the source distribution is
    # relocated, so replace the two location-bearing fields with a stable fixture
    # identity before the profile is fingerprinted by PlanCompiler.
    profile["explicit_context"]["cwd"] = FIXTURE_REPOSITORY_ROOT
    profile["repository"]["root"] = FIXTURE_REPOSITORY_ROOT
    return profile


def _fixture_run_ledger(workflow_plan: dict, run_id: str, verification_status: str) -> dict:
    if verification_status == "cancelled":
        return RunLedger(workflow_plan["fingerprint"], run_id=run_id).finalize("cancelled")

    verification_node = next(
        node for node in workflow_plan["nodes"]
        if node["capability"] == "verification.desktop.affected-tests"
    )
    machine = NodeStateMachine()
    attempt = machine.new_attempt(verification_node["id"])
    if verification_status == "passed":
        machine.transition(attempt, NodeStatus.READY, "fixture-ready")
        machine.transition(attempt, NodeStatus.RUNNING, "fixture-running")
        machine.transition(attempt, NodeStatus.PASSED, "fixture-passed")
        outcome_status = "completed"
        final_status = "completed"
        attribution = {"category": "none", "summary": "Controlled fixture validation completed."}
    elif verification_status in {"partial", "blocked"}:
        machine.transition(attempt, NodeStatus.BLOCKED, f"fixture-{verification_status}")
        outcome_status = verification_status
        final_status = verification_status
        attribution = {"category": "environment", "summary": "Controlled fixture validation is incomplete."}
    elif verification_status == "failed":
        machine.transition(attempt, NodeStatus.READY, "fixture-ready")
        machine.transition(attempt, NodeStatus.RUNNING, "fixture-running")
        machine.transition(attempt, NodeStatus.FAILED, "fixture-failed")
        outcome_status = "failed"
        final_status = "partial"
        attribution = {"category": "code", "summary": "Controlled fixture validation failed."}
    else:
        raise ValueError(f"unsupported fixture verification status: {verification_status}")
    ledger = RunLedger(workflow_plan["fingerprint"], run_id=run_id)
    ledger.append("node-attempt", attempt)
    ledger.append("adapter-outcome", {
        "attempt_id": attempt["attempt_id"],
        "cleanup": [],
        "failure_attribution": attribution,
        "invocation_id": f"invocation-{run_id}",
        "node_id": verification_node["id"],
        "provider": verification_node["provider"],
        "request_id": f"request-{run_id}",
        "status": outcome_status,
    })
    ledger.append("adapter-evidence", {
        "artifact_ids": [],
        "attempt_id": attempt["attempt_id"],
        "data": {"execution_kind": "controlled-conformance-runner"},
        "kind": "validation",
        "node_id": verification_node["id"],
        "provider": verification_node["provider"],
        "status": verification_status,
        "summary": f"Controlled fixture validation status: {verification_status}.",
    })
    return ledger.finalize(final_status)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "tests" / "golden" / "phase4-qa",
        help="Golden output directory.",
    )
    parser.add_argument("--check", action="store_true", help="Verify committed goldens without rewriting them.")
    arguments = parser.parse_args()
    if arguments.check:
        if not arguments.output.is_dir():
            print(f"FAIL missing Phase 4 QA golden directory: {arguments.output}")
            return 1
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "phase4-qa"
            build(generated)
            differences = _tree_differences(arguments.output, generated)
        if differences:
            print("FAIL Phase 4 QA goldens differ: " + ", ".join(differences))
            return 1
        print(f"PASS verified {len(OUTCOMES) * 3} Phase 4 QA workflow goldens")
        return 0
    index = build(arguments.output)
    print(f"PASS built {len(index['entries'])} Phase 4 QA workflow goldens")
    return 0


def _tree_differences(expected: Path, actual: Path) -> list[str]:
    expected_files = {path.relative_to(expected).as_posix(): path for path in expected.rglob("*") if path.is_file()}
    actual_files = {path.relative_to(actual).as_posix(): path for path in actual.rglob("*") if path.is_file()}
    differences = sorted(set(expected_files) ^ set(actual_files))
    differences.extend(
        name for name in sorted(set(expected_files) & set(actual_files))
        if expected_files[name].read_bytes() != actual_files[name].read_bytes()
    )
    return differences


if __name__ == "__main__":
    raise SystemExit(main())
