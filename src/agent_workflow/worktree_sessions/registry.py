"""Workflow-owned Session Registry stored under the primary Git common dir."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from ..canonical_json import dumps, load
from ..contracts import validate_worktree_session_context, validate_worktree_session_gate
from ..models import ContractError
from .git_workspace import freeze_checkpoint, refresh_session_source_identity, resolve_worktree


_TRANSITIONS = {
    "created": {"active", "blocked"},
    "active": {"checkpointed", "blocked"},
    "checkpointed": {"active", "gated", "blocked"},
    "gated": {"integrated", "blocked"},
    "integrated": {"closed", "blocked"},
    "blocked": {"active", "closed"},
    "closed": set(),
}


def new_session_context(
    *,
    session_id: str,
    project_id: str,
    repositories: list[dict[str, Any]],
    selected_platforms: list[str] | None = None,
    platform_contexts: dict[str, dict[str, Any]] | None = None,
    capability_closure: dict[str, dict[str, Any]] | None = None,
    dependencies: list[dict[str, Any]] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    platforms = selected_platforms or []
    if len(platforms) != len(set(platforms)):
        raise ContractError("selected platforms must be unique")
    context = {
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "capability_closure": deepcopy(capability_closure or {}),
        "dependencies": sorted(dependencies or [], key=lambda item: item["session_id"]),
        "lifecycle": {"state": "created"},
        "platform_contexts": deepcopy(platform_contexts or {}),
        "project_id": project_id,
        "repositories": sorted(deepcopy(repositories), key=lambda item: item["repository_id"]),
        "review": {"adapter_result_refs": [], "status": "pending"},
        "schema_version": "1.0",
        "selected_platforms": sorted(platforms),
        "session_id": session_id,
        "source_identity": {"algorithm": "session-source-v1", "mode": "working", "value": ""},
        "verification": {"adapter_result_refs": [], "status": "pending"},
    }
    refresh_session_source_identity(context)
    validate_worktree_session_context(context)
    return context


class SessionRegistry:
    def __init__(self, primary_worktree_or_common_dir: str | Path):
        candidate = Path(primary_worktree_or_common_dir).expanduser().resolve()
        if candidate.name == ".git" and candidate.is_dir() and not candidate.is_symlink():
            common = candidate
        else:
            _, common = resolve_worktree(candidate)
        self.common_dir = common
        self.directory = common / "agent-sessions"
        self.lock_path = self.directory / ".registry.lock"

    def assert_available(self, session_id: str) -> None:
        with self._locked():
            path = self._path(session_id)
            if path.exists() or path.is_symlink():
                raise ContractError(f"worktree session already exists: {session_id}")

    def create(self, context: dict[str, Any]) -> dict[str, Any]:
        validate_worktree_session_context(context)
        if context["lifecycle"]["state"] != "created":
            raise ContractError("new worktree session registry entries must start in created state")
        session_id = context["session_id"]
        with self._locked():
            self._validate_registry_owner(context)
            self._validate_stacked_dependencies(context)
            path = self._path(session_id)
            if path.exists() or path.is_symlink():
                raise ContractError(f"worktree session already exists: {session_id}")
            self._atomic_write(path, context)
        return deepcopy(context)

    def create_active(self, context: dict[str, Any]) -> dict[str, Any]:
        """Validate a created Context and publish only its active form."""
        validate_worktree_session_context(context)
        if context["lifecycle"]["state"] != "created":
            raise ContractError("new worktree session registry entries must start in created state")
        active = deepcopy(context)
        active["lifecycle"]["state"] = "active"
        validate_worktree_session_context(active)
        session_id = active["session_id"]
        with self._locked():
            self._validate_registry_owner(active)
            self._validate_stacked_dependencies(active)
            path = self._path(session_id)
            if path.exists() or path.is_symlink():
                raise ContractError(f"worktree session already exists: {session_id}")
            self._atomic_write(path, active)
        return deepcopy(active)

    def load(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"worktree session does not exist: {session_id}")
        value = load(path)
        validate_worktree_session_context(value)
        if value["session_id"] != session_id:
            raise ContractError("worktree session registry identity mismatch")
        self._validate_registry_owner(value)
        return value

    def list(self) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ContractError("worktree session registry directory is unsafe")
        result = []
        for path in sorted(self.directory.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise ContractError(f"worktree session registry entry is unsafe: {path.name}")
            value = load(path)
            validate_worktree_session_context(value)
            if path.stem != value["session_id"]:
                raise ContractError("worktree session registry filename mismatch")
            self._validate_registry_owner(value)
            result.append(value)
        return result

    def write(self, context: dict[str, Any]) -> dict[str, Any]:
        validate_worktree_session_context(context)
        with self._locked():
            current = self.load(context["session_id"])
            if current["created_at"] != context["created_at"] or current["project_id"] != context["project_id"]:
                raise ContractError("worktree session immutable registry identity changed")
            if self._immutable_identity(current) != self._immutable_identity(context):
                raise ContractError("worktree session immutable repository or dependency identity changed")
            current_state = current["lifecycle"]["state"]
            next_state = context["lifecycle"]["state"]
            if next_state != current_state and next_state not in _TRANSITIONS.get(current_state, set()):
                raise ContractError(f"illegal worktree session transition: {current_state} -> {next_state}")
            self._validate_stacked_dependencies(context)
            self._atomic_write(self._path(context["session_id"]), context)
        return deepcopy(context)

    def transition(
        self,
        session_id: str,
        target: str,
    ) -> dict[str, Any]:
        with self._locked():
            context = self.load(session_id)
            current = context["lifecycle"]["state"]
            if target not in _TRANSITIONS.get(current, set()):
                raise ContractError(f"illegal worktree session transition: {current} -> {target}")
            if target == "gated":
                raise ContractError("use evaluate_and_gate for the gated transition")
            if target == "active" and context["source_identity"]["mode"] == "committed":
                context["source_identity"]["mode"] = "working"
                refresh_session_source_identity(context)
                context["verification"] = {"adapter_result_refs": [], "status": "pending"}
                context["review"] = {"adapter_result_refs": [], "status": "pending"}
            context["lifecycle"]["state"] = target
            validate_worktree_session_context(context)
            self._atomic_write(self._path(session_id), context)
            return context

    def checkpoint(self, session_id: str) -> dict[str, Any]:
        """Freeze an active or newly-created Session without partial state."""
        with self._locked():
            context = self.load(session_id)
            if context["lifecycle"]["state"] == "created":
                context["lifecycle"]["state"] = "active"
                validate_worktree_session_context(context)
            if context["lifecycle"]["state"] != "active":
                raise ContractError("checkpoint requires an active worktree session")
            freeze_checkpoint(context)
            self._atomic_write(self._path(session_id), context)
            return context

    def evaluate_and_gate(
        self,
        session_id: str,
        *,
        adapter_pairs: list[dict[str, Any]],
        ledger: dict[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        """Revalidate live evidence and persist gated state in one Registry operation."""
        from .gate import evaluate_session_gate

        with self._locked():
            context = self.load(session_id)
            if context["lifecycle"]["state"] != "checkpointed":
                raise ContractError("Final Gate requires a checkpointed worktree session")
            result = evaluate_session_gate(
                context,
                adapter_pairs=adapter_pairs,
                ledger=ledger,
                artifact_root=artifact_root,
            )
            if result["status"] != "passed":
                return result
            self._validate_gate_result(context, result)
            context["lifecycle"]["state"] = "gated"
            validate_worktree_session_context(context)
            self._atomic_write(self._path(session_id), context)
            return result

    def attach_and_gate(
        self,
        session_id: str,
        *,
        adapter_pairs: list[dict[str, Any]],
        ledger: dict[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        """Attach evidence and conditionally gate in one locked operation."""
        from .gate import attach_adapter_result, evaluate_session_gate

        with self._locked():
            context = self.load(session_id)
            if context["lifecycle"]["state"] != "checkpointed":
                raise ContractError("Final Gate requires a checkpointed worktree session")
            for pair in adapter_pairs:
                if not isinstance(pair, dict) or set(pair) != {"attempt_id", "request", "result"}:
                    raise ContractError("gate pair file fields are invalid")
                attach_adapter_result(
                    context,
                    attempt_id=pair["attempt_id"],
                    request=pair["request"],
                    result=pair["result"],
                )
            result = evaluate_session_gate(
                context,
                adapter_pairs=adapter_pairs,
                ledger=ledger,
                artifact_root=artifact_root,
            )
            if result["status"] == "passed":
                self._validate_gate_result(context, result)
                context["lifecycle"]["state"] = "gated"
                validate_worktree_session_context(context)
            self._atomic_write(self._path(session_id), context)
            return result

    def _validate_stacked_dependencies(self, context: dict[str, Any]) -> None:
        for dependency in context["dependencies"]:
            upstream = self.load(dependency["session_id"])
            if upstream["source_identity"]["mode"] != "committed":
                raise ContractError("stacked dependency must reference a committed checkpoint")
            if upstream["source_identity"]["value"] != dependency["required_source_identity"]:
                raise ContractError("stacked dependency source identity is stale")

    def _validate_registry_owner(self, context: dict[str, Any]) -> None:
        primary = next(item for item in context["repositories"] if item["role"] == "primary")
        if Path(primary["git_common_dir"]).resolve() != self.common_dir:
            raise ContractError("worktree session primary repository does not belong to this registry")

    @staticmethod
    def _immutable_identity(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "created_at": context["created_at"],
            "capability_closure": context["capability_closure"],
            "dependencies": context["dependencies"],
            "project_id": context["project_id"],
            "platform_contexts": context["platform_contexts"],
            "repositories": [
                {
                    "base": item["base"],
                    "branch": item["branch"],
                    "git_common_dir": item["git_common_dir"],
                    "repository_id": item["repository_id"],
                    "role": item["role"],
                    "worktree_path": item["worktree_path"],
                }
                for item in context["repositories"]
            ],
            "selected_platforms": context["selected_platforms"],
            "session_id": context["session_id"],
        }

    @staticmethod
    def _validate_gate_result(context: dict[str, Any], result: dict[str, Any] | None) -> None:
        if result is None:
            raise ContractError("worktree session gated transition requires a validated Final Gate result")
        validate_worktree_session_gate(result)
        expected_commits = {
            item["repository_id"]: item["checkpoint"]["commit"]
            for item in context["repositories"]
            if item["checkpoint"] is not None
        }
        verification_refs = sorted(
            f"{item['attempt_id']}:{item['invocation_id']}"
            for item in context["verification"]["adapter_result_refs"]
        )
        review_refs = sorted(
            f"{item['attempt_id']}:{item['invocation_id']}"
            for item in context["review"]["adapter_result_refs"]
        )
        if (
            result["status"] != "passed"
            or result["session_id"] != context["session_id"]
            or result["source_identity"] != context["source_identity"]["value"]
            or result["checkpoint_commits"] != expected_commits
            or result["verification_refs"] != verification_refs
            or result["review_refs"] != review_refs
        ):
            raise ContractError("worktree session Final Gate result does not match current registry state")

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not session_id or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for character in session_id):
            raise ContractError("worktree session id is invalid")
        if session_id in {".", ".."}:
            raise ContractError("worktree session id is invalid")
        return self.directory / f"{session_id}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ContractError("worktree session registry directory is unsafe")
        if self.lock_path.is_symlink():
            raise ContractError("worktree session registry lock is unsafe")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            # fdopen owns and closes the descriptor on normal and exceptional paths.
            pass

    def _atomic_write(self, path: Path, value: dict[str, Any]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(dumps(value))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
