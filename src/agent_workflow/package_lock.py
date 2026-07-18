"""Persistent, deterministic package lock lifecycle for Phase 6."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .canonical_json import load, sha256
from .contracts import (
    MAX_INSTALL_DEPENDENCIES,
    MAX_INSTALL_PACKAGES,
    MAX_INSTALL_PROVIDERS,
    MAX_INSTALL_TREE_ENTRIES,
    validate_install_plan,
)
from .models import ContractError, require_fields, require_version
from .registry.versions import satisfies


LOCK_SCHEMA_VERSION = "1.0"
LOCK_MANAGER = "agent-development-skills"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
MAX_LOCK_PACKAGES = MAX_INSTALL_PACKAGES
MAX_LOCK_DEPENDENCIES = MAX_INSTALL_DEPENDENCIES
MAX_LOCK_PROVIDERS = MAX_INSTALL_PROVIDERS
MAX_LOCK_SCHEMAS = 65_536
MAX_PACKAGE_FILES = MAX_INSTALL_TREE_ENTRIES
MAX_SCHEMA_DIRECTORY_ENTRIES = 100_000
MAX_LOCK_PATH_BYTES = 4_096


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install_plan_identity_hash(install_plan: Mapping[str, Any]) -> str:
    """Return the non-circular Install Plan identity frozen by a package lock."""

    return sha256({
        key: value
        for key, value in install_plan.items()
        if key not in {"fingerprint", "package_lock_hash", "status"}
    })


def schema_inventory(schema_root: str | Path) -> dict[str, Any]:
    """Freeze the public Schema surface without recording host-absolute paths."""

    root = Path(schema_root)
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"schema root is missing or unsafe: {root}")
    repository_root = root.parent if root.name == "schemas" else root
    candidates = _schema_candidates(root, repository_root)
    files: list[dict[str, str]] = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"schema file is unsafe: {path.name}")
        try:
            value = load(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ContractError) as error:
            raise ContractError(f"schema file is invalid: {path.name}: {error}") from error
        if (
            not isinstance(value, dict)
            or value.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        ):
            raise ContractError(f"schema file has unsupported draft: {path.name}")
        files.append({
            "path": path.relative_to(repository_root).as_posix(),
            "sha256": _file_sha256(path),
        })
    if not files:
        raise ContractError("schema inventory must not be empty")
    return {
        "algorithm": "sha256",
        "content_sha256": sha256(files),
        "files": files,
    }


def _schema_candidates(root: Path, repository_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    entry_count = [0]
    _collect_direct_schema_files(root, candidates, entry_count)
    if root.name == "schemas":
        for container_name, child_names in (
            ("disciplines", ("contracts",)),
            ("platforms", ("contracts", "config")),
            ("stacks", ("contracts",)),
        ):
            container = repository_root / container_name
            if not container.exists():
                continue
            if container.is_symlink() or not container.is_dir():
                raise ContractError(f"schema directory is unsafe: {container}")
            for package in container.iterdir():
                entry_count[0] += 1
                if entry_count[0] > MAX_SCHEMA_DIRECTORY_ENTRIES:
                    raise ContractError(
                        "schema inventory exceeds maximum of "
                        f"{MAX_SCHEMA_DIRECTORY_ENTRIES} directory entries"
                    )
                if package.is_symlink() or not package.is_dir():
                    continue
                for child_name in child_names:
                    child = package / child_name
                    if child.exists() and not child.is_symlink() and child.is_dir():
                        _collect_direct_schema_files(child, candidates, entry_count)
    if len(candidates) > MAX_LOCK_SCHEMAS:
        raise ContractError(f"schema inventory exceeds maximum of {MAX_LOCK_SCHEMAS} files")
    return sorted(candidates)


def _collect_direct_schema_files(
    directory: Path,
    candidates: set[Path],
    entry_count: list[int],
) -> None:
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise ContractError(f"schema directory is unsafe: {directory}")
    for path in directory.iterdir():
        entry_count[0] += 1
        if entry_count[0] > MAX_SCHEMA_DIRECTORY_ENTRIES:
            raise ContractError(
                "schema inventory exceeds maximum of "
                f"{MAX_SCHEMA_DIRECTORY_ENTRIES} directory entries"
            )
        if not path.name.endswith(".schema.json"):
            continue
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"schema file is unsafe: {path.name}")
        candidates.add(path)


def _default_source(package_id: str) -> dict[str, Any]:
    return {"kind": "local-registry", "uri": f"registry://{package_id}"}


def _validate_source(source: Any, package_id: str) -> None:
    if not isinstance(source, dict) or set(source) != {"artifact_sha256", "kind", "sha256", "uri"}:
        raise ContractError(f"package lock source is invalid: {package_id}")
    kind = source["kind"]
    uri = source["uri"]
    if kind not in {"local-registry", "relative-path", "https"}:
        raise ContractError(f"package lock source kind is unsupported: {package_id}")
    if not isinstance(uri, str) or not uri or not _is_sha256(source["sha256"]):
        raise ContractError(f"package lock source identity is invalid: {package_id}")
    if "\\" in uri or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in uri):
        raise ContractError(f"package lock source identity is invalid: {package_id}")
    if len(uri.encode("utf-8")) > MAX_LOCK_PATH_BYTES:
        raise ContractError(f"package lock source identity is invalid: {package_id}")
    if kind == "local-registry":
        if source["artifact_sha256"] is not None:
            raise ContractError(f"package lock registry source must not declare an artifact: {package_id}")
        if uri != f"registry://{package_id}":
            raise ContractError(f"package lock registry source is invalid: {package_id}")
        return
    if kind == "relative-path":
        if source["artifact_sha256"] is not None:
            raise ContractError(f"package lock relative source must not declare an artifact: {package_id}")
        relative_value = uri[2:] if uri.startswith("./") else uri
        path = PurePosixPath(relative_value)
        if (
            not uri.startswith("./")
            or "\\" in uri
            or _WINDOWS_DRIVE.match(relative_value) is not None
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ContractError(f"package lock relative source is unsafe: {package_id}")
        return
    try:
        parsed = urlsplit(uri)
    except ValueError as error:
        raise ContractError(
            f"package lock HTTPS source is unsafe: {package_id}"
        ) from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError(f"package lock HTTPS source is unsafe: {package_id}")
    if not _is_sha256(source["artifact_sha256"]):
        raise ContractError(f"package lock HTTPS source requires an artifact SHA-256: {package_id}")


def resolve_package_lock(
    install_plan: dict[str, Any],
    *,
    schema_root: str | Path,
    package_sources: Mapping[str, Mapping[str, str]] | None = None,
    package_source_artifact_hashes: Mapping[str, str] | None = None,
    source_base: str | Path = ".",
    previous_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve an Install Plan v2 into a persistent byte-stable Lockfile."""

    validate_install_plan(install_plan)
    if install_plan.get("lock_schema_version") != "2.0":
        raise ContractError("persistent package lock requires Install Plan/Lock v2 metadata")
    if previous_lock is not None:
        validate_package_lock(previous_lock)
    sources = dict(package_sources or {})
    artifact_hashes = dict(package_source_artifact_hashes or {})
    package_ids = [item["id"] for item in install_plan["selected_packages"]]
    if len(package_ids) > MAX_LOCK_PACKAGES:
        raise ContractError(f"package lock exceeds maximum of {MAX_LOCK_PACKAGES} packages")
    unknown_sources = sorted(set(sources) - set(package_ids))
    if unknown_sources:
        raise ContractError("package lock source overrides reference unknown packages: " + ", ".join(unknown_sources))
    unknown_artifacts = sorted(set(artifact_hashes) - set(package_ids))
    if unknown_artifacts:
        raise ContractError("package lock artifact hashes reference unknown packages: " + ", ".join(unknown_artifacts))

    package_records = {item["id"]: item for item in install_plan["packages"]}
    packages = []
    for selected in install_plan["selected_packages"]:
        package_id = selected["id"]
        source = dict(sources.get(package_id, _default_source(package_id)))
        source["sha256"] = selected["source_sha256"]
        source["artifact_sha256"] = artifact_hashes.get(package_id)
        _validate_source(source, package_id)
        record = package_records[package_id]
        if source["kind"] == "relative-path":
            _validate_relative_source_snapshot(
                Path(source_base),
                source["uri"],
                record,
                package_id=package_id,
            )
        packages.append({
            "core_compatibility": selected["core_compatibility"],
            "id": package_id,
            "kind": selected["kind"],
            "manifest_sha256": record["manifest_sha256"],
            "provider_compatibility": selected["provider_compatibility"],
            "provider_manifest_sha256": record["provider_manifest_sha256"],
            "provider_version": selected["provider_version"],
            "source": source,
            "version": selected["version"],
        })

    core = next(item for item in packages if item["id"] == "core")
    body: dict[str, Any] = {
        "assets_sha256": install_plan["asset_summary"]["content_sha256"],
        "bindings_sha256": sha256(install_plan["bindings"]),
        "capability_providers": deepcopy(install_plan["capability_providers"]),
        "core": {
            "package_version": core["version"],
            "runtime_version": install_plan["core_version"],
            "source_sha256": core["source"]["sha256"],
        },
        "dependencies": deepcopy(install_plan["resolved_dependencies"]),
        "install_plan_identity_hash": install_plan_identity_hash(install_plan),
        "instructions": {
            "rule_trace_sha256": sha256(install_plan["instructions"]["rule_trace"]),
            "sha256": install_plan["instructions"]["sha256"],
        },
        "lineage": {
            "previous_lock_hash": previous_lock["fingerprint"] if previous_lock else None,
        },
        "manager": LOCK_MANAGER,
        "packages": packages,
        "permission_profiles": list(install_plan["permission_profiles"]),
        "schema_inventory": schema_inventory(schema_root),
        "schema_version": LOCK_SCHEMA_VERSION,
        "selection": {
            "disciplines": list(install_plan["selected_disciplines"]),
            "platforms": list(install_plan["selected_platforms"]),
            "runtime_configs": list(install_plan["selected_runtime_configs"]),
        },
        "side_effects": list(install_plan["side_effects"]),
    }
    body["fingerprint"] = sha256(body)
    validate_package_lock(body)
    anchored_hash = install_plan.get("package_lock_hash")
    if (
        anchored_hash is not None
        and not sources
        and not artifact_hashes
        and previous_lock is None
        and anchored_hash != body["fingerprint"]
    ):
        raise ContractError("Install Plan package lock anchor differs from resolved Lockfile")
    return body


def _validate_relative_source_snapshot(
    source_base: Path,
    uri: str,
    package_record: dict[str, Any],
    *,
    package_id: str,
) -> None:
    base = source_base.expanduser()
    if base.is_symlink() or not base.is_dir():
        raise ContractError("package lock source base is missing or unsafe")
    lexical = base
    for part in PurePosixPath(uri[2:]).parts:
        lexical /= part
        if lexical.is_symlink():
            raise ContractError(f"package lock relative source traverses a symlink: {package_id}")
    root = lexical.resolve()
    try:
        root.relative_to(base.resolve())
    except ValueError as error:
        raise ContractError(f"package lock relative source escapes source base: {package_id}") from error
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"package lock relative source is missing or unsafe: {package_id}")
    actual_files: list[dict[str, Any]] = []
    if len(package_record["files"]) > MAX_PACKAGE_FILES:
        raise ContractError(f"package source exceeds maximum of {MAX_PACKAGE_FILES} files")
    for expected in package_record["files"]:
        relative = expected["path"]
        relative_path = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath()
        if (
            not isinstance(relative, str)
            or not relative_path.parts
            or "\\" in relative
            or _WINDOWS_DRIVE.match(relative) is not None
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise ContractError(f"package lock relative source file is missing or unsafe: {package_id}")
        path = root
        for index, part in enumerate(relative_path.parts):
            path /= part
            if path.is_symlink():
                raise ContractError(
                    f"package lock relative source file is missing or unsafe: {package_id}"
                )
            if index < len(relative_path.parts) - 1 and not path.is_dir():
                raise ContractError(
                    f"package lock relative source file is missing or unsafe: {package_id}"
                )
        if not path.is_file():
            raise ContractError(f"package lock relative source file is missing or unsafe: {package_id}")
        actual_files.append({
            "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644,
            "path": expected["path"],
            "sha256": _file_sha256(path),
        })
    if actual_files != package_record["files"] or sha256(actual_files) != package_record["files_sha256"]:
        raise ContractError(f"package lock relative source content differs from Install Plan: {package_id}")


def validate_package_lock(value: dict[str, Any]) -> None:
    fields = {
        "assets_sha256", "bindings_sha256", "capability_providers", "core",
        "dependencies", "fingerprint", "install_plan_identity_hash", "instructions",
        "lineage", "manager", "packages", "permission_profiles", "schema_inventory",
        "schema_version", "selection", "side_effects",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError("agent-skills-lock fields are invalid")
    require_version(value)
    if value["schema_version"] != LOCK_SCHEMA_VERSION or value["manager"] != LOCK_MANAGER:
        raise ContractError("agent-skills-lock identity is invalid")
    for field in ("assets_sha256", "bindings_sha256", "install_plan_identity_hash", "fingerprint"):
        if not _is_sha256(value[field]):
            raise ContractError(f"agent-skills-lock {field} is invalid")

    core = value["core"]
    if not isinstance(core, dict) or set(core) != {"package_version", "runtime_version", "source_sha256"}:
        raise ContractError("agent-skills-lock core identity is invalid")
    if not all(
        isinstance(core[item], str)
        and re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", core[item])
        for item in ("package_version", "runtime_version")
    ):
        raise ContractError("agent-skills-lock core version is invalid")
    if not _is_sha256(core["source_sha256"]):
        raise ContractError("agent-skills-lock core source digest is invalid")

    packages = value["packages"]
    if not isinstance(packages, list) or not packages:
        raise ContractError("agent-skills-lock packages must not be empty")
    if len(packages) > MAX_LOCK_PACKAGES:
        raise ContractError(
            f"agent-skills-lock packages exceed maximum of {MAX_LOCK_PACKAGES}"
        )
    package_ids: list[str] = []
    package_by_id: dict[str, dict[str, Any]] = {}
    for package in packages:
        required = {
            "core_compatibility", "id", "kind", "manifest_sha256",
            "provider_compatibility", "provider_manifest_sha256", "provider_version",
            "source", "version",
        }
        if not isinstance(package, dict) or set(package) != required:
            raise ContractError("agent-skills-lock package fields are invalid")
        package_id = package["id"]
        if not isinstance(package_id, str) or not _SAFE_ID.fullmatch(package_id):
            raise ContractError("agent-skills-lock package id is invalid")
        if not isinstance(package["kind"], str) or package["kind"] not in {"core", "platform", "stack", "discipline", "adapter", "runtime-config"}:
            raise ContractError(f"agent-skills-lock package kind is invalid: {package_id}")
        if not isinstance(package["version"], str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", package["version"]):
            raise ContractError(f"agent-skills-lock package version is invalid: {package_id}")
        if not _is_sha256(package["manifest_sha256"]):
            raise ContractError(f"agent-skills-lock manifest digest is invalid: {package_id}")
        provider = package["provider_manifest_sha256"]
        if provider is not None and not _is_sha256(provider):
            raise ContractError(f"agent-skills-lock provider digest is invalid: {package_id}")
        _validate_source(package["source"], package_id)
        if not isinstance(package["core_compatibility"], str) or not satisfies(core["runtime_version"], package["core_compatibility"]):
            raise ContractError(f"agent-skills-lock core compatibility is not satisfied: {package_id}")
        provider_version = package["provider_version"]
        provider_compatibility = package["provider_compatibility"]
        if provider is None:
            if provider_version is not None or provider_compatibility is not None:
                raise ContractError(f"agent-skills-lock provider compatibility is unexpected: {package_id}")
        elif (
            not isinstance(provider_version, str)
            or not isinstance(provider_compatibility, str)
            or not satisfies(provider_version, provider_compatibility)
        ):
            raise ContractError(f"agent-skills-lock provider compatibility is not satisfied: {package_id}")
        package_ids.append(package_id)
        package_by_id[package_id] = package
    if package_ids[0] != "core" or len(package_ids) != len(set(package_ids)):
        raise ContractError("agent-skills-lock package order or identity is invalid")
    if (
        core["package_version"] != package_by_id["core"]["version"]
        or core["source_sha256"] != package_by_id["core"]["source"]["sha256"]
        or package_by_id["core"]["kind"] != "core"
        or any(
            package["kind"] == "core"
            for package_id, package in package_by_id.items()
            if package_id != "core"
        )
    ):
        raise ContractError("agent-skills-lock core identity differs from package closure")

    inventory = value["schema_inventory"]
    if not isinstance(inventory, dict) or set(inventory) != {"algorithm", "content_sha256", "files"}:
        raise ContractError("agent-skills-lock schema inventory is invalid")
    if inventory["algorithm"] != "sha256" or not _is_sha256(inventory["content_sha256"]):
        raise ContractError("agent-skills-lock schema inventory identity is invalid")
    if not isinstance(inventory["files"], list):
        raise ContractError("agent-skills-lock schema files are invalid")
    if len(inventory["files"]) > MAX_LOCK_SCHEMAS:
        raise ContractError(
            f"agent-skills-lock schema files exceed maximum of {MAX_LOCK_SCHEMAS}"
        )
    schema_paths: list[str] = []
    for item in inventory["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ContractError("agent-skills-lock schema entry is invalid")
        path = item["path"]
        schema_path = PurePosixPath(path) if isinstance(path, str) else PurePosixPath()
        if (
            not isinstance(path, str)
            or not path.endswith(".schema.json")
            or len(path.encode("utf-8")) > MAX_LOCK_PATH_BYTES
            or "\\" in path
            or _WINDOWS_DRIVE.match(path) is not None
            or schema_path.is_absolute()
            or any(part in {"", ".", ".."} for part in schema_path.parts)
        ):
            raise ContractError("agent-skills-lock schema path is invalid")
        if not _is_sha256(item["sha256"]):
            raise ContractError("agent-skills-lock schema digest is invalid")
        schema_paths.append(path)
    if not schema_paths or schema_paths != sorted(set(schema_paths)) or inventory["content_sha256"] != sha256(inventory["files"]):
        raise ContractError("agent-skills-lock schema inventory digest is invalid")

    selection = value["selection"]
    if not isinstance(selection, dict) or set(selection) != {"disciplines", "platforms", "runtime_configs"}:
        raise ContractError("agent-skills-lock selection is invalid")
    for key in selection:
        items = selection[key]
        if (
            not isinstance(items, list)
            or len(items) > MAX_LOCK_PACKAGES
            or any(not isinstance(item, str) or not item for item in items)
            or items != sorted(set(items))
        ):
            raise ContractError(f"agent-skills-lock selection {key} is invalid")
    selected = set(selection["disciplines"] + selection["platforms"] + selection["runtime_configs"])
    if not selected <= set(package_ids):
        raise ContractError("agent-skills-lock selection references unknown packages")

    for field in ("permission_profiles", "side_effects"):
        items = value[field]
        if (
            not isinstance(items, list)
            or len(items) > MAX_LOCK_PROVIDERS
            or any(not isinstance(item, str) or not item for item in items)
            or items != sorted(set(items))
        ):
            raise ContractError(f"agent-skills-lock {field} is invalid")

    providers = value["capability_providers"]
    if not isinstance(providers, dict):
        raise ContractError("agent-skills-lock capability providers are invalid")
    if len(providers) > MAX_LOCK_PROVIDERS:
        raise ContractError(
            "agent-skills-lock capability providers exceed maximum of "
            f"{MAX_LOCK_PROVIDERS}"
        )
    for capability, provider in providers.items():
        if not isinstance(capability, str) or not capability or not isinstance(provider, dict):
            raise ContractError("agent-skills-lock capability provider is invalid")
        provider_fields = {"binding", "package", "package_version", "permission_profile", "source_sha256"}
        if set(provider) != provider_fields:
            raise ContractError("agent-skills-lock capability provider fields are invalid")
        require_fields(provider, provider_fields, "agent-skills-lock.capability-provider")
        package_id = provider["package"]
        if not isinstance(package_id, str) or package_id not in package_by_id:
            raise ContractError("agent-skills-lock capability provider references unknown package")
        package = package_by_id[package_id]
        if (
            provider["package_version"] != package["version"]
            or provider["source_sha256"] != package["source"]["sha256"]
            or not isinstance(provider["permission_profile"], str)
            or not provider["permission_profile"]
            or provider["permission_profile"] not in value["permission_profiles"]
        ):
            raise ContractError("agent-skills-lock capability provider identity is stale")
    expected_bindings = {
        capability: {"binding": provider["binding"], "package": provider["package"]}
        for capability, provider in sorted(providers.items())
    }
    if value["bindings_sha256"] != sha256(expected_bindings):
        raise ContractError("agent-skills-lock bindings digest is inconsistent")

    dependencies = value["dependencies"]
    if not isinstance(dependencies, list):
        raise ContractError("agent-skills-lock dependencies are invalid")
    if len(dependencies) > MAX_LOCK_DEPENDENCIES:
        raise ContractError(
            f"agent-skills-lock dependencies exceed maximum of {MAX_LOCK_DEPENDENCIES}"
        )
    dependency_edges: list[tuple[str, str]] = []
    for dependency in dependencies:
        fields = {"from", "to", "requirement", "version", "required_capabilities"}
        if not isinstance(dependency, dict) or set(dependency) != fields:
            raise ContractError("agent-skills-lock dependency fields are invalid")
        require_fields(dependency, fields, "agent-skills-lock.dependency")
        required_capabilities = dependency["required_capabilities"]
        if (
            not isinstance(dependency["from"], str)
            or not isinstance(dependency["to"], str)
            or dependency["from"] not in package_by_id
            or dependency["to"] not in package_by_id
            or dependency["from"] == dependency["to"]
            or dependency["requirement"] not in {"required", "optional"}
            or not isinstance(dependency["version"], str)
            or not isinstance(required_capabilities, list)
            or not required_capabilities
            or len(required_capabilities) > MAX_LOCK_PROVIDERS
            or any(not isinstance(item, str) or not item for item in required_capabilities)
            or required_capabilities != sorted(set(required_capabilities))
        ):
            raise ContractError("agent-skills-lock dependency references unknown package")
        if not satisfies(package_by_id[dependency["to"]]["version"], dependency["version"]):
            raise ContractError("agent-skills-lock dependency version is not satisfied")
        dependency_edges.append((dependency["from"], dependency["to"]))
    if dependency_edges != sorted(set(dependency_edges)):
        raise ContractError("agent-skills-lock dependency edges must be sorted and unique")
    incoming = {package_id: 0 for package_id in package_ids}
    outgoing: dict[str, list[str]] = {package_id: [] for package_id in package_ids}
    for source, target in dependency_edges:
        incoming[target] += 1
        outgoing[source].append(target)
    queue = [package_id for package_id in package_ids if incoming[package_id] == 0]
    visited = 0
    while queue:
        source = queue.pop()
        visited += 1
        for target in outgoing[source]:
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if visited != len(package_ids):
        raise ContractError("agent-skills-lock dependency graph contains a cycle")
    reachable_packages = {"core", *selected}
    changed = True
    while changed:
        changed = False
        for dependency in dependencies:
            if dependency["from"] in reachable_packages and dependency["to"] not in reachable_packages:
                reachable_packages.add(dependency["to"])
                changed = True
    if reachable_packages != set(package_ids):
        raise ContractError("agent-skills-lock contains packages outside the selected dependency closure")
    for selection_key, expected_kind in (
        ("disciplines", "discipline"),
        ("platforms", "platform"),
        ("runtime_configs", "runtime-config"),
    ):
        if any(package_by_id[package_id]["kind"] != expected_kind for package_id in selection[selection_key]):
            raise ContractError(f"agent-skills-lock selection {selection_key} package kind is invalid")

    instructions = value["instructions"]
    if not isinstance(instructions, dict) or set(instructions) != {"rule_trace_sha256", "sha256"} or any(not _is_sha256(item) for item in instructions.values()):
        raise ContractError("agent-skills-lock instructions identity is invalid")
    lineage = value["lineage"]
    if not isinstance(lineage, dict) or set(lineage) != {"previous_lock_hash"}:
        raise ContractError("agent-skills-lock lineage is invalid")
    previous = lineage["previous_lock_hash"]
    if previous is not None and not _is_sha256(previous):
        raise ContractError("agent-skills-lock previous lock hash is invalid")
    expected = sha256({key: item for key, item in value.items() if key != "fingerprint"})
    if value["fingerprint"] != expected:
        raise ContractError("agent-skills-lock fingerprint mismatch")


def diff_package_locks(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, field-oriented Lockfile diff."""

    validate_package_lock(before)
    validate_package_lock(after)
    before_packages = {item["id"]: item for item in before["packages"]}
    after_packages = {item["id"]: item for item in after["packages"]}
    before_caps = before["capability_providers"]
    after_caps = after["capability_providers"]
    before_schemas = {item["path"]: item["sha256"] for item in before["schema_inventory"]["files"]}
    after_schemas = {item["path"]: item["sha256"] for item in after["schema_inventory"]["files"]}

    def changes(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "added": sorted(set(right) - set(left)),
            "changed": sorted(key for key in set(left) & set(right) if left[key] != right[key]),
            "removed": sorted(set(left) - set(right)),
        }

    body = {
        "bindings": changes(before_caps, after_caps),
        "from_lock_hash": before["fingerprint"],
        "packages": changes(before_packages, after_packages),
        "permissions": {
            "added": sorted(set(after["permission_profiles"]) - set(before["permission_profiles"])),
            "changed_capabilities": sorted(
                capability
                for capability in set(before_caps) & set(after_caps)
                if before_caps[capability]["permission_profile"] != after_caps[capability]["permission_profile"]
            ),
            "removed": sorted(set(before["permission_profiles"]) - set(after["permission_profiles"])),
        },
        "schema_version": LOCK_SCHEMA_VERSION,
        "schemas": changes(before_schemas, after_schemas),
        "selection_changed": before["selection"] != after["selection"],
        "to_lock_hash": after["fingerprint"],
    }
    body["status"] = "unchanged" if before["fingerprint"] == after["fingerprint"] else "changed"
    body["fingerprint"] = sha256(body)
    return body


def explain_package_lock(value: dict[str, Any]) -> dict[str, Any]:
    validate_package_lock(value)
    return {
        "binding_count": len(value["capability_providers"]),
        "core_version": value["core"]["runtime_version"],
        "lock_hash": value["fingerprint"],
        "package_count": len(value["packages"]),
        "packages": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "source": item["source"]["uri"],
                "version": item["version"],
            }
            for item in value["packages"]
        ],
        "permission_profiles": value["permission_profiles"],
        "schema_count": len(value["schema_inventory"]["files"]),
        "schema_version": LOCK_SCHEMA_VERSION,
        "selection": value["selection"],
        "status": "locked",
    }


def validate_plan_package_lock(plan: dict[str, Any], package_lock: dict[str, Any]) -> None:
    """Reject a valid but unrelated Lockfile before a Workflow Plan is used."""

    from .contracts import validate_workflow_plan

    validate_workflow_plan(plan)
    validate_package_lock(package_lock)
    if plan.get("package_lock_hash") != package_lock["fingerprint"]:
        raise ContractError("workflow plan package lock hash does not match Lockfile")
    providers = package_lock["capability_providers"]
    packages = {item["id"]: item for item in package_lock["packages"]}
    for node in plan.get("nodes", []):
        if node.get("provider") is None:
            continue
        capability = node.get("capability")
        locked = providers.get(capability)
        if locked is None:
            raise ContractError(f"workflow capability is not frozen by package lock: {capability}")
        if node.get("binding") != locked["binding"]:
            raise ContractError(f"workflow binding differs from package lock: {capability}")
        if node.get("permission_profile") != locked["permission_profile"]:
            raise ContractError(f"workflow permission differs from package lock: {capability}")
        package = packages[locked["package"]]
        expected_manifest = package["provider_manifest_sha256"] or package["manifest_sha256"]
        node_manifest = node.get("provider_manifest_digest")
        if node_manifest is not None and node_manifest != expected_manifest:
            raise ContractError(f"workflow provider manifest differs from package lock: {capability}")
