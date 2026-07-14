"""Runtime validation for Phase 1 artifacts.

JSON Schema files document the external contract. These validators enforce the
cross-reference and enum rules that are important to the workflow runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import ContractError, NodeStatus, require_fields, require_version


LEGAL_NODE_TRANSITIONS = {
    "pending": {"ready", "blocked", "skipped", "cancelled"},
    "ready": {"running", "blocked", "cancelled", "stale"},
    "running": {"passed", "failed", "blocked", "cancelled"},
    "passed": {"stale"}, "failed": {"stale"}, "blocked": {"ready", "stale"},
    "skipped": {"stale"}, "cancelled": {"stale"}, "stale": {"ready"},
}


def _base(value: dict[str, Any], fields: set[str], kind: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{kind} must be an object")
    require_version(value)
    require_fields(value, fields | {"schema_version"}, kind)


def validate_project_profile(value: dict[str, Any]) -> None:
    _base(value, {"repository", "platforms", "modules", "ambiguities"}, "project-profile")
    require_fields(value["repository"], {"root", "kind"}, "project-profile.repository")
    if value["repository"].get("kind") not in {"single", "multi-module", "monorepo", "unknown"}:
        raise ContractError("project-profile repository.kind is invalid")
    if not isinstance(value["repository"]["root"], str) or not value["repository"]["root"]:
        raise ContractError("project-profile repository.root is invalid")
    if not isinstance(value["platforms"], list) or any(not isinstance(item, str) for item in value["platforms"]):
        raise ContractError("project-profile platforms must be strings")
    if len(value["platforms"]) != len(set(value["platforms"])):
        raise ContractError("project-profile platforms must be unique")
    for module in value["modules"]:
        require_fields(module, {"path", "platform", "confidence", "evidence"}, "project-profile.module")
        if not isinstance(module["confidence"], (int, float)) or not 0 <= module["confidence"] <= 1:
            raise ContractError("project-profile module confidence is invalid")
        if not isinstance(module["evidence"], list) or any(not isinstance(item, str) for item in module["evidence"]):
            raise ContractError("project-profile module evidence is invalid")
    for ambiguity in value["ambiguities"]:
        require_fields(ambiguity, {"path", "candidates", "reason"}, "project-profile.ambiguity")


def validate_manifest(value: dict[str, Any]) -> None:
    _base(value, {"id", "kind", "detection", "capabilities"}, "plugin-manifest")
    if value["kind"] not in {"platform", "stack", "discipline", "adapter"}:
        raise ContractError("plugin-manifest kind is invalid")
    detection = value["detection"]
    require_fields(detection, {"strong", "medium", "weak"}, "plugin-manifest.detection")
    ids = [item.get("id") for item in value["capabilities"]]
    if None in ids or len(ids) != len(set(ids)):
        raise ContractError("plugin-manifest capability ids must be present and unique")


def validate_capability_contract(value: dict[str, Any]) -> None:
    _base(
        value,
        {
            "id",
            "version",
            "input_schema",
            "output_schema",
            "permission_profile",
            "side_effects",
            "idempotent",
            "concurrency_keys",
            "failure_codes",
        },
        "capability-contract",
    )


def validate_resolved_policy(value: dict[str, Any]) -> None:
    _base(value, {"selected_platforms", "task", "decisions", "constraints", "fingerprint"}, "resolved-policy")
    if not isinstance(value["fingerprint"], str) or not value["fingerprint"]:
        raise ContractError("resolved-policy fingerprint is invalid")
    require_fields(value["task"], {"text", "type", "risk", "disciplines"}, "resolved-policy.task")
    for decision in value["decisions"]:
        require_fields(
            decision,
            {"decision", "reason_code", "source", "confidence", "merge_strategy", "overridden_candidates"},
            "decision",
        )
        if not isinstance(decision["confidence"], (int, float)) or not 0 <= decision["confidence"] <= 1:
            raise ContractError("decision confidence is invalid")
        if decision["merge_strategy"] not in {"replace", "append", "union", "intersect", "deny-wins", "locked"}:
            raise ContractError("decision merge strategy is invalid")


def validate_workflow_plan(value: dict[str, Any]) -> None:
    _base(value, {"plan_id", "fingerprint", "nodes", "edges", "status"}, "workflow-plan")
    node_ids = [node.get("id") for node in value["nodes"]]
    if None in node_ids or len(node_ids) != len(set(node_ids)):
        raise ContractError("workflow-plan node ids must be present and unique")
    known = set(node_ids)
    for node in value["nodes"]:
        require_fields(
            node,
            {"id", "capability", "mandatory", "status", "timeout_seconds", "max_retries"},
            "workflow-plan.node",
        )
        if node["timeout_seconds"] <= 0 or node["max_retries"] < 0:
            raise ContractError("workflow-plan node retry or timeout metadata is invalid")
    for edge in value["edges"]:
        require_fields(edge, {"from", "to"}, "workflow-plan.edge")
        if edge["from"] not in known or edge["to"] not in known:
            raise ContractError("workflow-plan edge references unknown node")
    incoming = {node_id: 0 for node_id in known}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in known}
    for edge in value["edges"]:
        incoming[edge["to"]] += 1
        outgoing[edge["from"]].append(edge["to"])
    queue = sorted(node_id for node_id, count in incoming.items() if count == 0)
    visited = 0
    while queue:
        node_id = queue.pop(0)
        visited += 1
        for target in sorted(outgoing[node_id]):
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if visited != len(known):
        raise ContractError("workflow-plan contains dependency cycle")


def validate_node_attempt(value: dict[str, Any]) -> None:
    _base(value, {"attempt_id", "node_id", "status", "events", "attempt_number", "max_retries", "timeout_seconds", "deadline"}, "node-attempt")
    try:
        NodeStatus(value["status"])
    except ValueError as error:
        raise ContractError("node-attempt status is invalid") from error
    if value["attempt_number"] < 1 or value["max_retries"] < 0 or value["timeout_seconds"] <= 0:
        raise ContractError("node-attempt retry or timeout metadata is invalid")
    events = value["events"]
    if not events or events[0].get("from") is not None or events[0].get("to") != "pending":
        raise ContractError("node-attempt must start with a pending creation event")
    previous = None
    for index, event in enumerate(events):
        require_fields(event, {"at", "from", "to", "reason"}, "node-attempt.event")
        target = event["to"]
        if target not in {status.value for status in NodeStatus}:
            raise ContractError("node-attempt event status is invalid")
        if index and (event["from"] != previous or target not in LEGAL_NODE_TRANSITIONS[previous]):
            raise ContractError("node-attempt event transition is invalid")
        previous = target
    if previous != value["status"]:
        raise ContractError("node-attempt final event does not match status")


def validate_run_ledger(value: dict[str, Any]) -> None:
    _base(
        value,
        {"run_id", "plan_fingerprint", "node_attempts", "resource_events", "approval_records", "artifact_hashes", "final_status"},
        "run-ledger",
    )
    for attempt in value["node_attempts"]:
        validate_node_attempt(attempt)
    for event in value["resource_events"]:
        validate_resource_event(event)
    for record in value["approval_records"]:
        validate_approval_record(record)
    attempts = {attempt["attempt_id"] for attempt in value["node_attempts"]}
    if any(event["attempt_id"] not in attempts for event in value["resource_events"]):
        raise ContractError("resource-event references unknown attempt")
    if any(record["attempt_id"] not in attempts for record in value["approval_records"]):
        raise ContractError("approval-record references unknown attempt")
    sequences = [event["sequence"] for event in value["resource_events"]]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise ContractError("resource-event sequences must be increasing and unique")
    by_node: dict[str, list[int]] = {}
    for attempt in value["node_attempts"]:
        by_node.setdefault(attempt["node_id"], []).append(attempt["attempt_number"])
    if any(numbers != sorted(numbers) for numbers in by_node.values()):
        raise ContractError("node attempt numbers must be monotonic")


def validate_resource_event(value: dict[str, Any]) -> None:
    _base(value, {"sequence", "attempt_id", "resource_key", "action"}, "resource-event")
    if value["action"] not in {"requested", "acquired", "released", "timed-out", "cancelled"}:
        raise ContractError("resource-event action is invalid")


def validate_approval_record(value: dict[str, Any]) -> None:
    _base(value, {"attempt_id", "action", "reason", "scope", "scope_hash", "status"}, "approval-record")
    if value["status"] not in {"pending", "granted", "denied", "expired"}:
        raise ContractError("approval-record status is invalid")


def validate_delivery_report(value: dict[str, Any]) -> None:
    _base(value, {"run_id", "status", "routing", "validation", "known_risks", "blocked_items"}, "delivery-report")
    if value["status"] not in {"completed", "partial", "blocked", "cancelled"}:
        raise ContractError("delivery-report status is invalid")


VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "approval-record": validate_approval_record,
    "capability-contract": validate_capability_contract,
    "delivery-report": validate_delivery_report,
    "node-attempt": validate_node_attempt,
    "plugin-manifest": validate_manifest,
    "project-profile": validate_project_profile,
    "resolved-policy": validate_resolved_policy,
    "resource-event": validate_resource_event,
    "run-ledger": validate_run_ledger,
    "workflow-plan": validate_workflow_plan,
}


def validate(kind: str, value: dict[str, Any]) -> None:
    try:
        validator = VALIDATORS[kind]
    except KeyError as error:
        raise ContractError(f"unknown artifact kind: {kind}") from error
    validator(value)
