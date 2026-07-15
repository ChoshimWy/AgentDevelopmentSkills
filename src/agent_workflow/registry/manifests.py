"""Read-only plugin Manifest registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

from .. import __version__ as CORE_VERSION
from ..canonical_json import load, sha256
from ..contracts import validate_capability_contract, validate_manifest
from ..models import ContractError
from ..recipes import automatic_recipe_capabilities
from .versions import satisfies


@dataclass(frozen=True)
class RegisteredManifest:
    path: Path
    value: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class ResolvedBinding:
    capability_id: str
    provider_id: str
    binding: dict[str, str]
    contract: dict[str, Any]
    manifest_digest: str


class ManifestRegistry:
    def __init__(self, manifests: Iterable[RegisteredManifest], *, core_version: str = CORE_VERSION) -> None:
        items = sorted(manifests, key=lambda item: item.value["id"])
        ids = [item.value["id"] for item in items]
        if len(ids) != len(set(ids)):
            raise ContractError("manifest ids must be unique")
        self._items = tuple(items)
        self.core_version = core_version
        self._validate_graph()

    @classmethod
    def from_directory(
        cls,
        root: str | Path,
        *,
        provider_roots: Iterable[str | Path] = (),
        disabled_providers: Iterable[str] = (),
        core_version: str = CORE_VERSION,
    ) -> "ManifestRegistry":
        root_path = Path(root)
        package_roots: list[str | Path] = [root_path]
        if root_path.is_dir() and root_path.name == "platforms":
            for name in ("disciplines", "stacks", "runtime-configs"):
                candidate = root_path.parent / name
                if candidate.is_symlink():
                    raise ContractError(f"manifest package collection must not be a symlink: {name}")
                if candidate.is_dir():
                    package_roots.append(candidate)
        return cls.from_directories(
            [*package_roots, *provider_roots],
            disabled_providers=disabled_providers,
            core_version=core_version,
        )

    @classmethod
    def from_directories(
        cls,
        roots: Iterable[str | Path],
        *,
        disabled_providers: Iterable[str] = (),
        core_version: str = CORE_VERSION,
    ) -> "ManifestRegistry":
        manifests: list[RegisteredManifest] = []
        disabled = set(disabled_providers)
        seen_paths: set[Path] = set()
        for root in roots:
            root_path = Path(root).resolve()
            paths = [root_path] if root_path.is_file() else sorted(root_path.rglob("manifest.json"))
            for path in paths:
                resolved = path.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                value = load(resolved)
                validate_manifest(value)
                if value.get("role") == "provider" and value["id"] in disabled:
                    continue
                manifests.append(RegisteredManifest(path=resolved, value=value, digest=sha256(value)))
        return cls(manifests, core_version=core_version)

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

    def bootstrap_requirement(self, platform: str) -> dict[str, Any] | None:
        """Return an unresolved platform bootstrap contract, if one exists."""
        candidates = [
            item.value
            for item in self._items
            if item.value.get("role") == "bootstrap"
            and (item.value.get("id") == platform or platform in item.value.get("targets", []))
        ]
        if len(candidates) > 1:
            raise ContractError(f"ambiguous platform bootstrap contract: {platform}")
        if not candidates:
            return None
        contract = candidates[0]["provider_contract"]
        provider = self.by_id(contract["package_id"])
        if provider is not None and provider.value.get("role") == "provider":
            return None
        return {
            "package_compatibility": contract["package_compatibility"],
            "platform": platform,
            "provider": contract["package_id"],
            "required_capabilities": sorted(contract["required_capabilities"]),
        }

    def resolve_binding(self, capability_id: str, *, platform: str | None = None) -> ResolvedBinding | None:
        providers = self.capability_providers(capability_id)
        if platform is not None:
            providers = [item for item in providers if _supports_platform(item.value, platform)]
        if not providers:
            return None
        if len(providers) > 1:
            raise ContractError(f"ambiguous capability provider: {capability_id}")
        registered = providers[0]
        raw = registered.value.get("bindings", {}).get(capability_id)
        if raw is None:
            raise ContractError(f"capability has no binding: {capability_id}")
        binding = _normalize_binding(raw)
        entry = next(item for item in registered.value["capabilities"] if item["id"] == capability_id)
        contract = _normalized_contract(registered.value, entry)
        validate_capability_contract(contract)
        return ResolvedBinding(
            capability_id=capability_id,
            provider_id=registered.value["id"],
            binding=binding,
            contract=contract,
            manifest_digest=registered.digest,
        )

    def digest(self) -> str:
        return sha256([{"id": item.value["id"], "sha256": item.digest} for item in self._items])

    def _validate_graph(self) -> None:
        installed = {item.value["id"] for item in self._items}
        for item in self._items:
            manifest = item.value
            if manifest.get("role") == "provider":
                self._validate_provider(item)
            conflicts = installed & set(manifest.get("conflicts", []))
            if conflicts:
                raise ContractError(f"manifest {manifest['id']} conflicts with: {', '.join(sorted(conflicts))}")
            for capability in manifest.get("requires", []):
                if not self.capability_providers(capability):
                    raise ContractError(f"manifest {manifest['id']} requires missing capability: {capability}")
            for capability in manifest["capabilities"]:
                _normalized = _normalized_contract(manifest, capability)
                validate_capability_contract(_normalized)
                if manifest.get("role") != "bootstrap":
                    raw_binding = manifest.get("bindings", {}).get(capability["id"])
                    if raw_binding is None:
                        raise ContractError(f"manifest {manifest['id']} capability has no binding: {capability['id']}")
                    binding = _normalize_binding(raw_binding)
                    supported_modes = capability.get("supported_modes")
                    if supported_modes is not None:
                        if (
                            not isinstance(supported_modes, list)
                            or not supported_modes
                            or any(not isinstance(mode, str) or not mode for mode in supported_modes)
                            or len(supported_modes) != len(set(supported_modes))
                        ):
                            raise ContractError(
                                f"manifest {manifest['id']} capability supported_modes are invalid: {capability['id']}"
                            )
                        selected_mode = binding.get("mode", "default")
                        if selected_mode not in supported_modes:
                            raise ContractError(
                                f"manifest {manifest['id']} binding mode is unsupported for {capability['id']}"
                            )
                    binding_permission = capability.get("binding_permission_profile")
                    if binding_permission is not None and binding_permission != _normalized["permission_profile"]:
                        raise ContractError(
                            f"manifest {manifest['id']} binding permission is incompatible for {capability['id']}"
                        )
        self._validate_requirement_cycles()

    def _validate_provider(self, item: RegisteredManifest) -> None:
        manifest = item.value
        package = manifest.get("package")
        if not isinstance(package, dict):
            raise ContractError(f"provider {manifest['id']} package metadata is required")
        for field in ("version", "core_compatibility"):
            if not isinstance(package.get(field), str) or not package[field]:
                raise ContractError(f"provider {manifest['id']} package.{field} is required")
        if not satisfies(self.core_version, package["core_compatibility"]):
            raise ContractError(
                f"provider {manifest['id']} is incompatible with core {self.core_version}: "
                f"{package['core_compatibility']}"
            )

        bootstraps = [
            candidate.value
            for candidate in self._items
            if candidate.value.get("provider_contract", {}).get("package_id") == manifest["id"]
        ]
        if len(bootstraps) != 1:
            raise ContractError(f"provider {manifest['id']} requires exactly one bootstrap contract")
        bootstrap = bootstraps[0]
        contract = bootstrap["provider_contract"]
        allowed_targets = {bootstrap["id"], *bootstrap.get("targets", [])}
        provider_targets = set(manifest.get("targets", []))
        if not provider_targets or not provider_targets <= allowed_targets:
            raise ContractError(f"provider {manifest['id']} targets are outside its bootstrap contract")
        if not satisfies(package["version"], contract["package_compatibility"]):
            raise ContractError(
                f"provider {manifest['id']} version {package['version']} is outside "
                f"{contract['package_compatibility']}"
            )
        required = set(contract.get("required_capabilities", []))
        optional = set(contract.get("optional_capabilities", []))
        advisory = set(contract.get("advisory_capabilities", []))
        provided_entries = {entry["id"]: entry for entry in manifest["capabilities"]}
        invalid_reachability = sorted(
            capability_id
            for capability_id, entry in provided_entries.items()
            if entry.get("reachability") not in {"recipe", "manual-only"}
        )
        if invalid_reachability:
            raise ContractError(
                f"provider {manifest['id']} capabilities lack reachability: "
                + ", ".join(invalid_reachability)
            )
        manual_entries = {
            capability_id
            for capability_id, entry in provided_entries.items()
            if entry["reachability"] == "manual-only"
        }
        automatic_capabilities = automatic_recipe_capabilities(tuple(sorted(provider_targets)))
        forged_recipe_entries = sorted(
            capability_id
            for capability_id, entry in provided_entries.items()
            if entry["reachability"] == "recipe" and capability_id not in automatic_capabilities
        )
        if forged_recipe_entries:
            raise ContractError(
                f"provider {manifest['id']} capabilities are not reachable from a recipe: "
                + ", ".join(forged_recipe_entries)
            )
        manual_declared = manifest.get("manual_only_capabilities")
        if not isinstance(manual_declared, list) or set(manual_declared) != manual_entries:
            raise ContractError(f"provider {manifest['id']} manual-only capability list is inconsistent")
        manual_metadata = manifest.get("manual_only_metadata")
        if not isinstance(manual_metadata, dict) or set(manual_metadata) != manual_entries:
            raise ContractError(f"provider {manifest['id']} manual-only metadata is inconsistent")
        for capability_id, metadata in manual_metadata.items():
            if not isinstance(metadata, dict) or set(metadata) != {"entrypoint", "reason", "review_by"}:
                raise ContractError(f"provider {manifest['id']} manual-only metadata is invalid: {capability_id}")
            binding = _normalize_binding(manifest["bindings"].get(capability_id))
            if (
                metadata.get("entrypoint") != binding["name"]
                or not isinstance(metadata.get("reason"), str)
                or not metadata["reason"].strip()
                or not isinstance(metadata.get("review_by"), str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata["review_by"])
            ):
                raise ContractError(f"provider {manifest['id']} manual-only metadata is invalid: {capability_id}")
        missing = sorted(required - provided_entries.keys())
        if missing:
            raise ContractError(f"provider {manifest['id']} is missing required capabilities: {', '.join(missing)}")
        undeclared = sorted(provided_entries.keys() - required - optional - advisory)
        if undeclared:
            raise ContractError(f"provider {manifest['id']} has undeclared capabilities: {', '.join(undeclared)}")
        allowed_permissions = set(contract.get("allowed_permission_profiles", []))
        allowed_effects = set(contract.get("allowed_side_effects", []))
        capability_permissions = contract.get("capability_permissions", {})
        capability_effects = contract.get("capability_side_effects", {})
        for capability_id, entry in provided_entries.items():
            normalized = _normalized_contract(manifest, entry)
            if normalized["permission_profile"] not in allowed_permissions:
                raise ContractError(f"provider {manifest['id']} expands permission for {capability_id}")
            if normalized["permission_profile"] != capability_permissions.get(capability_id):
                raise ContractError(f"provider {manifest['id']} expands capability permission for {capability_id}")
            expanded = set(normalized["side_effects"]) - allowed_effects
            if expanded:
                raise ContractError(f"provider {manifest['id']} expands side effects for {capability_id}")
            capability_expanded = set(normalized["side_effects"]) - set(capability_effects.get(capability_id, []))
            if capability_expanded:
                raise ContractError(f"provider {manifest['id']} expands capability side effects for {capability_id}")

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


def _normalize_binding(value: Any) -> dict[str, str]:
    if isinstance(value, str) and value:
        return {"kind": "skill", "name": value}
    if not isinstance(value, dict):
        raise ContractError("binding must be a string or object")
    unknown = set(value) - {"kind", "name", "mode"}
    if unknown:
        raise ContractError(f"binding has unknown fields: {', '.join(sorted(unknown))}")
    if value.get("kind") not in {"skill", "agent", "script", "tool"}:
        raise ContractError("binding kind is invalid")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ContractError("binding name is invalid")
    if "mode" in value and (not isinstance(value["mode"], str) or not value["mode"]):
        raise ContractError("binding mode is invalid")
    return dict(value)


def _supports_platform(manifest: dict[str, Any], platform: str) -> bool:
    if manifest.get("role") != "provider":
        return True
    targets = set(manifest.get("targets", []))
    return not targets if platform == "*" else platform in targets
