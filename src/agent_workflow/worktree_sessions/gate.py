"""Workflow Session Final Gate backed by Adapter Request/Result and Run Ledger."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Iterable

from ..adapters.contracts import validate_adapter_request, validate_adapter_result
from ..canonical_json import dumps
from ..contracts import (
    validate_run_ledger,
    validate_worktree_session_context,
    validate_worktree_session_gate,
)
from ..models import ContractError
from .git_workspace import refresh_session_source_identity


@dataclass(frozen=True)
class _ArtifactRoot:
    descriptor: int
    path: Path


def attach_adapter_result(
    context: dict[str, Any],
    *,
    attempt_id: str,
    request: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Index one validated result without making the Session Context an evidence truth source."""
    validate_adapter_result(request, result)
    _validate_request_session_identity(context, request)
    if result["status"] != "completed":
        raise ContractError("only completed adapter results can be attached as passed session evidence")
    if not result["artifacts"]:
        raise ContractError("worktree session evidence requires at least one hashed artifact")
    capability = result["capability"]
    if capability.startswith("verification."):
        label, kind = "verification", "validation"
    elif capability == "review.independent":
        label, kind = "review", "review"
    else:
        raise ContractError("only verification or review results can be attached to a session gate")
    _validate_result_group(context, label, result)
    matching_evidence = [item for item in result["evidence"] if item["kind"] == kind]
    if not matching_evidence or any(item["status"] not in {"passed", "completed"} for item in matching_evidence):
        raise ContractError(f"worktree session {label} evidence is not passed")
    reference = {
        "artifact_hashes": sorted(
            (
                {"artifact_id": item["artifact_id"], "sha256": item["sha256"], "uri": item["uri"]}
                for item in result["artifacts"]
            ),
            key=lambda item: item["artifact_id"],
        ),
        "attempt_id": attempt_id,
        "binding": deepcopy(result["binding"]),
        "capability": capability,
        "invocation_id": result["invocation_id"],
        "node_id": result["node_id"],
        "plan_fingerprint": result["plan_fingerprint"],
        "provider": result["provider"],
        "request_id": result["request_id"],
    }
    refs = [
        item for item in context[label]["adapter_result_refs"]
        if (item["attempt_id"], item["invocation_id"]) != (attempt_id, result["invocation_id"])
    ]
    refs.append(reference)
    context[label]["adapter_result_refs"] = sorted(refs, key=lambda item: (item["attempt_id"], item["invocation_id"]))
    context[label]["status"] = "passed"
    validate_worktree_session_context(context)
    return context


def evaluate_session_gate(
    context: dict[str, Any],
    *,
    adapter_pairs: Iterable[dict[str, Any]],
    ledger: dict[str, Any],
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Validate immutable source, latest attempts, evidence identities and artifact bytes."""
    validate_worktree_session_context(context)
    validate_run_ledger(ledger)
    diagnostics: list[dict[str, str]] = []
    frozen_identity = context["source_identity"]["value"]
    if context["source_identity"]["mode"] != "committed":
        diagnostics.append({"code": "source-not-committed", "message": "Final Gate requires committed source identity"})
    else:
        try:
            refreshed = refresh_session_source_identity(deepcopy(context))
            if refreshed["source_identity"]["value"] != frozen_identity:
                diagnostics.append({"code": "source-stale", "message": "Checkpoint source identity changed"})
            if [item["checkpoint"] for item in refreshed["repositories"]] != [item["checkpoint"] for item in context["repositories"]]:
                diagnostics.append({"code": "checkpoint-stale", "message": "Checkpoint Commit or tree changed"})
        except (ContractError, OSError) as error:
            diagnostics.append({"code": "source-invalid", "message": str(error)})

    pair_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for pair in adapter_pairs:
        if not isinstance(pair, dict) or set(pair) != {"attempt_id", "request", "result"}:
            raise ContractError("worktree session adapter pair fields are invalid")
        key = (pair["attempt_id"], pair["result"].get("invocation_id"))
        if key in pair_lookup:
            raise ContractError("worktree session adapter pairs must be unique")
        pair_lookup[key] = pair

    latest_attempts = _latest_attempts_by_node(ledger)
    root: _ArtifactRoot | None = None
    root_error: str | None = None
    try:
        root = _open_artifact_root(Path(artifact_root).expanduser())
    except (ContractError, OSError) as error:
        root_error = str(error)
    try:
        verification_ids = _validate_reference_group(
            context,
            "verification",
            pair_lookup,
            ledger,
            latest_attempts,
            root,
            root_error,
            diagnostics,
        )
        review_ids = _validate_reference_group(
            context,
            "review",
            pair_lookup,
            ledger,
            latest_attempts,
            root,
            root_error,
            diagnostics,
        )
    finally:
        if root is not None:
            os.close(root.descriptor)
    if context["verification"]["status"] != "passed" or not verification_ids:
        diagnostics.append({"code": "verification-missing", "message": "Passed verification evidence is required"})
    else:
        valid_verification_capabilities = {
            item["capability"]
            for item in context["verification"]["adapter_result_refs"]
            if f"{item['attempt_id']}:{item['invocation_id']}" in set(verification_ids)
        }
        if context["selected_platforms"]:
            missing_platforms = [
                platform
                for platform in context["selected_platforms"]
                if not any(capability.startswith(f"verification.{platform}.") for capability in valid_verification_capabilities)
            ]
            if missing_platforms:
                diagnostics.append({
                    "code": "verification-platform-missing",
                    "message": f"Passed verification evidence is missing for: {', '.join(missing_platforms)}",
                })
        elif "verification.git.repository" not in valid_verification_capabilities:
            diagnostics.append({
                "code": "verification-git-missing",
                "message": "Pure Git Sessions require verification.git.repository evidence",
            })
    if context["review"]["status"] != "passed" or not review_ids:
        diagnostics.append({"code": "review-missing", "message": "Passed independent review evidence is required"})

    result = {
        "checkpoint_commits": {
            item["repository_id"]: item["checkpoint"]["commit"]
            for item in context["repositories"]
            if item["checkpoint"] is not None
        },
        "diagnostics": _unique_diagnostics(diagnostics),
        "review_refs": sorted(review_ids),
        "schema_version": "1.0",
        "session_id": context["session_id"],
        "source_identity": frozen_identity,
        "status": "blocked" if diagnostics else "passed",
        "verification_refs": sorted(verification_ids),
    }
    validate_worktree_session_gate(result)
    return result


def _validate_reference_group(
    context: dict[str, Any],
    label: str,
    pair_lookup: dict[tuple[str, str], dict[str, Any]],
    ledger: dict[str, Any],
    latest_attempts: dict[str, str],
    artifact_root: _ArtifactRoot | None,
    artifact_root_error: str | None,
    diagnostics: list[dict[str, str]],
) -> list[str]:
    valid: list[str] = []
    for reference in context[label]["adapter_result_refs"]:
        key = (reference["attempt_id"], reference["invocation_id"])
        pair = pair_lookup.get(key)
        identity = f"{reference['attempt_id']}:{reference['invocation_id']}"
        if pair is None:
            diagnostics.append({"code": f"{label}-pair-missing", "message": f"Missing Adapter pair for {identity}"})
            continue
        request, result = pair["request"], pair["result"]
        try:
            validate_adapter_result(request, result)
            _validate_result_group(context, label, result)
            _validate_request_session_identity(context, request)
            _validate_reference_identity(reference, request, result)
            _validate_ledger_link(reference, result, ledger, latest_attempts)
            if artifact_root is None:
                raise ContractError(artifact_root_error or "adapter artifact root is missing or unsafe")
            _validate_artifact_bytes(reference["artifact_hashes"], artifact_root)
        except (ContractError, OSError) as error:
            diagnostics.append({"code": f"{label}-invalid", "message": f"{identity}: {error}"})
            continue
        valid.append(identity)
    return valid


def _validate_result_group(context: dict[str, Any], label: str, result: dict[str, Any]) -> None:
    capability = result["capability"]
    frozen_provider = context["capability_closure"].get(capability)
    if (
        not isinstance(frozen_provider, dict)
        or frozen_provider["provider_id"] != result["provider"]
        or frozen_provider["binding"] != result["binding"]
    ):
        raise ContractError("Adapter Result is outside the frozen capability/provider/binding closure")
    if label == "verification":
        parts = capability.split(".")
        if capability == "verification.git.repository":
            if context["selected_platforms"]:
                raise ContractError("generic Git verification cannot replace selected-platform verification")
            expected_capability = True
        else:
            if len(parts) < 3 or parts[0] != "verification" or parts[1] not in context["selected_platforms"]:
                raise ContractError("verification capability does not belong to a selected platform")
            platform_context = context["platform_contexts"][parts[1]]
            expected_capability = platform_context["bindings"].get(capability) == result["binding"]
    else:
        expected_capability = capability == "review.independent"
    expected_kind = "validation" if label == "verification" else "review"
    evidence = [item for item in result["evidence"] if item["kind"] == expected_kind]
    if not expected_capability or not evidence or any(
        item["status"] not in {"passed", "completed"} for item in evidence
    ):
        raise ContractError(f"Adapter Result is not passed {label} evidence")


def _validate_request_session_identity(context: dict[str, Any], request: dict[str, Any]) -> None:
    validate_adapter_request(request)
    session = request["task_context"].get("worktree_session")
    if not isinstance(session, dict) or set(session) != {"session_id", "source_identity"}:
        raise ContractError("adapter request lacks frozen worktree session identity")
    if session["session_id"] != context["session_id"] or session["source_identity"] != context["source_identity"]["value"]:
        raise ContractError("adapter request worktree session identity is stale")


def _validate_reference_identity(
    reference: dict[str, Any], request: dict[str, Any], result: dict[str, Any]
) -> None:
    for field in (
        "request_id", "invocation_id", "plan_fingerprint", "node_id", "capability", "provider", "binding",
    ):
        if reference[field] != result[field] or result[field] != request[field]:
            raise ContractError(f"adapter reference {field} identity mismatch")
    expected_artifacts = sorted(
        ({"artifact_id": item["artifact_id"], "sha256": item["sha256"], "uri": item["uri"]} for item in result["artifacts"]),
        key=lambda item: item["artifact_id"],
    )
    if reference["artifact_hashes"] != expected_artifacts:
        raise ContractError("adapter reference artifact identity mismatch")


def _validate_ledger_link(
    reference: dict[str, Any],
    result: dict[str, Any],
    ledger: dict[str, Any],
    latest_attempts: dict[str, str],
) -> None:
    attempt_id = reference["attempt_id"]
    if ledger["plan_fingerprint"] != reference["plan_fingerprint"]:
        raise ContractError("run ledger plan fingerprint does not match Adapter evidence")
    if latest_attempts.get(reference["node_id"]) != attempt_id:
        raise ContractError("adapter evidence does not belong to the latest node attempt")
    outcomes = [
        item for item in ledger.get("adapter_outcomes", [])
        if item["attempt_id"] == attempt_id and item["invocation_id"] == reference["invocation_id"]
    ]
    if len(outcomes) != 1:
        raise ContractError("adapter evidence is not uniquely linked to a ledger outcome")
    outcome = outcomes[0]
    for field in ("node_id", "provider", "request_id", "invocation_id"):
        if outcome[field] != reference[field]:
            raise ContractError(f"ledger outcome {field} mismatch")
    if outcome["status"] != "completed" or result["status"] != "completed":
        raise ContractError("ledger outcome is not completed")
    ledger_artifacts = sorted(
        (
            {"artifact_id": item["artifact_id"], "sha256": item["sha256"], "uri": item["uri"]}
            for item in ledger.get("artifact_hashes", [])
            if item["attempt_id"] == attempt_id and item["node_id"] == reference["node_id"]
        ),
        key=lambda item: item["artifact_id"],
    )
    if ledger_artifacts != reference["artifact_hashes"]:
        raise ContractError("ledger artifact hashes do not match the Adapter Result")
    result_evidence = sorted(dumps(item) for item in result["evidence"])
    ledger_evidence = sorted(
        dumps({
            "artifact_ids": item["artifact_ids"],
            "data": item["data"],
            "kind": item["kind"],
            "status": item["status"],
            "summary": item["summary"],
        })
        for item in ledger.get("evidence", [])
        if item["attempt_id"] == attempt_id and item["node_id"] == reference["node_id"]
    )
    if not result_evidence or result_evidence != ledger_evidence:
        raise ContractError("run ledger and Adapter Result evidence semantics differ")


def _validate_artifact_bytes(artifacts: list[dict[str, str]], artifact_root: _ArtifactRoot) -> None:
    for artifact in artifacts:
        raw = artifact["uri"].removeprefix("file://")
        path = Path(raw)
        candidate = path if path.is_absolute() else artifact_root.path / path
        normalized = Path(os.path.abspath(candidate))
        try:
            relative = normalized.relative_to(artifact_root.path)
        except ValueError as error:
            raise ContractError("adapter artifact escapes the allowed artifact root") from error
        if not relative.parts:
            raise ContractError(f"adapter artifact is missing or unsafe: {artifact['artifact_id']}")
        directory = os.dup(artifact_root.descriptor)
        try:
            for part in relative.parts[:-1]:
                next_directory = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=directory,
                )
                os.close(directory)
                directory = next_directory
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(relative.parts[-1], flags, dir_fd=directory)
        except OSError as error:
            raise ContractError(
                f"adapter artifact is missing or unsafe: {artifact['artifact_id']}"
            ) from error
        finally:
            os.close(directory)
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ContractError(f"adapter artifact is missing or unsafe: {artifact['artifact_id']}")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        directory = os.dup(artifact_root.descriptor)
        try:
            for part in relative.parts[:-1]:
                next_directory = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=directory,
                )
                os.close(directory)
                directory = next_directory
            reopened_descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory,
            )
            reopened = os.fstat(reopened_descriptor)
            os.close(reopened_descriptor)
        except OSError as error:
            raise ContractError(
                f"adapter artifact changed while hashing: {artifact['artifact_id']}"
            ) from error
        finally:
            os.close(directory)
        if _stat_identity(opened) != _stat_identity(reopened):
            raise ContractError(
                f"adapter artifact changed while hashing: {artifact['artifact_id']}"
            )
        if digest.hexdigest() != artifact["sha256"]:
            raise ContractError(f"adapter artifact hash mismatch: {artifact['artifact_id']}")


def _open_artifact_root(path: Path) -> _ArtifactRoot:
    input_path = Path(os.path.abspath(path))
    try:
        before = input_path.lstat()
    except OSError as error:
        raise ContractError(f"adapter artifact root is missing or unsafe: {input_path}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ContractError(f"adapter artifact root is missing or unsafe: {input_path}")
    normalized = input_path.resolve(strict=True)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(normalized.anchor, flags)
    try:
        for part in normalized.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        opened = os.fstat(descriptor)
        after = input_path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or _stat_identity(before) != _stat_identity(opened)
            or _stat_identity(opened) != _stat_identity(after)
        ):
            raise ContractError(f"adapter artifact root changed while opening: {input_path}")
        return _ArtifactRoot(descriptor=descriptor, path=normalized)
    except OSError as error:
        os.close(descriptor)
        raise ContractError(
            f"adapter artifact root is missing or unsafe: {normalized}"
        ) from error
    except BaseException:
        os.close(descriptor)
        raise


def _latest_attempts_by_node(ledger: dict[str, Any]) -> dict[str, str]:
    latest: dict[str, tuple[int, str]] = {}
    for attempt in ledger["node_attempts"]:
        number = attempt.get("attempt_number")
        node_id = attempt.get("node_id")
        attempt_id = attempt.get("attempt_id")
        if not isinstance(number, int) or not isinstance(node_id, str) or not isinstance(attempt_id, str):
            raise ContractError("run ledger node attempt identity is invalid")
        if node_id not in latest or number > latest[node_id][0]:
            latest[node_id] = (number, attempt_id)
    return {node_id: item[1] for node_id, item in latest.items()}


def _unique_diagnostics(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result = []
    for item in items:
        key = (item["code"], item["message"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
