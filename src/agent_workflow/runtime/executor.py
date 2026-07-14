"""Deterministic fake-adapter executor used by Phase 1 conformance tests."""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import NodeStatus
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
        resume: bool = False,
    ) -> dict[str, Any]:
        ledger_file = Path(ledger_path) if ledger_path else None
        if resume:
            if ledger_file is None or not ledger_file.exists():
                raise ValueError("resume requires an existing ledger path")
            ledger = RunLedger.replay(ledger_file, plan["fingerprint"])
            ledger.append("run-resumed", {"plan_fingerprint": plan["fingerprint"]})
        else:
            ledger = RunLedger(plan["fingerprint"], path=ledger_file)
            ledger.append("run-started", {"plan_fingerprint": plan["fingerprint"]})
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
            if latest.get(node_id) == NodeStatus.PASSED:
                _record_successors(plan, node_id, NodeStatus.PASSED, predecessor_status)
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
                if not self.scheduler.acquire(attempt["attempt_id"], node.get("resource_keys", [])):
                    self.machine.transition(attempt, NodeStatus.BLOCKED, "resource-unavailable")
                    ledger.append("node-attempt", deepcopy(attempt))
                    final_status = NodeStatus.BLOCKED
                    break
                resource_cursor = self._flush_resource_events(ledger, resource_cursor)

                self.machine.transition(attempt, NodeStatus.RUNNING, "fake-adapter-started")
                behavior = self._next_behavior(node["capability"])
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
                    if target not in {NodeStatus.PASSED, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.CANCELLED}:
                        target = NodeStatus.FAILED
                    reason = f"fake-adapter-{target.value}"
                self.machine.transition(attempt, target, reason)
                release_action = "timed-out" if behavior == "timed-out" else "cancelled" if target == NodeStatus.CANCELLED else "released"
                self.scheduler.release(attempt["attempt_id"], action=release_action)
                resource_cursor = self._flush_resource_events(ledger, resource_cursor)
                ledger.append("node-attempt", deepcopy(attempt))
                final_status = target

                retryable = target == NodeStatus.FAILED or behavior == "timed-out"
                if not retryable or not self.machine.can_auto_retry(
                    idempotent=node.get("idempotent", False),
                    attempt_count=attempt["attempt_number"],
                    max_retries=node.get("max_retries", 0),
                ):
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
        return ledger.finalize(final)

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

    def _flush_resource_events(self, ledger: RunLedger, cursor: int) -> int:
        for event in self.scheduler.events[cursor:]:
            ledger.append("resource-event", deepcopy(event))
        return len(self.scheduler.events)


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
