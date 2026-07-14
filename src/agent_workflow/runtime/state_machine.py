"""Fail-closed node lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..models import ContractError, NodeStatus


ALLOWED_TRANSITIONS: dict[NodeStatus, set[NodeStatus]] = {
    NodeStatus.PENDING: {NodeStatus.READY, NodeStatus.BLOCKED, NodeStatus.SKIPPED, NodeStatus.CANCELLED},
    NodeStatus.READY: {NodeStatus.RUNNING, NodeStatus.BLOCKED, NodeStatus.CANCELLED, NodeStatus.STALE},
    NodeStatus.RUNNING: {NodeStatus.PASSED, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.CANCELLED},
    NodeStatus.PASSED: {NodeStatus.STALE},
    NodeStatus.FAILED: {NodeStatus.STALE},
    NodeStatus.BLOCKED: {NodeStatus.READY, NodeStatus.STALE},
    NodeStatus.SKIPPED: {NodeStatus.STALE},
    NodeStatus.CANCELLED: {NodeStatus.STALE},
    NodeStatus.STALE: {NodeStatus.READY},
}


class NodeStateMachine:
    def new_attempt(
        self,
        node_id: str,
        *,
        attempt_number: int = 1,
        max_retries: int = 0,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        if attempt_number < 1 or max_retries < 0 or timeout_seconds <= 0:
            raise ContractError("invalid attempt retry or timeout metadata")
        now = _now()
        deadline = (datetime.fromisoformat(now) + timedelta(seconds=timeout_seconds)).isoformat()
        return {
            "attempt_id": f"attempt-{uuid4().hex}",
            "attempt_number": attempt_number,
            "deadline": deadline,
            "events": [{"at": now, "from": None, "reason": "created", "to": NodeStatus.PENDING.value}],
            "max_retries": max_retries,
            "node_id": node_id,
            "schema_version": "1.0",
            "status": NodeStatus.PENDING.value,
            "timeout_seconds": timeout_seconds,
        }

    def transition(self, attempt: dict[str, Any], target: NodeStatus, reason: str) -> dict[str, Any]:
        current = NodeStatus(attempt["status"])
        if target not in ALLOWED_TRANSITIONS[current]:
            raise ContractError(f"illegal node transition: {current.value} -> {target.value}")
        attempt["events"].append({"at": _now(), "from": current.value, "reason": reason, "to": target.value})
        attempt["status"] = target.value
        return attempt

    @staticmethod
    def can_auto_retry(*, idempotent: bool, attempt_count: int, max_retries: int) -> bool:
        return idempotent and attempt_count <= max_retries

    @staticmethod
    def has_timed_out(attempt: dict[str, Any], *, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        deadline = datetime.fromisoformat(attempt["deadline"])
        return current >= deadline


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
