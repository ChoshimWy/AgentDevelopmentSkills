from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS

from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.models import ContractError, NodeStatus
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
from agent_workflow.registry import ManifestRegistry
from agent_workflow.runtime import ApprovalGate, FakeAdapterExecutor, NodeStateMachine, ResourceScheduler, RunLedger


class StateMachineTests(unittest.TestCase):
    def test_valid_lifecycle(self) -> None:
        machine = NodeStateMachine()
        attempt = machine.new_attempt("node")
        machine.transition(attempt, NodeStatus.READY, "ready")
        machine.transition(attempt, NodeStatus.RUNNING, "run")
        machine.transition(attempt, NodeStatus.PASSED, "pass")
        self.assertEqual(attempt["status"], "passed")

    def test_illegal_transition_fails_closed(self) -> None:
        machine = NodeStateMachine()
        attempt = machine.new_attempt("node")
        with self.assertRaises(ContractError):
            machine.transition(attempt, NodeStatus.PASSED, "skip guards")

    def test_only_idempotent_nodes_auto_retry(self) -> None:
        self.assertTrue(NodeStateMachine.can_auto_retry(idempotent=True, attempt_count=1, max_retries=1))
        self.assertFalse(NodeStateMachine.can_auto_retry(idempotent=False, attempt_count=1, max_retries=2))
        self.assertFalse(NodeStateMachine.can_auto_retry(idempotent=True, attempt_count=3, max_retries=2))

    def test_attempt_freezes_timeout_and_retry_metadata(self) -> None:
        attempt = NodeStateMachine().new_attempt("node", attempt_number=2, max_retries=2, timeout_seconds=60)
        self.assertEqual((attempt["attempt_number"], attempt["max_retries"], attempt["timeout_seconds"]), (2, 2, 60))
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        self.assertTrue(NodeStateMachine.has_timed_out(attempt, now=future))


class SchedulerAndApprovalTests(unittest.TestCase):
    def test_resource_lock_and_release(self) -> None:
        scheduler = ResourceScheduler()
        self.assertTrue(scheduler.acquire("a", ["git-index:repo"]))
        self.assertFalse(scheduler.acquire("b", ["git-index:repo"]))
        scheduler.release("a")
        self.assertTrue(scheduler.acquire("b", ["git-index:repo"]))

    def test_approval_scope_cannot_expand(self) -> None:
        gate = ApprovalGate()
        record = gate.request("attempt", "design-write", "test", {"node": "1"})
        with self.assertRaises(ContractError):
            gate.decide(record, "granted", {"node": "2"})

    def test_granted_approval_can_only_be_revoked_for_its_exact_scope(self) -> None:
        gate = ApprovalGate()
        record = gate.request("attempt", "design-write", "test", {"node": "1"})
        gate.decide(record, "granted", {"node": "1"})
        with self.assertRaisesRegex(ContractError, "scope differs"):
            gate.revoke(record, {"node": "2"})
        gate.revoke(record, {"node": "1"})
        self.assertEqual(record["status"], "revoked")
        self.assertFalse(gate.is_granted(record, "attempt", {"node": "1"}))
        with self.assertRaisesRegex(ContractError, "only a granted"):
            gate.revoke(record, {"node": "1"})


class ExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        profile = DiscoveryEngine(registry).discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        cls.plan = PlanCompiler(registry).compile(profile, policy)

    def test_fake_execution_completes(self) -> None:
        ledger = FakeAdapterExecutor().run(self.plan)
        self.assertEqual(ledger["final_status"], "completed")
        self.assertTrue(all(item["status"] == "passed" for item in ledger["node_attempts"]))

    def test_failure_blocks_downstream(self) -> None:
        ledger = FakeAdapterExecutor({"implementation.apple": "failed"}).run(self.plan)
        statuses = {item["node_id"]: item["status"] for item in ledger["node_attempts"]}
        self.assertEqual(statuses["apple-1"], "failed")
        self.assertEqual(statuses["apple-2"], "blocked")
        self.assertEqual(statuses["review"], "blocked")

    def test_idempotent_failure_retries_and_can_recover(self) -> None:
        ledger = FakeAdapterExecutor({"verification.apple.affected-tests": ["failed", "passed"]}).run(self.plan)
        attempts = [item for item in ledger["node_attempts"] if item["node_id"] == "apple-2"]
        self.assertEqual([item["status"] for item in attempts], ["failed", "passed"])
        self.assertEqual([item["attempt_number"] for item in attempts], [1, 2])
        self.assertEqual(ledger["final_status"], "completed")

    def test_non_idempotent_failure_is_not_retried(self) -> None:
        ledger = FakeAdapterExecutor({"implementation.apple": ["failed", "passed"]}).run(self.plan)
        attempts = [item for item in ledger["node_attempts"] if item["node_id"] == "apple-1"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "failed")

    def test_timeout_retries_only_to_limit_and_releases_resource(self) -> None:
        plan = deepcopy(self.plan)
        verification = next(item for item in plan["nodes"] if item["id"] == "apple-2")
        verification["resource_keys"] = ["build-queue:{target-root}"]
        executor = FakeAdapterExecutor({"verification.apple.affected-tests": "timed-out"})
        ledger = executor.run(plan)
        attempts = [item for item in ledger["node_attempts"] if item["node_id"] == "apple-2"]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(item["status"] == "blocked" for item in attempts))
        self.assertIsNone(executor.scheduler.owner("build-queue:{target-root}"))
        self.assertEqual(
            [item["action"] for item in ledger["resource_events"] if item["resource_key"] == "build-queue:{target-root}"].count("timed-out"),
            2,
        )

    def test_blocked_plan_cannot_fake_success(self) -> None:
        blocked = deepcopy(self.plan)
        blocked["status"] = "blocked"
        blocked["missing_capabilities"] = ["routing.platform-selection"]
        ledger = FakeAdapterExecutor().run(blocked)
        self.assertEqual(ledger["final_status"], "blocked")
        self.assertEqual(ledger["node_attempts"], [])

    def test_optional_skip_allows_review_and_reports_partial(self) -> None:
        plan = deepcopy(self.plan)
        implementation = next(item for item in plan["nodes"] if item["id"] == "apple-1")
        verification = next(item for item in plan["nodes"] if item["id"] == "apple-2")
        verification["mandatory"] = False
        verification["provider"] = None
        plan["status"] = "degraded"
        plan["missing_capabilities"] = [verification["capability"]]
        ledger = FakeAdapterExecutor().run(plan)
        latest = {item["node_id"]: item["status"] for item in ledger["node_attempts"]}
        self.assertEqual(latest[implementation["id"]], "passed")
        self.assertEqual(latest[verification["id"]], "skipped")
        self.assertEqual(latest["review"], "passed")
        self.assertEqual(ledger["final_status"], "partial")

    def test_pending_approval_can_be_granted_on_resume(self) -> None:
        plan = deepcopy(self.plan)
        intent = next(item for item in plan["nodes"] if item["id"] == "intent")
        intent["approval"] = {"action": "repository-read", "reason": "fixture", "scope": {"root": "."}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            first = FakeAdapterExecutor().run(plan, ledger_path=path)
            self.assertEqual(first["final_status"], "blocked")
            resumed = FakeAdapterExecutor(
                approval_decisions={"core.intent-lock": "granted"}
            ).run(plan, ledger_path=path, resume=True)
            self.assertEqual(resumed["final_status"], "completed")
            self.assertEqual(resumed["approval_records"][-1]["status"], "granted")

    def test_denied_and_expired_approvals_remain_blocked(self) -> None:
        for decision in ("denied", "expired"):
            with self.subTest(decision=decision):
                plan = deepcopy(self.plan)
                intent = next(item for item in plan["nodes"] if item["id"] == "intent")
                intent["approval"] = {"action": "repository-read", "reason": "fixture", "scope": {"root": "."}}
                ledger = FakeAdapterExecutor(
                    approval_decisions={"core.intent-lock": decision}
                ).run(plan)
                self.assertEqual(ledger["final_status"], "blocked")
                self.assertEqual(ledger["approval_records"][-1]["status"], decision)

    def test_ledger_jsonl_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            FakeAdapterExecutor().run(self.plan, ledger_path=path)
            self.assertTrue(path.exists())
            original = path.read_bytes()
            replayed = RunLedger.replay(path, self.plan["fingerprint"])
            self.assertGreater(len(replayed.value["node_attempts"]), 0)
            self.assertEqual(replayed.value["final_status"], "completed")
            self.assertEqual(path.read_bytes(), original)

    def test_ledger_replay_rejects_unknown_event_type(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            ledger = RunLedger(self.plan["fingerprint"], path=path)
            ledger.append("run-started", {"plan_fingerprint": self.plan["fingerprint"]})
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "event_type": "invented-event",
                    "run_id": ledger.value["run_id"],
                    "value": {},
                }) + "\n")
            with self.assertRaisesRegex(ContractError, "unknown ledger event type"):
                RunLedger.replay(path, self.plan["fingerprint"])

    def test_resume_retries_failed_nodes_and_preserves_passed_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            first = FakeAdapterExecutor({"implementation.apple": "failed"}).run(self.plan, ledger_path=path)
            self.assertEqual(first["final_status"], "blocked")
            resumed = FakeAdapterExecutor().run(self.plan, ledger_path=path, resume=True)
            latest = {item["node_id"]: item["status"] for item in resumed["node_attempts"]}
            self.assertEqual(resumed["final_status"], "completed")
            self.assertTrue(all(status == "passed" for status in latest.values()))

    def test_resume_rejects_changed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            FakeAdapterExecutor().run(self.plan, ledger_path=path)
            changed = dict(self.plan)
            changed["fingerprint"] = "different"
            with self.assertRaises(ContractError):
                FakeAdapterExecutor().run(changed, ledger_path=path, resume=True)

    def test_cancelled_adapter_releases_resource(self) -> None:
        executor = FakeAdapterExecutor({"implementation.apple": "cancelled"})
        ledger = executor.run(self.plan)
        self.assertEqual(ledger["final_status"], "cancelled")
        self.assertIsNone(executor.scheduler.owner("repository-write:{target-root}"))
        self.assertIn("cancelled", [item["action"] for item in ledger["resource_events"]])


if __name__ == "__main__":
    unittest.main()
