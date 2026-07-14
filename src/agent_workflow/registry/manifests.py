"""Read-only plugin Manifest registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..canonical_json import load, sha256
from ..contracts import validate_capability_contract, validate_manifest
from ..models import ContractError


@dataclass(frozen=True)
class RegisteredManifest:
    path: Path
    value: dict[str, Any]
    digest: str


class ManifestRegistry:
    def __init__(self, manifests: Iterable[RegisteredManifest]) -> None:
        items = sorted(manifests, key=lambda item: item.value["id"])
        ids = [item.value["id"] for item in items]
        if len(ids) != len(set(ids)):
            raise ContractError("manifest ids must be unique")
        self._items = tuple(items)
        self._validate_graph()

    @classmethod
    def from_directory(cls, root: str | Path) -> "ManifestRegistry":
        root_path = Path(root).resolve()
        manifests: list[RegisteredManifest] = []
        for path in sorted(root_path.rglob("manifest.json")):
            value = load(path)
            validate_manifest(value)
            manifests.append(RegisteredManifest(path=path, value=value, digest=sha256(value)))
        return cls(manifests)

    @property
    def manifests(self) -> tuple[RegisteredManifest, ...]:
        return self._items

    def by_id(self, manifest_id: str) -> RegisteredManifest | None:
        return next((item for item in self._items if item.value["id"] == manifest_id), None)

    def capability_providers(self, capability_id: str) -> list[RegisteredManifest]:
        return [
            item
            for item in self._items
            if any(capability.get("id") == capability_id for capability in item.value["capabilities"])
        ]

    def capability_contract(self, capability_id: str) -> dict[str, Any] | None:
        providers = self.capability_providers(capability_id)
        if not providers:
            return None
        if len(providers) > 1:
            raise ContractError(f"ambiguous capability provider: {capability_id}")
        manifest = providers[0].value
        entry = next(item for item in manifest["capabilities"] if item["id"] == capability_id)
        contract = _normalized_contract(manifest, entry)
        validate_capability_contract(contract)
        return contract

    def digest(self) -> str:
        return sha256([{"id": item.value["id"], "sha256": item.digest} for item in self._items])

    def _validate_graph(self) -> None:
        installed = {item.value["id"] for item in self._items}
        for item in self._items:
            manifest = item.value
            conflicts = installed & set(manifest.get("conflicts", []))
            if conflicts:
                raise ContractError(f"manifest {manifest['id']} conflicts with: {', '.join(sorted(conflicts))}")
            for capability in manifest.get("requires", []):
                if not self.capability_providers(capability):
                    raise ContractError(f"manifest {manifest['id']} requires missing capability: {capability}")
            for capability in manifest["capabilities"]:
                _normalized = _normalized_contract(manifest, capability)
                validate_capability_contract(_normalized)
        self._validate_requirement_cycles()

    def _validate_requirement_cycles(self) -> None:
        graph: dict[str, set[str]] = {}
        for item in self._items:
            required = set(item.value.get("requires", []))
            for capability in item.value["capabilities"]:
                graph.setdefault(capability["id"], set()).update(required)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(capability: str, path: list[str]) -> None:
            if capability in visiting:
                start = path.index(capability)
                cycle = path[start:] + [capability]
                raise ContractError(f"manifest capability dependency cycle: {' -> '.join(cycle)}")
            if capability in visited:
                return
            visiting.add(capability)
            path.append(capability)
            for dependency in sorted(graph.get(capability, set())):
                visit(dependency, path)
            path.pop()
            visiting.remove(capability)
            visited.add(capability)

        for capability in sorted(graph):
            visit(capability, [])


def _normalized_contract(manifest: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    capability_id = entry["id"]
    prefix = capability_id.split(".", 1)[0]
    permission_key = "implementation" if prefix == "implementation" else "verification" if prefix == "verification" else "detection"
    permission = entry.get("permission_profile") or manifest.get("permissions", {}).get(permission_key, "repository-read-only")
    side_effects = entry.get("side_effects")
    if side_effects is None:
        side_effects = ["project-files"] if prefix == "implementation" else ["validation-artifacts"] if prefix == "verification" else []
    concurrency_keys = entry.get("concurrency_keys")
    if concurrency_keys is None:
        concurrency_keys = [f"repository-write:{manifest['id']}"] if prefix == "implementation" else [f"build-queue:{manifest['id']}"] if prefix == "verification" else []
    return {
        "concurrency_keys": concurrency_keys,
        "failure_codes": entry.get("failure_codes", ["tool-unavailable", "environment-blocked", "contract-violation"]),
        "id": capability_id,
        "idempotent": entry.get("idempotent", prefix != "implementation"),
        "input_schema": entry.get("input_schema", "generic-request-v1"),
        "output_schema": entry.get("output_schema", "generic-result-v1"),
        "permission_profile": permission,
        "schema_version": "1.0",
        "side_effects": side_effects,
        "version": entry.get("version", "1.0"),
    }
