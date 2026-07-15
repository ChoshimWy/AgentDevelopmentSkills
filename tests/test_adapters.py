from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS, ROOT  # noqa: F401

from agent_workflow.adapters import build_adapter_request, validate_adapter_result
from agent_workflow.contracts import validate
from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.models import ContractError
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
from agent_workflow.registry import ManifestRegistry
from agent_workflow.reporting import delivery_report
from agent_workflow.runtime import RecordedAdapterExecutor, RunLedger


class StructuredAdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = {
            "schema_version": "1.0",
            "plan_id": "plan-apple",
            "fingerprint": "plan-fingerprint",
            "nodes": [
                {
                    "id": "apple-verify",
                    "capability": "verification.apple.affected-tests",
                    "provider": "apple",
                    "binding": {"kind": "skill", "name": "ios-verification", "mode": "affected-tests"},
                }
            ],
        }
        self.context = {
            "task": {"risk": "medium", "text": "验证目标模块", "type": "code-medium"},
            "target_modules": ["AppCore"],
            "user_constraints": ["最窄验证", "不修改业务仓"],
            "checkpoints": {"CP0": "completed", "CP1": "in_progress", "CP2": "pending", "CP3": "pending"},
            "actors": {"implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
        }
        self.request = build_adapter_request(
            self.plan,
            "apple-verify",
            context=self.context,
            invocation_id="verify-invocation-1",
        )

    def result(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "request_id": self.request["request_id"],
            "invocation_id": self.request["invocation_id"],
            "plan_fingerprint": self.request["plan_fingerprint"],
            "node_id": self.request["node_id"],
            "capability": self.request["capability"],
            "provider": self.request["provider"],
            "binding": self.request["binding"],
            "status": "completed",
            "failure_attribution": {"category": "none", "summary": "未发现失败"},
            "cleanup": [],
            "evidence": [
                {
                    "kind": "validation",
                    "status": "passed",
                    "summary": "最窄测试通过",
                    "data": {"level": "affected-tests", "tests": 3},
                    "artifact_ids": ["test-report"],
                }
            ],
            "artifacts": [
                {
                    "artifact_id": "test-report",
                    "kind": "test-report",
                    "sha256": "a" * 64,
                    "uri": "artifacts/test-summary.json",
                }
            ],
        }

    def test_request_preserves_plan_node_binding_context_and_checkpoints(self) -> None:
        self.assertEqual(self.request["plan_fingerprint"], "plan-fingerprint")
        self.assertEqual(self.request["node_id"], "apple-verify")
        self.assertEqual(self.request["capability"], "verification.apple.affected-tests")
        self.assertEqual(self.request["provider"], "apple")
        self.assertEqual(self.request["binding"]["name"], "ios-verification")
        self.assertEqual(self.request["task_context"], self.context)
        self.assertEqual(self.request["checkpoints"], self.context["checkpoints"])
        self.context["target_modules"].append("MutatedLater")
        self.assertEqual(self.request["task_context"]["target_modules"], ["AppCore"])

    def test_request_rejects_missing_node_provider_or_checkpoints(self) -> None:
        with self.assertRaises(ContractError):
            build_adapter_request(self.plan, "missing", context=self.context, invocation_id="missing-1")
        plan = deepcopy(self.plan)
        plan["nodes"][0]["provider"] = None
        with self.assertRaises(ContractError):
            build_adapter_request(plan, "apple-verify", context=self.context, invocation_id="verify-2")
        with self.assertRaises(ContractError):
            build_adapter_request(self.plan, "apple-verify", context={}, invocation_id="verify-3")

    def test_structured_validation_result_is_accepted(self) -> None:
        validate_adapter_result(self.request, self.result())

    def test_automatic_verification_requires_execution_or_accepted_evidence(self) -> None:
        plan = deepcopy(self.plan)
        plan["nodes"][0]["capability"] = "verification.apple.auto"
        plan["nodes"][0]["binding"]["mode"] = "auto"
        request = build_adapter_request(
            plan,
            "apple-verify",
            context=self.context,
            invocation_id="verify-auto-invocation-1",
        )
        result = self.result()
        for field in (
            "request_id", "invocation_id", "plan_fingerprint", "node_id",
            "capability", "provider", "binding",
        ):
            result[field] = request[field]
        result["evidence"][0]["data"] = {"level": "affected-tests", "tests": 3}
        with self.assertRaisesRegex(ContractError, "requires executed_validation or accepted_evidence"):
            validate_adapter_result(request, result)
        result["evidence"][0]["data"] = {
            "level": "unit",
            "executed_validation": [{"kind": "affected-tests", "status": "passed"}],
        }
        with self.assertRaisesRegex(ContractError, "selection-only or invalid evidence"):
            validate_adapter_result(request, result)
        result["evidence"][0]["data"] = {
            "level": "unit",
            "executed_validation": [{"kind": "quick-verify", "status": "failed"}],
        }
        with self.assertRaisesRegex(ContractError, "requires successful evidence"):
            validate_adapter_result(request, result)
        result["evidence"][0]["data"] = {
            "level": "unit",
            "executed_validation": [{"kind": "quick-verify", "status": "passed"}],
            "accepted_evidence": [],
        }
        validate_adapter_result(request, result)
        result["evidence"][0]["data"] = {
            "level": "unit",
            "executed_validation": [],
            "accepted_evidence": [{"kind": "cached-quick-verify", "status": "passed"}],
        }
        validate_adapter_result(request, result)

    def test_result_identity_must_match_request(self) -> None:
        for field in ("request_id", "invocation_id", "plan_fingerprint", "node_id", "capability", "provider", "binding"):
            with self.subTest(field=field):
                result = self.result()
                result[field] = {"kind": "skill", "name": "mismatch"} if field == "binding" else "mismatch"
                with self.assertRaises(ContractError):
                    validate_adapter_result(self.request, result)

    def test_request_id_is_recomputed_from_frozen_identity(self) -> None:
        request = deepcopy(self.request)
        request["request_id"] = "adapter-request-tampered"
        with self.assertRaisesRegex(ContractError, "frozen identity"):
            validate_adapter_result(request, self.result())

    def test_raw_log_only_cannot_satisfy_validation_evidence(self) -> None:
        result = self.result()
        result["evidence"] = [
            {
                "kind": "diagnostic",
                "status": "completed",
                "summary": "保留原始日志",
                "data": {"lines": 20},
                "artifact_ids": ["raw-log"],
            }
        ]
        result["artifacts"] = [
            {"artifact_id": "raw-log", "kind": "raw-log", "sha256": "b" * 64, "uri": "logs/raw.log"}
        ]
        with self.assertRaises(ContractError):
            validate_adapter_result(self.request, result)

    def test_explicit_no_test_gap_is_accepted_with_suggestion(self) -> None:
        result = self.result()
        result.update(
            {
                "status": "partial",
                "evidence": [],
                "artifacts": [],
                "no_test_reason": "目标模块没有低成本自动化测试入口",
                "suggested_validation": "在获准环境运行最小受影响 bundle",
            }
        )
        validate_adapter_result(self.request, result)

    def test_no_test_gap_requires_both_fields_and_non_success_status(self) -> None:
        result = self.result()
        result["no_test_reason"] = "无测试入口"
        with self.assertRaises(ContractError):
            validate_adapter_result(self.request, result)
        result["suggested_validation"] = "运行 smoke"
        with self.assertRaises(ContractError):
            validate_adapter_result(self.request, result)

    def test_artifact_hash_and_references_are_strict(self) -> None:
        result = self.result()
        result["artifacts"][0]["sha256"] = "not-a-hash"
        with self.assertRaises(ContractError):
            validate_adapter_result(self.request, result)
        result = self.result()
        result["evidence"][0]["artifact_ids"] = ["missing"]
        with self.assertRaises(ContractError):
            validate_adapter_result(self.request, result)

    def test_failure_attribution_and_cleanup_are_auditable(self) -> None:
        result = self.result()
        result["status"] = "blocked"
        with self.assertRaises(ContractError):
            validate_adapter_result(self.request, result)
        result["failure_attribution"] = {"category": "environment", "summary": "目标设备不可用"}
        result["cleanup"] = [{"resource": "device:fixture", "status": "completed", "detail": "session released"}]
        result["evidence"][0]["status"] = "blocked"
        validate_adapter_result(self.request, result)
        result["cleanup"][0]["status"] = "invented"
        with self.assertRaises(ContractError):
            validate_adapter_result(self.request, result)

    def test_result_status_must_match_structured_evidence(self) -> None:
        result = self.result()
        result["evidence"][0]["status"] = "failed"
        with self.assertRaisesRegex(ContractError, "conflicts"):
            validate_adapter_result(self.request, result)

        result = self.result()
        result["status"] = "partial"
        result["evidence"][0]["status"] = "blocked"
        with self.assertRaisesRegex(ContractError, "partial status conflicts"):
            validate_adapter_result(self.request, result)

        result = self.result()
        result["cleanup"] = [{"resource": "device", "status": "failed", "detail": "release failed"}]
        with self.assertRaisesRegex(ContractError, "failed cleanup"):
            validate_adapter_result(self.request, result)

    def test_review_capability_requires_review_evidence(self) -> None:
        plan = deepcopy(self.plan)
        plan["nodes"][0].update({"id": "review", "capability": "review.independent", "provider": "core"})
        plan["nodes"][0]["binding"] = {"kind": "skill", "name": "code-review"}
        request = build_adapter_request(plan, "review", context=self.context, invocation_id="review-invocation-1")
        result = self.result()
        for field in ("request_id", "invocation_id", "plan_fingerprint", "node_id", "capability", "provider", "binding"):
            result[field] = request[field]
        with self.assertRaises(ContractError):
            validate_adapter_result(request, result)
        result["evidence"] = [
            {
                "kind": "review",
                "status": "passed",
                "summary": "阻塞问题：无",
                "data": {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
                "artifact_ids": [],
            }
        ]
        result["artifacts"] = []
        validate_adapter_result(request, result)

        result["evidence"][0]["data"]["reviewer_actor"] = "builder-1"
        with self.assertRaises(ContractError):
            validate_adapter_result(request, result)

        result["evidence"][0]["data"].update(
            {"implementation_actor": "builder-2", "reviewer_actor": "reviewer-2"}
        )
        with self.assertRaisesRegex(ContractError, "orchestrator-frozen"):
            validate_adapter_result(request, result)

        result = self.result()
        for field in ("request_id", "invocation_id", "plan_fingerprint", "node_id", "capability", "provider", "binding"):
            result[field] = request[field]
        result["evidence"] = [{
            "kind": "review", "status": "passed", "summary": "发现阻塞问题",
            "data": {"blocking_issues": ["P1"], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
            "artifact_ids": [],
        }]
        result["artifacts"] = []
        with self.assertRaisesRegex(ContractError, "must block"):
            validate_adapter_result(request, result)


class AppleProviderAnchorSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        profile = DiscoveryEngine(registry).discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        self.plan = PlanCompiler(registry).compile(profile, policy)
        self.context = {
            "task": policy["task"],
            "target_modules": profile["target_modules"],
            "user_constraints": ["最窄验证", "独立 reviewer"],
            "checkpoints": {"CP0": "completed", "CP1": "in_progress", "CP2": "pending", "CP3": "pending"},
            "actors": {"implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
        }

    def _result(
        self,
        node_id: str,
        kind: str,
        data: dict[str, object],
        *,
        context: dict[str, object] | None = None,
        invocation_id: str | None = None,
    ) -> dict[str, object]:
        request = build_adapter_request(
            self.plan,
            node_id,
            context=context or self.context,
            invocation_id=invocation_id or f"{node_id}-invocation-1",
        )
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
                "status": "passed" if kind in {"validation", "review"} else "completed",
                "summary": f"{kind} structured evidence",
                "data": data,
                "artifact_ids": [],
            }],
            "artifacts": [],
        }

    def _supporting_results(
        self,
        *,
        context: dict[str, object] | None = None,
        invocation_suffix: str = "1",
        include_downstream: bool = True,
    ) -> dict[str, dict[str, object]]:
        active_context = context or self.context
        results = {
            "workflow-analysis": self._result(
                "workflow-analysis", "diagnostic", {"scope": "apple"},
                context=active_context, invocation_id=f"workflow-analysis-invocation-{invocation_suffix}",
            ),
            "workflow-orchestration": self._result(
                "workflow-orchestration", "diagnostic", {"checkpoint": "CP0"},
                context=active_context, invocation_id=f"workflow-orchestration-invocation-{invocation_suffix}",
            ),
        }
        if include_downstream:
            results.update({
                "apple-3": self._result(
                    "apple-3", "validation",
                    {
                        "level": "unit",
                        "executed_validation": [{"kind": "quick-verify", "status": "passed"}],
                    },
                    context=active_context, invocation_id=f"apple-3-invocation-{invocation_suffix}",
                ),
                "review-apple": self._result(
                    "review-apple", "review",
                    {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
                    context=active_context, invocation_id=f"review-apple-invocation-{invocation_suffix}",
                ),
                "report": self._result(
                    "report", "delivery", {"acceptance_matrix": []},
                    context=active_context, invocation_id=f"report-invocation-{invocation_suffix}",
                ),
            })
        return results

    def test_discovery_to_plan_to_recorded_adapter_to_report(self) -> None:
        results = {
            **self._supporting_results(),
            "apple-1": self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]}),
            "apple-2": self._result("apple-2", "validation", {"level": "affected-tests", "tests": 1}),
            "review": self._result(
                "review", "review",
                {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
            ),
        }
        ledger = RecordedAdapterExecutor(results, context=self.context).run(self.plan)
        report = delivery_report(self.plan, ledger)
        self.assertEqual(ledger["final_status"], "completed")
        self.assertEqual(report["validation"]["mode"], "structured-provider")
        self.assertEqual(report["review"]["status"], "passed")
        self.assertEqual(report["known_risks"], [])
        self.assertEqual(len(ledger["evidence"]), 8)
        self.assertEqual(len(ledger["adapter_outcomes"]), 8)

    def test_missing_recorded_provider_result_blocks_anchor(self) -> None:
        ledger = RecordedAdapterExecutor({}, context=self.context).run(self.plan)
        self.assertEqual(ledger["final_status"], "blocked")
        self.assertEqual(ledger["evidence"], [])

    def test_recorded_result_is_never_reused_as_an_automatic_retry(self) -> None:
        failed = self._result("apple-2", "validation", {"level": "affected-tests", "tests": 1})
        failed.update({
            "status": "failed",
            "failure_attribution": {"category": "code", "summary": "test failed"},
        })
        failed["evidence"][0]["status"] = "failed"
        ledger = RecordedAdapterExecutor(
            {
                **self._supporting_results(include_downstream=False),
                "apple-1": self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]}),
                "apple-2": failed,
            },
            context=self.context,
        ).run(self.plan)
        attempts = [item for item in ledger["node_attempts"] if item["node_id"] == "apple-2"]
        outcomes = [item for item in ledger["adapter_outcomes"] if item["node_id"] == "apple-2"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(ledger["final_status"], "blocked")

    def test_partial_recorded_result_remains_partial_after_resume(self) -> None:
        results = {
            **self._supporting_results(),
            "apple-1": self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]}),
            "apple-2": self._result("apple-2", "validation", {"level": "none"}),
            "review": self._result(
                "review", "review",
                {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
            ),
        }
        results["apple-2"].update({
            "status": "partial",
            "evidence": [],
            "artifacts": [],
            "no_test_reason": "fixture has no executable test target",
            "suggested_validation": "run the smallest project smoke when available",
        })
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            first = RecordedAdapterExecutor(results, context=self.context).run(self.plan, ledger_path=ledger_path)
            resumed = RecordedAdapterExecutor(results, context=self.context).run(
                self.plan, ledger_path=ledger_path, resume=True
            )
        self.assertEqual(first["final_status"], "partial")
        self.assertEqual(resumed["final_status"], "partial")

    def test_auto_no_test_gap_allows_independent_review_and_reports_partial(self) -> None:
        results = {
            **self._supporting_results(),
            "apple-1": self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]}),
            "apple-2": self._result(
                "apple-2", "validation", {"level": "affected-tests", "tests": 0}
            ),
            "review": self._result(
                "review", "review",
                {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
            ),
        }
        results["apple-3"].update({
            "status": "partial",
            "evidence": [],
            "artifacts": [],
            "no_test_reason": "fixture has no executable test target",
            "suggested_validation": "run the smallest approved project smoke",
        })
        ledger = RecordedAdapterExecutor(results, context=self.context).run(self.plan)
        report = delivery_report(self.plan, ledger)
        latest = {
            item["node_id"]: item["status"]
            for item in ledger["node_attempts"]
        }
        self.assertEqual(ledger["final_status"], "partial")
        self.assertEqual(latest["apple-3"], "skipped")
        self.assertEqual(latest["review-apple"], "passed")
        self.assertEqual(latest["review"], "passed")
        self.assertEqual(report["validation"]["status"], "partial")

    def test_historical_partial_does_not_override_current_completed_or_blocked(self) -> None:
        initial_results = {
            **self._supporting_results(include_downstream=False),
            "apple-1": self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]}),
            "apple-2": self._result("apple-2", "validation", {"level": "none"}),
        }
        initial_results["apple-2"].update({
            "status": "partial",
            "evidence": [],
            "artifacts": [],
            "no_test_reason": "fixture has no executable test target",
            "suggested_validation": "run the smallest project smoke when available",
        })
        changed_context = deepcopy(self.context)
        changed_context["target_modules"] = ["ChangedModule"]

        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "completed-ledger.jsonl"
            first = RecordedAdapterExecutor(initial_results, context=self.context).run(
                self.plan, ledger_path=ledger_path
            )
            self.assertEqual(first["final_status"], "partial")
            recovered_results = {
                **self._supporting_results(context=changed_context, invocation_suffix="2"),
                "apple-1": self._result(
                    "apple-1", "delivery", {"changed_files": ["Fixture.swift"]},
                    context=changed_context, invocation_id="apple-1-invocation-2",
                ),
                "apple-2": self._result(
                    "apple-2", "validation", {"level": "affected-tests", "tests": 1},
                    context=changed_context, invocation_id="apple-2-invocation-2",
                ),
                "review": self._result(
                    "review", "review",
                    {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
                    context=changed_context, invocation_id="review-invocation-2",
                ),
            }
            recovered = RecordedAdapterExecutor(recovered_results, context=changed_context).run(
                self.plan, ledger_path=ledger_path, resume=True
            )
            validate("run-ledger", recovered)
            self.assertEqual(recovered["final_status"], "completed")
            recovered_report = delivery_report(self.plan, recovered)
            self.assertEqual(recovered_report["blocked_items"], [])
            self.assertEqual(
                [item["status"] for item in recovered_report["validation"]["evidence"]],
                ["passed", "passed"],
            )

        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "blocked-ledger.jsonl"
            RecordedAdapterExecutor(initial_results, context=self.context).run(
                self.plan, ledger_path=ledger_path
            )
            missing_results = {
                **self._supporting_results(
                    context=changed_context, invocation_suffix="3", include_downstream=False
                ),
                "apple-1": self._result(
                    "apple-1", "delivery", {"changed_files": ["Fixture.swift"]},
                    context=changed_context, invocation_id="apple-1-invocation-3",
                )
            }
            blocked = RecordedAdapterExecutor(missing_results, context=changed_context).run(
                self.plan, ledger_path=ledger_path, resume=True
            )
            validate("run-ledger", blocked)
            self.assertEqual(blocked["final_status"], "blocked")
            blocked_report = delivery_report(self.plan, blocked)
            self.assertEqual(
                blocked_report["blocked_items"],
                ["apple-2", "apple-3", "review-apple", "review", "report"],
            )
            self.assertEqual(blocked_report["validation"]["evidence"], [])
            self.assertEqual(blocked_report["validation"]["status"], "blocked")

    def test_invalid_result_can_resume_with_corrected_new_invocation(self) -> None:
        invalid = self._result(
            "apple-1", "delivery", {"changed_files": ["Fixture.swift"]},
            invocation_id="apple-1-invalid-invocation",
        )
        invalid["request_id"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            with self.assertRaises(ContractError):
                RecordedAdapterExecutor(
                    {**self._supporting_results(include_downstream=False), "apple-1": invalid},
                    context=self.context,
                ).run(
                    self.plan, ledger_path=ledger_path
                )
            corrected_results = {
                **self._supporting_results(invocation_suffix="corrected"),
                "apple-1": self._result(
                    "apple-1", "delivery", {"changed_files": ["Fixture.swift"]},
                    invocation_id="apple-1-corrected-invocation",
                ),
                "apple-2": self._result(
                    "apple-2", "validation", {"level": "affected-tests", "tests": 1},
                    invocation_id="apple-2-corrected-invocation",
                ),
                "review": self._result(
                    "review", "review",
                    {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
                    invocation_id="review-corrected-invocation",
                ),
            }
            recovered = RecordedAdapterExecutor(corrected_results, context=self.context).run(
                self.plan, ledger_path=ledger_path, resume=True
            )
            validate("run-ledger", recovered)
        self.assertEqual(recovered["final_status"], "completed")
        invocation_ids = [item["invocation_id"] for item in recovered["adapter_outcomes"]]
        self.assertEqual(len(invocation_ids), len(set(invocation_ids)))

    def test_resume_rejects_stale_adapter_evidence_when_context_changes(self) -> None:
        results = {
            **self._supporting_results(),
            "apple-1": self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]}),
            "apple-2": self._result("apple-2", "validation", {"level": "affected-tests", "tests": 1}),
            "review": self._result(
                "review", "review",
                {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            RecordedAdapterExecutor(results, context=self.context).run(self.plan, ledger_path=ledger_path)
            changed_context = deepcopy(self.context)
            changed_context["target_modules"] = ["DifferentModule"]
            with self.assertRaisesRegex(ContractError, "does not match request"):
                RecordedAdapterExecutor(results, context=changed_context).run(
                    self.plan, ledger_path=ledger_path, resume=True
                )
            replayed = RunLedger.replay(ledger_path, self.plan["fingerprint"])
            validate("run-ledger", replayed.value)
            invocation_ids = [item["invocation_id"] for item in replayed.value["adapter_outcomes"]]
            self.assertEqual(len(invocation_ids), len(set(invocation_ids)))
            self.assertTrue(invocation_ids[-1].startswith("contract-failure-attempt-"))

    def test_invalid_result_never_acquires_resources_for_its_attempt(self) -> None:
        result = self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]})
        result["request_id"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            with self.assertRaises(ContractError):
                RecordedAdapterExecutor(
                    {**self._supporting_results(include_downstream=False), "apple-1": result},
                    context=self.context,
                ).run(
                    self.plan, ledger_path=ledger_path
                )
            events = [__import__("json").loads(line) for line in ledger_path.read_text().splitlines()]
        attempts = [event["value"] for event in events if event["event_type"] == "node-attempt"]
        invalid_attempt = next(item for item in attempts if item["node_id"] == "apple-1")
        target_resource_events = [
            event for event in events
            if event["event_type"] == "resource-event"
            and event["value"]["attempt_id"] == invalid_attempt["attempt_id"]
        ]
        self.assertEqual(target_resource_events, [])

    def test_ledger_adapter_cross_references_fail_closed(self) -> None:
        results = {
            **self._supporting_results(),
            "apple-1": self._result("apple-1", "delivery", {"changed_files": ["Fixture.swift"]}),
            "apple-2": self._result("apple-2", "validation", {"level": "affected-tests", "tests": 1}),
            "review": self._result(
                "review", "review",
                {"blocking_issues": [], "implementation_actor": "builder-1", "reviewer_actor": "reviewer-1"},
            ),
        }
        ledger = RecordedAdapterExecutor(results, context=self.context).run(self.plan)
        ledger["evidence"][0]["attempt_id"] = "unknown-attempt"
        with self.assertRaisesRegex(ContractError, "unknown attempt"):
            validate("run-ledger", ledger)


if __name__ == "__main__":
    unittest.main()
