"""Deterministic fake-adapter executor used by Phase 1 conformance tests."""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from ..adapters import build_adapter_request, validate_adapter_result
from ..models import ContractError, NodeStatus
from ..package_lock import validate_plan_package_lock
from .approval import ApprovalGate
from .ledger import RunLedger
from .scheduler import ResourceScheduler
from .state_machine import NodeStateMachine


class FakeAdapterExecutor:
    """Exercise Runtime contracts without claiming real platform execution."""

    def __init__(
        self,
        behaviors: dict[str, str | list[str]] | None = None,
        *,
        approval_decisions: dict[str, str] | None = None,
    ) -> None:
        self.behaviors = behaviors or {}
        self.approval_decisions = approval_decisions or {}
        self.behavior_cursors: dict[str, int] = defaultdict(int)
        self.machine = NodeStateMachine()
        self.scheduler = ResourceScheduler()
        self.approvals = ApprovalGate()

    def run(
        self,
        plan: dict[str, Any],
        *,
        ledger_path: str | Path | None = None,
        package_lock: dict[str, Any] | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        if plan.get("package_lock_hash") is not None:
            if package_lock is None:
                raise ContractError("locked workflow execution requires the current package Lockfile")
            validate_plan_package_lock(plan, package_lock)
        elif package_lock is not None:
            raise ContractError("workflow plan is not frozen to the supplied package Lockfile")
        ledger_file = Path(ledger_path) if ledger_path else None
        if resume:
            if ledger_file is None or not ledger_file.exists():
                raise ValueError("resume requires an existing ledger path")
            ledger = RunLedger.replay(ledger_file, plan["fingerprint"])
            if ledger.value["package_lock_hash"] != plan.get("package_lock_hash", ""):
                raise ContractError("cannot resume ledger with a different package lock")
            ledger.append("run-resumed", {
                "package_lock_hash": plan.get("package_lock_hash", ""),
                "plan_fingerprint": plan["fingerprint"],
            })
        else:
            ledger = RunLedger(
                plan["fingerprint"],
                path=ledger_file,
                package_lock_hash=plan.get("package_lock_hash", ""),
            )
            ledger.append("run-started", {
                "package_lock_hash": plan.get("package_lock_hash", ""),
                "plan_fingerprint": plan["fingerprint"],
            })
        if plan.get("status") == "blocked":
            ledger.append("run-blocked", {"missing_capabilities": plan.get("missing_capabilities", [])})
            return ledger.finalize("blocked")

        prior_sequences = [event["sequence"] for event in ledger.value["resource_events"]]
        self.scheduler.seed_sequence(max(prior_sequences, default=-1) + 1)

        nodes = {node["id"]: deepcopy(node) for node in plan["nodes"]}
        predecessor_status: dict[str, list[NodeStatus]] = defaultdict(list)
        order = _topological(plan)
        latest = _latest_statuses(ledger.value["node_attempts"])
        resource_cursor = 0

        for node_id in order:
            node = nodes[node_id]
            reusable_status = self._reusable_status(plan, node, ledger, latest.get(node_id))
            if reusable_status is not None:
                _record_successors(plan, node_id, reusable_status, predecessor_status)
                continue
            incoming = predecessor_status[node_id]
            if any(status not in {NodeStatus.PASSED, NodeStatus.SKIPPED} for status in incoming):
                attempt = self._new_attempt(node, ledger)
                self.machine.transition(attempt, NodeStatus.BLOCKED, "upstream-not-passed")
                ledger.append("node-attempt", deepcopy(attempt))
                _record_successors(plan, node_id, NodeStatus.BLOCKED, predecessor_status)
                continue
            if not node.get("provider"):
                attempt = self._new_attempt(node, ledger)
                target = NodeStatus.BLOCKED if node.get("mandatory") else NodeStatus.SKIPPED
                self.machine.transition(attempt, target, "capability-provider-missing")
                ledger.append("node-attempt", deepcopy(attempt))
                _record_successors(plan, node_id, target, predecessor_status)
                continue

            attempt, approval_outcome = self._prepare_approval(node, ledger, resume=resume)
            if approval_outcome in {"pending", "denied", "expired"}:
                _record_successors(plan, node_id, NodeStatus.BLOCKED, predecessor_status)
                continue

            final_status = NodeStatus.FAILED
            while True:
                if attempt["status"] == NodeStatus.PENDING.value:
                    self.machine.transition(attempt, NodeStatus.READY, "dependencies-satisfied")
                try:
                    self._prepare_adapter(plan, node, attempt, ledger)
                except ContractError as error:
                    self.machine.transition(attempt, NodeStatus.BLOCKED, "adapter-contract-invalid")
                    ledger.append("node-attempt", deepcopy(attempt))
                    self._record_adapter_contract_failure(plan, node, attempt, ledger, error)
                    raise
                if not self.scheduler.acquire(attempt["attempt_id"], node.get("resource_keys", [])):
                    self.machine.transition(attempt, NodeStatus.BLOCKED, "resource-unavailable")
                    ledger.append("node-attempt", deepcopy(attempt))
                    final_status = NodeStatus.BLOCKED
                    break
                resource_cursor = self._flush_resource_events(ledger, resource_cursor)

                self.machine.transition(attempt, NodeStatus.RUNNING, "fake-adapter-started")
                behavior = self._adapter_outcome(plan, node, attempt, ledger)
                if behavior == "timed-out":
                    deadline = datetime.fromisoformat(attempt["deadline"])
                    if not self.machine.has_timed_out(attempt, now=deadline):
                        raise AssertionError("deadline must be timed out at its own instant")
                    target = NodeStatus.BLOCKED
                    reason = "fake-adapter-timed-out"
                else:
                    try:
                        target = NodeStatus(behavior)
                    except ValueError:
                        target = NodeStatus.FAILED
                    if target not in {
                        NodeStatus.PASSED,
                        NodeStatus.FAILED,
                        NodeStatus.BLOCKED,
                        NodeStatus.SKIPPED,
                        NodeStatus.CANCELLED,
                    }:
                        target = NodeStatus.FAILED
                    reason = f"fake-adapter-{target.value}"
                self.machine.transition(attempt, target, reason)
                release_action = "timed-out" if behavior == "timed-out" else "cancelled" if target == NodeStatus.CANCELLED else "released"
                self.scheduler.release(attempt["attempt_id"], action=release_action)
                resource_cursor = self._flush_resource_events(ledger, resource_cursor)
                ledger.append("node-attempt", deepcopy(attempt))
                final_status = target

                retryable = target == NodeStatus.FAILED or behavior == "timed-out"
                if not retryable or not self._can_auto_retry(node, attempt):
                    break
                attempt = self._new_attempt(node, ledger)

            _record_successors(plan, node_id, final_status, predecessor_status)

        statuses = list(_latest_statuses(ledger.value["node_attempts"]).values())
        if statuses and all(status in {NodeStatus.PASSED, NodeStatus.SKIPPED} for status in statuses):
            final = "partial" if plan.get("status") == "degraded" or NodeStatus.SKIPPED in statuses else "completed"
        elif any(status == NodeStatus.CANCELLED for status in statuses):
            final = "cancelled"
        elif any(status == NodeStatus.BLOCKED for status in statuses):
            final = "blocked"
        else:
            final = "partial"
        return ledger.finalize(self._adjust_final_status(final, ledger))

    def _adjust_final_status(self, status: str, ledger: RunLedger) -> str:
        return status

    def _can_auto_retry(self, node: dict[str, Any], attempt: dict[str, Any]) -> bool:
        return self.machine.can_auto_retry(
            idempotent=node.get("idempotent", False),
            attempt_count=attempt["attempt_number"],
            max_retries=node.get("max_retries", 0),
        )

    def _new_attempt(self, node: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
        count = sum(item["node_id"] == node["id"] for item in ledger.value["node_attempts"])
        return self.machine.new_attempt(
            node["id"], attempt_number=count + 1,
            max_retries=node.get("max_retries", 0), timeout_seconds=node.get("timeout_seconds", 300),
        )

    def _prepare_approval(
        self, node: dict[str, Any], ledger: RunLedger, *, resume: bool,
    ) -> tuple[dict[str, Any], str]:
        approval = node.get("approval")
        previous_attempt = _latest_attempt(ledger.value["node_attempts"], node["id"])
        previous_record = _approval_for_attempt(ledger.value["approval_records"], previous_attempt)
        decision = self.approval_decisions.get(node["capability"])

        if approval and resume and previous_attempt and previous_record and previous_attempt["status"] == "blocked":
            attempt = deepcopy(previous_attempt)
            record = deepcopy(previous_record)
            if record["status"] == "pending" and decision:
                self.approvals.decide(record, decision, approval.get("scope", {}))
                ledger.append("approval-record", deepcopy(record))
            if record["status"] == "granted":
                self.machine.transition(attempt, NodeStatus.READY, "approval-granted-on-resume")
                return attempt, "granted"
            return attempt, record["status"]

        attempt = self._new_attempt(node, ledger)
        self.machine.transition(attempt, NodeStatus.READY, "dependencies-satisfied")
        if not approval:
            return attempt, "not-required"
        record = self.approvals.request(
            attempt["attempt_id"], approval["action"], approval["reason"], approval.get("scope", {})
        )
        if decision:
            self.approvals.decide(record, decision, approval.get("scope", {}))
        ledger.append("approval-record", deepcopy(record))
        if record["status"] != "granted":
            self.machine.transition(attempt, NodeStatus.BLOCKED, f"approval-{record['status']}")
            ledger.append("node-attempt", deepcopy(attempt))
        return attempt, record["status"]

    def _next_behavior(self, capability: str) -> str:
        configured = self.behaviors.get(capability, "passed")
        if isinstance(configured, str):
            return configured
        cursor = self.behavior_cursors[capability]
        self.behavior_cursors[capability] += 1
        return configured[min(cursor, len(configured) - 1)] if configured else "passed"

    def _adapter_outcome(
        self,
        plan: dict[str, Any],
        node: dict[str, Any],
        attempt: dict[str, Any],
        ledger: RunLedger,
    ) -> str:
        return self._next_behavior(node["capability"])

    def _prepare_adapter(
        self, plan: dict[str, Any], node: dict[str, Any], attempt: dict[str, Any], ledger: RunLedger,
    ) -> None:
        """Validate all preconditions before Runtime acquires node resources."""

    def _reusable_status(
        self,
        plan: dict[str, Any],
        node: dict[str, Any],
        ledger: RunLedger,
        latest_status: NodeStatus | None,
    ) -> NodeStatus | None:
        return NodeStatus.PASSED if latest_status == NodeStatus.PASSED else None

    def _record_adapter_contract_failure(
        self,
        plan: dict[str, Any],
        node: dict[str, Any],
        attempt: dict[str, Any],
        ledger: RunLedger,
        error: ContractError,
    ) -> None:
        """Let structured executors persist contract-failure provenance."""

    def _flush_resource_events(self, ledger: RunLedger, cursor: int) -> int:
        for event in self.scheduler.events[cursor:]:
            ledger.append("resource-event", deepcopy(event))
        return len(self.scheduler.events)


class RecordedAdapterExecutor(FakeAdapterExecutor):
    """Replay externally produced structured results through Runtime contracts.

    This executor never invokes a Skill or command. External Provider nodes must
    have a recorded Adapter Result v1; Core-internal nodes retain the deterministic
    fake behavior used by the runtime conformance harness.
    """

    def __init__(self, results: dict[str, dict[str, Any]], *, context: dict[str, Any]) -> None:
        super().__init__()
        self.results = results
        self.context = context
        self.prepared: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def _adjust_final_status(self, status: str, ledger: RunLedger) -> str:
        if status != "blocked":
            return status
        latest_attempts = _latest_attempts(ledger.value["node_attempts"])
        outcomes_by_attempt = {
            item["attempt_id"]: item for item in ledger.value["adapter_outcomes"]
        }
        has_current_partial = False
        for attempt in latest_attempts.values():
            if attempt["status"] != NodeStatus.BLOCKED.value:
                continue
            outcome = outcomes_by_attempt.get(attempt["attempt_id"])
            if outcome and outcome.get("status") == "partial":
                has_current_partial = True
                continue
            reason = attempt["events"][-1].get("reason") if attempt.get("events") else None
            if reason == "upstream-not-passed":
                continue
            return status
        if has_current_partial:
            return "partial"
        return status

    def _can_auto_retry(self, node: dict[str, Any], attempt: dict[str, Any]) -> bool:
        # One recorded result represents one external invocation. A retry needs
        # a new orchestrator envelope rather than replaying the same evidence.
        return False

    def _prepare_adapter(
        self, plan: dict[str, Any], node: dict[str, Any], attempt: dict[str, Any], ledger: RunLedger,
    ) -> None:
        if node.get("provider") == "core":
            return
        result = self.results.get(node["id"])
        if result is None:
            return
        if not isinstance(result, dict):
            raise ContractError("recorded adapter result must be an object")
        invocation_id = result.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise ContractError("recorded adapter result invocation_id is required")
        request = build_adapter_request(
            plan, node["id"], context=self.context, invocation_id=invocation_id
        )
        validate_adapter_result(request, result)
        if any(
            item.get("request_id") == request["request_id"]
            or item.get("invocation_id") == invocation_id
            for item in ledger.value["adapter_outcomes"]
        ):
            raise ContractError("recorded adapter result has already been consumed by an attempt")
        self.prepared[node["id"]] = (request, result)

    def _reusable_status(
        self,
        plan: dict[str, Any],
        node: dict[str, Any],
        ledger: RunLedger,
        latest_status: NodeStatus | None,
    ) -> NodeStatus | None:
        if node.get("provider") == "core":
            return NodeStatus.PASSED if latest_status == NodeStatus.PASSED else None
        result = self.results.get(node["id"])
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("invocation_id"), str)
            or not result["invocation_id"]
        ):
            return None
        request = build_adapter_request(
            plan, node["id"], context=self.context, invocation_id=result["invocation_id"]
        )
        latest_attempt = _latest_attempt(ledger.value["node_attempts"], node["id"])
        if latest_attempt is None:
            return None
        outcome = next(
            (
                item for item in reversed(ledger.value["adapter_outcomes"])
                if item.get("attempt_id") == latest_attempt["attempt_id"]
            ),
            None,
        )
        if (
            outcome is None
            or outcome.get("request_id") != request["request_id"]
            or outcome.get("status") != result.get("status")
        ):
            return None
        if latest_status == NodeStatus.PASSED and outcome.get("status") == "completed":
            return NodeStatus.PASSED
        if latest_status == NodeStatus.SKIPPED and outcome.get("status") == "partial":
            return NodeStatus.SKIPPED
        if latest_status == NodeStatus.BLOCKED and outcome.get("status") == "partial":
            return NodeStatus.BLOCKED
        return None

    def _record_adapter_contract_failure(
        self,
        plan: dict[str, Any],
        node: dict[str, Any],
        attempt: dict[str, Any],
        ledger: RunLedger,
        error: ContractError,
    ) -> None:
        result = self.results.get(node["id"])
        submitted_invocation_id = (
            result.get("invocation_id")
            if isinstance(result, dict) and isinstance(result.get("invocation_id"), str) and result["invocation_id"]
            else "unavailable"
        )
        invocation_id = f"contract-failure-{attempt['attempt_id']}"
        try:
            request_id = build_adapter_request(
                plan, node["id"], context=self.context, invocation_id=invocation_id
            )["request_id"]
        except ContractError:
            request_id = f"adapter-request-unavailable-{attempt['attempt_id']}"
        ledger.append(
            "adapter-outcome",
            {
                "attempt_id": attempt["attempt_id"],
                "cleanup": [],
                "failure_attribution": {
                    "category": "contract",
                    "summary": f"{error}; submitted_invocation_id={submitted_invocation_id}",
                },
                "invocation_id": invocation_id,
                "node_id": node["id"],
                "provider": node.get("provider") or "unknown-provider",
                "request_id": request_id,
                "status": "blocked",
            },
        )

    def _adapter_outcome(
        self,
        plan: dict[str, Any],
        node: dict[str, Any],
        attempt: dict[str, Any],
        ledger: RunLedger,
    ) -> str:
        if node.get("provider") == "core":
            return "passed"
        result = self.results.get(node["id"])
        if result is None:
            return "blocked"
        prepared = self.prepared.get(node["id"])
        if prepared is None:
            raise ContractError("recorded adapter result was not validated before resource acquisition")
        request, result = prepared
        ledger.append(
            "adapter-outcome",
            {
                "attempt_id": attempt["attempt_id"],
                "cleanup": deepcopy(result["cleanup"]),
                "failure_attribution": deepcopy(result["failure_attribution"]),
                "invocation_id": request["invocation_id"],
                "node_id": node["id"],
                "provider": node["provider"],
                "request_id": request["request_id"],
                "status": result["status"],
            },
        )
        for artifact in result["artifacts"]:
            ledger.append(
                "artifact-hash",
                {"attempt_id": attempt["attempt_id"], "node_id": node["id"], **deepcopy(artifact)},
            )
        for evidence in result["evidence"]:
            ledger.append(
                "adapter-evidence",
                {
                    "attempt_id": attempt["attempt_id"],
                    "node_id": node["id"],
                    "provider": node["provider"],
                    **deepcopy(evidence),
                },
            )
        if "no_test_reason" in result:
            ledger.append(
                "adapter-evidence",
                {
                    "attempt_id": attempt["attempt_id"],
                    "node_id": node["id"],
                    "provider": node["provider"],
                    "kind": "validation",
                    "status": result["status"],
                    "summary": result["no_test_reason"],
                    "data": {"suggested_validation": result["suggested_validation"]},
                    "artifact_ids": [],
                },
            )
        if (
            result["status"] == "partial"
            and "no_test_reason" in result
            and node["capability"].endswith(".auto")
        ):
            return "skipped"
        return {
            "completed": "passed",
            "partial": "blocked",
            "blocked": "blocked",
            "failed": "failed",
        }[result["status"]]


def _topological(plan: dict[str, Any]) -> list[str]:
    ids = {node["id"] for node in plan["nodes"]}
    incoming = {node_id: 0 for node_id in ids}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in plan["edges"]:
        incoming[edge["to"]] += 1
        outgoing[edge["from"]].append(edge["to"])
    queue = deque(sorted(node_id for node_id, count in incoming.items() if count == 0))
    result: list[str] = []
    while queue:
        node_id = queue.popleft()
        result.append(node_id)
        for target in sorted(outgoing[node_id]):
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    return result


def _record_successors(
    plan: dict[str, Any], node_id: str, status: NodeStatus, destination: dict[str, list[NodeStatus]],
) -> None:
    for edge in plan["edges"]:
        if edge["from"] == node_id:
            destination[edge["to"]].append(status)


def _latest_statuses(attempts: list[dict[str, Any]]) -> dict[str, NodeStatus]:
    return {node_id: NodeStatus(attempt["status"]) for node_id, attempt in _latest_attempts(attempts).items()}


def _latest_attempts(attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        latest[attempt["node_id"]] = attempt
    return latest


def _latest_attempt(attempts: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    return _latest_attempts(attempts).get(node_id)


def _approval_for_attempt(
    records: list[dict[str, Any]], attempt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not attempt:
        return None
    matching = [record for record in records if record["attempt_id"] == attempt["attempt_id"]]
    return matching[-1] if matching else None
