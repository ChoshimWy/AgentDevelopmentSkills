from __future__ import annotations

import json
from pathlib import Path
import unittest

from tests.support import FIXTURES, MANIFESTS

from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.models import ContractError
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
from agent_workflow.qa import (
    FailFixReportGuard,
    evidence_reuse_status,
    qa_fingerprint,
    refresh_regression_set,
    reopen_defect,
)
from agent_workflow.registry import ManifestRegistry


ROOT = Path(__file__).resolve().parents[1]


class QARuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = json.loads(
            (ROOT / "tests" / "golden" / "phase4-qa" / "bug" / "passed.json").read_text(encoding="utf-8")
        )

    def test_environment_test_data_and_source_changes_make_result_stale(self) -> None:
        result = self.workflow["test_results"][0]
        source = [qa_fingerprint({"source": "v1"})]
        current = evidence_reuse_status(
            result,
            plan_fingerprint=result["plan_fingerprint"],
            environment_fingerprint=result["environment_fingerprint"],
            test_data_fingerprint=result["test_data_fingerprint"],
            recorded_source_fingerprints=source,
            current_source_fingerprints=source,
        )
        self.assertEqual(current["status"], "reusable")
        self.assertEqual(current["current_identity"], current["recorded_identity"])

        stale = evidence_reuse_status(
            result,
            plan_fingerprint=result["plan_fingerprint"],
            environment_fingerprint=qa_fingerprint({"environment": "changed"}),
            test_data_fingerprint=qa_fingerprint({"test-data": "changed"}),
            recorded_source_fingerprints=source,
            current_source_fingerprints=[qa_fingerprint({"source": "v2"})],
        )
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["reasons"], [
            "environment-fingerprint-changed",
            "source-fingerprint-changed",
            "test-data-fingerprint-changed",
        ])

    def test_regression_set_stales_when_environment_changes_or_defect_reopens(self) -> None:
        regression = self.workflow["regression_set"]
        changed = refresh_regression_set(
            regression,
            environment_fingerprints=[qa_fingerprint({"environment": "v2"})],
            source_fingerprints=regression["source_fingerprints"],
        )
        self.assertEqual(changed["status"], "stale")
        self.assertEqual(changed["stale_reasons"], ["environment-fingerprint-changed"])
        self.assertNotEqual(changed["fingerprint"], regression["fingerprint"])

        defect = self.workflow["defect_reports"][0]
        reopened = reopen_defect(
            defect,
            evidence_refs=[{
                "kind": "log",
                "sha256": "9" * 64,
                "uri": "artifact://qa/reopened-defect",
            }],
        )
        self.assertEqual(reopened["status"], "reopened")
        stale = refresh_regression_set(
            regression,
            environment_fingerprints=regression["environment_fingerprints"],
            source_fingerprints=regression["source_fingerprints"],
            reopened_defect_refs=[reopened["defect_id"]],
        )
        self.assertEqual(stale["stale_reasons"], [f"defect-reopened:{reopened['defect_id']}"])

        with self.assertRaisesRegex(ContractError, "outside the regression set"):
            refresh_regression_set(
                regression,
                environment_fingerprints=regression["environment_fingerprints"],
                source_fingerprints=regression["source_fingerprints"],
                reopened_defect_refs=["UNKNOWN"],
            )

    def test_same_issue_class_blocks_after_two_fail_fix_iterations(self) -> None:
        guard = FailFixReportGuard(max_attempts=2)
        first = guard.record("desktop.window.dpi", "failed")
        second = guard.record("desktop.window.dpi", "failed")
        self.assertEqual(first["status"], "retrying")
        self.assertEqual(first["next_action"], "fix")
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(second["next_action"], "independent-triage")
        self.assertEqual(guard.record("desktop.window.dpi", "resolved")["action"], "report")

    def test_companion_and_independent_qa_use_the_same_plan_schema_with_different_nodes(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        profile = DiscoveryEngine(registry).discover(FIXTURES / "desktop-tauri")
        compiler = PlanCompiler(registry)
        companion_policy = PolicyResolver().resolve(
            profile,
            "实现 Desktop 功能并补充 QA 测试",
            explicit_platforms=["desktop"],
        )
        independent_policy = PolicyResolver().resolve(
            profile,
            "执行 Desktop 回归测试",
            explicit_platforms=["desktop"],
        )
        companion = compiler.compile(profile, companion_policy)
        independent = compiler.compile(profile, independent_policy)
        companion_ids = {node["id"] for node in companion["nodes"]}
        independent_ids = {node["id"] for node in independent["nodes"]}
        self.assertIn("qa-report", companion_ids)
        self.assertNotIn("qa-triage", companion_ids)
        self.assertNotIn("qa-regression", companion_ids)
        self.assertIn("qa-triage", independent_ids)
        self.assertIn("qa-regression", independent_ids)
        self.assertEqual(companion["schema_version"], independent["schema_version"])
        self.assertIn("regression-owner", companion["workflow"]["roles"])


if __name__ == "__main__":
    unittest.main()
