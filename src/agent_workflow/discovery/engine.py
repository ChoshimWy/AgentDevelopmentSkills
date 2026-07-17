"""Read-only repository discovery with evidence-scoped module inference."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Iterable

from ..contracts import validate_project_profile
from ..models import ContractError
from ..registry import ManifestRegistry


IGNORED_DIRECTORIES = {
    ".git", ".build", ".cache", ".gradle", ".venv", "DerivedData", "Pods",
    "build", "dist", "fixtures", "node_modules", "testdata",
}
WEIGHTS = {"strong": 0.65, "medium": 0.25, "weak": 0.10}
ORTHOGONAL_PLATFORM_SETS = {frozenset({"desktop", "web"})}
MODULE_GROUP_DIRECTORIES = {"apps", "packages", "services"}
MAX_REPOSITORY_ENTRIES = 100_000
MAX_DISCOVERY_FILES = 100_000
MAX_DISCOVERY_MATCH_WORK_UNITS = 10_000_000
MAX_DISCOVERY_EVIDENCE = 100_000
MAX_DISCOVERY_PATH_BYTES = 4_096


class DiscoveryEngine:
    def __init__(self, registry: ManifestRegistry, max_depth: int = 4) -> None:
        self.registry = registry
        self.max_depth = max_depth

    def discover(
        self,
        repository: str | Path,
        *,
        target_files: Iterable[str] = (),
        changed_files: Iterable[str] = (),
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        requested = Path(repository).resolve()
        root = self._repository_root(requested)
        files = self._files(root)
        evidence: list[dict[str, Any]] = []
        modules: list[dict[str, Any]] = []
        match_work_units = 0

        for registered in self.registry.manifests:
            manifest = registered.value
            platform_id = manifest["id"]
            matches: list[dict[str, str]] = []
            for level in ("strong", "medium", "weak"):
                for pattern in manifest["detection"][level]:
                    if len(pattern.encode("utf-8")) > MAX_DISCOVERY_PATH_BYTES:
                        raise ContractError(
                            f"manifest detection pattern exceeds maximum of {MAX_DISCOVERY_PATH_BYTES} bytes"
                        )
                    for relative in files:
                        match_work_units += len(pattern) * (
                            len(relative) + len(Path(relative).name)
                        )
                        if match_work_units > MAX_DISCOVERY_MATCH_WORK_UNITS:
                            raise ContractError(
                                "repository discovery exceeds maximum of "
                                f"{MAX_DISCOVERY_MATCH_WORK_UNITS} match work units"
                            )
                        if _matches(relative, pattern):
                            if len(evidence) >= MAX_DISCOVERY_EVIDENCE:
                                raise ContractError(
                                    "repository discovery exceeds maximum of "
                                    f"{MAX_DISCOVERY_EVIDENCE} evidence entries"
                                )
                            match = {"level": level, "path": relative, "pattern": pattern}
                            matches.append(match)
                            evidence.append({"kind": "manifest-signal", "manifest": platform_id, **match})

            strong_roots = _collapse_roots(
                [_signal_root(match["path"], match["pattern"]) for match in matches if match["level"] == "strong"]
            )
            for module_path in strong_roots:
                local_matches = [match for match in matches if _belongs_to(match["path"], module_path)]
                levels = {match["level"] for match in local_matches}
                confidence = round(min(1.0, sum(WEIGHTS[level] for level in levels)), 2)
                modules.append(
                    {
                        "confidence": confidence,
                        "evidence": sorted({match["path"] for match in local_matches}),
                        "path": module_path,
                        "platform": platform_id,
                    }
                )

        module_values = sorted(modules, key=lambda item: (item["path"], item["platform"]))
        platforms = sorted({entry["platform"] for entry in module_values})
        ambiguities = _ambiguities(module_values)
        kind = _repository_kind(module_values, platforms)
        explicit = {
            "changed_files": sorted(set(changed_files)),
            "cwd": str(Path(cwd).resolve()) if cwd else str(requested),
            "target_files": sorted(set(target_files)),
        }
        shared_contracts = _shared_contracts(files)
        target_modules = _target_modules(module_values, explicit, root, shared_contracts)
        profile: dict[str, Any] = {
            "ambiguities": ambiguities,
            "evidence": sorted(evidence, key=lambda item: (item["path"], item["manifest"], item["level"])),
            "explicit_context": explicit,
            "modules": module_values,
            "platforms": platforms,
            "repository": {"kind": kind, "root": str(root)},
            "schema_version": "1.0",
            "shared_contracts": shared_contracts,
            "testing": _testing_profile(files),
            "target_modules": target_modules,
        }
        validate_project_profile(profile)
        return profile

    def _repository_root(self, requested: Path) -> Path:
        """Prefer a Git root, then a conservative monorepo-structure fallback."""

        current = requested if requested.is_dir() else requested.parent
        for candidate in (current, *current.parents):
            if candidate.name in IGNORED_DIRECTORIES:
                break
            if (candidate / ".git").exists():
                return candidate

        candidates = [current]
        for candidate in current.parents:
            if candidate.name in IGNORED_DIRECTORIES:
                break
            candidates.append(candidate)
        for candidate in candidates[1:]:
            groups = {child.name for child in candidate.iterdir() if child.is_dir()} & MODULE_GROUP_DIRECTORIES
            try:
                relative = current.relative_to(candidate)
            except ValueError:
                continue
            inside_module_group = bool(relative.parts) and relative.parts[0] in groups
            if inside_module_group and (len(groups) >= 2 or "apps" in groups):
                return candidate
        return current

    def _files(self, root: Path) -> list[str]:
        found: list[str] = []
        entry_count = 0
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entry_count += 1
                        if entry_count > MAX_REPOSITORY_ENTRIES:
                            raise ContractError(
                                "repository discovery exceeds maximum of "
                                f"{MAX_REPOSITORY_ENTRIES} directory entries"
                            )
                        path = Path(entry.path)
                        try:
                            relative = path.relative_to(root)
                        except ValueError:
                            continue
                        if len(relative.parts) > self.max_depth:
                            continue
                        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
                            continue
                        relative_text = relative.as_posix()
                        if len(relative_text.encode("utf-8")) > MAX_DISCOVERY_PATH_BYTES:
                            raise ContractError(
                                f"repository path exceeds maximum of {MAX_DISCOVERY_PATH_BYTES} bytes"
                            )
                        is_container = path.suffix in {".xcodeproj", ".xcworkspace"}
                        if entry.is_symlink():
                            if path.is_file() or is_container:
                                found.append(relative_text)
                                if len(found) > MAX_DISCOVERY_FILES:
                                    raise ContractError(
                                        "repository discovery exceeds maximum of "
                                        f"{MAX_DISCOVERY_FILES} files"
                                    )
                            continue
                        if entry.is_file(follow_symlinks=False) or is_container:
                            found.append(relative_text)
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                        if len(found) > MAX_DISCOVERY_FILES:
                            raise ContractError(
                                "repository discovery exceeds maximum of "
                                f"{MAX_DISCOVERY_FILES} files"
                            )
            except OSError as error:
                raise ContractError(
                    f"repository directory cannot be read: {directory}: {error}"
                ) from error
        return sorted(found)


def _matches(relative: str, pattern: str) -> bool:
    name = Path(relative).name
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern)


def _signal_root(relative: str, pattern: str) -> str:
    path_parts = Path(relative).parts
    pattern_parts = Path(pattern).parts
    if len(pattern_parts) > 1 and not any(any(char in part for char in "*?[") for part in pattern_parts):
        prefix_length = max(0, len(path_parts) - len(pattern_parts))
        return Path(*path_parts[:prefix_length]).as_posix() if prefix_length else "."
    return Path(relative).parent.as_posix() or "."


def _collapse_roots(roots: list[str]) -> list[str]:
    result: list[str] = []
    for candidate in sorted(set(roots), key=lambda value: (len(Path(value).parts), value)):
        if any(
            _path_contains(existing, candidate) and not _is_structural_child(existing, candidate)
            for existing in result
        ):
            continue
        result.append(candidate)
    return sorted(result)


def _is_structural_child(parent: str, child: str) -> bool:
    if parent == child or not _path_contains(parent, child):
        return False
    parent_parts = Path(parent).parts if parent != "." else ()
    child_parts = Path(child).parts
    relative_parts = child_parts[len(parent_parts):]
    return bool(relative_parts) and (
        relative_parts[0] in MODULE_GROUP_DIRECTORIES
        or (parent_parts and parent_parts[-1] in MODULE_GROUP_DIRECTORIES)
    )


def _path_contains(parent: str, child: str) -> bool:
    return parent == "." or child == parent or child.startswith(parent + "/")


def _belongs_to(relative: str, module_path: str) -> bool:
    return _path_contains(module_path, relative)


def _repository_kind(modules: list[dict[str, Any]], platforms: list[str]) -> str:
    if not modules:
        return "unknown"
    roots = {item["path"] for item in modules}
    if len(roots) == 1:
        return "single"
    if len(platforms) > 1:
        return "monorepo"
    return "multi-module"


def _ambiguities(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, list[str]] = {}
    for module in modules:
        by_path.setdefault(module["path"], []).append(module["platform"])
    result: list[dict[str, Any]] = []
    for path, platforms in sorted(by_path.items()):
        candidates = frozenset(platforms)
        if len(candidates) > 1 and candidates not in ORTHOGONAL_PLATFORM_SETS:
            result.append({"candidates": sorted(candidates), "path": path, "reason": "multiple-platform-signals"})
    return result


def _testing_profile(files: list[str]) -> dict[str, Any]:
    frameworks: list[str] = []
    joined = "\n".join(files)
    checks = {
        "XCTest": ("Tests/", ".xctestplan"), "JUnit": ("src/test",),
        "Playwright": ("playwright.config",), "pytest": ("pytest.ini", "test_"),
    }
    for framework, markers in checks.items():
        if any(marker in joined for marker in markers):
            frameworks.append(framework)
    return {"unit": {"available": bool(frameworks), "frameworks": frameworks}}


def _target_modules(
    modules: list[dict[str, Any]], explicit: dict[str, Any], root: Path,
    shared_contracts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = explicit["target_files"] or explicit["changed_files"]
    contract_paths = {item["path"] for item in shared_contracts}
    normalized_candidates = {_normalize_candidate(item) for item in candidates}
    if normalized_candidates & contract_paths:
        return sorted(modules, key=lambda item: (item["path"], item["platform"]))
    if not candidates:
        try:
            cwd_relative = Path(explicit["cwd"]).resolve().relative_to(root).as_posix()
        except ValueError:
            cwd_relative = "."
        candidates = [] if cwd_relative == "." else [cwd_relative]
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        normalized = _normalize_candidate(candidate)
        matches = [module for module in modules if _path_contains(module["path"], normalized)]
        if matches:
            longest = max(len(Path(module["path"]).parts) for module in matches)
            selected.extend(module for module in matches if len(Path(module["path"]).parts) == longest)
    unique = {(module["path"], module["platform"]): module for module in selected}
    return sorted(unique.values(), key=lambda item: (item["path"], item["platform"]))


def _normalize_candidate(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_DISCOVERY_PATH_BYTES:
        raise ContractError(f"target path exceeds maximum of {MAX_DISCOVERY_PATH_BYTES} bytes")
    normalized = value.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or (
            len(normalized) >= 2
            and normalized[0].isascii()
            and normalized[0].isalpha()
            and normalized[1] == ":"
        )
    ):
        raise ContractError(f"target path must be repository-relative: {value}")
    components = [item for item in normalized.split("/") if item not in {"", "."}]
    if ".." in components:
        raise ContractError(f"target path cannot escape repository root: {value}")
    return "/".join(components)


def _shared_contracts(files: list[str]) -> list[dict[str, Any]]:
    patterns = {
        "graphql": ("*.graphql", "schema.graphql"),
        "openapi": ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json"),
        "protobuf": ("*.proto",),
    }
    contracts: list[dict[str, Any]] = []
    for relative in files:
        for kind, kind_patterns in patterns.items():
            if any(_matches(relative, pattern) for pattern in kind_patterns):
                contracts.append({"consumer_resolution": "conservative-all-modules", "kind": kind, "path": relative})
                break
    return contracts
