"""Pure contract bridge between workflow nodes and external providers.

This module intentionally performs no provider invocation, command execution, or
repository writes.  It only freezes request identity and validates structured
provider output before the runtime may consume it.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from ..canonical_json import sha256
from ..models import ContractError, require_fields, require_version


_RESULT_STATUSES = {"completed", "partial", "blocked", "failed"}
_EVIDENCE_KINDS = {"validation", "review", "delivery", "diagnostic"}
_EVIDENCE_STATUSES = {"passed", "completed", "partial", "blocked", "failed"}
_ARTIFACT_KINDS = {
    "structured-report",
    "test-report",
    "review-report",
    "delivery-report",
    "diagnostics",
    "raw-log",
    "other",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "invocation_id",
    "plan_id",
    "plan_fingerprint",
    "node_id",
    "capability",
    "provider",
    "binding",
    "task_context",
    "checkpoints",
}
_RESULT_FIELDS = {
    "schema_version",
    "request_id",
    "invocation_id",
    "plan_fingerprint",
    "node_id",
    "capability",
    "provider",
    "binding",
    "status",
    "evidence",
    "artifacts",
    "failure_attribution",
    "cleanup",
}


def build_adapter_request(
    plan: dict[str, Any],
    node_id: str,
    *,
    context: dict[str, Any],
    invocation_id: str,
) -> dict[str, Any]:
    """Freeze a plan node and caller context into an Adapter Request v1.

    ``context`` is copied in full as ``task_context``.  Its ``checkpoints``
    object is also promoted to an explicit field so providers cannot silently
    discard the CP0-CP3 execution boundary.
    """

    require_version(plan)
    require_fields(plan, {"plan_id", "fingerprint", "nodes"}, "workflow-plan")
    if not isinstance(node_id, str) or not node_id:
        raise ContractError("adapter-request node_id is invalid")
    if not isinstance(context, dict):
        raise ContractError("adapter-request context must be an object")
    if not _is_nonempty_string(invocation_id):
        raise ContractError("adapter-request invocation_id must be a non-empty string")
    if "checkpoints" not in context or not isinstance(context["checkpoints"], dict):
        raise ContractError("adapter-request context.checkpoints must be an object")

    matches = [node for node in plan["nodes"] if isinstance(node, dict) and node.get("id") == node_id]
    if len(matches) != 1:
        raise ContractError(f"adapter-request node is not uniquely present in plan: {node_id!r}")
    node = matches[0]
    require_fields(node, {"id", "capability", "provider", "binding"}, "workflow-plan.node")
    for field in ("plan_id", "fingerprint"):
        if not isinstance(plan[field], str) or not plan[field]:
            raise ContractError(f"adapter-request plan {field} is invalid")
    for field in ("capability", "provider"):
        if not isinstance(node[field], str) or not node[field]:
            raise ContractError(f"adapter-request node {field} is invalid")
    _validate_binding(node["binding"], "workflow-plan.node.binding")

    identity = {
        "binding": deepcopy(node["binding"]),
        "capability": node["capability"],
        "checkpoints": deepcopy(context["checkpoints"]),
        "invocation_id": invocation_id,
        "node_id": node["id"],
        "plan_fingerprint": plan["fingerprint"],
        "plan_id": plan["plan_id"],
        "provider": node["provider"],
        "schema_version": "1.0",
        "task_context": deepcopy(context),
    }
    request = {"request_id": f"adapter-request-{sha256(identity)[:16]}", **identity}
    validate_adapter_request(request)
    return request


def validate_adapter_request(value: dict[str, Any]) -> None:
    """Validate an Adapter Request v1 without resolving or invoking a provider."""

    _require_exact_object(value, _REQUEST_FIELDS, "adapter-request")
    require_version(value)
    _require_nonempty_strings(
        value,
        {"request_id", "invocation_id", "plan_id", "plan_fingerprint", "node_id", "capability", "provider"},
        "adapter-request",
    )
    _validate_binding(value["binding"], "adapter-request binding")
    if not isinstance(value["task_context"], dict):
        raise ContractError("adapter-request task_context must be an object")
    if not isinstance(value["checkpoints"], dict):
        raise ContractError("adapter-request checkpoints must be an object")
    if value["task_context"].get("checkpoints") != value["checkpoints"]:
        raise ContractError("adapter-request checkpoints do not match task_context")
    identity = {key: deepcopy(value[key]) for key in _REQUEST_FIELDS if key != "request_id"}
    expected_request_id = f"adapter-request-{sha256(identity)[:16]}"
    if value["request_id"] != expected_request_id:
        raise ContractError("adapter-request request_id does not match frozen identity")


def validate_adapter_result(request: dict[str, Any], result: dict[str, Any]) -> None:
    """Validate a provider result against its frozen request identity.

    Validation and review capabilities require their matching structured
    evidence.  A raw log can be attached for diagnostics but can never satisfy
    either gate.  A verification node may instead expose a deliberate test gap
    through ``no_test_reason`` and ``suggested_validation``.
    """

    validate_adapter_request(request)
    if not isinstance(result, dict):
        raise ContractError("adapter-result must be an object")
    allowed = _RESULT_FIELDS | {"no_test_reason", "suggested_validation"}
    unknown = sorted(result.keys() - allowed)
    if unknown:
        raise ContractError(f"adapter-result has unknown fields: {', '.join(unknown)}")
    require_version(result)
    require_fields(result, _RESULT_FIELDS, "adapter-result")
    _require_nonempty_strings(
        result,
        {"request_id", "invocation_id", "plan_fingerprint", "node_id", "capability", "provider"},
        "adapter-result",
    )
    _validate_binding(result["binding"], "adapter-result binding")

    for field in ("request_id", "invocation_id", "plan_fingerprint", "node_id", "capability", "provider", "binding"):
        if result[field] != request[field]:
            raise ContractError(f"adapter-result {field} does not match request")
    if result["status"] not in _RESULT_STATUSES:
        raise ContractError("adapter-result status is invalid")

    attribution = result["failure_attribution"]
    _require_exact_object(attribution, {"category", "summary"}, "adapter-result.failure_attribution")
    _require_nonempty_strings(attribution, {"category", "summary"}, "adapter-result.failure_attribution")
    if attribution["category"] not in {"none", "code", "environment", "provider", "contract"}:
        raise ContractError("adapter-result failure attribution category is invalid")
    if result["status"] in {"blocked", "failed"} and attribution["category"] == "none":
        raise ContractError("adapter-result blocked or failed status requires failure attribution")
    _validate_cleanup(result["cleanup"])

    artifact_ids = _validate_artifacts(result["artifacts"])
    evidence_kinds = _validate_evidence(result["evidence"], artifact_ids)
    evidence_statuses = {item["status"] for item in result["evidence"]}
    if result["status"] == "completed" and evidence_statuses - {"passed", "completed"}:
        raise ContractError("adapter-result completed status conflicts with evidence status")
    if result["status"] == "partial" and evidence_statuses - {"passed", "completed", "partial"}:
        raise ContractError("adapter-result partial status conflicts with evidence status")
    if result["status"] == "blocked" and "failed" in evidence_statuses:
        raise ContractError("adapter-result blocked status conflicts with failed evidence")
    if result["status"] == "failed" and "failed" not in evidence_statuses:
        raise ContractError("adapter-result failed status requires failed evidence")
    if any(item["status"] == "failed" for item in result["cleanup"]) and result["status"] not in {"blocked", "failed"}:
        raise ContractError("adapter-result failed cleanup must block or fail the result")
    no_test_reason = result.get("no_test_reason")
    suggested_validation = result.get("suggested_validation")
    if (no_test_reason is None) != (suggested_validation is None):
        raise ContractError("adapter-result no_test_reason and suggested_validation must be provided together")
    if no_test_reason is not None:
        if not _is_nonempty_string(no_test_reason) or not _is_nonempty_string(suggested_validation):
            raise ContractError("adapter-result validation gap fields must be non-empty strings")
        if not request["capability"].startswith("verification."):
            raise ContractError("adapter-result validation gap is only valid for verification capabilities")
        if result["status"] not in {"partial", "blocked"}:
            raise ContractError("adapter-result validation gap requires partial or blocked status")
    if result["status"] == "blocked" and no_test_reason is None and not evidence_statuses & {"blocked", "failed"}:
        raise ContractError("adapter-result blocked status requires blocked evidence")

    capability = request["capability"]
    if capability.startswith("verification.") and "validation" not in evidence_kinds and no_test_reason is None:
        raise ContractError("adapter-result verification requires structured validation evidence")
    if capability.startswith("verification.") and capability.endswith(".auto") and no_test_reason is None:
        validation = next(item for item in result["evidence"] if item["kind"] == "validation")
        executed = validation["data"].get("executed_validation")
        accepted = validation["data"].get("accepted_evidence")
        executed_valid = (
            _validate_successful_verification_entries(executed, "executed_validation")
            if executed is not None else False
        )
        accepted_valid = (
            _validate_successful_verification_entries(accepted, "accepted_evidence")
            if accepted is not None else False
        )
        if not executed_valid and not accepted_valid:
            raise ContractError(
                "adapter-result automatic verification requires executed_validation or accepted_evidence"
            )
    if (capability == "review.independent" or capability.startswith("review.")) and "review" not in evidence_kinds:
        raise ContractError("adapter-result review requires structured review evidence")
    if capability == "review.independent" or capability.startswith("review."):
        review = next(item for item in result["evidence"] if item["kind"] == "review")
        reviewer = review["data"].get("reviewer_actor")
        implementer = review["data"].get("implementation_actor")
        if not _is_nonempty_string(reviewer) or not _is_nonempty_string(implementer):
            raise ContractError("adapter-result review actor identities are required")
        if reviewer == implementer:
            raise ContractError("adapter-result reviewer must be independent from implementation actor")
        actors = request["task_context"].get("actors")
        if not isinstance(actors, dict):
            raise ContractError("adapter-request review requires orchestrator-frozen actors")
        if actors.get("implementation_actor") != implementer or actors.get("reviewer_actor") != reviewer:
            raise ContractError("adapter-result review actors do not match orchestrator-frozen actors")
        if actors.get("implementation_actor") == actors.get("reviewer_actor"):
            raise ContractError("adapter-request reviewer must be independent from implementation actor")
        if not isinstance(review["data"].get("blocking_issues"), list):
            raise ContractError("adapter-result review blocking_issues must be an array")
        blocking_issues = review["data"]["blocking_issues"]
        if blocking_issues and (result["status"] not in {"blocked", "failed"} or review["status"] not in {"blocked", "failed"}):
            raise ContractError("adapter-result review blocking issues must block the result")
        if not blocking_issues and result["status"] == "completed" and review["status"] != "passed":
            raise ContractError("adapter-result successful review evidence must be passed")
    if not result["evidence"] and no_test_reason is None:
        raise ContractError("adapter-result requires structured evidence")


def _validate_successful_verification_entries(value: Any, field: str) -> bool:
    if not isinstance(value, list):
        raise ContractError(f"adapter-result automatic verification {field} must be an array")
    if not value:
        return False
    selection_only = {"affected-tests", "route", "test-selection"}
    for entry in value:
        if not isinstance(entry, dict):
            raise ContractError(f"adapter-result automatic verification {field} entries must be objects")
        kind = entry.get("kind")
        status = entry.get("status")
        if not _is_nonempty_string(kind) or kind in selection_only:
            raise ContractError(
                f"adapter-result automatic verification {field} contains selection-only or invalid evidence"
            )
        if status not in {"passed", "completed"}:
            raise ContractError(
                f"adapter-result automatic verification {field} requires successful evidence"
            )
    return True


def _validate_artifacts(value: Any) -> set[str]:
    if not isinstance(value, list):
        raise ContractError("adapter-result artifacts must be an array")
    artifact_ids: set[str] = set()
    fields = {"artifact_id", "kind", "sha256", "uri"}
    for artifact in value:
        _require_exact_object(artifact, fields, "adapter-result.artifact")
        _require_nonempty_strings(artifact, fields, "adapter-result.artifact")
        if artifact["artifact_id"] in artifact_ids:
            raise ContractError("adapter-result artifact ids must be unique")
        if artifact["kind"] not in _ARTIFACT_KINDS:
            raise ContractError("adapter-result artifact kind is invalid")
        if not _SHA256.fullmatch(artifact["sha256"]):
            raise ContractError("adapter-result artifact sha256 is invalid")
        artifact_ids.add(artifact["artifact_id"])
    return artifact_ids


def _validate_evidence(value: Any, artifact_ids: set[str]) -> set[str]:
    if not isinstance(value, list):
        raise ContractError("adapter-result evidence must be an array")
    kinds: set[str] = set()
    fields = {"kind", "status", "summary", "data", "artifact_ids"}
    for evidence in value:
        _require_exact_object(evidence, fields, "adapter-result.evidence")
        _require_nonempty_strings(evidence, {"kind", "status", "summary"}, "adapter-result.evidence")
        if evidence["kind"] not in _EVIDENCE_KINDS:
            raise ContractError("adapter-result evidence kind is invalid")
        if evidence["status"] not in _EVIDENCE_STATUSES:
            raise ContractError("adapter-result evidence status is invalid")
        if not isinstance(evidence["data"], dict) or not evidence["data"]:
            raise ContractError("adapter-result evidence data must be a non-empty object")
        references = evidence["artifact_ids"]
        if not isinstance(references, list) or any(not _is_nonempty_string(item) for item in references):
            raise ContractError("adapter-result evidence artifact_ids must be strings")
        if len(references) != len(set(references)):
            raise ContractError("adapter-result evidence artifact_ids must be unique")
        unknown = sorted(set(references) - artifact_ids)
        if unknown:
            raise ContractError(f"adapter-result evidence references unknown artifacts: {', '.join(unknown)}")
        kinds.add(evidence["kind"])
    return kinds


def _validate_cleanup(value: Any) -> None:
    if not isinstance(value, list):
        raise ContractError("adapter-result cleanup must be an array")
    fields = {"resource", "status", "detail"}
    for item in value:
        _require_exact_object(item, fields, "adapter-result.cleanup")
        _require_nonempty_strings(item, fields, "adapter-result.cleanup")
        if item["status"] not in {"not-required", "completed", "failed"}:
            raise ContractError("adapter-result cleanup status is invalid")


def _require_exact_object(value: Any, fields: set[str], kind: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{kind} must be an object")
    require_fields(value, fields, kind)
    unknown = sorted(value.keys() - fields)
    if unknown:
        raise ContractError(f"{kind} has unknown fields: {', '.join(unknown)}")


def _require_nonempty_strings(value: dict[str, Any], fields: set[str], kind: str) -> None:
    for field in fields:
        if not _is_nonempty_string(value.get(field)):
            raise ContractError(f"{kind} {field} must be a non-empty string")


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_binding(value: Any, kind: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{kind} must be an object")
    required = {"kind", "name"}
    require_fields(value, required, kind)
    unknown = sorted(value.keys() - required - {"mode"})
    if unknown:
        raise ContractError(f"{kind} has unknown fields: {', '.join(unknown)}")
    if value["kind"] not in {"skill", "agent", "script", "tool"}:
        raise ContractError(f"{kind} kind is invalid")
    if not _is_nonempty_string(value["name"]):
        raise ContractError(f"{kind} name is invalid")
    if "mode" in value and not _is_nonempty_string(value["mode"]):
        raise ContractError(f"{kind} mode is invalid")
