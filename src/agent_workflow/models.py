"""Shared enums and lightweight validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ContractError(ValueError):
    """Raised when a versioned workflow contract is invalid."""


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    STALE = "stale"


TERMINAL_STATUSES = {
    NodeStatus.PASSED,
    NodeStatus.FAILED,
    NodeStatus.BLOCKED,
    NodeStatus.SKIPPED,
    NodeStatus.CANCELLED,
    NodeStatus.STALE,
}


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str = "$"
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocking": self.blocking,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


def require_fields(value: dict[str, Any], fields: set[str], kind: str) -> None:
    missing = sorted(fields - value.keys())
    if missing:
        raise ContractError(f"{kind} missing required fields: {', '.join(missing)}")


def require_version(value: dict[str, Any], expected: str = "1.0") -> None:
    actual = value.get("schema_version")
    if actual != expected:
        raise ContractError(f"unsupported schema_version: {actual!r}; expected {expected!r}")
