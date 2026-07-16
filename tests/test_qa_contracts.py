from __future__ import annotations

from copy import deepcopy
import json
import unittest

from tests.support import MANIFESTS

from agent_workflow.canonical_json import sha256
from agent_workflow.contracts import validate
from agent_workflow.installation import build_install_bundle
from agent_workflow.models import ContractError
from agent_workflow.qa import (
    compile_coverage,
    qa_fingerprint,
    validate_defect_report,
    validate_qa_plan,
    validate_qa_report,
    validate_regression_set,
    validate_test_case,
    validate_test_result,
)
from agent_workflow.registry import ManifestRegistry
from agent_workflow.reporting import delivery_report


def fingerprint(seed: str) -> str:
    return qa_fingerprint({"seed": seed})


def evidence(kind: str = "structured-report", seed: str = "1") -> dict:
    return {"kind": kind, "sha256": seed * 64, "uri": f"artifact://qa/{kind}-{seed}"}


def identified(label: str, identity_field: str, body: dict) -> dict:
    value = {identity_field: f"{label}-fixture", **body}
    return {**value, "fingerprint": qa_fingerprint(value)}


def risk() -> dict:
    return {
        "categories": ["compatibility", "functional"],
        "id": "risk-desktop-rendering",
        "impact": 4,
        "likelihood": 3,
        "requirement_refs": ["REQ-1"],
        "title": "DPI changes can corrupt layout",
    }


def qa_plan() -> dict:
    risks = [risk()]
    body = {
        "blockers": [],
        "coverage": compile_coverage(risks, workflow_kind="bug", requested_level="targeted"),
        "entry_criteria": ["reproduction-environment-frozen"],
        "environments": [{
            "attributes": {"arch": "arm64", "dpi": 2, "os": "macOS"},
            "fingerprint": fingerprint("environment"),
            "id": "macos-arm64-retina",
            "platform": "desktop",
        }],
        "exit_criteria": ["fix-verified", "regression-owned"],
        "objective": "Verify the desktop rendering defect and its regression surface.",
        "risks": risks,
        "schema_version": "1.0",
        "scope": {"excluded": ["installer"], "included": ["window-rendering"]},
        "status": "planned",
        "verification": {"evidence_refs": [], "level": "affected-tests", "status": "pending"},
        "workflow_kind": "bug",
    }
    return identified("qa-plan", "plan_id", body)


def test_case() -> dict:
    body = {
        "automation_suitability": "high",
        "coverage_tags": ["compatibility", "window"],
        "expected_results": ["layout-remains-within-window"],
        "preconditions": ["retina-display-connected"],
        "requirement_refs": ["REQ-1"],
        "risk_refs": ["risk-desktop-rendering"],
        "schema_version": "1.0",
        "status": "active",
        "steps": [{"action": "Move the window to the Retina display.", "expected": "Layout reflows without clipping.", "number": 1}],
        "test_data": {"window_size": "800x600"},
        "title": "Window survives a DPI transition",
    }
    return identified("test-case", "case_id", body)


def qa_report(*, status: str = "passed", verification_status: str = "passed") -> dict:
    quality = {
        "blocked": 0,
        "cancelled": 0,
        "coverage_level": "compatibility",
        "defect_refs": [],
        "executed": 1,
        "failed": 0,
        "gaps": [],
        "passed": 1,
        "skipped": 0,
        "total": 1,
    }
    blockers = []
    if status == "blocked":
        quality.update({"blocked": 1, "executed": 1, "passed": 0})
        blockers = ["desktop-environment-unavailable"]
    body = {
        "blockers": blockers,
        "delivery_context": {
            "run_id": "run-1",
            "workflow_plan_fingerprint": "workflow-plan-fingerprint-1",
            "workflow_plan_id": "plan-1",
        },
        "evidence_refs": [evidence("structured-report", "2")],
        "evaluated_on": "2026-07-16",
        "plan_fingerprint": qa_plan()["fingerprint"],
        "quality": quality,
        "release_recommendation": "not-applicable",
        "residual_risks": [],
        "schema_version": "1.0",
        "status": status,
        "verification": {
            "evidence_refs": [evidence("test-report", "1")] if verification_status == "passed" else [],
            "level": "affected-tests",
            "status": verification_status,
        },
        "waivers": [],
        "workflow_kind": "bug",
    }
    return identified("qa-report", "report_id", body)


class QACoverageCompilerTests(unittest.TestCase):
    def test_compiler_elevates_compatibility_risk_without_selecting_verification(self) -> None:
        coverage = compile_coverage([risk()], workflow_kind="bug", requested_level="targeted")
        self.assertEqual(coverage["compiled_level"], "compatibility")
        self.assertNotIn("verification", coverage)
        self.assertIn("category-floor:compatibility:compatibility", coverage["rationales"])

    def test_release_workflow_is_release_candidate_and_deterministic(self) -> None:
        first = compile_coverage([], workflow_kind="release")
        second = compile_coverage([], workflow_kind="release")
        self.assertEqual(first, second)
        self.assertEqual(first["compiled_level"], "release-candidate")

    def test_invalid_or_unsorted_risks_fail_closed(self) -> None:
        risks = [risk(), {**risk(), "id": "risk-a"}]
        with self.assertRaisesRegex(ContractError, "sorted and unique"):
            compile_coverage(risks, workflow_kind="prd")


class QAContractTests(unittest.TestCase):
    def test_qa_plan_and_case_are_deterministic_contracts(self) -> None:
        plan = qa_plan()
        case = test_case()
        validate_qa_plan(plan)
        validate_test_case(case)
        validate("qa-plan", plan)
        validate("test-case", case)
        self.assertEqual(plan["coverage"]["compiled_level"], "compatibility")

    def test_desktop_environment_fingerprint_flows_through_qa_artifacts(self) -> None:
        desktop_environment = "desktop-v1:" + "a" * 64
        plan = deepcopy(qa_plan())
        plan["environments"][0]["fingerprint"] = desktop_environment
        plan["fingerprint"] = qa_fingerprint({key: value for key, value in plan.items() if key != "fingerprint"})
        validate_qa_plan(plan)
        validate("qa-plan", plan)

        result = identified("test-result", "result_id", {
            "attempt_id": "desktop-attempt",
            "blockers": [],
            "case_id": test_case()["case_id"],
            "defect_refs": [],
            "environment_fingerprint": desktop_environment,
            "evidence_refs": [evidence("test-report", "5")],
            "plan_fingerprint": plan["fingerprint"],
            "schema_version": "1.0",
            "status": "passed",
            "test_data_fingerprint": fingerprint("test-data"),
        })
        validate_test_result(result)
        validate("test-result", result)

        defect = identified("defect-report", "defect_id", {
            "attribution": {"category": "code", "component": "DesktopWindow", "confidence": 0.8},
            "blockers": [],
            "environment_fingerprint": desktop_environment,
            "evidence_refs": [evidence("log", "6")],
            "fix_verification_result_refs": [],
            "impact": {"regression_risk": "medium", "scope": ["desktop-window"]},
            "owner": "desktop-ui",
            "priority": "p2",
            "regression_case_refs": [],
            "reproduction": {"actual": "Clipped", "expected": "Visible", "rate": "always", "steps": ["Resize window"]},
            "schema_version": "1.0",
            "severity": "minor",
            "status": "open",
            "title": "Desktop window clips",
        })
        validate_defect_report(defect)
        validate("defect-report", defect)

        regression = identified("regression-set", "set_id", {
            "case_refs": [test_case()["case_id"]],
            "defect_refs": [defect["defect_id"]],
            "environment_fingerprints": [desktop_environment],
            "schema_version": "1.0",
            "source_fingerprints": [defect["fingerprint"]],
            "stale_reasons": [],
            "status": "current",
        })
        validate_regression_set(regression)
        validate("regression-set", regression)

        invalid = deepcopy(result)
        invalid["environment_fingerprint"] = "unknown-v1:" + "a" * 64
        with self.assertRaisesRegex(ContractError, "environment_fingerprint"):
            validate_test_result(invalid)

    def test_stable_business_id_survives_artifact_revision(self) -> None:
        original = test_case()
        revised = deepcopy(original)
        revised["status"] = "deprecated"
        revised["fingerprint"] = qa_fingerprint({key: value for key, value in revised.items() if key != "fingerprint"})
        validate_test_case(revised)
        self.assertEqual(revised["case_id"], original["case_id"])
        self.assertNotEqual(revised["fingerprint"], original["fingerprint"])

    def test_qa_plan_cannot_bypass_canonical_risk_coverage(self) -> None:
        plan = qa_plan()
        plan["coverage"] = {
            "compiled_level": "targeted",
            "dimensions": ["functional"],
            "rationales": ["manual"],
            "requested_level": "targeted",
            "risk_refs": [],
        }
        with self.assertRaisesRegex(ContractError, "canonical risk compilation"):
            validate_qa_plan(plan)

    def test_test_result_requires_evidence_and_defect_for_failure(self) -> None:
        body = {
            "attempt_id": "attempt-1",
            "blockers": [],
            "case_id": test_case()["case_id"],
            "defect_refs": ["DEF-1"],
            "environment_fingerprint": fingerprint("environment"),
            "evidence_refs": [evidence("screenshot", "3")],
            "plan_fingerprint": qa_plan()["fingerprint"],
            "schema_version": "1.0",
            "status": "failed",
            "test_data_fingerprint": fingerprint("test-data"),
        }
        result = identified("test-result", "result_id", body)
        validate_test_result(result)
        invalid = deepcopy(result)
        invalid["defect_refs"] = []
        with self.assertRaisesRegex(ContractError, "requires defect_refs"):
            validate_test_result(invalid)

    def test_defect_cannot_close_without_fix_and_regression_ownership(self) -> None:
        body = {
            "attribution": {"category": "code", "component": "WindowLayout", "confidence": 0.9},
            "blockers": [],
            "environment_fingerprint": fingerprint("environment"),
            "evidence_refs": [evidence("screenshot", "3")],
            "fix_verification_result_refs": ["result-fix"],
            "impact": {"regression_risk": "high", "scope": ["window-rendering"]},
            "owner": "desktop-ui",
            "priority": "p1",
            "regression_case_refs": [test_case()["case_id"]],
            "reproduction": {"actual": "Content clips.", "expected": "Content reflows.", "rate": "always", "steps": ["Move window between displays."]},
            "schema_version": "1.0",
            "severity": "major",
            "status": "closed",
            "title": "Window clips after DPI transition",
        }
        defect = identified("defect-report", "defect_id", body)
        validate_defect_report(defect)
        invalid = deepcopy(defect)
        invalid["regression_case_refs"] = []
        with self.assertRaisesRegex(ContractError, "regression ownership"):
            validate_defect_report(invalid)

    def test_not_reproduced_defect_preserves_next_evidence_action(self) -> None:
        body = {
            "attribution": {"category": "unknown", "component": None, "confidence": 0},
            "blockers": ["collect-user-dpi-profile"],
            "environment_fingerprint": fingerprint("environment"),
            "evidence_refs": [evidence("log", "4")],
            "fix_verification_result_refs": [],
            "impact": {"regression_risk": "medium", "scope": ["window-rendering"]},
            "owner": "desktop-ui",
            "priority": "p2",
            "regression_case_refs": [],
            "reproduction": {"actual": "Not observed.", "expected": "Content clips.", "rate": "not-reproduced", "steps": ["Replay the reported display transition."]},
            "schema_version": "1.0",
            "severity": "minor",
            "status": "blocked",
            "title": "Reported DPI transition clipping",
        }
        validate_defect_report(identified("defect-report", "defect_id", body))

    def test_regression_set_becomes_stale_when_environment_changes(self) -> None:
        body = {
            "case_refs": [test_case()["case_id"]],
            "defect_refs": ["DEF-1"],
            "environment_fingerprints": [fingerprint("environment-v2")],
            "schema_version": "1.0",
            "source_fingerprints": [fingerprint("source")],
            "stale_reasons": ["environment-fingerprint-changed"],
            "status": "stale",
        }
        regression = identified("regression-set", "set_id", body)
        validate_regression_set(regression)
        invalid = deepcopy(regression)
        invalid["stale_reasons"] = []
        with self.assertRaisesRegex(ContractError, "requires reasons"):
            validate_regression_set(invalid)

    def test_verification_passed_does_not_override_blocked_qa(self) -> None:
        report = qa_report(status="blocked", verification_status="passed")
        validate_qa_report(report)
        self.assertEqual(report["verification"]["status"], "passed")
        self.assertEqual(report["status"], "blocked")

    def test_release_recommendations_are_fail_closed(self) -> None:
        report = qa_report()
        report["workflow_kind"] = "release"
        report["release_recommendation"] = "go"
        body = {key: value for key, value in report.items() if key not in {"report_id", "fingerprint"}}
        report = identified("qa-report", "report_id", body)
        validate_qa_report(report)
        invalid = deepcopy(report)
        invalid["residual_risks"] = ["untested-upgrade-path"]
        with self.assertRaisesRegex(ContractError, "go recommendation"):
            validate_qa_report(invalid)

    def test_passed_and_partial_reports_cannot_fabricate_coverage(self) -> None:
        passed = qa_report()
        passed["quality"]["total"] = 0
        passed["quality"]["executed"] = 0
        passed["quality"]["passed"] = 0
        with self.assertRaisesRegex(ContractError, "requires executed cases"):
            validate_qa_report(passed)

        partial = qa_report(status="partial")
        with self.assertRaisesRegex(ContractError, "explicit coverage gaps"):
            validate_qa_report(partial)

        skipped = qa_report()
        skipped["quality"].update({"executed": 0, "passed": 0, "skipped": 1})
        with self.assertRaisesRegex(ContractError, "cannot hide gaps"):
            validate_qa_report(skipped)

        partly_skipped = qa_report()
        partly_skipped["quality"].update({"passed": 1, "skipped": 1, "total": 2})
        with self.assertRaisesRegex(ContractError, "cannot hide gaps"):
            validate_qa_report(partly_skipped)

        partly_cancelled = qa_report()
        partly_cancelled["quality"].update({"cancelled": 1, "passed": 1, "total": 2})
        with self.assertRaisesRegex(ContractError, "cannot hide gaps"):
            validate_qa_report(partly_cancelled)

    def test_release_go_requires_passed_verification_and_unexpired_waivers(self) -> None:
        report = qa_report()
        report["workflow_kind"] = "release"
        report["release_recommendation"] = "go"
        report["verification"] = {"evidence_refs": [], "level": "full", "status": "failed"}
        report = identified(
            "qa-report",
            "report_id",
            {key: value for key, value in report.items() if key not in {"report_id", "fingerprint"}},
        )
        with self.assertRaisesRegex(ContractError, "passed QA and verification"):
            validate_qa_report(report)

        conditional = qa_report(status="partial")
        conditional["workflow_kind"] = "release"
        conditional["quality"].update({"gaps": ["upgrade-path"], "passed": 0, "skipped": 1, "executed": 0})
        conditional["release_recommendation"] = "conditional-go"
        conditional["residual_risks"] = ["upgrade-path"]
        conditional["waivers"] = [{"expires_on": "2026-07-15", "id": "waiver-1", "owner": "release-owner", "reason": "defer upgrade coverage"}]
        conditional = identified(
            "qa-report",
            "report_id",
            {key: value for key, value in conditional.items() if key not in {"report_id", "fingerprint"}},
        )
        with self.assertRaisesRegex(ContractError, "expired"):
            validate_qa_report(conditional)


class QAIntegrationTests(unittest.TestCase):
    def test_qa_discipline_manifest_registers_contract_capabilities(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        self.assertEqual(registry.resolve_binding("qa.coverage.compile").provider_id, "qa")
        self.assertEqual(registry.resolve_binding("qa.report.aggregate").binding["mode"], "report")

    def test_qa_discipline_is_explicitly_installable_with_one_skill_target(self) -> None:
        bundle = build_install_bundle(MANIFESTS, disciplines=["qa"])
        self.assertEqual(bundle.plan["status"], "planned")
        self.assertEqual(bundle.plan["selected_disciplines"], ["qa"])
        self.assertEqual(
            [item["name"] for item in bundle.plan["skills"] if item["package"] == "qa"],
            ["qa-workflow"],
        )
        qa_bindings = {
            capability: metadata["binding"]
            for capability, metadata in bundle.plan["bindings"].items()
            if metadata["package"] == "qa"
        }
        self.assertEqual({binding["name"] for binding in qa_bindings.values()}, {"qa-workflow"})

    def test_delivery_report_keeps_verification_and_quality_independent(self) -> None:
        plan = {
            "fingerprint": "workflow-plan-fingerprint-1",
            "missing_capabilities": [],
            "plan_id": "plan-1",
            "status": "ready",
        }
        ledger = {
            "adapter_outcomes": [],
            "evidence": [{"attempt_id": "attempt-verify", "kind": "validation", "status": "passed"}],
            "final_status": "completed",
            "node_attempts": [{"attempt_id": "attempt-verify", "node_id": "verify", "status": "passed"}],
            "run_id": "run-1",
        }
        report = delivery_report(plan, ledger, qa_report=qa_report(status="blocked", verification_status="passed"))
        validate("delivery-report", report)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["quality"]["status"], "blocked")
        self.assertEqual(report["quality"]["coverage_level"], "compatibility")
        forged = deepcopy(report)
        forged["quality"]["evidence_refs"][0]["uri"] = "file:///tmp/qa.json"
        with self.assertRaisesRegex(ContractError, "uncontrolled"):
            validate("delivery-report", forged)

        stale = qa_report(status="blocked", verification_status="passed")
        stale["delivery_context"]["run_id"] = "old-run"
        stale["fingerprint"] = qa_fingerprint({key: value for key, value in stale.items() if key != "fingerprint"})
        with self.assertRaisesRegex(ContractError, "does not match current workflow run"):
            delivery_report(plan, ledger, qa_report=stale)

        conflicting_ledger = deepcopy(ledger)
        conflicting_ledger["evidence"][0]["status"] = "failed"
        conflicting_ledger["final_status"] = "blocked"
        with self.assertRaisesRegex(ContractError, "conflicts with current ledger validation"):
            delivery_report(plan, conflicting_ledger, qa_report=qa_report())

        conditional = qa_report(status="partial", verification_status="partial")
        conditional["workflow_kind"] = "release"
        conditional["quality"].update({"executed": 0, "gaps": ["upgrade-path"], "passed": 0, "skipped": 1})
        conditional["release_recommendation"] = "conditional-go"
        conditional["residual_risks"] = ["upgrade-path"]
        conditional["waivers"] = [{"expires_on": "2026-07-17", "id": "waiver-1", "owner": "release-owner", "reason": "defer upgrade coverage"}]
        conditional["fingerprint"] = qa_fingerprint({key: value for key, value in conditional.items() if key != "fingerprint"})
        blocked_ledger = deepcopy(ledger)
        blocked_ledger["evidence"][0]["status"] = "partial"
        blocked_ledger["final_status"] = "blocked"
        with self.assertRaisesRegex(ContractError, "conditional-go conflicts"):
            delivery_report(plan, blocked_ledger, qa_report=conditional)

    def test_qa_schema_files_are_registered_as_install_assets(self) -> None:
        qa = ManifestRegistry.from_directory(MANIFESTS).by_id("qa")
        contract_root = qa.path.parent / "contracts"
        names = sorted(path.name for path in contract_root.glob("*.schema.json"))
        self.assertEqual(names, [
            "defect-report-v1.schema.json",
            "qa-coverage-request-v1.schema.json",
            "qa-coverage-result-v1.schema.json",
            "qa-plan-request-v1.schema.json",
            "qa-plan-v1.schema.json",
            "qa-report-request-v1.schema.json",
            "qa-report-v1.schema.json",
            "regression-set-v1.schema.json",
            "test-case-v1.schema.json",
            "test-result-v1.schema.json",
        ])
        self.assertEqual(sha256(names), sha256(sorted(names)))

        contract_names = set(names)
        registry = ManifestRegistry.from_directory(MANIFESTS)
        for capability_id in ("qa.coverage.compile", "qa.plan.compile", "qa.report.aggregate"):
            contract = registry.capability_contract(capability_id)
            self.assertIn(contract["input_schema"] + ".schema.json", contract_names)
            self.assertIn(contract["output_schema"] + ".schema.json", contract_names)

    def test_evidence_uri_schema_matches_runtime_control_boundary(self) -> None:
        expected = r"^artifact://(?!.*\.\.)(?!.*\s).+$"
        paths = [
            MANIFESTS.parent / "schemas" / "delivery-report-v1.schema.json",
            *sorted((MANIFESTS.parent / "disciplines" / "qa" / "contracts").glob("*.schema.json")),
        ]
        patterns: list[str] = []

        def visit(node: object) -> None:
            if isinstance(node, dict):
                pattern = node.get("pattern")
                if isinstance(pattern, str) and pattern.startswith("^artifact://"):
                    patterns.append(pattern)
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        for path in paths:
            visit(json.loads(path.read_text(encoding="utf-8")))
        self.assertTrue(patterns)
        self.assertEqual(set(patterns), {expected})


if __name__ == "__main__":
    unittest.main()
