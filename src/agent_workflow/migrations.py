"""Fail-closed versioned migration compatibility graph."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .canonical_json import sha256
from .contracts import validate_activation_lock
from .models import ContractError


MigrationTransform = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class MigrationEdge:
    artifact: str
    from_version: str
    to_version: str
    lossless: bool
    transform: MigrationTransform | None = None
    changes: tuple[str, ...] = ()


class MigrationRegistry:
    """Deterministic compatibility graph with before/after validation."""

    def __init__(self, edges: Iterable[MigrationEdge] = ()) -> None:
        items = tuple(sorted(edges, key=lambda item: (item.artifact, item.from_version, item.to_version)))
        identities = [(item.artifact, item.from_version, item.to_version) for item in items]
        if len(identities) != len(set(identities)):
            raise ContractError("migration registry contains duplicate edges")
        if any(item.from_version == item.to_version for item in items):
            raise ContractError("migration registry must not declare identity edges")
        if any(not isinstance(item.lossless, bool) for item in items):
            raise ContractError("migration registry lossless flags must be boolean")
        if any(item.changes != tuple(sorted(set(item.changes))) for item in items):
            raise ContractError("migration registry changes must be sorted and unique")
        self._edges = items

    def path(self, artifact: str, from_version: str, to_version: str) -> tuple[MigrationEdge, ...]:
        if not all(isinstance(item, str) and item for item in (artifact, from_version, to_version)):
            raise ContractError("migration request identity is invalid")
        if from_version == to_version:
            return ()
        outgoing: dict[str, list[MigrationEdge]] = {}
        for edge in self._edges:
            if edge.artifact == artifact:
                outgoing.setdefault(edge.from_version, []).append(edge)
        queue: deque[tuple[str, tuple[MigrationEdge, ...]]] = deque([(from_version, ())])
        visited = {from_version}
        while queue:
            version, path = queue.popleft()
            for edge in outgoing.get(version, []):
                candidate = (*path, edge)
                if edge.to_version == to_version:
                    return candidate
                if edge.to_version not in visited:
                    visited.add(edge.to_version)
                    queue.append((edge.to_version, candidate))
        raise ContractError(
            f"no supported migration path for {artifact}: {from_version} -> {to_version}"
        )

    def migrate(
        self,
        artifact: str,
        value: dict[str, Any],
        to_version: str,
        *,
        validator: Callable[[dict[str, Any]], None],
        status: str = "planned",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if status not in {"applied", "planned"}:
            raise ContractError("migration report status is invalid")
        if not isinstance(value, dict) or not isinstance(value.get("schema_version"), str):
            raise ContractError("migration input is missing schema_version")
        validator(value)
        source = deepcopy(value)
        current = deepcopy(value)
        steps = []
        for edge in self.path(artifact, value["schema_version"], to_version):
            if edge.transform is None:
                raise ContractError(
                    f"migration transform is unavailable for {artifact}: "
                    f"{edge.from_version} -> {edge.to_version}"
                )
            first = edge.transform(deepcopy(current))
            second = edge.transform(deepcopy(current))
            if first != second:
                raise ContractError("migration transform is not deterministic")
            if not isinstance(first, dict) or first.get("schema_version") != edge.to_version:
                raise ContractError("migration transform produced the wrong schema_version")
            validator(first)
            current = first
            steps.append({
                "changes": list(edge.changes),
                "from_version": edge.from_version,
                "lossless": edge.lossless,
                "to_version": edge.to_version,
            })
        report: dict[str, Any] = {
            "after_sha256": sha256(current),
            "artifact": artifact,
            "before_sha256": sha256(source),
            "from_version": source["schema_version"],
            "lossless": all(item["lossless"] for item in steps),
            "schema_version": "1.0",
            "status": status,
            "steps": steps,
            "to_version": to_version,
        }
        report["fingerprint"] = sha256(report)
        return current, report


def _activation_lock_v1_to_v2(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != "1.0" or "handler" in value:
        raise ContractError("activation-lock v1 -> v2 migration precondition failed")
    migrated = deepcopy(value)
    migrated["schema_version"] = "2.0"
    migrated["handler"] = "core.source-activation.apple-codex-v1"
    return migrated


def migrate_activation_lock(
    value: dict[str, Any],
    *,
    status: str = "planned",
) -> tuple[dict[str, Any], dict[str, Any]]:
    return DEFAULT_MIGRATION_REGISTRY.migrate(
        "activation-lock",
        value,
        "2.0",
        validator=validate_activation_lock,
        status=status,
    )


DEFAULT_MIGRATION_REGISTRY = MigrationRegistry((
    MigrationEdge(
        "activation-lock",
        "1.0",
        "2.0",
        True,
        _activation_lock_v1_to_v2,
        ("add:/handler", "replace:/schema_version"),
    ),
))
