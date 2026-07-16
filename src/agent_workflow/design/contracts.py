"""Runtime invariants for the Phase 3 platform-neutral design contracts."""

from __future__ import annotations

import re
from typing import Any

from ..canonical_json import sha256
from ..models import ContractError, require_fields, require_version


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_KINDS = {"figma", "sketch", "manual", "screenshot"}
_PROVENANCE_KINDS = {"source", "manual-contract", "inference", "unknown"}
_SCOPE_KINDS = {"document", "page", "node"}


def design_fingerprint(value: Any) -> str:
    """Return a namespaced, deterministic fingerprint for a design artifact."""

    return f"design-v1:{sha256(value)}"


def validate_design_evidence(value: dict[str, Any]) -> None:
    fields = {"schema_version", "evidence_id", "source", "permission", "slices", "unknowns", "status"}
    _exact(value, fields, "design-evidence")
    require_version(value)
    _nonempty(value, {"evidence_id"}, "design-evidence")
    if value["status"] not in {"complete", "partial", "blocked"}:
        raise ContractError("design-evidence status is invalid")

    source = value["source"]
    _exact(
        source,
        {"kind", "document_id", "document_version", "scope", "content_sha256", "parser"},
        "design-evidence.source",
    )
    _nonempty(source, {"kind", "document_id", "document_version", "content_sha256"}, "design-evidence.source")
    if source["kind"] not in _SOURCE_KINDS:
        raise ContractError("design-evidence source kind is invalid")
    if not _SHA256.fullmatch(source["content_sha256"]):
        raise ContractError("design-evidence source hash is invalid")
    _validate_scope(source["scope"], "design-evidence.source.scope")
    _exact(source["parser"], {"name", "version"}, "design-evidence.source.parser")
    _nonempty(source["parser"], {"name", "version"}, "design-evidence.source.parser")

    permission = value["permission"]
    _exact(permission, {"mode", "approval_id"}, "design-evidence.permission")
    if permission["mode"] not in {"read", "export", "write"}:
        raise ContractError("design-evidence permission mode is invalid")
    if permission["mode"] == "write" and not _is_nonempty(permission["approval_id"]):
        raise ContractError("design-evidence write permission requires approval_id")
    if permission["mode"] != "write" and permission["approval_id"] is not None:
        raise ContractError("design-evidence read/export permission cannot carry write approval")

    if not isinstance(value["slices"], list) or not value["slices"]:
        raise ContractError("design-evidence slices must be non-empty")
    slice_ids: list[str] = []
    for item in value["slices"]:
        _exact(item, {"id", "kind", "payload", "provenance"}, "design-evidence.slice")
        _nonempty(item, {"id", "kind"}, "design-evidence.slice")
        if item["kind"] not in {"screen", "region", "component", "token", "asset"}:
            raise ContractError("design-evidence slice kind is invalid")
        if not isinstance(item["payload"], dict) or not item["payload"]:
            raise ContractError("design-evidence slice payload must be non-empty")
        _validate_provenance(item["provenance"], "design-evidence.slice.provenance")
        slice_ids.append(item["id"])
    _unique_sorted(slice_ids, "design-evidence slice ids")
    _validate_unknowns(value["unknowns"], "design-evidence")
    if value["status"] == "complete" and any(item["blocking"] for item in value["unknowns"]):
        raise ContractError("complete design-evidence cannot contain blocking unknowns")
    if value["status"] == "blocked" and not any(item["blocking"] for item in value["unknowns"]):
        raise ContractError("blocked design-evidence requires a blocking unknown")


def validate_design_source_request(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "request_id", "attempt_id", "operation", "source_kind",
        "document_id", "document_version", "scope", "approval_id", "data_policy",
    }
    _exact(value, fields, "design-source-request")
    require_version(value)
    _nonempty(value, {"request_id", "attempt_id", "document_id", "document_version"}, "design-source-request")
    if value["operation"] not in {"read", "export", "write"}:
        raise ContractError("design-source-request operation is invalid")
    if value["source_kind"] not in {"figma", "sketch"}:
        raise ContractError("design-source-request source kind is invalid")
    _validate_scope(value["scope"], "design-source-request.scope")
    if value["operation"] == "write" and not _is_nonempty(value["approval_id"]):
        raise ContractError("design-source-request write requires approval_id")
    if value["operation"] != "write" and value["approval_id"] is not None:
        raise ContractError("design-source-request read/export cannot carry write approval")
    policy = value["data_policy"]
    _exact(policy, {"allow_credentials", "max_nodes", "retention"}, "design-source-request.data_policy")
    if policy["allow_credentials"] is not False or policy["retention"] != "task":
        raise ContractError("design-source-request data policy is invalid")
    if isinstance(policy["max_nodes"], bool) or not isinstance(policy["max_nodes"], int) or not 1 <= policy["max_nodes"] <= 128:
        raise ContractError("design-source-request max_nodes is invalid")


def validate_canonical_ui_ir(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "ir_id", "evidence_refs", "screens", "tokens", "interactions",
        "responsive", "accessibility", "assets", "unknowns", "fingerprint",
    }
    _exact(value, fields, "canonical-ui-ir")
    require_version(value)
    _nonempty(value, {"ir_id", "fingerprint"}, "canonical-ui-ir")
    _validate_fingerprint(value["fingerprint"], "canonical-ui-ir")
    _string_array(value["evidence_refs"], "canonical-ui-ir evidence_refs", nonempty=True)
    _unique_sorted(value["evidence_refs"], "canonical-ui-ir evidence_refs")
    if not isinstance(value["screens"], list) or not value["screens"]:
        raise ContractError("canonical-ui-ir screens must be non-empty")
    screen_ids: list[str] = []
    for screen in value["screens"]:
        _exact(screen, {"id", "regions", "states"}, "canonical-ui-ir.screen")
        _nonempty(screen, {"id"}, "canonical-ui-ir.screen")
        if not isinstance(screen["regions"], list) or not isinstance(screen["states"], list):
            raise ContractError("canonical-ui-ir screen regions/states must be arrays")
        _string_array(screen["states"], "canonical-ui-ir screen states", nonempty=True)
        _unique_sorted(screen["states"], "canonical-ui-ir screen states")
        region_ids: list[str] = []
        for region in screen["regions"]:
            _exact(region, {"id", "layout", "components"}, "canonical-ui-ir.region")
            _nonempty(region, {"id"}, "canonical-ui-ir.region")
            if not isinstance(region["layout"], dict) or not region["layout"]:
                raise ContractError("canonical-ui-ir region layout must be non-empty")
            if any(key in region["layout"] for key in {"swiftui", "uikit", "compose", "dom"}):
                raise ContractError("canonical-ui-ir must not contain platform bindings")
            if not isinstance(region["components"], list):
                raise ContractError("canonical-ui-ir components must be an array")
            component_ids: list[str] = []
            for component in region["components"]:
                _exact(
                    component,
                    {"id", "type", "variant", "slots", "token_refs", "states", "provenance"},
                    "canonical-ui-ir.component",
                )
                _nonempty(component, {"id", "type", "variant"}, "canonical-ui-ir.component")
                for key in ("slots",):
                    if not isinstance(component[key], dict):
                        raise ContractError(f"canonical-ui-ir component {key} must be an object")
                for key in ("token_refs", "states"):
                    _string_array(component[key], f"canonical-ui-ir component {key}")
                    _unique_sorted(component[key], f"canonical-ui-ir component {key}")
                _validate_provenance(component["provenance"], "canonical-ui-ir.component.provenance")
                component_ids.append(component["id"])
            _unique_sorted(component_ids, "canonical-ui-ir component ids")
            region_ids.append(region["id"])
        _unique_sorted(region_ids, "canonical-ui-ir region ids")
        screen_ids.append(screen["id"])
    _unique_sorted(screen_ids, "canonical-ui-ir screen ids")
    for field in ("tokens", "responsive", "accessibility"):
        if not isinstance(value[field], dict):
            raise ContractError(f"canonical-ui-ir {field} must be an object")
    for field in ("interactions", "assets"):
        if not isinstance(value[field], list):
            raise ContractError(f"canonical-ui-ir {field} must be an array")
    _validate_unknowns(value["unknowns"], "canonical-ui-ir")
    body = {key: value[key] for key in fields - {"ir_id", "fingerprint"}}
    expected = design_fingerprint(body)
    if value["fingerprint"] != expected or value["ir_id"] != f"ir-{expected[-16:]}":
        raise ContractError("canonical-ui-ir identity does not match artifact body")


def validate_design_system_registry(value: dict[str, Any]) -> None:
    fields = {"schema_version", "registry_id", "version", "tokens", "components", "binding_refs", "fingerprint"}
    _exact(value, fields, "design-system-registry")
    require_version(value)
    _nonempty(value, {"registry_id", "version", "fingerprint"}, "design-system-registry")
    _validate_fingerprint(value["fingerprint"], "design-system-registry")
    if not isinstance(value["tokens"], dict) or not isinstance(value["components"], dict):
        raise ContractError("design-system-registry tokens/components must be objects")
    _string_array(value["binding_refs"], "design-system-registry binding_refs")
    _unique_sorted(value["binding_refs"], "design-system-registry binding_refs")
    for component_id, component in value["components"].items():
        if not _is_nonempty(component_id) or not isinstance(component, dict):
            raise ContractError("design-system-registry component is invalid")
        _exact(component, {"variants", "slots", "token_refs", "states", "motion"}, "design-system-registry.component")
        for field in ("variants", "slots", "token_refs", "states"):
            _string_array(component[field], f"design-system-registry component {field}")
            _unique_sorted(component[field], f"design-system-registry component {field}")
        if not isinstance(component["motion"], dict):
            raise ContractError("design-system-registry component motion must be an object")
    body = {key: value[key] for key in fields - {"fingerprint"}}
    if value["fingerprint"] != design_fingerprint(body):
        raise ContractError("design-system-registry fingerprint does not match artifact body")


def validate_design_agent_packet(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "packet_id", "task_scope", "source_fingerprints", "ir_slice",
        "registry_slice", "unknowns", "stale_inputs", "fingerprint",
    }
    _exact(value, fields, "design-agent-packet")
    require_version(value)
    _nonempty(value, {"packet_id", "fingerprint"}, "design-agent-packet")
    _validate_fingerprint(value["fingerprint"], "design-agent-packet")
    _validate_scope(value["task_scope"], "design-agent-packet.task_scope", task_scope=True)
    _string_array(value["source_fingerprints"], "design-agent-packet source_fingerprints", nonempty=True)
    _unique_sorted(value["source_fingerprints"], "design-agent-packet source_fingerprints")
    for fingerprint in value["source_fingerprints"]:
        _validate_fingerprint(fingerprint, "design-agent-packet source")
    for field in ("ir_slice", "registry_slice"):
        if not isinstance(value[field], dict) or not value[field]:
            raise ContractError(f"design-agent-packet {field} must be non-empty")
    _validate_unknowns(value["unknowns"], "design-agent-packet")
    _string_array(value["stale_inputs"], "design-agent-packet stale_inputs")
    _unique_sorted(value["stale_inputs"], "design-agent-packet stale_inputs")
    for fingerprint in value["stale_inputs"]:
        _validate_fingerprint(fingerprint, "design-agent-packet stale input")
    _validate_packet_scope(value["task_scope"], value["ir_slice"])
    body = {key: value[key] for key in fields - {"packet_id", "fingerprint"}}
    expected = design_fingerprint(body)
    if value["fingerprint"] != expected or value["packet_id"] != f"packet-{expected[-16:]}":
        raise ContractError("design-agent-packet identity does not match artifact body")


def validate_ui_validation_report(value: dict[str, Any]) -> None:
    fields = {"schema_version", "report_id", "packet_fingerprint", "environment", "checks", "blockers", "status"}
    _exact(value, fields, "ui-validation-report")
    require_version(value)
    _nonempty(value, {"report_id", "packet_fingerprint"}, "ui-validation-report")
    _validate_fingerprint(value["packet_fingerprint"], "ui-validation-report packet")
    if value["status"] not in {"passed", "partial", "blocked", "failed"}:
        raise ContractError("ui-validation-report status is invalid")
    environment = value["environment"]
    _exact(environment, {"platform", "os_version", "viewport", "locale", "build_fingerprint"}, "ui-validation-report.environment")
    _nonempty(environment, {"platform", "os_version", "locale", "build_fingerprint"}, "ui-validation-report.environment")
    viewport = environment["viewport"]
    if not isinstance(viewport, dict) or set(viewport) != {"width", "height"}:
        raise ContractError("ui-validation-report viewport is invalid")
    if any(isinstance(viewport[key], bool) or not isinstance(viewport[key], (int, float)) or viewport[key] <= 0 for key in ("width", "height")):
        raise ContractError("ui-validation-report viewport dimensions are invalid")
    if not isinstance(value["checks"], list) or not value["checks"]:
        raise ContractError("ui-validation-report checks must be non-empty")
    for check in value["checks"]:
        _exact(check, {"kind", "target_id", "status", "classification", "evidence_refs"}, "ui-validation-report.check")
        _nonempty(check, {"kind", "target_id", "status", "classification"}, "ui-validation-report.check")
        if check["kind"] not in {"geometry", "semantic", "accessibility", "visual", "state"}:
            raise ContractError("ui-validation-report check kind is invalid")
        if check["status"] not in {"passed", "partial", "blocked", "failed"}:
            raise ContractError("ui-validation-report check status is invalid")
        if check["classification"] not in {"none", "source-gap", "binding-gap", "implementation-bug", "environment-noise"}:
            raise ContractError("ui-validation-report classification is invalid")
        if check["status"] == "passed" and check["classification"] != "none":
            raise ContractError("passed ui-validation-report check must use classification none")
        if check["status"] in {"blocked", "failed"} and check["classification"] == "none":
            raise ContractError("blocked/failed ui-validation-report check requires a classification")
        _validate_ui_evidence_refs(check["evidence_refs"])
        _validate_ui_check_evidence(check)
    _string_array(value["blockers"], "ui-validation-report blockers")
    _unique_sorted(value["blockers"], "ui-validation-report blockers")
    expected_check_blockers = sorted({
        f"{check['classification']}:{check['target_id']}"
        for check in value["checks"]
        if check["status"] in {"blocked", "failed"} and check["classification"] != "environment-noise"
    })
    missing = sorted(set(expected_check_blockers) - set(value["blockers"]))
    if missing:
        raise ContractError("ui-validation-report omits check blockers")
    non_check_blockers = [
        blocker for blocker in value["blockers"]
        if blocker.startswith("stale-input:") or blocker.startswith("unknown:")
    ]
    has_failed_check = any(
        check["status"] == "failed" and check["classification"] != "environment-noise"
        for check in value["checks"]
    )
    has_nonpassed_check = any(check["status"] != "passed" for check in value["checks"])
    if value["status"] == "passed" and (value["blockers"] or has_nonpassed_check):
        raise ContractError("passed ui-validation-report requires all checks passed and no blockers")
    if value["status"] == "failed" and not has_failed_check:
        raise ContractError("failed ui-validation-report requires a non-environment failed check")
    if value["status"] == "blocked" and not value["blockers"]:
        raise ContractError("blocked ui-validation-report requires blockers")
    if value["status"] == "partial" and (value["blockers"] or not has_nonpassed_check or has_failed_check):
        raise ContractError("partial ui-validation-report status is inconsistent")
    if value["status"] in {"passed", "partial"} and non_check_blockers:
        raise ContractError("ui-validation-report cannot ignore upstream blockers")
    identity = {
        "blockers": value["blockers"],
        "checks": value["checks"],
        "environment": value["environment"],
        "packet_fingerprint": value["packet_fingerprint"],
        "status": value["status"],
    }
    if value["report_id"] != f"ui-report-{sha256(identity)[:16]}":
        raise ContractError("ui-validation-report identity does not match artifact body")


def _validate_packet_scope(scope: dict[str, Any], ir_slice: dict[str, Any]) -> None:
    identity = scope["id"]
    if ir_slice.get("id") != identity:
        raise ContractError("design-agent-packet task scope does not match ir_slice")
    components = [
        component.get("id")
        for region in ir_slice.get("regions", [])
        for component in region.get("components", [])
        if isinstance(component, dict)
    ]
    if scope["kind"] == "component" and components != [identity]:
        raise ContractError("design-agent-packet component scope is not an exact single-component slice")
    if scope["kind"] == "region":
        regions = ir_slice.get("regions", [])
        if len(regions) != 1 or regions[0].get("id") != identity:
            raise ContractError("design-agent-packet region scope is not exact")


def _validate_ui_evidence_refs(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ContractError("ui-validation-report evidence_refs must be non-empty")
    identities: list[tuple[str, str]] = []
    for reference in value:
        _exact(reference, {"kind", "sha256", "uri"}, "ui-validation-report.evidence_ref")
        if reference["kind"] not in {"screenshot", "geometry", "semantic-tree", "accessibility-tree", "visual-diff"}:
            raise ContractError("ui-validation-report evidence kind is invalid")
        if not isinstance(reference["sha256"], str) or not _SHA256.fullmatch(reference["sha256"]):
            raise ContractError("ui-validation-report evidence sha256 is invalid")
        if (
            not isinstance(reference["uri"], str)
            or not reference["uri"].startswith("artifact://")
            or len(reference["uri"]) == len("artifact://")
            or ".." in reference["uri"]
            or any(character.isspace() for character in reference["uri"])
        ):
            raise ContractError("ui-validation-report evidence uri is uncontrolled")
        identities.append((reference["kind"], reference["uri"]))
    if identities != sorted(set(identities)):
        raise ContractError("ui-validation-report evidence refs must be sorted and unique")


def _validate_ui_check_evidence(check: dict[str, Any]) -> None:
    allowed = {
        "accessibility": {"accessibility-tree"},
        "geometry": {"geometry"},
        "semantic": {"semantic-tree"},
        "state": {"accessibility-tree", "screenshot", "semantic-tree", "visual-diff"},
        "visual": {"screenshot", "visual-diff"},
    }
    required_for_pass = {
        "accessibility": {"accessibility-tree"},
        "geometry": {"geometry"},
        "semantic": {"semantic-tree"},
        "state": {"screenshot", "semantic-tree"},
        "visual": {"screenshot", "visual-diff"},
    }
    kinds = {reference["kind"] for reference in check["evidence_refs"]}
    if not kinds <= allowed[check["kind"]]:
        raise ContractError("ui-validation-report check contains mismatched evidence kinds")
    if check["status"] == "passed" and not required_for_pass[check["kind"]] <= kinds:
        raise ContractError("passed ui-validation-report check lacks required evidence kinds")


def _validate_provenance(value: Any, label: str) -> None:
    _exact(value, {"kind", "evidence_ref", "confidence"}, label)
    if value["kind"] not in _PROVENANCE_KINDS:
        raise ContractError(f"{label} kind is invalid")
    if value["kind"] == "unknown" and value["evidence_ref"] is not None:
        raise ContractError(f"{label} unknown cannot reference evidence")
    if value["kind"] != "unknown" and not _is_nonempty(value["evidence_ref"]):
        raise ContractError(f"{label} requires evidence_ref")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ContractError(f"{label} confidence is invalid")
    if value["kind"] == "inference" and confidence >= 1:
        raise ContractError(f"{label} inference cannot have source certainty")


def _validate_unknowns(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ContractError(f"{label} unknowns must be an array")
    ids: list[str] = []
    for item in value:
        _exact(item, {"id", "reason", "blocking"}, f"{label}.unknown")
        _nonempty(item, {"id", "reason"}, f"{label}.unknown")
        if not isinstance(item["blocking"], bool):
            raise ContractError(f"{label} unknown blocking must be boolean")
        ids.append(item["id"])
    _unique_sorted(ids, f"{label} unknown ids")


def _validate_scope(value: Any, label: str, *, task_scope: bool = False) -> None:
    fields = {"kind", "id"} if task_scope else {"page_id", "node_ids"}
    _exact(value, fields, label)
    if task_scope:
        if value["kind"] not in {"screen", "region", "component"} or not _is_nonempty(value["id"]):
            raise ContractError(f"{label} is invalid")
        return
    if value["page_id"] is not None and not _is_nonempty(value["page_id"]):
        raise ContractError(f"{label} page_id is invalid")
    _string_array(value["node_ids"], f"{label} node_ids", nonempty=True)
    _unique_sorted(value["node_ids"], f"{label} node_ids")


def _validate_fingerprint(value: Any, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"design-v1:[0-9a-f]{64}", value):
        raise ContractError(f"{label} fingerprint is invalid")


def _exact(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    require_fields(value, fields, label)
    unknown = sorted(value.keys() - fields)
    if unknown:
        raise ContractError(f"{label} has unknown fields: {', '.join(unknown)}")


def _nonempty(value: dict[str, Any], fields: set[str], label: str) -> None:
    for field in fields:
        if not _is_nonempty(value.get(field)):
            raise ContractError(f"{label} {field} must be a non-empty string")


def _is_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_array(value: Any, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or (nonempty and not value) or any(not _is_nonempty(item) for item in value):
        raise ContractError(f"{label} must be {'non-empty ' if nonempty else ''}strings")


def _unique_sorted(values: list[str], label: str) -> None:
    if values != sorted(set(values)):
        raise ContractError(f"{label} must be sorted and unique")
