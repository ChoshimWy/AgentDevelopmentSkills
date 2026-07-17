"""Deterministic, platform-selective Agent Skills installation."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Callable, Iterable, Mapping

from . import __version__ as CORE_VERSION
from .canonical_json import dump, load, sha256
from .contracts import (
    validate_activation_lock,
    validate_install_plan,
    validate_manifest,
    validate_rollback_point,
)
from .models import ContractError
from .package_lock import install_plan_identity_hash, resolve_package_lock, validate_package_lock
from .registry.manifests import ManifestRegistry, RegisteredManifest
from .registry.versions import satisfies


MANAGER_ID = "agent-development-skills"
MANAGED_HEADER = "<!-- agent-development-skills:managed instructions-v1 -->"
MANAGED_ROOTS = ("AGENTS.md", "skills", ".agent-skills")
EXTERNAL_SKILL_ROOTS = (".system",)
EXTERNAL_ACTIVATION_LOCK = "activation-lock.json"
PERSISTENT_PACKAGE_LOCK = "agent-skills.lock"
ROLLBACK_POINT_DIRECTORY = "rollback-point"
LIFECYCLE_LOCK_DIRECTORY = ".agent-skills-lifecycle.lock"
IGNORED_OS_METADATA_FILES = (".DS_Store",)
MANAGED_FILE_MODE = 0o644
MANAGED_DIRECTORY_MODE = 0o755


@dataclass(frozen=True)
class _Skill:
    name: str
    root: Path
    files: tuple[dict[str, Any], ...]
    directories: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _Package:
    package_id: str
    root: Path
    manifest: dict[str, Any]
    manifest_digest: str
    provider: dict[str, Any] | None
    provider_digest: str | None
    files: tuple[dict[str, Any], ...]
    directories: tuple[dict[str, Any], ...]
    skills: tuple[_Skill, ...]
    fragments: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class InstallBundle:
    plan: dict[str, Any]
    instructions: str
    packages: tuple[_Package, ...]
    package_lock: dict[str, Any]


@dataclass(frozen=True)
class LifecycleLockToken:
    target: Path
    path: Path


@contextmanager
def target_lifecycle_lock(target_root: str | Path):
    """Serialize all lifecycle swaps for one installation root.

    The directory lock is deliberately outside the managed roots so a swap cannot
    replace it. A crashed process leaves a visible recovery residue and subsequent
    lifecycle operations fail closed rather than guessing whether it is stale.
    """

    raw_target = Path(target_root).expanduser()
    if raw_target.is_symlink():
        raise ContractError(f"lifecycle target must not be a symlink: {raw_target}")
    target = raw_target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    lock_path = target / LIFECYCLE_LOCK_DIRECTORY
    try:
        lock_path.mkdir(mode=MANAGED_DIRECTORY_MODE)
    except FileExistsError as error:
        raise ContractError(
            f"lifecycle operation is already active or recovery is required: {lock_path}"
        ) from error
    token = LifecycleLockToken(target=target, path=lock_path)
    try:
        yield token
    finally:
        try:
            lock_path.rmdir()
        except FileNotFoundError:
            pass


def _validate_lifecycle_token(target: Path, token: LifecycleLockToken) -> None:
    if token.target != target or token.path != target / LIFECYCLE_LOCK_DIRECTORY:
        raise ContractError("lifecycle lock token does not match target")
    if token.path.is_symlink() or not token.path.is_dir():
        raise ContractError("lifecycle lock token is no longer active")


@dataclass(frozen=True)
class _ResolvedPackages:
    package_roots: tuple[tuple[str, Path], ...]
    selected_disciplines: tuple[str, ...]
    selected_runtime_configs: tuple[str, ...]
    dependencies: tuple[dict[str, Any], ...]
    selection_reasons: dict[str, tuple[str, ...]]


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


RULE_MARKER = re.compile(
    r"^<!--\s*rule:(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\s+effect=(?P<effect>allow|deny)\s*-->$"
)
BUILTIN_BINDING_TARGETS = {("tool", "core.intent-lock")}


def _resolve_instruction_rules(
    fragments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Resolve rule identities and return trace, effective rules, and non-rule text."""

    resolved: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    fragment_text: dict[str, str] = {}
    next_order = 0
    for fragment in fragments:
        lines = fragment["content"].splitlines()
        passthrough: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            marker = RULE_MARKER.fullmatch(line.strip())
            if marker is None:
                passthrough.append(line)
                index += 1
                continue
            if index + 1 >= len(lines) or not lines[index + 1].lstrip().startswith("-"):
                raise ContractError(f"instruction rule marker has no bullet: {marker.group('id')}")
            rule_id = marker.group("id")
            effect = marker.group("effect")
            content = lines[index + 1].strip()
            identity_sha256 = _bytes_digest(f"{effect}\0{content}".encode("utf-8"))
            candidate = {
                "content": content,
                "content_sha256": identity_sha256,
                "effect": effect,
                "id": rule_id,
                "locked": fragment["merge_strategy"] == "locked",
                "order": resolved.get(rule_id, {}).get("order", next_order),
                "package": fragment["package"],
                "scope": fragment["scope"],
            }
            previous = resolved.get(rule_id)
            decision = "accepted"
            if previous is not None:
                if previous["locked"] and previous["content_sha256"] != candidate["content_sha256"]:
                    raise ContractError(f"locked instruction rule conflict: {rule_id}")
                if previous["content_sha256"] == candidate["content_sha256"]:
                    # A later identical locked declaration freezes the effective rule from
                    # this point onward; keeping the earlier unlocked record would let a
                    # subsequent lower-priority fragment silently replace it.
                    winner = candidate if candidate["locked"] and not previous["locked"] else previous
                elif previous["effect"] == "deny" or effect == "deny":
                    winner = previous if previous["effect"] == "deny" else candidate
                    decision = "deny-wins"
                else:
                    winner = candidate
                    decision = "replaced"
            else:
                winner = candidate
                next_order += 1
            resolved[rule_id] = winner
            trace.append({
                key: winner[key]
                for key in ("id", "effect", "locked", "package", "scope", "content_sha256")
            } | {"decision": decision})
            index += 2
        fragment_text[fragment["id"]] = "\n".join(passthrough).strip()
    effective = sorted(resolved.values(), key=lambda item: (item["order"], item["id"]))
    return trace, effective, fragment_text


def _binding_target_exists(
    binding: dict[str, Any], *, packages: tuple[_Package, ...], skill_names: set[str]
) -> bool:
    kind = binding.get("kind", "skill")
    name = binding.get("name")
    if not isinstance(name, str) or not name:
        return False
    if kind == "skill":
        return name in skill_names
    if (kind, name) in BUILTIN_BINDING_TARGETS:
        return True
    candidates: set[tuple[str, str]] = set()
    for package in packages:
        for entry in package.files:
            path = PurePosixPath(entry["path"])
            if name in {entry["path"], path.name, path.stem}:
                candidates.add((package.package_id, entry["path"]))
    return len(candidates) == 1


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_source_file_mode(path: Path) -> int:
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _capability_install_effects(manifest: dict[str, Any], capability: dict[str, Any]) -> tuple[str, list[str]]:
    capability_id = capability["id"]
    prefix = capability_id.split(".", 1)[0]
    permission_key = (
        "implementation" if prefix == "implementation"
        else "verification" if prefix == "verification"
        else "detection"
    )
    permission = capability.get("permission_profile") or manifest.get("permissions", {}).get(
        permission_key, "repository-read-only"
    )
    effects = capability.get("side_effects")
    if effects is None:
        effects = (
            ["project-files"] if prefix == "implementation"
            else ["validation-artifacts"] if prefix == "verification"
            else []
        )
    return permission, effects


def _resolve_child(root: Path, relative_value: str, *, label: str) -> Path:
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ContractError(f"{label} must be a package-relative path")
    resolved_root = root.resolve()
    lexical = root
    for part in relative.parts:
        lexical /= part
        if lexical.is_symlink():
            raise ContractError(f"{label} must not traverse a symlink: {relative_value}")
    resolved = lexical.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ContractError(f"{label} escapes package root")
    return resolved


def _collect_files(package_root: Path, roots: Iterable[str]) -> tuple[dict[str, Any], ...]:
    paths: set[Path] = {package_root / "manifest.json"}
    for metadata_name in ("migration-source.json", "migration-overrides.json"):
        metadata = package_root / metadata_name
        if metadata.is_symlink():
            raise ContractError(f"installation metadata must not be a symlink: {metadata_name}")
        if metadata.is_file():
            paths.add(metadata)
    for relative_value in roots:
        root = _resolve_child(package_root, relative_value, label="installation asset path")
        if not root.exists():
            raise ContractError(f"installation asset path is missing: {relative_value}")
        if root.is_symlink():
            raise ContractError(f"installation asset path must not be a symlink: {relative_value}")
        if root.is_file():
            paths.add(root)
            continue
        for path in root.rglob("*"):
            if "__pycache__" in path.parts or path.name == ".DS_Store" or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ContractError(f"installation asset must not be a symlink: {path.relative_to(package_root)}")
            if path.is_file():
                paths.add(path)
    entries = []
    for path in paths:
        relative = path.relative_to(package_root).as_posix()
        entries.append({"path": relative, "sha256": _file_digest(path), "mode": _canonical_source_file_mode(path)})
    entries.sort(key=lambda entry: entry["path"])
    return tuple(entries)


def _directories_for_files(root: Path, files: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    relative_paths: set[str] = set()
    for entry in files:
        parent = PurePosixPath(entry["path"]).parent
        while parent.parts:
            relative_paths.add(parent.as_posix())
            parent = parent.parent
    return tuple(
        {
            "path": relative,
            "mode": 0o755,
        }
        for relative in sorted(relative_paths)
    )


def _is_ignored_os_metadata(path: Path) -> bool:
    return path.name in IGNORED_OS_METADATA_FILES and path.is_file() and not path.is_symlink()


def _snapshot_tree(
    root: Path,
    *,
    ignore_source_cache: bool = False,
    ignore_os_metadata: bool = False,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"install tree is missing or unsafe: {root}")
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if ignore_os_metadata and _is_ignored_os_metadata(path):
            continue
        if ignore_source_cache and (
            "__pycache__" in relative.parts or path.name == ".DS_Store" or path.suffix == ".pyc"
        ):
            continue
        if path.is_symlink():
            raise ContractError(f"install tree must not contain symlinks: {relative.as_posix()}")
        mode = _canonical_source_file_mode(path) if ignore_source_cache and path.is_file() else path.stat().st_mode & 0o777
        if ignore_source_cache and path.is_dir():
            mode = 0o755
        entry = {"path": relative.as_posix(), "mode": mode}
        if path.is_dir():
            directories.append(entry)
        elif path.is_file():
            files.append({**entry, "sha256": _file_digest(path)})
        else:
            raise ContractError(f"install tree contains unsupported entry: {relative.as_posix()}")
    if ignore_source_cache:
        # Empty source directories are not Git identities and disappear from
        # source archives. Freeze only directories required by recorded files
        # so checkout and extracted-release Install Plans stay byte-identical.
        directories = list(_directories_for_files(root, files))
    return tuple(files), tuple(directories)


def _load_package(package_root: Path, package_id: str) -> _Package:
    if not package_id or not all(character.isalnum() or character in "._-" for character in package_id):
        raise ContractError(f"platform package id is unsafe: {package_id}")
    if package_root.is_symlink() or not package_root.is_dir():
        raise ContractError(f"package directory is missing or unsafe: {package_id}")
    manifest_path = package_root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ContractError(f"platform package is not installable: {package_id}")
    manifest = load(manifest_path)
    validate_manifest(manifest)
    if manifest["id"] != package_id:
        raise ContractError(f"platform directory and manifest id differ: {package_id}")
    installation = manifest.get("installation")
    if not isinstance(installation, dict):
        raise ContractError(f"platform package has no installation contract: {package_id}")

    provider = None
    provider_digest = None
    roots = [*installation["asset_roots"], *installation["skill_roots"]]
    provider_relative = installation.get("provider_manifest")
    if provider_relative is not None:
        provider_path = _resolve_child(package_root, provider_relative, label="provider manifest")
        if not provider_path.is_file():
            raise ContractError(f"provider manifest is missing: {provider_relative}")
        provider = load(provider_path)
        validate_manifest(provider)
        if provider.get("role") != "provider":
            raise ContractError(f"installation provider is not a provider manifest: {package_id}")
        provider_digest = sha256(provider)
        roots.append(provider_relative)

    fragments: list[dict[str, Any]] = []
    for raw in installation["instruction_fragments"]:
        fragment_path = _resolve_child(package_root, raw["path"], label="instruction fragment")
        if not fragment_path.is_file() or fragment_path.is_symlink():
            raise ContractError(f"instruction fragment is missing or unsafe: {raw['path']}")
        kind = manifest["kind"]
        expected_scope = "global" if package_id == "core" else f"{kind}:{package_id}"
        if raw["scope"] != expected_scope:
            raise ContractError(f"instruction fragment scope is invalid for {package_id}: {raw['scope']}")
        content = fragment_path.read_text(encoding="utf-8").strip() + "\n"
        fragments.append({
            **raw,
            "content": content,
            "package": package_id,
            "sha256": _bytes_digest(content.encode("utf-8")),
        })
        roots.append(raw["path"])

    skills: list[_Skill] = []
    for relative_value in installation["skill_roots"]:
        skill_root = _resolve_child(package_root, relative_value, label="skill root")
        if not skill_root.is_dir():
            raise ContractError(f"skill root is missing: {relative_value}")
        for candidate in sorted(skill_root.iterdir()):
            if candidate.is_dir() and (candidate / "SKILL.md").is_file():
                files, directories = _snapshot_tree(candidate, ignore_source_cache=True)
                skills.append(_Skill(candidate.name, candidate, files, directories))

    files = _collect_files(package_root, roots)
    if sha256(load(manifest_path)) != sha256(manifest):
        raise ContractError(f"platform manifest changed while building install plan: {package_id}")
    if provider_relative is not None:
        current_provider = load(_resolve_child(package_root, provider_relative, label="provider manifest"))
        if sha256(current_provider) != provider_digest:
            raise ContractError(f"provider manifest changed while building install plan: {package_id}")
    for fragment in fragments:
        current_content = _resolve_child(
            package_root, fragment["path"], label="instruction fragment"
        ).read_text(encoding="utf-8").strip() + "\n"
        if _bytes_digest(current_content.encode("utf-8")) != fragment["sha256"]:
            raise ContractError(f"instruction fragment changed while building install plan: {fragment['id']}")
    for skill in skills:
        current_files, current_directories = _snapshot_tree(skill.root, ignore_source_cache=True)
        if current_files != skill.files or current_directories != skill.directories:
            raise ContractError(f"skill changed while building install plan: {skill.name}")
    if _collect_files(package_root, roots) != files:
        raise ContractError(f"package files changed while building install plan: {package_id}")

    return _Package(
        package_id=package_id,
        root=package_root.resolve(),
        manifest=manifest,
        manifest_digest=sha256(manifest),
        provider=provider,
        provider_digest=provider_digest,
        files=files,
        directories=_directories_for_files(package_root, files),
        skills=tuple(skills),
        fragments=tuple(fragments),
    )


def available_platforms(platform_root: str | Path) -> tuple[str, ...]:
    root = Path(platform_root).resolve()
    result = []
    if not root.is_dir():
        raise ContractError(f"platform root does not exist: {root}")
    for candidate in sorted(root.iterdir()):
        manifest_path = candidate / "manifest.json"
        if candidate.is_symlink() or manifest_path.is_symlink():
            raise ContractError(f"platform package candidate is unsafe: {candidate.name}")
        if not candidate.is_dir():
            continue
        try:
            candidate.resolve().relative_to(root)
        except ValueError as error:
            raise ContractError(f"platform package candidate escapes platform root: {candidate.name}") from error
        if candidate.name == "core" or not manifest_path.is_file():
            continue
        value = load(manifest_path)
        validate_manifest(value)
        if value.get("kind") == "platform" and isinstance(value.get("installation"), dict):
            result.append(candidate.name)
    return tuple(result)


def _catalog_roots(platform_root: Path) -> tuple[Path, ...]:
    roots = [platform_root]
    if platform_root.name == "platforms":
        for name in ("disciplines", "stacks", "runtime-configs"):
            candidate = platform_root.parent / name
            if candidate.is_dir():
                roots.append(candidate)
    return tuple(roots)


def _package_catalog(platform_root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    catalog: dict[str, tuple[Path, dict[str, Any]]] = {}
    for collection_root in _catalog_roots(platform_root):
        for candidate in sorted(collection_root.iterdir()):
            manifest_path = candidate / "manifest.json"
            if candidate.is_symlink() or manifest_path.is_symlink():
                raise ContractError(f"package candidate is unsafe: {candidate.name}")
            if not candidate.is_dir() or not manifest_path.is_file():
                continue
            value = load(manifest_path)
            validate_manifest(value)
            if not isinstance(value.get("installation"), dict):
                continue
            package_id = value["id"]
            if candidate.name != package_id:
                raise ContractError(f"package directory and manifest id differ: {candidate.name}")
            if package_id in catalog:
                raise ContractError(f"package id is ambiguous: {package_id}")
            catalog[package_id] = (candidate.resolve(), value)
    return catalog


def _package_version(manifest: dict[str, Any]) -> str:
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ContractError(f"installable package {manifest['id']} version is required")
    return version


def _compose_instructions(packages: tuple[_Package, ...]) -> dict[str, Any]:
    fragments = sorted(
        (fragment for package in packages for fragment in package.fragments),
        key=lambda item: (
            next(index for index, package in enumerate(packages) if package.package_id == item["package"]),
            item["order"],
            item["id"],
        ),
    )
    fragment_ids = [item["id"] for item in fragments]
    if len(fragment_ids) != len(set(fragment_ids)):
        raise ContractError("instruction fragment ids conflict")
    rule_trace, effective_rules, fragment_text = _resolve_instruction_rules(fragments)
    content = MANAGED_HEADER + "\n# 全局 Agent Instructions\n\n"
    content += "> 此文件由 `agent-skills install` 确定性生成；请在源 Fragment 中修改。\n\n"
    for fragment in fragments:
        rendered = fragment_text[fragment["id"]]
        if not rendered:
            continue
        content += (
            f"<!-- fragment:{fragment['id']} scope={fragment['scope']} sha256={fragment['sha256']} -->\n"
            f"{rendered}\n\n"
        )
    if effective_rules:
        content += "## Effective Rules\n\n"
        for rule in effective_rules:
            content += (
                f"<!-- rule:{rule['id']} effect={rule['effect']} -->\n"
                f"{rule['content']}\n"
            )
        content += "\n"
    return {
        "content": content,
        "fragments": [
            {key: item[key] for key in ("id", "merge_strategy", "order", "package", "path", "scope", "sha256")}
            for item in fragments
        ],
        "rule_trace": rule_trace,
        "sha256": _bytes_digest(content.encode("utf-8")),
    }


def _derive_install_semantics(
    packages: tuple[_Package, ...],
    package_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive lockable semantics from validated package and Provider Manifests."""

    registry_entries: list[RegisteredManifest] = []
    for package in packages:
        registry_entries.append(RegisteredManifest(
            path=package.root / "manifest.json",
            value=package.manifest,
            digest=package.manifest_digest,
        ))
        if package.provider is not None:
            provider_path = _resolve_child(
                package.root,
                package.manifest["installation"]["provider_manifest"],
                label="provider manifest",
            )
            registry_entries.append(RegisteredManifest(
                path=provider_path,
                value=package.provider,
                digest=package.provider_digest or sha256(package.provider),
            ))
    ManifestRegistry(registry_entries, core_version=CORE_VERSION)

    skill_names = {skill.name for package in packages for skill in package.skills}
    bindings: dict[str, dict[str, Any]] = {}
    capability_permissions: dict[str, str] = {}
    permission_profiles: set[str] = set()
    side_effects: set[str] = set()
    selected_identities: dict[str, dict[str, Any]] = {}
    dependencies: list[dict[str, Any]] = []
    package_ids = {package.package_id for package in packages}
    for package in packages:
        binding_source = package.provider or package.manifest
        for capability_id, binding in sorted(binding_source.get("bindings", {}).items()):
            if capability_id in bindings:
                raise ContractError(f"installation binding conflict: {capability_id}")
            normalized = binding if isinstance(binding, dict) else {"kind": "skill", "name": binding}
            if not _binding_target_exists(normalized, packages=packages, skill_names=skill_names):
                raise ContractError(
                    "installation binding target is missing from dependency closure: "
                    f"{capability_id} -> {normalized.get('kind', 'skill')}:{normalized.get('name')}"
                )
            bindings[capability_id] = {"binding": binding, "package": package.package_id}
        for capability in binding_source["capabilities"]:
            permission, effects = _capability_install_effects(binding_source, capability)
            capability_permissions[capability["id"]] = permission
            permission_profiles.add(permission)
            side_effects.update(effects)
        selected_identities[package.package_id] = {
            "core_compatibility": (
                package.provider["package"]["core_compatibility"]
                if package.provider is not None
                else f"=={CORE_VERSION}"
            ),
            "kind": "core" if package.package_id == "core" else package.manifest["kind"],
            "provider_compatibility": (
                package.manifest["provider_contract"]["package_compatibility"]
                if package.provider is not None
                else None
            ),
            "provider_version": (
                package.provider["package"]["version"]
                if package.provider is not None
                else None
            ),
            "version": _package_version(package.manifest),
        }
        for dependency in package.manifest.get("package_requires", []):
            if dependency["requirement"] == "optional" and dependency["id"] not in package_ids:
                continue
            if dependency["id"] not in package_ids:
                raise ContractError(
                    f"installed package {package.package_id} requires missing package {dependency['id']}"
                )
            dependencies.append({
                "from": package.package_id,
                "required_capabilities": list(dependency["required_capabilities"]),
                "requirement": dependency["requirement"],
                "to": dependency["id"],
                "version": dependency["version"],
            })

    package_version_by_id = {
        package.package_id: selected_identities[package.package_id]["version"] for package in packages
    }
    capability_providers = {
        capability_id: {
            "binding": record["binding"],
            "package": record["package"],
            "package_version": package_version_by_id[record["package"]],
            "permission_profile": capability_permissions[capability_id],
            "source_sha256": package_records[record["package"]]["files_sha256"],
        }
        for capability_id, record in sorted(bindings.items())
    }
    instruction_semantics = _compose_instructions(packages)
    skill_identities = [
        {
            "directories": list(skill.directories),
            "file_count": len(skill.files),
            "files": list(skill.files),
            "name": skill.name,
            "package": package.package_id,
            "root_mode": 0o755,
            "sha256": sha256(list(skill.files)),
        }
        for package in packages
        for skill in package.skills
    ]
    return {
        "bindings": bindings,
        "capability_providers": capability_providers,
        "dependencies": sorted(dependencies, key=lambda item: (item["from"], item["to"])),
        "instructions": instruction_semantics,
        "permission_profiles": sorted(permission_profiles),
        "selected_package_identities": selected_identities,
        "side_effects": sorted(side_effects),
        "skills": skill_identities,
    }


def derive_installed_package_semantics(
    package_roots: Iterable[tuple[str, Path]],
    package_records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Rebuild lockable semantics from installed package files for Doctor."""

    records = {record["id"]: record for record in package_records}
    raw_roots = tuple(package_roots)
    for package_id, root in raw_roots:
        if root.is_symlink() or not root.is_dir():
            raise ContractError(f"installed semantic package root is missing or unsafe: {package_id}")
    roots = tuple((package_id, root.resolve()) for package_id, root in raw_roots)
    if [package_id for package_id, _ in roots] != list(records):
        raise ContractError("installed semantic package order differs from Install Lock")
    packages = tuple(_load_package(root, package_id) for package_id, root in roots)
    return _derive_install_semantics(packages, records)


def _resolve_packages(
    platform_root: Path,
    *,
    selected_platforms: tuple[str, ...],
    disciplines: Iterable[str],
    runtime_configs: Iterable[str],
) -> _ResolvedPackages:
    catalog = _package_catalog(platform_root)
    if "core" not in catalog:
        raise ContractError("core package is not installable")
    requested_disciplines = list(disciplines)
    if len(requested_disciplines) != len(set(requested_disciplines)):
        raise ContractError("selected disciplines must be unique")
    unknown_disciplines = sorted(
        item for item in requested_disciplines
        if item not in catalog or catalog[item][1].get("kind") != "discipline"
    )
    if unknown_disciplines:
        raise ContractError(f"discipline package is not installable: {', '.join(unknown_disciplines)}")
    requested_runtime_configs = list(runtime_configs)
    if len(requested_runtime_configs) != len(set(requested_runtime_configs)):
        raise ContractError("selected runtime configs must be unique")
    unknown_runtime_configs = sorted(
        item for item in requested_runtime_configs
        if item not in catalog or catalog[item][1].get("kind") != "runtime-config"
    )
    if unknown_runtime_configs:
        raise ContractError(f"runtime-config package is not installable: {', '.join(unknown_runtime_configs)}")

    explicit = {"core", *selected_platforms, *requested_disciplines, *requested_runtime_configs}
    reasons: dict[str, set[str]] = {"core": {"core"}}
    reasons.update({item: {f"platform:{item}"} for item in selected_platforms})
    reasons.update({item: {f"discipline:{item}"} for item in requested_disciplines})
    reasons.update({item: {f"runtime-config:{item}"} for item in requested_runtime_configs})
    resolved: set[str] = set()
    visiting: list[str] = []
    dependencies: list[dict[str, Any]] = []
    optional_dependencies: list[tuple[str, dict[str, Any]]] = []

    def visit(package_id: str) -> None:
        if package_id in visiting:
            start = visiting.index(package_id)
            raise ContractError(f"package dependency cycle: {' -> '.join(visiting[start:] + [package_id])}")
        if package_id in resolved:
            return
        candidate = catalog.get(package_id)
        if candidate is None:
            raise ContractError(f"required package is not installable: {package_id}")
        _, manifest = candidate
        visiting.append(package_id)
        for dependency in sorted(manifest.get("package_requires", []), key=lambda item: item["id"]):
            dependency_id = dependency["id"]
            if dependency["requirement"] == "optional":
                optional_dependencies.append((package_id, dependency))
                continue
            target = catalog.get(dependency_id)
            if target is None:
                raise ContractError(f"package {package_id} requires missing package: {dependency_id}")
            target_manifest = target[1]
            target_version = _package_version(target_manifest)
            if not satisfies(target_version, dependency["version"]):
                raise ContractError(
                    f"package {package_id} requires {dependency_id} {dependency['version']}, found {target_version}"
                )
            reasons.setdefault(dependency_id, set()).add(f"dependency:{package_id}")
            dependencies.append({
                "from": package_id,
                "required_capabilities": list(dependency["required_capabilities"]),
                "requirement": dependency["requirement"],
                "to": dependency_id,
                "version": dependency["version"],
            })
            visit(dependency_id)
        visiting.pop()
        resolved.add(package_id)

    for package_id in sorted(explicit):
        visit(package_id)

    rank = {"core": 0, "discipline": 1, "platform": 2, "stack": 3, "adapter": 4, "runtime-config": 5}
    for package_id, dependency in sorted(optional_dependencies, key=lambda item: (item[0], item[1]["id"])):
        dependency_id = dependency["id"]
        if dependency_id not in resolved:
            continue
        target_version = _package_version(catalog[dependency_id][1])
        if not satisfies(target_version, dependency["version"]):
            raise ContractError(
                f"package {package_id} optionally requires {dependency_id} {dependency['version']}, "
                f"found {target_version}"
            )
        dependencies.append({
            "from": package_id,
            "required_capabilities": list(dependency["required_capabilities"]),
            "requirement": "optional",
            "to": dependency_id,
            "version": dependency["version"],
        })

    incoming = {item: 0 for item in resolved}
    outgoing: dict[str, set[str]] = {item: set() for item in resolved}
    for dependency in dependencies:
        consumer = dependency["from"]
        provider = dependency["to"]
        if consumer not in outgoing[provider]:
            outgoing[provider].add(consumer)
            incoming[consumer] += 1

    def order_key(item: str) -> tuple[int, str]:
        return (0 if item == "core" else rank.get(catalog[item][1]["kind"], 9), item)

    queue = sorted((item for item, count in incoming.items() if count == 0), key=order_key)
    ordered: list[str] = []
    while queue:
        package_id = queue.pop(0)
        ordered.append(package_id)
        for consumer in sorted(outgoing[package_id], key=order_key):
            incoming[consumer] -= 1
            if incoming[consumer] == 0:
                queue.append(consumer)
                queue.sort(key=order_key)
    if len(ordered) != len(resolved):
        raise ContractError("package dependency cycle includes selected optional packages")
    return _ResolvedPackages(
        package_roots=tuple((item, catalog[item][0]) for item in ordered),
        selected_disciplines=tuple(sorted(requested_disciplines)),
        selected_runtime_configs=tuple(sorted(requested_runtime_configs)),
        dependencies=tuple(sorted(dependencies, key=lambda item: (item["from"], item["to"]))),
        selection_reasons={item: tuple(sorted(reasons[item])) for item in ordered},
    )


def resolve_platform_selection(
    platform_root: str | Path,
    *,
    platforms: Iterable[str] = (),
    core_only: bool = False,
) -> tuple[str, ...]:
    requested = list(platforms)
    if core_only and requested:
        raise ContractError("--core-only cannot be combined with --platform")
    if core_only:
        return ()
    if not requested:
        raise ContractError("select --core-only or at least one --platform")
    if "all" in requested:
        if len(requested) != 1:
            raise ContractError("--platform all cannot be combined with another platform")
        return available_platforms(platform_root)
    if len(requested) != len(set(requested)):
        raise ContractError("selected platforms must be unique")
    available = set(available_platforms(platform_root))
    unknown = sorted(set(requested) - available)
    if unknown:
        raise ContractError(f"platform package is not installable: {', '.join(unknown)}")
    return tuple(sorted(requested))


def build_install_bundle(
    platform_root: str | Path,
    *,
    platforms: Iterable[str] = (),
    disciplines: Iterable[str] = (),
    runtime_configs: Iterable[str] = (),
    core_only: bool = False,
    previous_lock: dict[str, Any] | None = None,
    schema_root: str | Path | None = None,
) -> InstallBundle:
    root = Path(platform_root).resolve()
    requested_disciplines = tuple(disciplines)
    requested_runtime_configs = tuple(runtime_configs)
    requested_platforms = tuple(platforms)
    if core_only and (requested_disciplines or requested_runtime_configs):
        raise ContractError("--core-only cannot be combined with --discipline or --runtime-config")
    if (requested_disciplines or requested_runtime_configs) and not requested_platforms and not core_only:
        selected: tuple[str, ...] = ()
    else:
        selected = resolve_platform_selection(root, platforms=requested_platforms, core_only=core_only)
    resolved = _resolve_packages(
        root,
        selected_platforms=selected,
        disciplines=requested_disciplines,
        runtime_configs=requested_runtime_configs,
    )
    packages = tuple(_load_package(package_root, item) for item, package_root in resolved.package_roots)

    packages_by_id = {package.package_id: package for package in packages}
    for dependency in resolved.dependencies:
        target = packages_by_id[dependency["to"]]
        binding_source = target.provider or target.manifest
        provided = {entry["id"] for entry in binding_source["capabilities"]}
        missing = sorted(set(dependency["required_capabilities"]) - provided)
        if missing:
            raise ContractError(
                f"package {dependency['from']} dependency {dependency['to']} is missing capabilities: "
                + ", ".join(missing)
            )
    instruction_semantics = _compose_instructions(packages)
    instructions = instruction_semantics["content"]

    skills: list[dict[str, Any]] = []
    skill_names: set[str] = set()
    for package in packages:
        for skill in package.skills:
            if skill.name in skill_names:
                raise ContractError(f"skill name conflict: {skill.name}")
            skill_names.add(skill.name)
            skills.append({
                "directories": list(skill.directories),
                "file_count": len(skill.files),
                "files": list(skill.files),
                "name": skill.name,
                "package": package.package_id,
                "root_mode": 0o755,
                "sha256": sha256(list(skill.files)),
            })

    package_records = []
    for package in packages:
        package_records.append({
            "directories": list(package.directories),
            "file_count": len(package.files),
            "files": list(package.files),
            "files_sha256": sha256(list(package.files)),
            "id": package.package_id,
            "manifest_sha256": package.manifest_digest,
            "provider_manifest_sha256": package.provider_digest,
            "root_mode": 0o755,
        })

    package_record_by_id = {item["id"]: item for item in package_records}
    semantics = _derive_install_semantics(packages, package_record_by_id)
    if semantics["dependencies"] != list(resolved.dependencies):
        raise ContractError("resolved package dependencies differ from installed Manifest semantics")
    if semantics["instructions"] != instruction_semantics:
        raise ContractError("rendered instructions differ from installed Manifest semantics")
    if semantics["skills"] != skills:
        raise ContractError("selected Skills differ from installed Manifest semantics")
    asset_allowlist = [
        {"mode": file["mode"], "package": package["id"], "path": file["path"], "sha256": file["sha256"]}
        for package in package_records
        for file in package["files"]
    ]

    plan = {
        "bindings": semantics["bindings"],
        "capability_providers": semantics["capability_providers"],
        "core_version": CORE_VERSION,
        "instructions": {
            "fragments": instruction_semantics["fragments"],
            "path": "AGENTS.md",
            "rule_trace": instruction_semantics["rule_trace"],
            "sha256": instruction_semantics["sha256"],
        },
        "asset_summary": {
            "content_sha256": sha256(asset_allowlist),
            "file_count": len(asset_allowlist),
            "package_count": len(package_records),
            "skill_count": len(skills),
        },
        "assets": asset_allowlist,
        "lock_schema_version": "2.0",
        "managed_roots": list(MANAGED_ROOTS),
        "manager": MANAGER_ID,
        "packages": package_records,
        "permission_profiles": semantics["permission_profiles"],
        "schema_version": "1.0",
        "resolved_dependencies": list(resolved.dependencies),
        "selected_disciplines": list(resolved.selected_disciplines),
        "selected_runtime_configs": list(resolved.selected_runtime_configs),
        "selected_packages": [
            {
                "id": package.package_id,
                **semantics["selected_package_identities"][package.package_id],
                "selection_reasons": list(resolved.selection_reasons[package.package_id]),
                "source_sha256": package_record_by_id[package.package_id]["files_sha256"],
            }
            for package in packages
        ],
        "selected_platforms": list(selected),
        "side_effects": semantics["side_effects"],
        "skills": skills,
        "status": "planned",
    }
    plan["fingerprint"] = sha256({key: value for key, value in plan.items() if key != "status"})
    validate_install_plan(plan)
    package_lock = resolve_package_lock(
        plan,
        schema_root=(
            Path(schema_root).resolve()
            if schema_root is not None
            else Path(__file__).resolve().parents[2] / "schemas"
        ),
        previous_lock=previous_lock,
    )
    plan["package_lock_hash"] = package_lock["fingerprint"]
    plan["fingerprint"] = sha256({key: value for key, value in plan.items() if key not in {"fingerprint", "status"}})
    validate_install_plan(plan)
    return InstallBundle(
        plan=plan,
        instructions=instructions,
        packages=packages,
        package_lock=package_lock,
    )


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _is_managed_install(target_root: Path) -> bool:
    managed_directory = target_root / ".agent-skills"
    if managed_directory.is_symlink() or not managed_directory.is_dir():
        return False
    if managed_directory.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE:
        return False
    lock_path = managed_directory / "install-lock.json"
    if lock_path.is_symlink() or not lock_path.is_file():
        return False
    if lock_path.stat().st_mode & 0o777 != MANAGED_FILE_MODE:
        return False
    try:
        lock = load(lock_path)
        validate_install_plan(lock)
        if lock["status"] != "installed":
            return False
        agents = target_root / "AGENTS.md"
        skills_root = target_root / "skills"
        if agents.is_symlink() or not agents.is_file() or skills_root.is_symlink() or not skills_root.is_dir():
            return False
        if agents.stat().st_mode & 0o777 != MANAGED_FILE_MODE:
            return False
        if skills_root.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE:
            return False
        if _bytes_digest(agents.read_bytes()) != lock["instructions"]["sha256"]:
            return False
        managed_entries = sorted(
            item.name
            for item in managed_directory.iterdir()
            if not _is_ignored_os_metadata(item)
        )
        allowed_entries = {"install-lock.json", "packages"}
        allowed_entries.update(
            item
            for item in (EXTERNAL_ACTIVATION_LOCK, PERSISTENT_PACKAGE_LOCK, ROLLBACK_POINT_DIRECTORY)
            if item in managed_entries
        )
        if set(managed_entries) != allowed_entries or len(managed_entries) != len(allowed_entries):
            return False
        package_lock_path = managed_directory / PERSISTENT_PACKAGE_LOCK
        if _path_exists(package_lock_path):
            if package_lock_path.is_symlink() or not package_lock_path.is_file():
                return False
            if package_lock_path.stat().st_mode & 0o777 != MANAGED_FILE_MODE:
                return False
            package_lock = load(package_lock_path)
            validate_package_lock(package_lock)
            if package_lock["fingerprint"] != lock.get("package_lock_hash"):
                return False
            if package_lock["install_plan_identity_hash"] != install_plan_identity_hash(lock):
                return False
        activation_lock_path = managed_directory / EXTERNAL_ACTIVATION_LOCK
        if _path_exists(activation_lock_path):
            if activation_lock_path.is_symlink() or not activation_lock_path.is_file():
                return False
            if activation_lock_path.stat().st_mode & 0o777 != MANAGED_FILE_MODE:
                return False
            activation_lock = load(activation_lock_path)
            try:
                validate_activation_lock(activation_lock)
            except ContractError:
                return False
            for entry in activation_lock["files"]:
                if not isinstance(entry, dict):
                    return False
                activated_path = _resolve_child(
                    target_root,
                    entry.get("path", ""),
                    label="activated file",
                )
                if activated_path.is_symlink() or not activated_path.is_file():
                    return False
                if (
                    activated_path.stat().st_mode & 0o777 != entry.get("mode")
                    or _bytes_digest(activated_path.read_bytes()) != entry.get("sha256")
                ):
                    return False
        rollback_path = managed_directory / ROLLBACK_POINT_DIRECTORY
        if _path_exists(rollback_path):
            _validate_rollback_point_directory(rollback_path)
        packages_root = managed_directory / "packages"
        if packages_root.is_symlink() or not packages_root.is_dir():
            return False
        if packages_root.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE:
            return False
        installed_package_ids = sorted(
            item.name
            for item in packages_root.iterdir()
            if not _is_ignored_os_metadata(item)
        )
        expected_package_ids = sorted(item["id"] for item in lock["packages"])
        if installed_package_ids != expected_package_ids:
            return False
        for item in lock["packages"]:
            directory = packages_root / item["id"]
            if directory.is_symlink() or not directory.is_dir():
                return False
            if directory.stat().st_mode & 0o777 != item["root_mode"]:
                return False
            files, directories = _snapshot_tree(directory, ignore_os_metadata=True)
            if not _tree_matches_record(files, directories, item, digest_field="files_sha256"):
                return False
        external_names = {
            item.name
            for item in skills_root.iterdir()
            if item.name in EXTERNAL_SKILL_ROOTS
            and item.is_dir()
            and not item.is_symlink()
        }
        metadata_names = {
            item.name
            for item in skills_root.iterdir()
            if _is_ignored_os_metadata(item)
        }
        installed_names = sorted(
            item.name
            for item in skills_root.iterdir()
            if item.name not in external_names and item.name not in metadata_names
        )
        expected_names = sorted(item["name"] for item in lock["skills"])
        if installed_names != expected_names:
            return False
        for item in lock["skills"]:
            directory = skills_root / item["name"]
            if directory.is_symlink() or not directory.is_dir():
                return False
            if directory.stat().st_mode & 0o777 != item["root_mode"]:
                return False
            files, directories = _snapshot_tree(directory, ignore_os_metadata=True)
            if not _tree_matches_record(files, directories, item, digest_field="sha256"):
                return False
    except (ContractError, KeyError, OSError, TypeError, ValueError):
        return False
    return True


def _preflight_install(target: Path) -> None:
    if target.exists() and not target.is_dir():
        raise ContractError(f"install target must be a directory: {target}")
    occupied = [name for name in MANAGED_ROOTS if _path_exists(target / name)]
    if occupied and not _is_managed_install(target):
        raise ContractError(
            "refusing to overwrite unmanaged or modified install roots: " + ", ".join(occupied)
        )


def _tree_matches_record(
    files: tuple[dict[str, Any], ...],
    directories: tuple[dict[str, Any], ...],
    record: dict[str, Any],
    *,
    digest_field: str,
) -> bool:
    return (
        list(files) == record["files"]
        and list(directories) == record["directories"]
        and len(files) == record["file_count"]
        and sha256(list(files)) == record[digest_field]
    )


def _copy_tree(
    source_root: Path,
    files: Iterable[dict[str, Any]],
    directories: Iterable[dict[str, Any]],
    destination: Path,
    *,
    root_mode: int,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(root_mode)
    for entry in directories:
        target = destination / entry["path"]
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(entry["mode"])
    for entry in files:
        source = source_root / entry["path"]
        if source.is_symlink() or not source.is_file():
            raise ContractError(f"installation source changed or became unsafe: {entry['path']}")
        target = destination / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(entry["mode"])


def _verify_staged_install(stage: Path, result: dict[str, Any]) -> None:
    agents = stage / "AGENTS.md"
    if agents.is_symlink() or not agents.is_file() or _bytes_digest(agents.read_bytes()) != result["instructions"]["sha256"]:
        raise ContractError("staged AGENTS.md differs from install plan")
    managed_root = stage / ".agent-skills"
    packages_root = managed_root / "packages"
    skills_root = stage / "skills"
    if agents.stat().st_mode & 0o777 != MANAGED_FILE_MODE:
        raise ContractError("staged AGENTS.md mode is not canonical")
    for directory in (managed_root, packages_root, skills_root):
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE:
            raise ContractError(f"staged managed directory mode is not canonical: {directory.name}")
    for item in result["packages"]:
        root = stage / ".agent-skills" / "packages" / item["id"]
        files, directories = _snapshot_tree(root)
        if root.stat().st_mode & 0o777 != item["root_mode"]:
            raise ContractError(f"staged package root mode differs from install plan: {item['id']}")
        if not _tree_matches_record(files, directories, item, digest_field="files_sha256"):
            raise ContractError(f"staged package differs from install plan: {item['id']}")
        manifest_path = root / "manifest.json"
        manifest = load(manifest_path)
        if sha256(manifest) != item["manifest_sha256"]:
            raise ContractError(f"staged package manifest differs from validated install plan: {item['id']}")
        provider_relative = manifest.get("installation", {}).get("provider_manifest")
        if item["provider_manifest_sha256"] is None:
            if provider_relative is not None:
                raise ContractError(f"staged package unexpectedly declares a provider: {item['id']}")
        else:
            if not isinstance(provider_relative, str):
                raise ContractError(f"staged package provider declaration is missing: {item['id']}")
            provider_path = _resolve_child(root, provider_relative, label="staged provider manifest")
            if not provider_path.is_file() or sha256(load(provider_path)) != item["provider_manifest_sha256"]:
                raise ContractError(f"staged provider manifest differs from validated install plan: {item['id']}")
    for item in result["skills"]:
        root = stage / "skills" / item["name"]
        files, directories = _snapshot_tree(root)
        if root.stat().st_mode & 0o777 != item["root_mode"]:
            raise ContractError(f"staged skill root mode differs from install plan: {item['name']}")
        if not _tree_matches_record(files, directories, item, digest_field="sha256"):
            raise ContractError(f"staged skill differs from install plan: {item['name']}")
    for fragment in result["instructions"]["fragments"]:
        fragment_path = _resolve_child(
            stage / ".agent-skills" / "packages" / fragment["package"],
            fragment["path"],
            label="staged instruction fragment",
        )
        if not fragment_path.is_file():
            raise ContractError(f"staged instruction fragment is missing: {fragment['id']}")
        content = fragment_path.read_text(encoding="utf-8").strip() + "\n"
        if _bytes_digest(content.encode("utf-8")) != fragment["sha256"]:
            raise ContractError(f"staged instruction fragment differs from install plan: {fragment['id']}")
    package_lock_path = managed_root / PERSISTENT_PACKAGE_LOCK
    if package_lock_path.is_symlink() or not package_lock_path.is_file():
        raise ContractError("staged persistent package lock is missing or unsafe")
    if package_lock_path.stat().st_mode & 0o777 != MANAGED_FILE_MODE:
        raise ContractError("staged persistent package lock mode is not canonical")
    package_lock = load(package_lock_path)
    validate_package_lock(package_lock)
    if package_lock["fingerprint"] != result.get("package_lock_hash"):
        raise ContractError("staged persistent package lock differs from install plan")
    if package_lock["install_plan_identity_hash"] != install_plan_identity_hash(result):
        raise ContractError("staged persistent package lock differs from install plan")


def _rollback_snapshot_identity(root: Path) -> str:
    files, directories = _snapshot_tree(root)
    filtered_files = [item for item in files if item["path"] != "rollback-point.json"]
    return sha256({"directories": list(directories), "files": filtered_files})


def _normalize_external_paths(target: Path, paths: Iterable[str]) -> tuple[str, ...]:
    target = target.resolve()
    normalized: list[str] = []
    for value in paths:
        path = _resolve_child(target, value, label="external lifecycle file")
        relative = path.relative_to(target).as_posix()
        if relative.split("/", 1)[0] in set(MANAGED_ROOTS) | {LIFECYCLE_LOCK_DIRECTORY}:
            raise ContractError(f"external lifecycle file overlaps a managed root: {relative}")
        normalized.append(relative)
    if normalized != sorted(set(normalized)):
        raise ContractError("external lifecycle files must be sorted and unique")
    return tuple(normalized)


def _snapshot_external_state(target: Path, root: Path, paths: Iterable[str]) -> dict[str, Any]:
    target = target.resolve()
    normalized = _normalize_external_paths(target, paths)
    files_root = root / "external-files"
    files_root.mkdir()
    files_root.chmod(MANAGED_DIRECTORY_MODE)
    files_root = files_root.resolve()
    directory_set: set[str] = set()
    for relative in normalized:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            directory_set.add(parent.as_posix())
            parent = parent.parent
    directory_paths = sorted(directory_set)
    directories: list[dict[str, Any]] = []
    for relative in directory_paths:
        path = _resolve_child(target, relative, label="external lifecycle directory")
        if not _path_exists(path):
            directories.append({"path": relative, "state": "absent"})
        elif path.is_symlink() or not path.is_dir():
            raise ContractError(f"external lifecycle directory is unsafe: {relative}")
        else:
            directories.append({
                "mode": path.stat().st_mode & 0o777,
                "path": relative,
                "state": "directory",
            })
    entries: list[dict[str, Any]] = []
    for relative in normalized:
        source = _resolve_child(target, relative, label="external lifecycle file")
        if not _path_exists(source):
            entries.append({"path": relative, "state": "absent"})
            continue
        if source.is_symlink() or not source.is_file():
            raise ContractError(f"external lifecycle file is not a regular file: {relative}")
        destination = _resolve_child(files_root, relative, label="external snapshot file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        for parent in destination.parents:
            if parent == files_root.parent:
                break
            parent.chmod(MANAGED_DIRECTORY_MODE)
        shutil.copyfile(source, destination)
        mode = source.stat().st_mode & 0o777
        destination.chmod(mode)
        entries.append({
            "mode": mode,
            "path": relative,
            "sha256": _bytes_digest(source.read_bytes()),
            "state": "file",
        })
    state = {"directories": directories, "entries": entries, "schema_version": "1.0"}
    state["fingerprint"] = sha256(state)
    dump(state, root / "external-state.json")
    (root / "external-state.json").chmod(MANAGED_FILE_MODE)
    return state


def _validate_external_state(root: Path) -> dict[str, Any]:
    state_path = root / "external-state.json"
    files_root = root / "external-files"
    if (
        state_path.is_symlink() or not state_path.is_file()
        or state_path.stat().st_mode & 0o777 != MANAGED_FILE_MODE
        or files_root.is_symlink() or not files_root.is_dir()
        or files_root.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE
    ):
        raise ContractError("rollback point external state is missing or unsafe")
    state = load(state_path)
    if not isinstance(state, dict) or set(state) != {"directories", "entries", "fingerprint", "schema_version"}:
        raise ContractError("rollback point external state shape is invalid")
    if (
        state["schema_version"] != "1.0"
        or not isinstance(state["entries"], list)
        or not isinstance(state["directories"], list)
    ):
        raise ContractError("rollback point external state version or entries are invalid")
    directory_paths: list[str] = []
    for entry in state["directories"]:
        if not isinstance(entry, dict) or entry.get("state") not in {"absent", "directory"}:
            raise ContractError("rollback point external directory entry is invalid")
        expected = {"path", "state"} if entry["state"] == "absent" else {"mode", "path", "state"}
        if set(entry) != expected or not isinstance(entry.get("path"), str):
            raise ContractError("rollback point external directory entry shape is invalid")
        _normalize_external_paths(Path("/external-root"), [f"{entry['path']}/placeholder"])
        if entry["state"] == "directory" and (
            not isinstance(entry["mode"], int) or isinstance(entry["mode"], bool)
            or entry["mode"] < 0 or entry["mode"] > 0o777
        ):
            raise ContractError("rollback point external directory mode is invalid")
        directory_paths.append(entry["path"])
    if directory_paths != sorted(set(directory_paths)):
        raise ContractError("rollback point external directories must be sorted and unique")
    paths: list[str] = []
    expected_files: list[str] = []
    for entry in state["entries"]:
        if not isinstance(entry, dict) or entry.get("state") not in {"absent", "file"}:
            raise ContractError("rollback point external state entry is invalid")
        expected_fields = {"path", "state"} if entry["state"] == "absent" else {"mode", "path", "sha256", "state"}
        if set(entry) != expected_fields:
            raise ContractError("rollback point external state entry shape is invalid")
        path = entry.get("path")
        if not isinstance(path, str):
            raise ContractError("rollback point external state path is invalid")
        _normalize_external_paths(Path("/external-root"), [path])
        paths.append(path)
        snapshot = _resolve_child(files_root, path, label="external snapshot file")
        if entry["state"] == "absent":
            if _path_exists(snapshot):
                raise ContractError(f"absent external snapshot unexpectedly exists: {path}")
            continue
        if (
            not isinstance(entry["mode"], int) or isinstance(entry["mode"], bool)
            or entry["mode"] < 0 or entry["mode"] > 0o777
            or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or snapshot.is_symlink() or not snapshot.is_file()
            or snapshot.stat().st_mode & 0o777 != entry["mode"]
            or _bytes_digest(snapshot.read_bytes()) != entry["sha256"]
        ):
            raise ContractError(f"external snapshot file differs from state: {path}")
        expected_files.append(path)
    if paths != sorted(set(paths)):
        raise ContractError("rollback point external paths must be sorted and unique")
    actual_files, directories = _snapshot_tree(files_root)
    if [item["path"] for item in actual_files] != expected_files:
        raise ContractError("rollback point external snapshot contains unknown files")
    if any(item["mode"] != MANAGED_DIRECTORY_MODE for item in directories):
        raise ContractError("rollback point external snapshot directory mode is invalid")
    if state["fingerprint"] != sha256({key: value for key, value in state.items() if key != "fingerprint"}):
        raise ContractError("rollback point external state fingerprint mismatch")
    return state


def _restore_external_state(root: Path, target: Path) -> None:
    target = target.resolve()
    state = _validate_external_state(root)
    for entry in state["directories"]:
        if entry["state"] != "directory":
            continue
        path = _resolve_child(target, entry["path"], label="external lifecycle directory")
        if not _path_exists(path):
            path.mkdir(parents=True)
        if path.is_symlink() or not path.is_dir():
            raise ContractError(f"external lifecycle directory is unsafe: {entry['path']}")
        path.chmod(entry["mode"])
    for entry in state["entries"]:
        destination = _resolve_child(target, entry["path"], label="external lifecycle file")
        if entry["state"] == "absent":
            if _path_exists(destination):
                if destination.is_symlink() or not destination.is_file():
                    raise ContractError(f"external lifecycle destination is unsafe: {entry['path']}")
                destination.unlink()
            continue
        source = _resolve_child(root / "external-files", entry["path"], label="external snapshot file")
        if _path_exists(destination) and (destination.is_symlink() or not destination.is_file()):
            raise ContractError(f"external lifecycle destination is unsafe: {entry['path']}")
        missing_parents: list[Path] = []
        parent = destination.parent
        while parent != target and not _path_exists(parent):
            missing_parents.append(parent)
            parent = parent.parent
        if parent != target and (parent.is_symlink() or not parent.is_dir()):
            raise ContractError(f"external lifecycle parent is unsafe: {entry['path']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        for parent in missing_parents:
            parent.chmod(MANAGED_DIRECTORY_MODE)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source.read_bytes())
            temporary.chmod(entry["mode"])
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    for entry in reversed(state["directories"]):
        path = _resolve_child(target, entry["path"], label="external lifecycle directory")
        if entry["state"] == "directory":
            if path.is_symlink() or not path.is_dir():
                raise ContractError(f"external lifecycle directory was not restored: {entry['path']}")
            path.chmod(entry["mode"])
            continue
        if _path_exists(path):
            if path.is_symlink() or not path.is_dir():
                raise ContractError(f"external lifecycle directory is unsafe: {entry['path']}")
            try:
                path.rmdir()
            except OSError as error:
                raise ContractError(
                    f"new external lifecycle directory is not empty: {entry['path']}"
                ) from error


def _validate_external_target_state(root: Path, target: Path) -> None:
    target = target.resolve()
    state = _validate_external_state(root)
    for entry in state["directories"]:
        path = _resolve_child(target, entry["path"], label="external lifecycle directory")
        if entry["state"] == "absent":
            if _path_exists(path):
                raise ContractError(f"external lifecycle directory changed after snapshot: {entry['path']}")
        elif (
            path.is_symlink() or not path.is_dir()
            or path.stat().st_mode & 0o777 != entry["mode"]
        ):
            raise ContractError(f"external lifecycle directory changed after snapshot: {entry['path']}")
    for entry in state["entries"]:
        path = _resolve_child(target, entry["path"], label="external lifecycle file")
        if entry["state"] == "absent":
            if _path_exists(path):
                raise ContractError(f"external lifecycle file changed after snapshot: {entry['path']}")
            continue
        if (
            path.is_symlink() or not path.is_file()
            or path.stat().st_mode & 0o777 != entry["mode"]
            or _bytes_digest(path.read_bytes()) != entry["sha256"]
        ):
            raise ContractError(f"external lifecycle file changed after snapshot: {entry['path']}")


def _validate_rollback_point_directory(root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE:
        raise ContractError("rollback point directory is missing or unsafe")
    entries = sorted(item.name for item in root.iterdir())
    expected_entries = [
        "AGENTS.md", PERSISTENT_PACKAGE_LOCK, "external-files", "external-state.json",
        "install-lock.json", "packages", "rollback-point.json", "skills",
    ]
    if "activation-lock.json" in entries:
        expected_entries.append("activation-lock.json")
        expected_entries.sort()
    if entries != expected_entries:
        raise ContractError("rollback point contains missing or unknown entries")
    for name in ("AGENTS.md", PERSISTENT_PACKAGE_LOCK, "install-lock.json", "rollback-point.json"):
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != MANAGED_FILE_MODE:
            raise ContractError(f"rollback point file is missing or unsafe: {name}")
    for name in ("packages", "skills"):
        path = root / name
        if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE:
            raise ContractError(f"rollback point directory is missing or unsafe: {name}")
    install_lock = load(root / "install-lock.json")
    package_lock = load(root / PERSISTENT_PACKAGE_LOCK)
    point = load(root / "rollback-point.json")
    external_state = _validate_external_state(root)
    activation_snapshot = root / EXTERNAL_ACTIVATION_LOCK
    if _path_exists(activation_snapshot):
        if (
            activation_snapshot.is_symlink() or not activation_snapshot.is_file()
            or activation_snapshot.stat().st_mode & 0o777 != MANAGED_FILE_MODE
        ):
            raise ContractError("rollback point activation lock is unsafe")
        activation_lock = load(activation_snapshot)
        records = activation_lock.get("files") if isinstance(activation_lock, dict) else None
        validate_activation_lock(activation_lock)
        external_by_path = {item["path"]: item for item in external_state["entries"]}
        for record in records:
            if not isinstance(record, dict) or set(record) != {"mode", "path", "sha256"}:
                raise ContractError("rollback point activation lock record is invalid")
            external = external_by_path.get(record["path"])
            if external is None or external.get("state") != "file" or any(
                record[field] != external[field] for field in ("mode", "sha256")
            ):
                raise ContractError("rollback point activation lock differs from external snapshot")
    validate_install_plan(install_lock)
    validate_package_lock(package_lock)
    validate_rollback_point(point)
    if install_lock["status"] != "installed":
        raise ContractError("rollback point Install Lock is not installed")
    if (
        point["install_plan_fingerprint"] != install_lock["fingerprint"]
        or point["package_lock_hash"] != package_lock["fingerprint"]
        or install_lock.get("package_lock_hash") != package_lock["fingerprint"]
        or package_lock["install_plan_identity_hash"] != install_plan_identity_hash(install_lock)
        or point["external_state_sha256"] != external_state["fingerprint"]
    ):
        raise ContractError("rollback point Lockfile identities are inconsistent")
    if _bytes_digest((root / "AGENTS.md").read_bytes()) != install_lock["instructions"]["sha256"]:
        raise ContractError("rollback point AGENTS.md differs from Install Lock")
    package_ids = sorted(item["id"] for item in install_lock["packages"])
    actual_packages = sorted(item.name for item in (root / "packages").iterdir())
    if actual_packages != package_ids:
        raise ContractError("rollback point package set differs from Install Lock")
    for record in install_lock["packages"]:
        package_root = root / "packages" / record["id"]
        if package_root.is_symlink() or not package_root.is_dir() or package_root.stat().st_mode & 0o777 != record["root_mode"]:
            raise ContractError(f"rollback point package is missing or unsafe: {record['id']}")
        files, directories = _snapshot_tree(package_root)
        if not _tree_matches_record(files, directories, record, digest_field="files_sha256"):
            raise ContractError(f"rollback point package differs from Install Lock: {record['id']}")
    skill_names = sorted(item["name"] for item in install_lock["skills"])
    actual_skills = sorted(item.name for item in (root / "skills").iterdir())
    if actual_skills != skill_names:
        raise ContractError("rollback point Skill set differs from Install Lock")
    for record in install_lock["skills"]:
        skill_root = root / "skills" / record["name"]
        if skill_root.is_symlink() or not skill_root.is_dir() or skill_root.stat().st_mode & 0o777 != record["root_mode"]:
            raise ContractError(f"rollback point Skill is missing or unsafe: {record['name']}")
        files, directories = _snapshot_tree(skill_root)
        if not _tree_matches_record(files, directories, record, digest_field="sha256"):
            raise ContractError(f"rollback point Skill differs from Install Lock: {record['name']}")
    semantics = derive_installed_package_semantics(
        [(record["id"], root / "packages" / record["id"]) for record in install_lock["packages"]],
        install_lock["packages"],
    )
    selected = {item["id"]: item for item in install_lock["selected_packages"]}
    locked_packages = {item["id"]: item for item in package_lock["packages"]}
    semantic_fields = {
        "core_compatibility", "kind", "provider_compatibility", "provider_version", "version"
    }
    if list(locked_packages) != list(selected):
        raise ContractError("rollback point package semantic closure differs between Lockfiles")
    for package_id, expected in semantics["selected_package_identities"].items():
        if (
            {field: selected[package_id][field] for field in semantic_fields} != expected
            or {field: locked_packages[package_id][field] for field in semantic_fields} != expected
        ):
            raise ContractError(f"rollback point package semantics differ from Manifests: {package_id}")
    if (
        install_lock["bindings"] != semantics["bindings"]
        or install_lock["capability_providers"] != semantics["capability_providers"]
        or package_lock["capability_providers"] != semantics["capability_providers"]
        or package_lock["bindings_sha256"] != sha256(semantics["bindings"])
        or install_lock["permission_profiles"] != semantics["permission_profiles"]
        or package_lock["permission_profiles"] != semantics["permission_profiles"]
        or install_lock["resolved_dependencies"] != semantics["dependencies"]
        or package_lock["dependencies"] != semantics["dependencies"]
        or install_lock["side_effects"] != semantics["side_effects"]
        or package_lock["side_effects"] != semantics["side_effects"]
    ):
        raise ContractError("rollback point runtime semantics differ from installed Manifests")
    skill_fields = ("file_count", "files", "name", "package", "sha256")
    if [
        {field: item[field] for field in skill_fields} for item in install_lock["skills"]
    ] != [
        {field: item[field] for field in skill_fields} for item in semantics["skills"]
    ]:
        raise ContractError("rollback point Skill semantics differ from installed Manifests")
    instructions = semantics["instructions"]
    if (
        install_lock["instructions"]["fragments"] != instructions["fragments"]
        or install_lock["instructions"]["rule_trace"] != instructions["rule_trace"]
        or install_lock["instructions"]["sha256"] != instructions["sha256"]
        or package_lock["instructions"] != {
            "rule_trace_sha256": sha256(instructions["rule_trace"]),
            "sha256": instructions["sha256"],
        }
        or (root / "AGENTS.md").read_text(encoding="utf-8") != instructions["content"]
    ):
        raise ContractError("rollback point AGENTS semantics differ from installed Manifests")
    if point["snapshot_sha256"] != _rollback_snapshot_identity(root):
        raise ContractError("rollback point snapshot digest is invalid")
    return point


def _write_rollback_point(
    target: Path,
    destination: Path,
    *,
    external_paths: Iterable[str] = (),
) -> dict[str, Any]:
    if not _is_managed_install(target):
        raise ContractError("persistent rollback requires an intact managed installation")
    install_lock = load(target / ".agent-skills" / "install-lock.json")
    package_lock = load(target / ".agent-skills" / PERSISTENT_PACKAGE_LOCK)
    if destination.exists() or destination.is_symlink():
        raise ContractError("rollback point destination already exists")
    destination.mkdir(parents=True)
    destination.chmod(MANAGED_DIRECTORY_MODE)
    shutil.copyfile(target / "AGENTS.md", destination / "AGENTS.md")
    (destination / "AGENTS.md").chmod(MANAGED_FILE_MODE)
    for name in ("skills", "packages"):
        (destination / name).mkdir()
        (destination / name).chmod(MANAGED_DIRECTORY_MODE)
    for record in install_lock["packages"]:
        _copy_tree(
            target / ".agent-skills" / "packages" / record["id"],
            record["files"],
            record["directories"],
            destination / "packages" / record["id"],
            root_mode=record["root_mode"],
        )
    for record in install_lock["skills"]:
        _copy_tree(
            target / "skills" / record["name"],
            record["files"],
            record["directories"],
            destination / "skills" / record["name"],
            root_mode=record["root_mode"],
        )
    for name in ("install-lock.json", PERSISTENT_PACKAGE_LOCK):
        shutil.copyfile(target / ".agent-skills" / name, destination / name)
        (destination / name).chmod(MANAGED_FILE_MODE)
    activation_lock = target / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK
    if _path_exists(activation_lock):
        if activation_lock.is_symlink() or not activation_lock.is_file():
            raise ContractError("persistent rollback activation lock is unsafe")
        shutil.copyfile(activation_lock, destination / EXTERNAL_ACTIVATION_LOCK)
        (destination / EXTERNAL_ACTIVATION_LOCK).chmod(MANAGED_FILE_MODE)
    external_state = _snapshot_external_state(target, destination, external_paths)
    point: dict[str, Any] = {
        "external_state_sha256": external_state["fingerprint"],
        "install_plan_fingerprint": install_lock["fingerprint"],
        "manager": MANAGER_ID,
        "package_lock_hash": package_lock["fingerprint"],
        "point_id": f"rollback-{package_lock['fingerprint'][:12]}",
        "schema_version": "1.0",
        "snapshot_sha256": _rollback_snapshot_identity(destination),
    }
    point["fingerprint"] = sha256(point)
    validate_rollback_point(point)
    dump(point, destination / "rollback-point.json")
    (destination / "rollback-point.json").chmod(MANAGED_FILE_MODE)
    _validate_rollback_point_directory(destination)
    return point


def preview_rollback_point(
    target_root: str | Path,
    *,
    external_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Derive the exact persistent rollback identity without writing the target."""

    raw_target = Path(target_root).expanduser()
    if raw_target.is_symlink():
        raise ContractError(f"rollback target must not be a symlink: {raw_target}")
    target = raw_target.resolve()
    with tempfile.TemporaryDirectory(prefix="agent-skills-rollback-preview-") as directory:
        return _write_rollback_point(
            target,
            Path(directory) / ROLLBACK_POINT_DIRECTORY,
            external_paths=external_paths,
        )


def rollback_install(
    target_root: str | Path,
    *,
    lifecycle_token: LifecycleLockToken | None = None,
    expected_current_lock_hash: str | None = None,
    expected_rollback_point_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _rollback_install(
        target_root,
        lifecycle_token=lifecycle_token,
        expected_current_lock_hash=expected_current_lock_hash,
        expected_rollback_point_fingerprint=expected_rollback_point_fingerprint,
    )


def _rollback_install(
    target_root: str | Path,
    *,
    lifecycle_token: LifecycleLockToken | None = None,
    expected_current_lock_hash: str | None = None,
    expected_rollback_point_fingerprint: str | None = None,
) -> dict[str, Any]:
    raw_target = Path(target_root).expanduser()
    if raw_target.is_symlink():
        raise ContractError(f"rollback target must not be a symlink: {raw_target}")
    target = raw_target.resolve()
    if lifecycle_token is None:
        with target_lifecycle_lock(target) as token:
            return _rollback_install(
                target,
                lifecycle_token=token,
                expected_current_lock_hash=expected_current_lock_hash,
                expected_rollback_point_fingerprint=expected_rollback_point_fingerprint,
            )
    _validate_lifecycle_token(target, lifecycle_token)
    if not _is_managed_install(target):
        raise ContractError("rollback target is not an intact managed installation")
    source = target / ".agent-skills" / ROLLBACK_POINT_DIRECTORY
    restored_point = _validate_rollback_point_directory(source)
    restored_external = _validate_external_state(source)
    external_paths = tuple(item["path"] for item in restored_external["entries"])
    restored_lock = load(source / "install-lock.json")
    current_lock = load(target / ".agent-skills" / PERSISTENT_PACKAGE_LOCK)
    if expected_current_lock_hash is not None and current_lock["fingerprint"] != expected_current_lock_hash:
        raise ContractError("rollback current Lockfile differs from the approved identity")
    if (
        expected_rollback_point_fingerprint is not None
        and restored_point["fingerprint"] != expected_rollback_point_fingerprint
    ):
        raise ContractError("rollback point differs from the approved identity")
    stage = Path(tempfile.mkdtemp(prefix=".agent-skills-stage-", dir=target))
    backup = Path(tempfile.mkdtemp(prefix=".agent-skills-backup-", dir=target))
    moved_existing: list[str] = []
    moved_new: list[str] = []
    external_mutation_started = False
    preserve_backup = False
    try:
        shutil.copyfile(source / "AGENTS.md", stage / "AGENTS.md")
        (stage / "AGENTS.md").chmod(MANAGED_FILE_MODE)
        shutil.copytree(source / "skills", stage / "skills", symlinks=False)
        (stage / "skills").chmod(MANAGED_DIRECTORY_MODE)
        system_skills = target / "skills" / ".system"
        if system_skills.is_dir() and not system_skills.is_symlink():
            shutil.copytree(system_skills, stage / "skills" / ".system", symlinks=True)
        (stage / ".agent-skills").mkdir()
        (stage / ".agent-skills").chmod(MANAGED_DIRECTORY_MODE)
        shutil.copytree(source / "packages", stage / ".agent-skills" / "packages", symlinks=False)
        (stage / ".agent-skills" / "packages").chmod(MANAGED_DIRECTORY_MODE)
        for name in ("install-lock.json", PERSISTENT_PACKAGE_LOCK):
            shutil.copyfile(source / name, stage / ".agent-skills" / name)
            (stage / ".agent-skills" / name).chmod(MANAGED_FILE_MODE)
        activation = source / EXTERNAL_ACTIVATION_LOCK
        if activation.is_file() and not activation.is_symlink():
            shutil.copyfile(activation, stage / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK)
            (stage / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK).chmod(MANAGED_FILE_MODE)
        reverse_point = _write_rollback_point(
            target,
            stage / ".agent-skills" / ROLLBACK_POINT_DIRECTORY,
            external_paths=external_paths,
        )
        _verify_staged_install(stage, restored_lock)
        for name in MANAGED_ROOTS:
            destination = target / name
            os.replace(destination, backup / name)
            moved_existing.append(name)
            os.replace(stage / name, destination)
            moved_new.append(name)
        external_mutation_started = True
        _restore_external_state(
            backup / ".agent-skills" / ROLLBACK_POINT_DIRECTORY,
            target,
        )
        if not _is_managed_install(target):
            raise ContractError("restored rollback state failed managed-install verification")
    except Exception as primary_error:
        recovery_errors: list[str] = []
        reverse_external = target / ".agent-skills" / ROLLBACK_POINT_DIRECTORY
        if external_mutation_started and reverse_external.is_dir():
            try:
                _restore_external_state(reverse_external, target)
            except (ContractError, OSError) as error:
                recovery_errors.append(f"restore external lifecycle state: {error}")
        for name in reversed(moved_new):
            path = target / name
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif _path_exists(path):
                    path.unlink()
            except OSError as error:
                recovery_errors.append(f"remove {name}: {error}")
        for name in reversed(moved_existing):
            try:
                os.replace(backup / name, target / name)
            except OSError as error:
                recovery_errors.append(f"restore {name}: {error}")
        if recovery_errors:
            preserve_backup = True
            raise ContractError(
                f"rollback failed ({primary_error}); recovery incomplete; backup preserved at {backup}: "
                + "; ".join(recovery_errors)
            ) from primary_error
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if not preserve_backup:
            shutil.rmtree(backup, ignore_errors=True)
    return {
        "from_lock_hash": current_lock["fingerprint"],
        "restored_lock_hash": restored_point["package_lock_hash"],
        "rollback_point": reverse_point,
        "status": "rolled-back",
    }


def install_bundle(
    bundle: InstallBundle,
    target_root: str | Path,
    *,
    dry_run: bool = False,
    post_install: Callable[[Path, dict[str, Any]], None] | None = None,
    persistent_rollback: bool = False,
    lifecycle_token: LifecycleLockToken | None = None,
    expected_install_fingerprint: str | None = None,
    expected_package_lock_hash: str | None = None,
    expected_rollback_point_fingerprint: str | None = None,
    persistent_rollback_external_paths: Iterable[str] = (),
) -> dict[str, Any]:
    raw_target = Path(target_root).expanduser()
    if raw_target.is_symlink():
        raise ContractError(f"install target must not be a symlink: {raw_target}")
    target = raw_target.resolve()
    if not dry_run and lifecycle_token is None:
        with target_lifecycle_lock(target) as token:
            return install_bundle(
                bundle,
                target,
                dry_run=False,
                post_install=post_install,
                persistent_rollback=persistent_rollback,
                lifecycle_token=token,
                expected_install_fingerprint=expected_install_fingerprint,
                expected_package_lock_hash=expected_package_lock_hash,
                expected_rollback_point_fingerprint=expected_rollback_point_fingerprint,
                persistent_rollback_external_paths=persistent_rollback_external_paths,
            )
    if lifecycle_token is not None:
        _validate_lifecycle_token(target, lifecycle_token)
    result = dict(bundle.plan)
    result["status"] = "planned" if dry_run else "installed"
    validate_install_plan(result)
    _preflight_install(target)
    if expected_install_fingerprint is not None or expected_package_lock_hash is not None:
        current_install = load(target / ".agent-skills" / "install-lock.json")
        current_package = load(target / ".agent-skills" / PERSISTENT_PACKAGE_LOCK)
        if (
            current_install.get("fingerprint") != expected_install_fingerprint
            or current_package.get("fingerprint") != expected_package_lock_hash
        ):
            raise ContractError("current installation differs from the approved upgrade identity")
    existing_rollback = target / ".agent-skills" / ROLLBACK_POINT_DIRECTORY
    if _path_exists(existing_rollback) and not persistent_rollback:
        raise ContractError("refusing to discard a persistent rollback point; use the upgrade lifecycle")
    if dry_run:
        return result

    target.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".agent-skills-stage-", dir=target))
    backup = Path(tempfile.mkdtemp(prefix=".agent-skills-backup-", dir=target))
    moved_existing: list[str] = []
    moved_new: list[str] = []
    preserve_backup = False
    external_mutation_started = False
    try:
        (stage / "AGENTS.md").write_text(bundle.instructions, encoding="utf-8")
        (stage / "AGENTS.md").chmod(MANAGED_FILE_MODE)
        (stage / "skills").mkdir()
        (stage / "skills").chmod(MANAGED_DIRECTORY_MODE)
        (stage / ".agent-skills" / "packages").mkdir(parents=True)
        (stage / ".agent-skills").chmod(MANAGED_DIRECTORY_MODE)
        (stage / ".agent-skills" / "packages").chmod(MANAGED_DIRECTORY_MODE)
        skill_records = {item["name"]: item for item in result["skills"]}
        for package in bundle.packages:
            package_record = next(item for item in result["packages"] if item["id"] == package.package_id)
            _copy_tree(
                package.root,
                package_record["files"],
                package_record["directories"],
                stage / ".agent-skills" / "packages" / package.package_id,
                root_mode=package_record["root_mode"],
            )
            for skill in package.skills:
                skill_record = skill_records[skill.name]
                _copy_tree(
                    skill.root,
                    skill_record["files"],
                    skill_record["directories"],
                    stage / "skills" / skill.name,
                    root_mode=skill_record["root_mode"],
                )
        existing_skills = target / "skills"
        for name in EXTERNAL_SKILL_ROOTS:
            source = existing_skills / name
            if source.is_dir() and not source.is_symlink():
                # `.system` 由 Codex 自身维护，不进入本仓 Lock；更新受管 Skills 时原样保留。
                shutil.copytree(source, stage / "skills" / name, symlinks=True)
        activation_lock = target / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK
        if activation_lock.is_file() and not activation_lock.is_symlink():
            shutil.copyfile(
                activation_lock,
                stage / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK,
            )
            (stage / ".agent-skills" / EXTERNAL_ACTIVATION_LOCK).chmod(MANAGED_FILE_MODE)
        dump(bundle.package_lock, stage / ".agent-skills" / PERSISTENT_PACKAGE_LOCK)
        (stage / ".agent-skills" / PERSISTENT_PACKAGE_LOCK).chmod(MANAGED_FILE_MODE)
        if persistent_rollback:
            point = _write_rollback_point(
                target,
                stage / ".agent-skills" / ROLLBACK_POINT_DIRECTORY,
                external_paths=persistent_rollback_external_paths,
            )
            if (
                expected_rollback_point_fingerprint is not None
                and point["fingerprint"] != expected_rollback_point_fingerprint
            ):
                raise ContractError("rollback point differs from the approved upgrade plan")
            _validate_external_target_state(
                stage / ".agent-skills" / ROLLBACK_POINT_DIRECTORY,
                target,
            )
        elif expected_rollback_point_fingerprint is not None:
            raise ContractError("rollback point approval requires persistent rollback")
        _verify_staged_install(stage, result)
        lock = dict(result)
        lock["status"] = "installed"
        dump(lock, stage / ".agent-skills" / "install-lock.json")
        (stage / ".agent-skills" / "install-lock.json").chmod(MANAGED_FILE_MODE)

        for name in MANAGED_ROOTS:
            destination = target / name
            if _path_exists(destination):
                os.replace(destination, backup / name)
                moved_existing.append(name)
            os.replace(stage / name, destination)
            moved_new.append(name)
        if post_install is not None:
            # 后置激活与 smoke 必须留在同一回滚窗口内；回调失败时恢复受管目录。
            external_mutation_started = True
            post_install(target, result)
        if not _is_managed_install(target):
            raise ContractError("installed state failed managed-install verification")
    except Exception as primary_error:
        recovery_errors: list[str] = []
        rollback_point = target / ".agent-skills" / ROLLBACK_POINT_DIRECTORY
        if external_mutation_started and rollback_point.is_dir():
            try:
                _restore_external_state(rollback_point, target)
            except (ContractError, OSError) as error:
                recovery_errors.append(f"restore external lifecycle state: {error}")
        for name in reversed(moved_new):
            path = target / name
            try:
                if _path_exists(path):
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            except OSError as error:
                recovery_errors.append(f"remove {name}: {error}")
        for name in reversed(moved_existing):
            try:
                os.replace(backup / name, target / name)
            except OSError as error:
                recovery_errors.append(f"restore {name}: {error}")
        if recovery_errors:
            preserve_backup = True
            raise ContractError(
                f"install failed ({primary_error}); rollback incomplete; recovery backup preserved at {backup}: "
                + "; ".join(recovery_errors)
            ) from primary_error
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if not preserve_backup:
            shutil.rmtree(backup, ignore_errors=True)
    return result
