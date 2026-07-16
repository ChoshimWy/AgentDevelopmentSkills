"""Runtime validation for Phase 1 artifacts.

JSON Schema files document the external contract. These validators enforce the
cross-reference and enum rules that are important to the workflow runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .canonical_json import sha256
from .models import ContractError, NodeStatus, require_fields, require_version
from .design.contracts import (
    validate_canonical_ui_ir,
    validate_design_agent_packet,
    validate_design_evidence,
    validate_design_source_request,
    validate_design_system_registry,
    validate_ui_validation_report,
)


LEGAL_NODE_TRANSITIONS = {
    "pending": {"ready", "blocked", "skipped", "cancelled"},
    "ready": {"running", "blocked", "cancelled", "stale"},
    "running": {"passed", "failed", "blocked", "skipped", "cancelled"},
    "passed": {"stale"}, "failed": {"stale"}, "blocked": {"ready", "stale"},
    "skipped": {"stale"}, "cancelled": {"stale"}, "stale": {"ready"},
}

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY_PATCH = re.compile(r"^repository-patch:[0-9a-f]{64}$")
_SESSION_SOURCE = re.compile(r"^session-source:[0-9a-f]{64}$")


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


def validate_worktree_session_context(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "session_id", "project_id", "selected_platforms", "created_at",
        "repositories", "dependencies", "source_identity", "platform_contexts",
        "capability_closure", "verification", "review", "lifecycle",
    }
    _exact_object(value, fields, "worktree-session-context")
    require_version(value)
    for field in ("session_id", "project_id"):
        if not isinstance(value[field], str) or not _SESSION_ID.fullmatch(value[field]):
            raise ContractError(f"worktree-session-context {field} is invalid")
    if not isinstance(value["created_at"], str) or not value["created_at"]:
        raise ContractError("worktree-session-context created_at is invalid")
    platforms = value["selected_platforms"]
    if (
        not isinstance(platforms, list)
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item) for item in platforms)
        or platforms != sorted(set(platforms))
    ):
        raise ContractError("worktree-session-context selected_platforms must be sorted unique platform ids")
    contexts = value["platform_contexts"]
    if not isinstance(contexts, dict) or set(contexts) != set(platforms):
        raise ContractError("worktree-session-context requires one provider closure per selected platform")
    for platform, context in contexts.items():
        _exact_object(context, {"provider_id", "bindings", "context"}, f"worktree-session-context.platform_contexts.{platform}")
        if not isinstance(context["provider_id"], str) or not context["provider_id"]:
            raise ContractError("worktree-session-context platform provider_id is invalid")
        if not isinstance(context["context"], dict) or not isinstance(context["bindings"], dict) or not context["bindings"]:
            raise ContractError("worktree-session-context platform binding closure is invalid")
        for capability, binding in context["bindings"].items():
            if not isinstance(capability, str) or not capability or not _valid_session_binding(binding):
                raise ContractError("worktree-session-context platform binding closure is invalid")
    closure = value["capability_closure"]
    if not isinstance(closure, dict):
        raise ContractError("worktree-session-context capability_closure is invalid")
    for capability, provider in closure.items():
        _exact_object(provider, {"provider_id", "binding"}, "worktree-session-context.capability_closure.provider")
        if (
            not isinstance(capability, str)
            or not capability
            or not isinstance(provider["provider_id"], str)
            or not provider["provider_id"]
            or not _valid_session_binding(provider["binding"])
        ):
            raise ContractError("worktree-session-context capability_closure entry is invalid")

    repositories = value["repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise ContractError("worktree-session-context repositories must be a non-empty array")
    repository_ids: list[str] = []
    worktree_paths: list[str] = []
    common_dirs: list[str] = []
    primary_count = 0
    for repository in repositories:
        _validate_session_repository(repository)
        repository_ids.append(repository["repository_id"])
        worktree_paths.append(repository["worktree_path"])
        common_dirs.append(repository["git_common_dir"])
        primary_count += repository["role"] == "primary"
    if repository_ids != sorted(repository_ids) or len(repository_ids) != len(set(repository_ids)):
        raise ContractError("worktree-session-context repositories must be sorted by unique repository_id")
    if len(worktree_paths) != len(set(worktree_paths)) or len(common_dirs) != len(set(common_dirs)):
        raise ContractError("worktree-session-context repository paths must be unique")
    if primary_count != 1:
        raise ContractError("worktree-session-context requires exactly one primary repository")

    dependencies = value["dependencies"]
    if not isinstance(dependencies, list):
        raise ContractError("worktree-session-context dependencies must be an array")
    dependency_ids: list[str] = []
    for dependency in dependencies:
        _exact_object(
            dependency,
            {"session_id", "dependency_type", "required_source_identity"},
            "worktree-session-context.dependency",
        )
        if (
            not isinstance(dependency["session_id"], str)
            or not _SESSION_ID.fullmatch(dependency["session_id"])
            or dependency["session_id"] == value["session_id"]
            or dependency["dependency_type"] != "stacked"
            or not isinstance(dependency["required_source_identity"], str)
            or not _SESSION_SOURCE.fullmatch(dependency["required_source_identity"])
        ):
            raise ContractError("worktree-session-context dependency is invalid")
        dependency_ids.append(dependency["session_id"])
    if dependency_ids != sorted(dependency_ids) or len(dependency_ids) != len(set(dependency_ids)):
        raise ContractError("worktree-session-context dependencies must be sorted and unique")

    identity = value["source_identity"]
    _exact_object(identity, {"algorithm", "mode", "value"}, "worktree-session-context.source_identity")
    if identity["algorithm"] != "session-source-v1" or identity["mode"] not in {"working", "committed"}:
        raise ContractError("worktree-session-context source identity metadata is invalid")
    if not isinstance(identity["value"], str) or not _SESSION_SOURCE.fullmatch(identity["value"]):
        raise ContractError("worktree-session-context source identity value is invalid")

    _validate_session_evidence_index(value["verification"], "verification")
    _validate_session_evidence_index(value["review"], "review")
    lifecycle = value["lifecycle"]
    _exact_object(lifecycle, {"state"}, "worktree-session-context.lifecycle")
    if lifecycle["state"] not in {"created", "active", "checkpointed", "gated", "integrated", "closed", "blocked"}:
        raise ContractError("worktree-session-context lifecycle state is invalid")
    committed_states = {"checkpointed", "gated", "integrated", "closed"}
    if lifecycle["state"] in {"created", "active"} and (
        identity["mode"] != "working" or any(item["checkpoint"] is not None for item in repositories)
    ):
        raise ContractError("worktree-session-context editable state requires working source identity")
    if lifecycle["state"] in committed_states or identity["mode"] == "committed":
        if identity["mode"] != "committed" or any(item["checkpoint"] is None for item in repositories):
            raise ContractError("worktree-session-context committed state requires every repository checkpoint")
    if lifecycle["state"] in {"gated", "integrated", "closed"} and (
        value["verification"]["status"] != "passed" or value["review"]["status"] != "passed"
    ):
        raise ContractError("worktree-session-context gated state requires passed verification and review")


def _validate_session_repository(value: Any) -> None:
    fields = {
        "repository_id", "role", "branch", "worktree_path", "git_common_dir",
        "base", "checkpoint", "change_set",
    }
    _exact_object(value, fields, "worktree-session-context.repository")
    if not isinstance(value["repository_id"], str) or not _SESSION_ID.fullmatch(value["repository_id"]):
        raise ContractError("worktree-session-context repository_id is invalid")
    if value["role"] not in {"primary", "dependency"}:
        raise ContractError("worktree-session-context repository role is invalid")
    if value["branch"] is not None and (not isinstance(value["branch"], str) or not value["branch"]):
        raise ContractError("worktree-session-context repository branch is invalid")
    for field in ("worktree_path", "git_common_dir"):
        path = value[field]
        if not isinstance(path, str) or not path or not Path(path).is_absolute():
            raise ContractError(f"worktree-session-context repository {field} must be absolute")
    base = value["base"]
    _exact_object(base, {"ref", "commit", "source", "dirty_worktree_inherited"}, "worktree-session-context.repository.base")
    if (
        not isinstance(base["ref"], str)
        or not base["ref"]
        or not isinstance(base["commit"], str)
        or not _GIT_OID.fullmatch(base["commit"])
        or base["source"] not in {"explicit", "integration-checkpoint", "stacked-checkpoint", "clean-head"}
        or base["dirty_worktree_inherited"] is not False
    ):
        raise ContractError("worktree-session-context repository base is invalid")
    checkpoint = value["checkpoint"]
    if checkpoint is not None:
        _exact_object(checkpoint, {"commit", "tree"}, "worktree-session-context.repository.checkpoint")
        if any(not isinstance(checkpoint[field], str) or not _GIT_OID.fullmatch(checkpoint[field]) for field in ("commit", "tree")):
            raise ContractError("worktree-session-context repository checkpoint is invalid")
    change_set = value["change_set"]
    _exact_object(
        change_set,
        {"algorithm", "patch_hash", "changed_files", "untracked_files"},
        "worktree-session-context.repository.change_set",
    )
    if change_set["algorithm"] != "repository-patch-v1":
        raise ContractError("worktree-session-context repository patch algorithm is invalid")
    if not isinstance(change_set["patch_hash"], str) or not _REPOSITORY_PATCH.fullmatch(change_set["patch_hash"]):
        raise ContractError("worktree-session-context repository patch hash is invalid")
    for field in ("changed_files", "untracked_files"):
        paths = change_set[field]
        if (
            not isinstance(paths, list)
            or paths != sorted(set(paths))
            or any(not isinstance(path, str) or not _safe_session_relative_path(path) for path in paths)
        ):
            raise ContractError(f"worktree-session-context repository {field} is invalid")


def _validate_session_evidence_index(value: Any, label: str) -> None:
    _exact_object(value, {"adapter_result_refs", "status"}, f"worktree-session-context.{label}")
    if value["status"] not in {"pending", "passed", "stale", "blocked"} or not isinstance(value["adapter_result_refs"], list):
        raise ContractError(f"worktree-session-context {label} is invalid")
    identities: list[tuple[str, str]] = []
    for reference in value["adapter_result_refs"]:
        fields = {
            "attempt_id", "request_id", "invocation_id", "plan_fingerprint", "node_id",
            "capability", "provider", "binding", "artifact_hashes",
        }
        _exact_object(reference, fields, f"worktree-session-context.{label}.adapter_result_ref")
        for field in fields - {"binding", "artifact_hashes"}:
            if not isinstance(reference[field], str) or not reference[field]:
                raise ContractError(f"worktree-session-context {label} adapter reference is invalid")
        binding = reference["binding"]
        if not _valid_session_binding(binding):
            raise ContractError(f"worktree-session-context {label} binding is invalid")
        artifacts = reference["artifact_hashes"]
        if not isinstance(artifacts, list) or not artifacts:
            raise ContractError(f"worktree-session-context {label} adapter reference requires artifacts")
        artifact_ids: list[str] = []
        for artifact in artifacts:
            _exact_object(artifact, {"artifact_id", "sha256", "uri"}, f"worktree-session-context.{label}.artifact")
            if (
                any(not isinstance(artifact[field], str) or not artifact[field] for field in ("artifact_id", "uri"))
                or not isinstance(artifact["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
            ):
                raise ContractError(f"worktree-session-context {label} artifact is invalid")
            artifact_ids.append(artifact["artifact_id"])
        if artifact_ids != sorted(artifact_ids) or len(artifact_ids) != len(set(artifact_ids)):
            raise ContractError(f"worktree-session-context {label} artifact ids must be sorted and unique")
        identities.append((reference["attempt_id"], reference["invocation_id"]))
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        raise ContractError(f"worktree-session-context {label} adapter refs must be sorted and unique")


def validate_worktree_session_gate(value: dict[str, Any]) -> None:
    fields = {
        "schema_version", "session_id", "source_identity", "checkpoint_commits",
        "verification_refs", "review_refs", "status", "diagnostics",
    }
    _exact_object(value, fields, "worktree-session-gate")
    require_version(value)
    if not isinstance(value["session_id"], str) or not _SESSION_ID.fullmatch(value["session_id"]):
        raise ContractError("worktree-session-gate session_id is invalid")
    if not isinstance(value["source_identity"], str) or not _SESSION_SOURCE.fullmatch(value["source_identity"]):
        raise ContractError("worktree-session-gate source_identity is invalid")
    commits = value["checkpoint_commits"]
    if (
        not isinstance(commits, dict)
        or not commits
        or list(commits) != sorted(commits)
        or any(
            not isinstance(key, str)
            or not _SESSION_ID.fullmatch(key)
            or not isinstance(item, str)
            or not _GIT_OID.fullmatch(item)
            for key, item in commits.items()
        )
    ):
        raise ContractError("worktree-session-gate checkpoint_commits are invalid")
    for field in ("verification_refs", "review_refs"):
        refs = value[field]
        if not isinstance(refs, list) or refs != sorted(set(refs)) or any(not isinstance(item, str) or not item for item in refs):
            raise ContractError(f"worktree-session-gate {field} is invalid")
    if value["status"] not in {"passed", "blocked"} or not isinstance(value["diagnostics"], list):
        raise ContractError("worktree-session-gate status or diagnostics are invalid")
    for diagnostic in value["diagnostics"]:
        _exact_object(diagnostic, {"code", "message"}, "worktree-session-gate.diagnostic")
        if any(not isinstance(diagnostic[field], str) or not diagnostic[field] for field in ("code", "message")):
            raise ContractError("worktree-session-gate diagnostic is invalid")
    if value["status"] == "passed" and (value["diagnostics"] or not value["verification_refs"] or not value["review_refs"]):
        raise ContractError("worktree-session-gate passed result requires evidence and no diagnostics")
    if value["status"] == "blocked" and not value["diagnostics"]:
        raise ContractError("worktree-session-gate blocked result requires diagnostics")


def validate_worktree_session_operation_result(value: dict[str, Any]) -> None:
    _exact_object(value, {"schema_version", "operation", "notice", "session"}, "worktree-session-operation-result")
    require_version(value)
    if value["operation"] not in {"create", "checkpoint"} or not isinstance(value["notice"], dict):
        raise ContractError("worktree-session-operation-result operation or notice is invalid")
    validate_worktree_session_context(value["session"])


def validate_worktree_session_list(value: dict[str, Any]) -> None:
    _exact_object(value, {"schema_version", "sessions"}, "worktree-session-list")
    require_version(value)
    if not isinstance(value["sessions"], list):
        raise ContractError("worktree-session-list sessions must be an array")
    for session in value["sessions"]:
        validate_worktree_session_context(session)
    session_ids = [session["session_id"] for session in value["sessions"]]
    if session_ids != sorted(session_ids) or len(session_ids) != len(set(session_ids)):
        raise ContractError("worktree-session-list sessions must be sorted and unique")


def _exact_object(value: Any, fields: set[str], kind: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(f"{kind} fields are invalid")


def _safe_session_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and "\\" not in value and all(part not in {"", ".", ".."} for part in path.parts)


def _valid_session_binding(binding: Any) -> bool:
    return bool(
        isinstance(binding, dict)
        and set(binding) in ({"kind", "name"}, {"kind", "name", "mode"})
        and binding.get("kind") in {"skill", "agent", "script", "tool"}
        and isinstance(binding.get("name"), str)
        and binding["name"]
        and ("mode" not in binding or (isinstance(binding["mode"], str) and binding["mode"]))
    )


def validate_manifest(value: dict[str, Any]) -> None:
    _base(value, {"id", "kind", "detection", "capabilities"}, "plugin-manifest")
    if value["kind"] not in {"core", "platform", "stack", "discipline", "adapter", "runtime-config"}:
        raise ContractError("plugin-manifest kind is invalid")
    implementation_status = value.get("implementation_status")
    if implementation_status not in {None, "implemented", "bootstrap-only"}:
        raise ContractError("plugin-manifest implementation_status is invalid")
    if implementation_status is not None and value["kind"] != "platform":
        raise ContractError("plugin-manifest implementation_status is only valid for platform packages")
    detection = value["detection"]
    require_fields(detection, {"strong", "medium", "weak"}, "plugin-manifest.detection")
    ids = [item.get("id") for item in value["capabilities"]]
    if None in ids or len(ids) != len(set(ids)):
        raise ContractError("plugin-manifest capability ids must be present and unique")
    role = value.get("role", "builtin")
    if role not in {"builtin", "bootstrap", "provider"}:
        raise ContractError("plugin-manifest role is invalid")
    bindings = value.get("bindings", {})
    if not isinstance(bindings, dict):
        raise ContractError("plugin-manifest bindings must be an object")
    installation = value.get("installation")
    if implementation_status == "bootstrap-only" and (
        role != "bootstrap" or value["capabilities"] or bindings or installation is not None
    ):
        raise ContractError(
            "bootstrap-only platform must use a bootstrap role without capabilities, bindings, or installation"
        )
    if implementation_status == "implemented" and installation is None:
        raise ContractError("implemented platform must provide an installation contract")
    if installation is not None:
        if not isinstance(installation, dict):
            raise ContractError("plugin-manifest installation must be an object")
        require_fields(
            installation,
            {"asset_roots", "instruction_fragments", "skill_roots"},
            "plugin-manifest.installation",
        )
        for field in ("asset_roots", "skill_roots"):
            items = installation[field]
            if (
                not isinstance(items, list)
                or any(not isinstance(item, str) or not item for item in items)
                or len(items) != len(set(items))
            ):
                raise ContractError(f"plugin-manifest.installation {field} must be unique strings")
        fragments = installation["instruction_fragments"]
        if not isinstance(fragments, list):
            raise ContractError("plugin-manifest.installation instruction_fragments must be an array")
        fragment_ids = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise ContractError("plugin-manifest.installation instruction fragment must be an object")
            require_fields(
                fragment,
                {"id", "path", "scope", "order", "merge_strategy"},
                "plugin-manifest.installation.instruction-fragment",
            )
            if (
                any(not isinstance(fragment[field], str) or not fragment[field] for field in ("id", "path", "scope"))
                or not isinstance(fragment["order"], int)
                or fragment["merge_strategy"] not in {"append", "locked"}
            ):
                raise ContractError("plugin-manifest.installation instruction fragment is invalid")
            fragment_ids.append(fragment["id"])
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ContractError("plugin-manifest.installation instruction fragment ids must be unique")
        provider_manifest = installation.get("provider_manifest")
        if provider_manifest is not None and (not isinstance(provider_manifest, str) or not provider_manifest):
            raise ContractError("plugin-manifest.installation provider_manifest is invalid")
        version = value.get("version")
        if not isinstance(version, str) or not version:
            raise ContractError("installable plugin-manifest version is required")
    if role == "bootstrap":
        contract = value.get("provider_contract")
        if not isinstance(contract, dict):
            raise ContractError("bootstrap manifest provider_contract is required")
        require_fields(
            contract,
            {
                "package_id", "package_compatibility", "required_capabilities",
                "optional_capabilities", "advisory_capabilities",
                "allowed_permission_profiles", "allowed_side_effects",
                "capability_permissions", "capability_side_effects",
            },
            "plugin-manifest.provider_contract",
        )
        for field in (
            "required_capabilities", "optional_capabilities", "advisory_capabilities",
            "allowed_permission_profiles", "allowed_side_effects",
        ):
            items = contract[field]
            if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
                raise ContractError(f"plugin-manifest.provider_contract {field} must be strings")
            if len(items) != len(set(items)):
                raise ContractError(f"plugin-manifest.provider_contract {field} must be unique")
        capability_groups = [
            set(contract["required_capabilities"]),
            set(contract["optional_capabilities"]),
            set(contract["advisory_capabilities"]),
        ]
        if capability_groups[0] & capability_groups[1] or capability_groups[0] & capability_groups[2] or capability_groups[1] & capability_groups[2]:
            raise ContractError("plugin-manifest.provider_contract capability groups must not overlap")
        declared = set().union(*capability_groups)
        for field in ("capability_permissions", "capability_side_effects"):
            mapping = contract[field]
            if not isinstance(mapping, dict) or set(mapping) != declared:
                raise ContractError(f"plugin-manifest.provider_contract {field} must cover every declared capability")
        if any(not isinstance(item, str) or not item for item in contract["capability_permissions"].values()):
            raise ContractError("plugin-manifest.provider_contract capability_permissions values must be strings")
        if any(
            not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items)
            for items in contract["capability_side_effects"].values()
        ):
            raise ContractError("plugin-manifest.provider_contract capability_side_effects values must be string arrays")
    if role == "provider":
        package = value.get("package")
        if not isinstance(package, dict):
            raise ContractError("provider manifest package metadata is required")
        require_fields(package, {"version", "core_compatibility"}, "plugin-manifest.package")
    package_requires = value.get("package_requires", [])
    if not isinstance(package_requires, list):
        raise ContractError("plugin-manifest package_requires must be an array")
    dependency_ids: list[str] = []
    for dependency in package_requires:
        if not isinstance(dependency, dict):
            raise ContractError("plugin-manifest package dependency must be an object")
        require_fields(
            dependency,
            {"id", "version", "requirement", "required_capabilities"},
            "plugin-manifest.package-dependency",
        )
        if (
            not isinstance(dependency["id"], str)
            or not dependency["id"]
            or not isinstance(dependency["version"], str)
            or not dependency["version"]
            or dependency["requirement"] not in {"required", "optional"}
            or not isinstance(dependency["required_capabilities"], list)
            or not dependency["required_capabilities"]
            or any(not isinstance(item, str) or not item for item in dependency["required_capabilities"])
            or len(dependency["required_capabilities"]) != len(set(dependency["required_capabilities"]))
        ):
            raise ContractError("plugin-manifest package dependency is invalid")
        dependency_ids.append(dependency["id"])
    if len(dependency_ids) != len(set(dependency_ids)):
        raise ContractError("plugin-manifest package dependency ids must be unique")


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
    if "workflow" in value:
        require_fields(value["workflow"], {"roles", "checkpoints", "independent_review"}, "workflow-plan.workflow")
    bootstrap_required = value.get("bootstrap_required", [])
    if not isinstance(bootstrap_required, list):
        raise ContractError("workflow-plan bootstrap_required must be an array")
    bootstrap_platforms: list[str] = []
    for requirement in bootstrap_required:
        require_fields(
            requirement,
            {"platform", "provider", "package_compatibility", "required_capabilities"},
            "workflow-plan.bootstrap-required",
        )
        if (
            any(
                not isinstance(requirement[field], str) or not requirement[field]
                for field in ("platform", "provider", "package_compatibility")
            )
            or not isinstance(requirement["required_capabilities"], list)
            or not requirement["required_capabilities"]
            or any(not isinstance(item, str) or not item for item in requirement["required_capabilities"])
            or len(requirement["required_capabilities"]) != len(set(requirement["required_capabilities"]))
        ):
            raise ContractError("workflow-plan bootstrap_required entry is invalid")
        bootstrap_platforms.append(requirement["platform"])
    if len(bootstrap_platforms) != len(set(bootstrap_platforms)):
        raise ContractError("workflow-plan bootstrap_required platforms must be unique")
    if bootstrap_required and value["status"] != "blocked":
        raise ContractError("workflow-plan bootstrap_required must block execution")
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
        {"run_id", "plan_fingerprint", "node_attempts", "resource_events", "approval_records", "final_status"},
        "run-ledger",
    )
    for attempt in value["node_attempts"]:
        validate_node_attempt(attempt)
    for event in value["resource_events"]:
        validate_resource_event(event)
    for record in value["approval_records"]:
        validate_approval_record(record)
    attempt_ids = [attempt["attempt_id"] for attempt in value["node_attempts"]]
    if len(attempt_ids) != len(set(attempt_ids)):
        raise ContractError("run-ledger attempt ids must be globally unique")
    attempt_keys = [(attempt["node_id"], attempt["attempt_number"]) for attempt in value["node_attempts"]]
    if len(attempt_keys) != len(set(attempt_keys)):
        raise ContractError("run-ledger attempt numbers must be unique per node")
    attempts = set(attempt_ids)
    if any(event["attempt_id"] not in attempts for event in value["resource_events"]):
        raise ContractError("resource-event references unknown attempt")
    if any(record["attempt_id"] not in attempts for record in value["approval_records"]):
        raise ContractError("approval-record references unknown attempt")
    attempts_by_id = {attempt["attempt_id"]: attempt for attempt in value["node_attempts"]}
    artifacts = value.get("artifact_hashes", [])
    outcomes = value.get("adapter_outcomes", [])
    evidence = value.get("evidence", [])
    if not all(isinstance(items, list) for items in (artifacts, outcomes, evidence)):
        raise ContractError("run-ledger adapter collections must be arrays")
    artifact_keys: set[tuple[str, str]] = set()
    artifact_kinds = {
        "structured-report", "test-report", "review-report", "delivery-report",
        "diagnostics", "raw-log", "other",
    }
    for artifact in artifacts:
        _validate_ledger_object(
            artifact, {"attempt_id", "node_id", "artifact_id", "kind", "sha256", "uri"},
            "run-ledger.artifact-hash",
        )
        _validate_attempt_node_reference(artifact, attempts_by_id, "artifact-hash")
        if not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
            raise ContractError("run-ledger artifact-hash sha256 is invalid")
        if artifact["kind"] not in artifact_kinds:
            raise ContractError("run-ledger artifact-hash kind is invalid")
        key = (artifact["attempt_id"], artifact["artifact_id"])
        if key in artifact_keys:
            raise ContractError("run-ledger artifact ids must be unique per attempt")
        artifact_keys.add(key)
    outcome_attempts: set[str] = set()
    outcome_providers: dict[str, str] = {}
    outcome_statuses: dict[str, str] = {}
    request_ids: set[str] = set()
    invocation_ids: set[str] = set()
    for outcome in outcomes:
        _validate_ledger_object(
            outcome,
            {"attempt_id", "node_id", "provider", "request_id", "invocation_id", "status", "failure_attribution", "cleanup"},
            "run-ledger.adapter-outcome",
        )
        _validate_attempt_node_reference(outcome, attempts_by_id, "adapter-outcome")
        if outcome["status"] not in {"completed", "partial", "blocked", "failed"}:
            raise ContractError("run-ledger adapter-outcome status is invalid")
        if outcome["attempt_id"] in outcome_attempts:
            raise ContractError("run-ledger allows only one adapter-outcome per attempt")
        outcome_attempts.add(outcome["attempt_id"])
        outcome_providers[outcome["attempt_id"]] = outcome["provider"]
        outcome_statuses[outcome["attempt_id"]] = outcome["status"]
        if outcome["request_id"] in request_ids:
            raise ContractError("run-ledger adapter request ids must be unique per attempt consumption")
        request_ids.add(outcome["request_id"])
        if outcome["invocation_id"] in invocation_ids:
            raise ContractError("run-ledger adapter invocation ids must be unique per attempt consumption")
        invocation_ids.add(outcome["invocation_id"])
        attempt_status = attempts_by_id[outcome["attempt_id"]]["status"]
        if outcome["status"] == "partial":
            if attempt_status not in {"blocked", "skipped"}:
                raise ContractError("run-ledger adapter-outcome status conflicts with node attempt")
        else:
            expected_attempt_status = {
                "completed": "passed", "blocked": "blocked", "failed": "failed",
            }[outcome["status"]]
            if attempt_status != expected_attempt_status:
                raise ContractError("run-ledger adapter-outcome status conflicts with node attempt")
        attribution = outcome["failure_attribution"]
        if not isinstance(attribution, dict) or set(attribution) != {"category", "summary"}:
            raise ContractError("run-ledger adapter-outcome failure attribution is invalid")
        if attribution["category"] not in {"none", "code", "environment", "provider", "contract"}:
            raise ContractError("run-ledger adapter-outcome failure category is invalid")
        if not isinstance(attribution["summary"], str) or not attribution["summary"]:
            raise ContractError("run-ledger adapter-outcome failure summary is invalid")
        if not isinstance(outcome["cleanup"], list):
            raise ContractError("run-ledger adapter-outcome cleanup is invalid")
        for cleanup in outcome["cleanup"]:
            if (
                not isinstance(cleanup, dict)
                or set(cleanup) != {"resource", "status", "detail"}
                or cleanup.get("status") not in {"not-required", "completed", "failed"}
                or any(not isinstance(cleanup.get(field), str) or not cleanup[field] for field in cleanup)
            ):
                raise ContractError("run-ledger adapter-outcome cleanup entry is invalid")
        if outcome["status"] in {"blocked", "failed"} and attribution["category"] == "none":
            raise ContractError("run-ledger blocked or failed outcome requires failure attribution")
        if any(item["status"] == "failed" for item in outcome["cleanup"]) and outcome["status"] not in {"blocked", "failed"}:
            raise ContractError("run-ledger failed cleanup must block or fail the outcome")
    evidence_statuses: dict[str, set[str]] = {}
    for item in evidence:
        _validate_ledger_object(
            item,
            {"attempt_id", "node_id", "provider", "kind", "status", "summary", "data", "artifact_ids"},
            "run-ledger.evidence",
        )
        _validate_attempt_node_reference(item, attempts_by_id, "adapter-evidence")
        if item["attempt_id"] not in outcome_attempts:
            raise ContractError("run-ledger adapter-evidence references attempt without adapter-outcome")
        if item["provider"] != outcome_providers[item["attempt_id"]]:
            raise ContractError("run-ledger adapter-evidence provider does not match adapter-outcome")
        if not isinstance(item["data"], dict) or not item["data"] or not isinstance(item["artifact_ids"], list):
            raise ContractError("run-ledger adapter-evidence payload is invalid")
        if item["kind"] not in {"validation", "review", "delivery", "diagnostic"}:
            raise ContractError("run-ledger adapter-evidence kind is invalid")
        if item["status"] not in {"passed", "completed", "partial", "blocked", "failed"}:
            raise ContractError("run-ledger adapter-evidence status is invalid")
        if (
            any(not isinstance(artifact_id, str) or not artifact_id for artifact_id in item["artifact_ids"])
            or len(item["artifact_ids"]) != len(set(item["artifact_ids"]))
        ):
            raise ContractError("run-ledger adapter-evidence artifact ids are invalid")
        evidence_statuses.setdefault(item["attempt_id"], set()).add(item["status"])
        unknown_artifacts = [
            artifact_id for artifact_id in item["artifact_ids"]
            if (item["attempt_id"], artifact_id) not in artifact_keys
        ]
        if unknown_artifacts:
            raise ContractError("run-ledger adapter-evidence references unknown artifact")
    for attempt_id, outcome_status in outcome_statuses.items():
        statuses = evidence_statuses.get(attempt_id, set())
        if outcome_status == "completed" and statuses - {"passed", "completed"}:
            raise ContractError("run-ledger completed outcome conflicts with evidence status")
        if outcome_status == "partial" and statuses - {"passed", "completed", "partial"}:
            raise ContractError("run-ledger partial outcome conflicts with evidence status")
        if outcome_status == "blocked" and "failed" in statuses:
            raise ContractError("run-ledger blocked outcome conflicts with failed evidence")
        if outcome_status == "failed" and "failed" not in statuses:
            raise ContractError("run-ledger failed outcome requires failed evidence")
        if outcome_status == "partial" and attempts_by_id[attempt_id]["status"] == "skipped":
            has_validation_gap = any(
                item["attempt_id"] == attempt_id
                and item["kind"] == "validation"
                and item["status"] == "partial"
                and isinstance(item["data"].get("suggested_validation"), str)
                and bool(item["data"]["suggested_validation"])
                for item in evidence
            )
            if not has_validation_gap:
                raise ContractError("run-ledger skipped partial outcome requires validation gap evidence")
    sequences = [event["sequence"] for event in value["resource_events"]]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise ContractError("resource-event sequences must be increasing and unique")
    by_node: dict[str, list[int]] = {}
    for attempt in value["node_attempts"]:
        by_node.setdefault(attempt["node_id"], []).append(attempt["attempt_number"])
    if any(numbers != sorted(numbers) or len(numbers) != len(set(numbers)) for numbers in by_node.values()):
        raise ContractError("node attempt numbers must be strictly monotonic")


def _validate_ledger_object(value: Any, fields: set[str], kind: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(f"{kind} fields are invalid")
    for field in fields - {"failure_attribution", "cleanup", "data", "artifact_ids"}:
        if not isinstance(value[field], str) or not value[field]:
            raise ContractError(f"{kind} {field} is invalid")


def _validate_attempt_node_reference(
    value: dict[str, Any], attempts_by_id: dict[str, dict[str, Any]], kind: str,
) -> None:
    attempt = attempts_by_id.get(value["attempt_id"])
    if attempt is None or attempt["node_id"] != value["node_id"]:
        raise ContractError(f"{kind} references unknown attempt or mismatched node")


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


def _install_version_satisfies(version: str, expression: str) -> bool:
    actual = tuple(int(part) for part in version.split("."))
    for token in expression.split():
        match = re.fullmatch(r"(>=|<=|>|<|==)(\d+)\.(\d+)\.(\d+)", token)
        if match is None:
            return False
        operator, *parts = match.groups()
        expected = tuple(int(part) for part in parts)
        if not {
            ">=": actual >= expected,
            "<=": actual <= expected,
            ">": actual > expected,
            "<": actual < expected,
            "==": actual == expected,
        }[operator]:
            return False
    return True


def validate_install_plan(value: dict[str, Any]) -> None:
    _base(
        value,
        {
            "manager", "core_version", "selected_platforms", "packages", "bindings",
            "permission_profiles", "side_effects", "instructions", "skills",
            "managed_roots", "status", "fingerprint",
        },
        "install-plan",
    )
    if value["manager"] != "agent-development-skills":
        raise ContractError("install-plan manager is invalid")
    if value["managed_roots"] != ["AGENTS.md", "skills", ".agent-skills"]:
        raise ContractError("install-plan managed roots are invalid")
    if value["status"] not in {"planned", "installed"}:
        raise ContractError("install-plan status is invalid")
    lock_schema_version = value.get("lock_schema_version")
    if lock_schema_version not in {None, "2.0"}:
        raise ContractError("install-plan lock_schema_version is unsupported")
    is_lock_v2 = lock_schema_version == "2.0"
    if is_lock_v2:
        required_v2 = {
            "asset_summary", "assets", "capability_providers", "resolved_dependencies",
            "selected_disciplines", "selected_packages", "selected_runtime_configs",
        }
        missing_v2 = sorted(required_v2 - set(value))
        if missing_v2:
            raise ContractError(f"install-plan lock v2 metadata is incomplete: {', '.join(missing_v2)}")
    for field in ("selected_platforms", "permission_profiles", "side_effects"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
            raise ContractError(f"install-plan {field} must be strings")
        if len(items) != len(set(items)):
            raise ContractError(f"install-plan {field} must be unique")
    selected_disciplines = value.get("selected_disciplines", [])
    if (
        not isinstance(selected_disciplines, list)
        or any(not isinstance(item, str) or not item for item in selected_disciplines)
        or len(selected_disciplines) != len(set(selected_disciplines))
    ):
        raise ContractError("install-plan selected_disciplines must be unique strings")
    selected_runtime_configs = value.get("selected_runtime_configs", [])
    if (
        not isinstance(selected_runtime_configs, list)
        or any(not isinstance(item, str) or not item for item in selected_runtime_configs)
        or len(selected_runtime_configs) != len(set(selected_runtime_configs))
    ):
        raise ContractError("install-plan selected_runtime_configs must be unique strings")
    package_ids = [item.get("id") for item in value["packages"] if isinstance(item, dict)]
    skill_names = [item.get("name") for item in value["skills"] if isinstance(item, dict)]
    safe_name = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
    if (
        len(package_ids) != len(value["packages"])
        or any(not isinstance(item, str) or not safe_name.fullmatch(item) for item in package_ids)
        or len(package_ids) != len(set(package_ids))
    ):
        raise ContractError("install-plan package ids are invalid")
    if (
        len(skill_names) != len(value["skills"])
        or any(not isinstance(item, str) or not safe_name.fullmatch(item) for item in skill_names)
        or len(skill_names) != len(set(skill_names))
    ):
        raise ContractError("install-plan skill names are invalid")
    package_set = set(package_ids)
    new_dependency_fields = {
        "selected_packages", "selected_disciplines", "selected_runtime_configs", "resolved_dependencies"
    }
    present_dependency_fields = new_dependency_fields & set(value)
    if present_dependency_fields and present_dependency_fields != new_dependency_fields:
        raise ContractError("install-plan dependency metadata must be complete")
    selected_packages = value.get("selected_packages", [])
    selected_package_ids = [item.get("id") for item in selected_packages if isinstance(item, dict)]
    if present_dependency_fields and not selected_packages:
        raise ContractError("install-plan selected_packages must not be empty")
    package_metadata: dict[str, dict[str, Any]] = {}
    if selected_packages:
        if selected_package_ids != package_ids:
            raise ContractError("install-plan selected_packages must match package order")
        if selected_package_ids[0] != "core":
            raise ContractError("install-plan core package must be first")
        for item in selected_packages:
            require_fields(item, {"id", "kind", "version", "selection_reasons"}, "install-plan.selected-package")
            if (
                item["kind"] not in {"core", "platform", "stack", "discipline", "adapter", "runtime-config"}
                or not isinstance(item["version"], str)
                or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", item["version"])
                or not isinstance(item["selection_reasons"], list)
                or not item["selection_reasons"]
                or any(not isinstance(reason, str) or not reason for reason in item["selection_reasons"])
                or len(item["selection_reasons"]) != len(set(item["selection_reasons"]))
            ):
                raise ContractError("install-plan selected package is invalid")
            package_metadata[item["id"]] = item
            source_sha256 = item.get("source_sha256")
            if is_lock_v2 and source_sha256 is None:
                raise ContractError("install-plan lock v2 selected package source digest is required")
            if source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                raise ContractError("install-plan selected package source digest is invalid")
        if package_metadata["core"]["kind"] != "core" or package_metadata["core"]["selection_reasons"] != ["core"]:
            raise ContractError("install-plan core package metadata is invalid")
        explicit_platforms = {
            item["id"] for item in selected_packages if f"platform:{item['id']}" in item["selection_reasons"]
        }
        explicit_disciplines = {
            item["id"] for item in selected_packages if f"discipline:{item['id']}" in item["selection_reasons"]
        }
        explicit_runtime_configs = {
            item["id"] for item in selected_packages if f"runtime-config:{item['id']}" in item["selection_reasons"]
        }
        if explicit_platforms != set(value["selected_platforms"]):
            raise ContractError("install-plan selected platforms differ from package reasons")
        if explicit_disciplines != set(selected_disciplines):
            raise ContractError("install-plan selected disciplines differ from package reasons")
        if explicit_runtime_configs != set(selected_runtime_configs):
            raise ContractError("install-plan selected runtime configs differ from package reasons")
        if any(package_metadata[item]["kind"] != "platform" for item in explicit_platforms):
            raise ContractError("install-plan selected platform kind is invalid")
        if any(package_metadata[item]["kind"] != "discipline" for item in explicit_disciplines):
            raise ContractError("install-plan selected discipline kind is invalid")
        if any(package_metadata[item]["kind"] != "runtime-config" for item in explicit_runtime_configs):
            raise ContractError("install-plan selected runtime config kind is invalid")

    dependencies = value.get("resolved_dependencies", [])
    if not isinstance(dependencies, list):
        raise ContractError("install-plan resolved_dependencies must be an array")
    dependency_edges: set[tuple[str, str]] = set()
    required_edges: set[tuple[str, str]] = set()
    package_positions = {item: index for index, item in enumerate(package_ids)}
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ContractError("install-plan resolved dependency must be an object")
        require_fields(
            dependency,
            {"from", "to", "requirement", "version", "required_capabilities"},
            "install-plan.resolved-dependency",
        )
        if (
            dependency["from"] not in package_set
            or dependency["to"] not in package_set
            or dependency["from"] == dependency["to"]
            or dependency["requirement"] not in {"required", "optional"}
            or not isinstance(dependency["version"], str)
            or not re.fullmatch(
                r"(?:>=|<=|>|<|==)(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
                r"(?: (?:>=|<=|>|<|==)(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))*",
                dependency["version"],
            )
            or not isinstance(dependency["required_capabilities"], list)
            or not dependency["required_capabilities"]
            or any(not isinstance(item, str) or not item for item in dependency["required_capabilities"])
            or len(dependency["required_capabilities"]) != len(set(dependency["required_capabilities"]))
        ):
            raise ContractError("install-plan resolved dependency is invalid")
        edge = (dependency["from"], dependency["to"])
        if edge in dependency_edges:
            raise ContractError("install-plan resolved dependency edges must be unique")
        dependency_edges.add(edge)
        if package_positions[dependency["to"]] >= package_positions[dependency["from"]]:
            raise ContractError("install-plan package order violates dependency topology")
        if not _install_version_satisfies(package_metadata[dependency["to"]]["version"], dependency["version"]):
            raise ContractError("install-plan dependency version is not satisfied")
        if dependency["requirement"] == "required":
            required_edges.add(edge)
    if selected_packages:
        for item in selected_packages[1:]:
            allowed = {
                f"platform:{item['id']}", f"discipline:{item['id']}", f"runtime-config:{item['id']}"
            }
            for consumer, provider in required_edges:
                if provider == item["id"]:
                    allowed.add(f"dependency:{consumer}")
            if any(reason not in allowed for reason in item["selection_reasons"]):
                raise ContractError("install-plan package selection reason is invalid")
        for consumer, provider in required_edges:
            if f"dependency:{consumer}" not in package_metadata[provider]["selection_reasons"]:
                raise ContractError("install-plan required dependency selection reason is missing")
    for package in value["packages"]:
        require_fields(
            package,
            {
                "directories", "file_count", "files", "files_sha256", "id",
                "manifest_sha256", "provider_manifest_sha256", "root_mode",
            },
            "install-plan.package",
        )
        _validate_install_tree_record(package, "files_sha256", "install-plan.package")
        if not re.fullmatch(r"[0-9a-f]{64}", package["manifest_sha256"]):
            raise ContractError("install-plan package manifest digest is invalid")
        provider_digest = package["provider_manifest_sha256"]
        if provider_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", provider_digest):
            raise ContractError("install-plan package provider digest is invalid")
    package_records = {item["id"]: item for item in value["packages"]}
    if is_lock_v2:
        for package_id, metadata in package_metadata.items():
            if metadata["source_sha256"] != package_records[package_id]["files_sha256"]:
                raise ContractError("install-plan selected package source digest differs from package files")
    for skill in value["skills"]:
        require_fields(
            skill,
            {"directories", "file_count", "files", "name", "package", "root_mode", "sha256"},
            "install-plan.skill",
        )
        _validate_install_tree_record(skill, "sha256", "install-plan.skill")
        if skill["package"] not in package_set:
            raise ContractError("install-plan skill references an unknown package")
    instructions = value["instructions"]
    require_fields(instructions, {"fragments", "path", "sha256"}, "install-plan.instructions")
    if instructions["path"] != "AGENTS.md" or not re.fullmatch(r"[0-9a-f]{64}", instructions["sha256"]):
        raise ContractError("install-plan instructions identity is invalid")
    fragment_ids = [item.get("id") for item in instructions["fragments"] if isinstance(item, dict)]
    if len(fragment_ids) != len(instructions["fragments"]) or None in fragment_ids or len(fragment_ids) != len(set(fragment_ids)):
        raise ContractError("install-plan instruction fragment ids are invalid")
    for fragment in instructions["fragments"]:
        require_fields(
            fragment,
            {"id", "merge_strategy", "order", "package", "path", "scope", "sha256"},
            "install-plan.instruction-fragment",
        )
        if (
            fragment["package"] not in package_set
            or not isinstance(fragment["path"], str)
            or not fragment["path"]
            or "\\" in fragment["path"]
            or fragment["path"].startswith("/")
            or any(part in {"", ".", ".."} for part in PurePosixPath(fragment["path"]).parts)
            or not re.fullmatch(r"[0-9a-f]{64}", fragment["sha256"])
        ):
            raise ContractError("install-plan instruction fragment identity is invalid")
    if is_lock_v2 and "rule_trace" not in instructions:
        raise ContractError("install-plan lock v2 instruction rule trace is required")
    rule_trace = instructions.get("rule_trace", [])
    if not isinstance(rule_trace, list):
        raise ContractError("install-plan instruction rule trace must be an array")
    for rule in rule_trace:
        require_fields(
            rule,
            {"id", "effect", "locked", "package", "scope", "content_sha256", "decision"},
            "install-plan.instruction-rule",
        )
        if (
            rule["effect"] not in {"allow", "deny"}
            or not isinstance(rule["locked"], bool)
            or rule["decision"] not in {"accepted", "replaced", "deny-wins"}
            or not re.fullmatch(r"[0-9a-f]{64}", rule["content_sha256"])
        ):
            raise ContractError("install-plan instruction rule trace is invalid")
    assets = value.get("assets", [])
    asset_summary = value.get("asset_summary")
    if is_lock_v2 or assets or asset_summary is not None:
        if not isinstance(assets, list) or not isinstance(asset_summary, dict):
            raise ContractError("install-plan asset allowlist metadata is incomplete")
        require_fields(
            asset_summary,
            {"content_sha256", "file_count", "package_count", "skill_count"},
            "install-plan.asset-summary",
        )
        expected_assets = [
            {"mode": entry["mode"], "package": package["id"], "path": entry["path"], "sha256": entry["sha256"]}
            for package in value["packages"]
            for entry in package["files"]
        ]
        if assets != expected_assets:
            raise ContractError("install-plan asset allowlist differs from selected package files")
        if (
            asset_summary["content_sha256"] != sha256(assets)
            or asset_summary["file_count"] != len(assets)
            or asset_summary["package_count"] != len(value["packages"])
            or asset_summary["skill_count"] != len(value["skills"])
        ):
            raise ContractError("install-plan asset allowlist digest is invalid")
    capability_providers = value.get("capability_providers", {})
    if not isinstance(capability_providers, dict):
        raise ContractError("install-plan capability provider mapping must be an object")
    if is_lock_v2 or capability_providers:
        if set(capability_providers) != set(value["bindings"]):
            raise ContractError("install-plan capability provider mapping differs from bindings")
        expected_providers = {}
        for capability_id, binding_record in value["bindings"].items():
            if not isinstance(binding_record, dict):
                raise ContractError("install-plan binding record is invalid")
            require_fields(binding_record, {"binding", "package"}, "install-plan.binding")
            package_id = binding_record["package"]
            if package_id not in package_records or package_id not in package_metadata:
                raise ContractError("install-plan binding references an unknown package")
            expected_providers[capability_id] = {
                "binding": binding_record["binding"],
                "package": package_id,
                "package_version": package_metadata[package_id]["version"],
                "source_sha256": package_records[package_id]["files_sha256"],
            }
        if capability_providers != expected_providers:
            raise ContractError("install-plan capability provider mapping is inconsistent")
    fingerprint_value = {key: item for key, item in value.items() if key not in {"fingerprint", "status"}}
    if value["fingerprint"] != sha256(fingerprint_value):
        raise ContractError("install-plan fingerprint mismatch")


def _validate_install_tree_record(value: dict[str, Any], digest_field: str, label: str) -> None:
    files = value["files"]
    directories = value["directories"]
    if not isinstance(files, list) or not isinstance(directories, list):
        raise ContractError(f"{label} tree entries must be arrays")
    file_paths: list[str] = []
    directory_paths: list[str] = []
    for entry in files:
        require_fields(entry, {"path", "sha256", "mode"}, f"{label}.file")
        _validate_install_entry(entry, f"{label}.file")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ContractError(f"{label} file digest is invalid")
        file_paths.append(entry["path"])
    for entry in directories:
        require_fields(entry, {"path", "mode"}, f"{label}.directory")
        _validate_install_entry(entry, f"{label}.directory")
        directory_paths.append(entry["path"])
    if file_paths != sorted(file_paths) or len(file_paths) != len(set(file_paths)):
        raise ContractError(f"{label} file paths must be sorted and unique")
    if directory_paths != sorted(directory_paths) or len(directory_paths) != len(set(directory_paths)):
        raise ContractError(f"{label} directory paths must be sorted and unique")
    if set(file_paths) & set(directory_paths):
        raise ContractError(f"{label} file and directory paths conflict")
    if not isinstance(value["file_count"], int) or value["file_count"] != len(files):
        raise ContractError(f"{label} file count is invalid")
    if value[digest_field] != sha256(files):
        raise ContractError(f"{label} tree digest mismatch")
    if label == "install-plan.package" and "manifest.json" not in file_paths:
        raise ContractError("install-plan package tree must contain manifest.json")
    if not isinstance(value["root_mode"], int) or isinstance(value["root_mode"], bool) or not 0 <= value["root_mode"] <= 0o777:
        raise ContractError(f"{label} root mode is invalid")


def _validate_install_entry(entry: dict[str, Any], label: str) -> None:
    path = entry["path"]
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
    ):
        raise ContractError(f"{label} path is unsafe")
    if not isinstance(entry["mode"], int) or isinstance(entry["mode"], bool) or not 0 <= entry["mode"] <= 0o777:
        raise ContractError(f"{label} mode is invalid")


VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "approval-record": validate_approval_record,
    "capability-contract": validate_capability_contract,
    "delivery-report": validate_delivery_report,
    "design-agent-packet": validate_design_agent_packet,
    "design-evidence": validate_design_evidence,
    "design-source-request": validate_design_source_request,
    "design-system-registry": validate_design_system_registry,
    "install-plan": validate_install_plan,
    "node-attempt": validate_node_attempt,
    "plugin-manifest": validate_manifest,
    "project-profile": validate_project_profile,
    "resolved-policy": validate_resolved_policy,
    "resource-event": validate_resource_event,
    "run-ledger": validate_run_ledger,
    "workflow-plan": validate_workflow_plan,
    "canonical-ui-ir": validate_canonical_ui_ir,
    "ui-validation-report": validate_ui_validation_report,
    "worktree-session-context": validate_worktree_session_context,
    "worktree-session-gate": validate_worktree_session_gate,
    "worktree-session-list": validate_worktree_session_list,
    "worktree-session-operation-result": validate_worktree_session_operation_result,
}


def validate(kind: str, value: dict[str, Any]) -> None:
    try:
        validator = VALIDATORS[kind]
    except KeyError as error:
        raise ContractError(f"unknown artifact kind: {kind}") from error
    validator(value)
