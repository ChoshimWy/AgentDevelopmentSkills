"""Attempt-scoped approval records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..canonical_json import sha256
from ..models import ContractError


class ApprovalGate:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def request(self, attempt_id: str, action: str, reason: str, scope: dict[str, Any]) -> dict[str, Any]:
        record = {
            "action": action,
            "attempt_id": attempt_id,
            "reason": reason,
            "schema_version": "1.0",
            "scope": deepcopy(scope),
            "scope_hash": sha256(scope),
            "status": "pending",
        }
        self.records.append(record)
        return record

    def decide(self, record: dict[str, Any], status: str, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        if status not in {"granted", "denied", "expired"}:
            raise ContractError(f"invalid approval status: {status}")
        if scope is not None and sha256(scope) != record["scope_hash"]:
            raise ContractError("approval scope cannot be expanded or changed")
        if record["status"] != "pending":
            raise ContractError("approval has already been decided")
        record["status"] = status
        return record

    def revoke(self, record: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
        """Revoke one exact granted attempt/scope without affecting any other approval."""

        if record.get("status") != "granted":
            raise ContractError("only a granted approval can be revoked")
        if sha256(scope) != record.get("scope_hash"):
            raise ContractError("approval revocation scope differs from the grant")
        record["status"] = "revoked"
        return record

    @staticmethod
    def is_granted(record: dict[str, Any], attempt_id: str, scope: dict[str, Any]) -> bool:
        return (
            record.get("attempt_id") == attempt_id
            and record.get("status") == "granted"
            and record.get("scope_hash") == sha256(scope)
        )
