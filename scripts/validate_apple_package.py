#!/usr/bin/env python3
"""Validate the in-tree Apple package and isolated selective-install smoke."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dumps, load, sha256  # noqa: E402
from agent_workflow.installation import build_install_bundle, install_bundle  # noqa: E402
from agent_workflow.discovery import DiscoveryEngine  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402
from agent_workflow.planning import PlanCompiler  # noqa: E402
from agent_workflow.policy import PolicyResolver  # noqa: E402
from agent_workflow.registry import ManifestRegistry  # noqa: E402


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_mode(path: Path) -> int:
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a string")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ContractError(f"{label} is unsafe")
    return relative


def _package_catalog(repository_root: Path) -> dict[str, tuple[Path, dict[str, object]]]:
    result: dict[str, tuple[Path, dict[str, object]]] = {}
    for collection in ("platforms", "disciplines", "stacks", "runtime-configs"):
        root = repository_root / collection
        if not root.is_dir():
            continue
        for candidate in sorted(root.iterdir()):
            manifest_path = candidate / "manifest.json"
            if candidate.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
                continue
            manifest = load(manifest_path)
            if not isinstance(manifest.get("installation"), dict):
                continue
            package_id = manifest.get("id")
            if not isinstance(package_id, str) or package_id in result:
                raise ContractError("migration audit package ids are invalid or ambiguous")
            result[package_id] = (candidate.resolve(), manifest)
    return result


def _without_content_digest(document: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in document.items() if key != "content_sha256"}


def _controlled_files(package_root: Path, roots: list[str]) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for root_name in roots:
        relative_root = _safe_relative(root_name, label="package inventory root")
        root = package_root / relative_root
        if root.is_symlink() or not root.is_dir():
            raise ContractError(f"migration audit controlled root is missing or unsafe: {root_name}")
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(package_root)
            if "__pycache__" in relative.parts or path.name == ".DS_Store" or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ContractError(f"migration audit controlled root contains a symlink: {relative.as_posix()}")
            if path.is_file():
                files.append({
                    "mode": canonical_mode(path),
                    "path": relative.as_posix(),
                    "sha256": file_digest(path),
                })
    return files


def validate_migration_audit(
    repository_root: Path,
    source_inventory: dict[str, object],
    audit: dict[str, object],
    current_inventory: dict[str, object],
) -> None:
    if audit.get("schema_version") != "2.0" or audit.get("content_sha256") != sha256(_without_content_digest(audit)):
        raise ContractError("migration audit v2 content hash is invalid")
    if (
        current_inventory.get("schema_version") != "1.0"
        or current_inventory.get("content_sha256") != sha256(_without_content_digest(current_inventory))
    ):
        raise ContractError("current package inventory content hash is invalid")
    source = audit.get("source")
    if not isinstance(source, dict) or (
        source.get("repository") != source_inventory.get("source_repository")
        or source.get("repository_url") != source_inventory.get("source_repository_url")
        or source.get("commit") != source_inventory.get("source_head")
        or source.get("content_sha256") != source_inventory.get("source_content_sha256")
        or source.get("inventory") != "platforms/apple/migration-source.json"
    ):
        raise ContractError("migration audit source provenance differs from immutable inventory")
    license_record = source.get("license")
    if not isinstance(license_record, dict) or license_record.get("status") not in {"pending", "verified"}:
        raise ContractError("migration audit license provenance is invalid")
    if license_record["status"] == "pending":
        if any(license_record.get(field) is not None for field in ("spdx", "notice_path", "notice_sha256")):
            raise ContractError("pending migration license must not claim verified evidence")
    else:
        if (
            not isinstance(license_record.get("spdx"), str)
            or not license_record["spdx"]
            or not isinstance(license_record.get("notice_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", license_record["notice_sha256"])
        ):
            raise ContractError("verified migration license requires SPDX and notice digest")
        notice_relative = _safe_relative(license_record.get("notice_path"), label="migration license notice path")
        notice_path = repository_root / notice_relative
        if (
            notice_path.is_symlink()
            or not notice_path.is_file()
            or file_digest(notice_path) != license_record["notice_sha256"]
        ):
            raise ContractError("verified migration license notice evidence differs")

    source_files = source_inventory.get("files")
    entries = audit.get("entries")
    additions = audit.get("additions")
    if not isinstance(source_files, list) or not isinstance(entries, list) or not isinstance(additions, list):
        raise ContractError("migration audit collections are invalid")
    source_by_path = {entry["path"]: entry for entry in source_files}
    if len(source_by_path) != len(source_files):
        raise ContractError("migration source paths are duplicated")
    entry_by_path = {entry.get("source_path"): entry for entry in entries if isinstance(entry, dict)}
    if len(entry_by_path) != len(entries) or set(entry_by_path) != set(source_by_path):
        raise ContractError("migration audit must map every source path exactly once")

    catalog = _package_catalog(repository_root)
    target_keys: set[tuple[str, str]] = set()
    expected_by_package: dict[str, dict[str, dict[str, object]]] = {}

    def validate_target(target: object) -> tuple[str, str]:
        if not isinstance(target, dict):
            raise ContractError("migration audit target must be an object")
        package_id = target.get("package")
        if not isinstance(package_id, str) or package_id not in catalog:
            raise ContractError(f"migration audit target package is unknown: {package_id}")
        package_root, manifest = catalog[package_id]
        relative = _safe_relative(target.get("path"), label="migration audit target path")
        installation = manifest["installation"]
        allowed_roots = [*installation["asset_roots"], *installation["skill_roots"]]
        if relative.parts[0] not in allowed_roots:
            raise ContractError(f"migration audit target is outside controlled roots: {relative.as_posix()}")
        key = (package_id, relative.as_posix())
        if key in target_keys:
            raise ContractError(f"migration audit target is duplicated: {package_id}:{relative.as_posix()}")
        target_keys.add(key)
        path = package_root / relative
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"migration audit target is missing or unsafe: {package_id}:{relative.as_posix()}")
        if file_digest(path) != target.get("sha256") or canonical_mode(path) != target.get("mode"):
            raise ContractError(f"migration audit target differs from record: {package_id}:{relative.as_posix()}")
        record = {
            "mode": target["mode"],
            "path": relative.as_posix(),
            "sha256": target["sha256"],
        }
        expected_by_package.setdefault(package_id, {})[relative.as_posix()] = record
        return package_id, relative.as_posix()

    for source_path, entry in entry_by_path.items():
        disposition = entry.get("disposition")
        targets = entry.get("targets")
        reason = entry.get("reason")
        if disposition not in {"retained", "relocated", "transformed", "removed"} or not isinstance(targets, list):
            raise ContractError(f"migration audit disposition is invalid: {source_path}")
        if disposition == "removed":
            if targets or not isinstance(reason, str) or not reason:
                raise ContractError(f"removed migration entry requires a reason and no targets: {source_path}")
            continue
        if not targets:
            raise ContractError(f"migration audit entry has no target: {source_path}")
        if disposition == "transformed" and (not isinstance(reason, str) or not reason):
            raise ContractError(f"transformed migration entry requires a reason: {source_path}")
        for target in targets:
            package_id, target_path = validate_target(target)
            if disposition == "retained" and (package_id != "apple" or target_path != source_path):
                raise ContractError(f"retained migration entry changed location: {source_path}")
            if disposition in {"retained", "relocated"} and (
                target["sha256"] != source_by_path[source_path]["sha256"]
                or target["mode"] != source_by_path[source_path]["mode"]
            ):
                raise ContractError(f"unchanged migration entry differs from source: {source_path}")

    for addition in additions:
        if (
            not isinstance(addition, dict)
            or not isinstance(addition.get("reason"), str)
            or not addition["reason"]
        ):
            raise ContractError("migration audit addition requires a reason")
        validate_target(addition.get("target"))

    packages = current_inventory.get("packages")
    if not isinstance(packages, list):
        raise ContractError("current package inventory packages are invalid")
    inventory_ids = [item.get("id") for item in packages if isinstance(item, dict)]
    if len(inventory_ids) != len(packages) or len(inventory_ids) != len(set(inventory_ids)):
        raise ContractError("current package inventory ids are invalid")
    if set(inventory_ids) != set(expected_by_package):
        raise ContractError("current package inventory package set differs from migration targets")
    for package in packages:
        package_id = package["id"]
        package_root, manifest = catalog[package_id]
        roots = package.get("roots")
        files = package.get("files")
        if not isinstance(roots, list) or not isinstance(files, list):
            raise ContractError(f"current package inventory is invalid: {package_id}")
        expected_roots = sorted([*manifest["installation"]["asset_roots"], *manifest["installation"]["skill_roots"]])
        if sorted(roots) != expected_roots:
            raise ContractError(f"current package inventory roots differ: {package_id}")
        contract_manifest = manifest
        provider_relative = manifest["installation"].get("provider_manifest")
        if provider_relative is not None:
            contract_manifest = load(package_root / provider_relative)
        expected_capabilities = [
            {
                "binding_permission_profile": capability.get("binding_permission_profile"),
                "id": capability["id"],
                "permission_profile": capability.get("permission_profile"),
                "version": capability["version"],
            }
            for capability in contract_manifest.get("capabilities", [])
        ]
        expected_permissions = {
            "binding_profiles": {
                capability["id"]: capability["binding_permission_profile"]
                for capability in expected_capabilities
                if capability["binding_permission_profile"] is not None
            },
            "capability_profiles": {
                capability["id"]: capability["permission_profile"]
                for capability in expected_capabilities
                if capability["permission_profile"] is not None
            },
            "package": manifest.get("permissions", {}),
        }
        if package.get("version") != manifest.get("version"):
            raise ContractError(f"current package inventory version differs: {package_id}")
        if package.get("capabilities") != expected_capabilities:
            raise ContractError(f"current package inventory capabilities differ: {package_id}")
        if package.get("permissions") != expected_permissions:
            raise ContractError(f"current package inventory permissions differ: {package_id}")
        if package.get("content_sha256") != sha256(files):
            raise ContractError(f"current package inventory package hash differs: {package_id}")
        actual = _controlled_files(package_root, roots)
        if files != actual:
            raise ContractError(f"current package inventory is stale: {package_id}")
        recorded = {item["path"]: item for item in files}
        if recorded != expected_by_package[package_id]:
            raise ContractError(f"current package inventory differs from migration targets: {package_id}")


def validate_migration_file_set(
    apple_root: Path,
    inventory: dict[str, object],
    override_document: dict[str, object],
) -> None:
    manifest = load(apple_root / "manifest.json")
    installation = manifest["installation"]
    controlled_roots = sorted([*installation["asset_roots"], *installation["skill_roots"]])
    allowed_roots = inventory.get("allowed_roots")
    if not isinstance(allowed_roots, list) or sorted(allowed_roots) != controlled_roots:
        raise ContractError("Apple migration allowed roots differ from installation roots")

    additions = override_document.get("additions")
    if not isinstance(additions, list):
        raise ContractError("Apple migration additions contract is invalid")
    inventory_paths = {entry["path"] for entry in inventory["files"]}
    addition_paths: set[str] = set()
    for entry in additions:
        if not isinstance(entry, dict):
            raise ContractError("Apple migration addition must be an object")
        relative = Path(entry.get("path", ""))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] not in allowed_roots
            or entry["path"] in inventory_paths
            or entry["path"] in addition_paths
        ):
            raise ContractError("Apple migration addition path is unsafe or duplicated")
        addition_paths.add(entry["path"])
        path = apple_root / relative
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"Apple migration addition is missing or unsafe: {entry['path']}")
        if (
            file_digest(path) != entry.get("package_sha256")
            or canonical_mode(path) != entry.get("mode")
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"]
        ):
            raise ContractError(f"Apple migration addition differs from audit record: {entry['path']}")

    actual_paths: set[str] = set()
    for root_name in allowed_roots:
        root = apple_root / root_name
        if root.is_symlink() or not root.is_dir():
            raise ContractError(f"Apple migration controlled root is missing or unsafe: {root_name}")
        for path in root.rglob("*"):
            relative = path.relative_to(apple_root)
            if "__pycache__" in relative.parts or path.name == ".DS_Store" or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ContractError(f"Apple migration controlled root contains a symlink: {relative.as_posix()}")
            if path.is_file():
                actual_paths.add(relative.as_posix())
    expected_paths = inventory_paths | addition_paths
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        raise ContractError(f"Apple migration controlled file set differs: missing={missing}, extra={extra}")


def main() -> int:
    apple_root = ROOT / "platforms" / "apple"
    try:
        inventory = load(apple_root / "migration-source.json")
        override_document = load(apple_root / "migration-overrides.json")
        audit = load(ROOT / "migration" / "ios-agent-skills-map-v2.json")
        current_inventory = load(ROOT / "migration" / "current-package-inventory-v1.json")
        files = inventory["files"]
        if inventory.get("source_repository") != "iOSAgentSkills":
            raise ContractError("Apple migration source identity is invalid")
        if inventory.get("source_content_sha256") != sha256(files):
            raise ContractError("Apple migration source inventory hash mismatch")
        override_entries = override_document.get("overrides", [])
        overrides = {entry["path"]: entry for entry in override_entries}
        if (
            override_document.get("schema_version") != "1.0"
            or not isinstance(override_document.get("additions"), list)
            or len(overrides) != len(override_entries)
        ):
            raise ContractError("Apple migration override contract is invalid")
        seen: set[str] = set()
        for entry in files:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or entry["path"] in seen:
                raise ContractError("Apple migration source inventory path is unsafe or duplicated")
            seen.add(entry["path"])
            if (
                not isinstance(entry.get("sha256"), str)
                or len(entry["sha256"]) != 64
                or entry.get("mode") not in {0o644, 0o755}
            ):
                raise ContractError(f"Apple migration source record is invalid: {entry['path']}")
        unknown_overrides = sorted(set(overrides) - seen)
        if unknown_overrides:
            raise ContractError("Apple migration overrides reference unknown source files")
        audit_entries = {entry["source_path"]: entry for entry in audit.get("entries", [])}
        for path, override in overrides.items():
            audit_entry = audit_entries.get(path)
            targets = audit_entry.get("targets", []) if isinstance(audit_entry, dict) else []
            if (
                override.get("source_sha256") != next(item["sha256"] for item in files if item["path"] == path)
                or not isinstance(override.get("reason"), str)
                or not override["reason"]
                or not isinstance(audit_entry, dict)
                or audit_entry.get("disposition") != "transformed"
                or audit_entry.get("reason") != override["reason"]
                or not any(target.get("sha256") == override.get("package_sha256") for target in targets)
            ):
                raise ContractError(f"Apple migration override differs from audit v2: {path}")
        validate_migration_audit(ROOT, inventory, audit, current_inventory)

        apple = build_install_bundle(ROOT / "platforms", platforms=["apple"])
        core = build_install_bundle(ROOT / "platforms", core_only=True)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            apple_result = install_bundle(apple, target)
            if not (target / "skills" / "ios-feature-implementation" / "SKILL.md").is_file():
                raise ContractError("isolated Apple install is missing the implementation skill")
            if (target / "AGENTS.md").read_text(encoding="utf-8") != apple.instructions:
                raise ContractError("isolated Apple install instructions are not canonical")
            installed_registry = ManifestRegistry.from_directory(target / ".agent-skills" / "packages")
            profile = DiscoveryEngine(installed_registry).discover(ROOT / "tests" / "fixtures" / "apple-app")
            policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
            installed_plan = PlanCompiler(installed_registry).compile(profile, policy)
            if installed_plan["status"] != "ready":
                raise ContractError("isolated Apple installation cannot compile a ready Apple plan")
            core_result = install_bundle(core, target)
            if any((target / "skills").iterdir()):
                raise ContractError("core-only profile retained Apple skills")
            if "platform.apple.global" in (target / "AGENTS.md").read_text(encoding="utf-8"):
                raise ContractError("core-only profile retained Apple instructions")
    except (ContractError, KeyError, OSError, ValueError) as error:
        print(dumps({"error": str(error), "status": "blocked"}), end="", file=sys.stderr)
        return 2

    print(dumps({
        "apple_install_fingerprint": apple_result["fingerprint"],
        "core_only_install_fingerprint": core_result["fingerprint"],
        "migration_file_count": len(files),
        "migration_override_count": len(overrides),
        "migration_v2_content_sha256": audit["content_sha256"],
        "installed_plan_fingerprint": installed_plan["fingerprint"],
        "source_content_sha256": inventory["source_content_sha256"],
        "status": "passed",
    }), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
