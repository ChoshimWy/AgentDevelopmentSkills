"""Deterministic, platform-selective Agent Skills installation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Iterable

from . import __version__ as CORE_VERSION
from .canonical_json import dump, load, sha256
from .contracts import validate_install_plan, validate_manifest
from .models import ContractError
from .registry.manifests import ManifestRegistry, RegisteredManifest
from .registry.versions import satisfies


MANAGER_ID = "agent-development-skills"
MANAGED_HEADER = "<!-- agent-development-skills:managed instructions-v1 -->"
MANAGED_ROOTS = ("AGENTS.md", "skills", ".agent-skills")
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
    for path in sorted(paths):
        relative = path.relative_to(package_root).as_posix()
        entries.append({"path": relative, "sha256": _file_digest(path), "mode": _canonical_source_file_mode(path)})
    return tuple(entries)


def _directories_for_files(root: Path, files: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    relative_paths: set[Path] = set()
    for entry in files:
        parent = Path(entry["path"]).parent
        while parent.parts:
            relative_paths.add(parent)
            parent = parent.parent
    return tuple(
        {
            "path": relative.as_posix(),
            "mode": 0o755,
        }
        for relative in sorted(relative_paths)
    )


def _snapshot_tree(root: Path, *, ignore_source_cache: bool = False) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"install tree is missing or unsafe: {root}")
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
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
    # 安装前即执行与运行时 Registry 相同的兼容、权限、side-effect 和必需能力门禁。
    ManifestRegistry(registry_entries, core_version=CORE_VERSION)

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
    instructions = MANAGED_HEADER + "\n# 全局 Agent Instructions\n\n"
    instructions += "> 此文件由 `agent-skills install` 确定性生成；请在源 Fragment 中修改。\n\n"
    for fragment in fragments:
        content = fragment_text[fragment["id"]]
        if not content:
            continue
        instructions += (
            f"<!-- fragment:{fragment['id']} scope={fragment['scope']} sha256={fragment['sha256']} -->\n"
            f"{content}\n\n"
        )
    if effective_rules:
        instructions += "## Effective Rules\n\n"
        for rule in effective_rules:
            instructions += (
                f"<!-- rule:{rule['id']} effect={rule['effect']} -->\n"
                f"{rule['content']}\n"
            )
        instructions += "\n"

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

    for package in packages:
        binding_source = package.provider or package.manifest
        for capability_id, raw_binding in binding_source.get("bindings", {}).items():
            binding = raw_binding if isinstance(raw_binding, dict) else {"kind": "skill", "name": raw_binding}
            if not _binding_target_exists(binding, packages=packages, skill_names=skill_names):
                raise ContractError(
                    f"installation binding target is missing from dependency closure: "
                    f"{capability_id} -> {binding.get('kind', 'skill')}:{binding.get('name')}"
                )

    package_records = []
    bindings: dict[str, dict[str, Any]] = {}
    permission_profiles: set[str] = set()
    side_effects: set[str] = set()
    for package in packages:
        binding_source = package.provider or package.manifest
        for capability_id, binding in sorted(binding_source.get("bindings", {}).items()):
            if capability_id in bindings:
                raise ContractError(f"installation binding conflict: {capability_id}")
            bindings[capability_id] = {"binding": binding, "package": package.package_id}
        for capability in binding_source["capabilities"]:
            permission, effects = _capability_install_effects(binding_source, capability)
            permission_profiles.add(permission)
            side_effects.update(effects)
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
    package_version_by_id = {package.package_id: _package_version(package.manifest) for package in packages}
    capability_providers = {
        capability_id: {
            "binding": record["binding"],
            "package": record["package"],
            "package_version": package_version_by_id[record["package"]],
            "source_sha256": package_record_by_id[record["package"]]["files_sha256"],
        }
        for capability_id, record in sorted(bindings.items())
    }
    asset_allowlist = [
        {"mode": file["mode"], "package": package["id"], "path": file["path"], "sha256": file["sha256"]}
        for package in package_records
        for file in package["files"]
    ]

    public_fragments = [
        {key: item[key] for key in ("id", "merge_strategy", "order", "package", "path", "scope", "sha256")}
        for item in fragments
    ]
    plan = {
        "bindings": bindings,
        "capability_providers": capability_providers,
        "core_version": CORE_VERSION,
        "instructions": {
            "fragments": public_fragments,
            "path": "AGENTS.md",
            "rule_trace": rule_trace,
            "sha256": _bytes_digest(instructions.encode("utf-8")),
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
        "permission_profiles": sorted(permission_profiles),
        "schema_version": "1.0",
        "resolved_dependencies": list(resolved.dependencies),
        "selected_disciplines": list(resolved.selected_disciplines),
        "selected_runtime_configs": list(resolved.selected_runtime_configs),
        "selected_packages": [
            {
                "id": package.package_id,
                "kind": "core" if package.package_id == "core" else package.manifest["kind"],
                "selection_reasons": list(resolved.selection_reasons[package.package_id]),
                "source_sha256": package_record_by_id[package.package_id]["files_sha256"],
                "version": _package_version(package.manifest),
            }
            for package in packages
        ],
        "selected_platforms": list(selected),
        "side_effects": sorted(side_effects),
        "skills": skills,
        "status": "planned",
    }
    plan["fingerprint"] = sha256({key: value for key, value in plan.items() if key != "status"})
    validate_install_plan(plan)
    return InstallBundle(plan=plan, instructions=instructions, packages=packages)


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
        if sorted(item.name for item in managed_directory.iterdir()) != ["install-lock.json", "packages"]:
            return False
        packages_root = managed_directory / "packages"
        if packages_root.is_symlink() or not packages_root.is_dir():
            return False
        if packages_root.stat().st_mode & 0o777 != MANAGED_DIRECTORY_MODE:
            return False
        installed_package_ids = sorted(item.name for item in packages_root.iterdir())
        expected_package_ids = sorted(item["id"] for item in lock["packages"])
        if installed_package_ids != expected_package_ids:
            return False
        for item in lock["packages"]:
            directory = packages_root / item["id"]
            if directory.is_symlink() or not directory.is_dir():
                return False
            if directory.stat().st_mode & 0o777 != item["root_mode"]:
                return False
            files, directories = _snapshot_tree(directory)
            if not _tree_matches_record(files, directories, item, digest_field="files_sha256"):
                return False
        installed_names = sorted(item.name for item in skills_root.iterdir())
        expected_names = sorted(item["name"] for item in lock["skills"])
        if installed_names != expected_names:
            return False
        for item in lock["skills"]:
            directory = skills_root / item["name"]
            if directory.is_symlink() or not directory.is_dir():
                return False
            if directory.stat().st_mode & 0o777 != item["root_mode"]:
                return False
            files, directories = _snapshot_tree(directory)
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


def install_bundle(bundle: InstallBundle, target_root: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    raw_target = Path(target_root).expanduser()
    if raw_target.is_symlink():
        raise ContractError(f"install target must not be a symlink: {raw_target}")
    target = raw_target.resolve()
    result = dict(bundle.plan)
    result["status"] = "planned" if dry_run else "installed"
    validate_install_plan(result)
    _preflight_install(target)
    if dry_run:
        return result

    target.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".agent-skills-stage-", dir=target))
    backup = Path(tempfile.mkdtemp(prefix=".agent-skills-backup-", dir=target))
    moved_existing: list[str] = []
    moved_new: list[str] = []
    preserve_backup = False
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
    except Exception as primary_error:
        recovery_errors: list[str] = []
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
