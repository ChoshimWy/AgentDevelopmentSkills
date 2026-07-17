from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from tests.support import FIXTURES, MANIFESTS

from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.models import ContractError, NodeStatus
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
from agent_workflow.qa import (
    PRD_DIMENSIONS,
    aggregate_workflow_results,
    compile_bug_workflow,
    compile_prd_workflow,
    compile_release_workflow,
    qa_fingerprint,
    validate_workflow_bundle,
)
from agent_workflow.registry import ManifestRegistry
from agent_workflow.reporting import delivery_report
from agent_workflow.runtime import NodeStateMachine, RunLedger
from scripts.build_phase4_qa_goldens import _canonical_fixture_profile


ROOT = Path(__file__).resolve().parents[1]
GOLDENS = ROOT / "tests" / "golden" / "phase4-qa"


class QAWorkflowGoldenTests(unittest.TestCase):
    def test_three_workflows_freeze_all_terminal_outcomes(self) -> None:
        index = json.loads((GOLDENS / "golden-index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["entries"]), 15)
        observed = set()
        for entry in index["entries"]:
            path = GOLDENS / entry["path"]
            artifact = json.loads(path.read_text(encoding="utf-8"))
            validate_workflow_bundle(artifact)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
            self.assertEqual(artifact["fingerprint"], entry["artifact_fingerprint"])
            observed.add((artifact["workflow_kind"], artifact["status"]))
        self.assertEqual(observed, {
            (kind, status)
            for kind in ("prd", "bug", "release")
            for status in ("passed", "failed", "blocked", "partial", "cancelled")
        })

    def test_prd_matrix_covers_all_mandatory_dimensions(self) -> None:
        workflow = _golden("prd", "passed")
        self.assertEqual(workflow["traceability"][0]["coverage_dimensions"], sorted(PRD_DIMENSIONS))
        self.assertEqual(len(workflow["test_cases"]), len(PRD_DIMENSIONS))
        self.assertTrue(all(case["requirement_refs"] == ["REQ-DESKTOP-WORKSPACE"] for case in workflow["test_cases"]))

    def test_bug_closure_requires_fix_verification_and_regression_ownership(self) -> None:
        passed = _golden("bug", "passed")
        defect = passed["defect_reports"][0]
        self.assertEqual(defect["status"], "closed")
        self.assertTrue(defect["fix_verification_result_refs"])
        self.assertEqual(defect["regression_case_refs"], [case["case_id"] for case in passed["test_cases"]])
        self.assertEqual(passed["regression_set"]["status"], "current")

        blocked = _golden("bug", "blocked")
        defect = blocked["defect_reports"][0]
        self.assertEqual(defect["reproduction"]["rate"], "not-reproduced")
        self.assertEqual(defect["status"], "blocked")
        self.assertIn("collect-evidence", blocked["next_actions"])

    def test_release_decision_is_derived_from_quality_outcome(self) -> None:
        self.assertEqual(_golden("release", "passed")["report"]["release_recommendation"], "go")
        partial = _golden("release", "partial")["report"]
        self.assertEqual(partial["release_recommendation"], "conditional-go")
        self.assertTrue(partial["waivers"])
        self.assertTrue(partial["residual_risks"])
        for status in ("failed", "blocked", "cancelled"):
            self.assertEqual(_golden("release", status)["report"]["release_recommendation"], "no-go")

        request = _release_request_for_test("known-issue")
        request["known_issues"] = ["unwaived installer risk"]
        compiled = compile_release_workflow(request)
        self.assertEqual(compiled["known_issues"], ["unwaived installer risk"])
        self.assertEqual(compiled["plan"]["verification"]["status"], "pending")
        self.assertNotIn("report", compiled)
        self.assertNotIn("test_results", compiled)

    def test_public_compilers_and_aggregator_cannot_accept_a_claimed_outcome(self) -> None:
        request = _release_request_for_test("forged")
        request["outcome"] = "passed"
        with self.assertRaisesRegex(ContractError, "planning inputs only"):
            compile_release_workflow(request)

        compiled = compile_release_workflow(_release_request_for_test("aggregate"))
        with self.assertRaisesRegex(ContractError, "execution fields"):
            aggregate_workflow_results(compiled, {"schema_version": "1.0", "outcome": "passed"})

    def test_aggregator_replays_only_supplied_artifacts_and_rejects_cross_environment_results(self) -> None:
        golden = _golden("prd", "passed")
        compiled = compile_prd_workflow({
            "environments": golden["plan"]["environments"],
            "objective": golden["plan"]["objective"],
            "requested_level": golden["plan"]["coverage"]["requested_level"],
            "requirements": [{
                "acceptance_criteria": ["The workspace remains usable across supported desktop environments."],
                "id": "REQ-DESKTOP-WORKSPACE",
                "title": "Open the desktop workspace",
            }],
            "risks": golden["plan"]["risks"],
            "schema_version": "1.0",
            "scope": golden["plan"]["scope"],
            "verification_level": golden["report"]["verification"]["level"],
            "workflow_id": golden["workflow_id"],
        })
        execution = _execution_from_golden(golden)
        workflow_plan = execution["workflow_plan"]
        aggregated = aggregate_workflow_results(compiled, execution)
        self.assertEqual(aggregated, golden)
        delivery = delivery_report(workflow_plan, execution["run_ledger"], qa_report=aggregated["report"])
        self.assertEqual(delivery["quality"]["workflow_plan_fingerprint"], workflow_plan["fingerprint"])
        self.assertEqual(delivery["quality"]["status"], "passed")

        forged = deepcopy(execution)
        forged["test_results"][0]["environment_fingerprint"] = "desktop-v1:" + "f" * 64
        forged["test_results"][0]["fingerprint"] = qa_fingerprint({
            key: value for key, value in forged["test_results"][0].items() if key != "fingerprint"
        })
        with self.assertRaisesRegex(ContractError, "outside the frozen plan"):
            aggregate_workflow_results(compiled, forged)

    def test_multi_platform_validation_aggregates_the_worst_current_status(self) -> None:
        golden = _golden("prd", "passed")
        compiled = compile_prd_workflow({
            "environments": golden["plan"]["environments"],
            "objective": golden["plan"]["objective"],
            "requested_level": golden["plan"]["coverage"]["requested_level"],
            "requirements": [{
                "acceptance_criteria": ["The workspace remains usable across supported desktop environments."],
                "id": "REQ-DESKTOP-WORKSPACE",
                "title": "Open the desktop workspace",
            }],
            "risks": golden["plan"]["risks"],
            "schema_version": "1.0",
            "scope": golden["plan"]["scope"],
            "verification_level": golden["report"]["verification"]["level"],
            "workflow_id": golden["workflow_id"],
        })
        execution = _execution_from_golden(golden)
        workflow_plan = _multi_platform_workflow_plan()
        run_ledger = _multi_validation_ledger(workflow_plan, execution["run_ledger"]["run_id"])
        execution["workflow_plan"] = workflow_plan
        execution["run_ledger"] = run_ledger

        self.assertEqual(delivery_report(workflow_plan, run_ledger)["validation"]["status"], "failed")
        with self.assertRaisesRegex(ContractError, "verification conflicts"):
            aggregate_workflow_results(compiled, execution)

    def test_release_cannot_hide_an_unreferenced_open_critical_defect(self) -> None:
        golden = _golden("release", "passed")
        compiled = compile_release_workflow({
            "changes": [{
                "acceptance_criteria": "The window reflows without clipping.",
                "id": "CHANGE-DPI-LAYOUT",
                "title": "DPI-aware window layout",
            }],
            "environments": golden["plan"]["environments"],
            "known_issues": [],
            "objective": golden["plan"]["objective"],
            "requested_level": golden["plan"]["coverage"]["requested_level"],
            "risks": golden["plan"]["risks"],
            "schema_version": "1.0",
            "scope": golden["plan"]["scope"],
            "verification_level": golden["report"]["verification"]["level"],
            "workflow_id": golden["workflow_id"],
        })
        execution = _execution_from_golden(golden)
        defect = {
            "attribution": {"category": "code", "component": "Installer", "confidence": 0.9},
            "blockers": [],
            "defect_id": "DEF-OPEN-CRITICAL",
            "environment_fingerprint": golden["plan"]["environments"][0]["fingerprint"],
            "evidence_refs": [{"kind": "log", "sha256": "a" * 64, "uri": "artifact://qa-test/open-critical"}],
            "fix_verification_result_refs": [],
            "impact": {"regression_risk": "high", "scope": ["desktop-orchestration"]},
            "owner": "release-owner",
            "priority": "p0",
            "regression_case_refs": [],
            "reproduction": {"actual": "Upgrade fails.", "expected": "Upgrade succeeds.", "rate": "always", "steps": ["Run upgrade."]},
            "schema_version": "1.0",
            "severity": "critical",
            "status": "open",
            "title": "Upgrade is blocked",
        }
        defect["fingerprint"] = qa_fingerprint(defect)
        execution["defect_reports"] = [defect]
        with self.assertRaisesRegex(ContractError, "conditional release requires"):
            aggregate_workflow_results(compiled, execution)

    def test_bug_passed_rejects_stale_regression_and_failed_fix_reference(self) -> None:
        golden = _golden("bug", "passed")
        compiled = compile_bug_workflow({
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
            "environments": golden["plan"]["environments"],
            "objective": golden["plan"]["objective"],
            "requested_level": golden["plan"]["coverage"]["requested_level"],
            "risks": golden["plan"]["risks"],
            "schema_version": "1.0",
            "scope": golden["plan"]["scope"],
            "verification_level": golden["report"]["verification"]["level"],
            "workflow_id": golden["workflow_id"],
        })

        stale = _execution_from_golden(golden)
        stale["regression_set"]["status"] = "stale"
        stale["regression_set"]["stale_reasons"] = ["environment-fingerprint-changed"]
        stale["regression_set"]["fingerprint"] = qa_fingerprint({
            key: value for key, value in stale["regression_set"].items() if key != "fingerprint"
        })
        with self.assertRaisesRegex(ContractError, "current regression ownership"):
            aggregate_workflow_results(compiled, stale)

        failed_fix = _execution_from_golden(golden)
        result = failed_fix["test_results"][0]
        result["status"] = "failed"
        result["defect_refs"] = [golden["defect_reports"][0]["defect_id"]]
        result["fingerprint"] = qa_fingerprint({key: value for key, value in result.items() if key != "fingerprint"})
        with self.assertRaisesRegex(ContractError, "fix verification must reference passed"):
            aggregate_workflow_results(compiled, failed_fix)

    def test_partial_blocked_and_cancelled_cannot_hide_as_passed(self) -> None:
        for kind in ("prd", "bug", "release"):
            partial = _golden(kind, "partial")
            self.assertTrue(partial["gaps"])
            self.assertTrue(any(result["status"] == "skipped" for result in partial["test_results"]))
            blocked = _golden(kind, "blocked")
            self.assertTrue(blocked["blockers"])
            self.assertTrue(any(result["status"] == "blocked" for result in blocked["test_results"]))
            cancelled = _golden(kind, "cancelled")
            self.assertTrue(any(result["status"] == "cancelled" for result in cancelled["test_results"]))

            forged = deepcopy(partial)
            forged["status"] = "passed"
            forged["report"]["status"] = "passed"
            forged["report"]["fingerprint"] = qa_fingerprint({key: value for key, value in forged["report"].items() if key != "fingerprint"})
            forged["fingerprint"] = qa_fingerprint({key: value for key, value in forged.items() if key != "fingerprint"})
            with self.assertRaises(ContractError):
                validate_workflow_bundle(forged)

    def test_compilers_fail_closed_on_missing_traceable_inputs(self) -> None:
        base = {
            "environments": [{"attributes": {"os": "macOS"}, "fingerprint": qa_fingerprint({"environment": "macos"}), "id": "macos", "platform": "desktop"}],
            "objective": "invalid",
            "risks": [],
            "schema_version": "1.0",
            "scope": {"excluded": [], "included": ["desktop"]},
            "workflow_id": "invalid",
        }
        with self.assertRaisesRegex(ContractError, "requirements must be non-empty"):
            compile_prd_workflow({**base, "requirements": []})
        with self.assertRaisesRegex(ContractError, "defect fields"):
            compile_bug_workflow({**base, "defect": {}})
        with self.assertRaisesRegex(ContractError, "changes must be non-empty"):
            compile_release_workflow({**base, "changes": []})


def _golden(kind: str, status: str) -> dict:
    return json.loads((GOLDENS / kind / f"{status}.json").read_text(encoding="utf-8"))


def _release_request_for_test(identity: str) -> dict:
    return {
        "changes": [{"acceptance_criteria": "Layout remains visible.", "id": "CHANGE-1", "title": "Layout"}],
        "environments": [{"attributes": {"os": "macOS"}, "fingerprint": qa_fingerprint({"environment": "macos"}), "id": "macos", "platform": "desktop"}],
        "known_issues": [],
        "objective": "release QA",
        "risks": [],
        "schema_version": "1.0",
        "scope": {"excluded": [], "included": ["desktop"]},
        "workflow_id": f"release-{identity}",
    }


def _workflow_plan() -> dict:
    registry = ManifestRegistry.from_directory(MANIFESTS)
    profile = _canonical_fixture_profile(
        DiscoveryEngine(registry).discover(FIXTURES / "desktop-tauri")
    )
    policy = PolicyResolver().resolve(profile, "执行 Desktop Bug 回归测试", explicit_platforms=["desktop"])
    return PlanCompiler(registry).compile(profile, policy)


def _multi_platform_workflow_plan() -> dict:
    registry = ManifestRegistry.from_directory(MANIFESTS)
    profile = DiscoveryEngine(registry).discover(FIXTURES / "unknown")
    policy = PolicyResolver().resolve(
        profile,
        "执行 Apple 与 Desktop 回归测试",
        explicit_platforms=["apple", "desktop"],
    )
    return PlanCompiler(registry).compile(profile, policy)


def _execution_from_golden(golden: dict) -> dict:
    workflow_plan = _workflow_plan()
    return {
        "blockers": deepcopy(golden["blockers"]),
        "declared_gaps": deepcopy(golden["gaps"]),
        "defect_reports": deepcopy(golden["defect_reports"]),
        "evaluated_on": golden["report"]["evaluated_on"],
        "qa_evidence_refs": deepcopy(golden["report"]["evidence_refs"]),
        "regression_set": deepcopy(golden["regression_set"]),
        "residual_risks": deepcopy(golden["report"]["residual_risks"]),
        "run_ledger": _run_ledger(workflow_plan, golden["ledger_identity"]["run_id"]),
        "schema_version": "1.0",
        "test_results": deepcopy(golden["test_results"]),
        "verification": deepcopy(golden["report"]["verification"]),
        "waivers": deepcopy(golden["report"]["waivers"]),
        "workflow_plan": workflow_plan,
    }


def _run_ledger(workflow_plan: dict, run_id: str) -> dict:
    node = next(
        item for item in workflow_plan["nodes"]
        if item["capability"] == "verification.desktop.affected-tests"
    )
    machine = NodeStateMachine()
    attempt = machine.new_attempt(node["id"])
    machine.transition(attempt, NodeStatus.READY, "test-ready")
    machine.transition(attempt, NodeStatus.RUNNING, "test-running")
    machine.transition(attempt, NodeStatus.PASSED, "test-passed")
    ledger = RunLedger(workflow_plan["fingerprint"], run_id=run_id)
    ledger.append("node-attempt", attempt)
    ledger.append("adapter-outcome", {
        "attempt_id": attempt["attempt_id"],
        "cleanup": [],
        "failure_attribution": {"category": "none", "summary": "completed"},
        "invocation_id": f"invocation-{run_id}",
        "node_id": node["id"],
        "provider": node["provider"],
        "request_id": f"request-{run_id}",
        "status": "completed",
    })
    ledger.append("adapter-evidence", {
        "artifact_ids": [],
        "attempt_id": attempt["attempt_id"],
        "data": {"execution_kind": "test"},
        "kind": "validation",
        "node_id": node["id"],
        "provider": node["provider"],
        "status": "passed",
        "summary": "passed",
    })
    return ledger.finalize("completed")


def _multi_validation_ledger(workflow_plan: dict, run_id: str) -> dict:
    nodes = [node for node in workflow_plan["nodes"] if node["capability"].startswith("verification.")]
    ledger = RunLedger(workflow_plan["fingerprint"], run_id=run_id)
    machine = NodeStateMachine()
    for index, node in enumerate(nodes):
        failed = index == 0
        attempt = machine.new_attempt(node["id"])
        machine.transition(attempt, NodeStatus.READY, "test-ready")
        machine.transition(attempt, NodeStatus.RUNNING, "test-running")
        machine.transition(attempt, NodeStatus.FAILED if failed else NodeStatus.PASSED, "test-terminal")
        ledger.append("node-attempt", attempt)
        ledger.append("adapter-outcome", {
            "attempt_id": attempt["attempt_id"],
            "cleanup": [],
            "failure_attribution": {
                "category": "code" if failed else "none",
                "summary": "failed validation" if failed else "completed validation",
            },
            "invocation_id": f"invocation-{run_id}-{index}",
            "node_id": node["id"],
            "provider": node["provider"],
            "request_id": f"request-{run_id}-{index}",
            "status": "failed" if failed else "completed",
        })
        ledger.append("adapter-evidence", {
            "artifact_ids": [],
            "attempt_id": attempt["attempt_id"],
            "data": {"execution_kind": "test"},
            "kind": "validation",
            "node_id": node["id"],
            "provider": node["provider"],
            "status": "failed" if failed else "passed",
            "summary": "failed" if failed else "passed",
        })
    return ledger.finalize("partial")


if __name__ == "__main__":
    unittest.main()
