#!/usr/bin/env python3
"""Build or check the canonical iOSAgentSkills migration audit v2 artifacts."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dump, load, sha256  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402


SOURCE_URL = "git@github.com:ChoshimWy/iOSAgentSkills.git"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_mode(path: Path) -> int:
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _with_content_digest(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["content_sha256"] = sha256(value)
    return result


def _initial_audit(source: dict[str, Any], overrides_document: dict[str, Any]) -> dict[str, Any]:
    """Create the one-time v2 baseline without constraining later relocations."""
    apple_root = ROOT / "platforms" / "apple"
    overrides = {entry["path"]: entry for entry in overrides_document["overrides"]}
    entries = []
    for item in source["files"]:
        override = overrides.get(item["path"])
        target_path = override.get("target_path", item["path"]) if override else item["path"]
        target = {
            "mode": item["mode"],
            "package": "apple",
            "path": target_path,
            "sha256": override["package_sha256"] if override else item["sha256"],
        }
        entries.append({
            "disposition": "transformed" if override else "retained",
            "reason": override["reason"] if override else None,
            "source_path": item["path"],
            "targets": [target],
        })

    return _with_content_digest({
        "additions": [],
        "entries": entries,
        "schema_version": "2.0",
        "source": {
            "commit": source["source_head"],
            "content_sha256": source["source_content_sha256"],
            "inventory": "platforms/apple/migration-source.json",
            "license": {"notice_path": None, "notice_sha256": None, "spdx": None, "status": "pending"},
            "repository": source["source_repository"],
            "repository_url": source.get("source_repository_url", SOURCE_URL),
        },
    })


def _package_catalog() -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    for collection_name in ("platforms", "disciplines", "stacks", "runtime-configs"):
        collection = ROOT / collection_name
        if not collection.is_dir():
            continue
        if collection.is_symlink():
            raise ContractError(f"migration package collection must not be a symlink: {collection_name}")
        for candidate in sorted(collection.iterdir()):
            manifest_path = candidate / "manifest.json"
            if candidate.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
                continue
            manifest = load(manifest_path)
            if not isinstance(manifest.get("installation"), dict):
                continue
            package_id = manifest.get("id")
            if not isinstance(package_id, str) or candidate.name != package_id or package_id in result:
                raise ContractError("migration package ids are invalid or ambiguous")
            result[package_id] = (candidate, manifest)
    return result


def _apply_apple_override_records(
    audit_template: dict[str, Any],
    source: dict[str, Any],
    overrides_document: dict[str, Any],
) -> dict[str, Any]:
    """Refresh audited Apple hashes from the explicit provenance records."""
    result = deepcopy(audit_template)
    source_files = {item["path"]: item for item in source["files"]}
    entries = {entry["source_path"]: entry for entry in result["entries"]}
    for override in overrides_document["overrides"]:
        path = override["path"]
        target_path = override.get("target_path", path)
        source_item = source_files.get(path)
        entry = entries.get(path)
        if source_item is None or entry is None or source_item["sha256"] != override["source_sha256"]:
            raise ContractError(f"Apple migration override source identity is stale: {path}")
        targets = [
            target for target in entry["targets"]
            if target["package"] == "apple" and target["path"] == target_path
        ]
        if len(targets) != 1:
            raise ContractError(f"Apple migration override target is missing or ambiguous: {path}")
        targets[0]["sha256"] = override["package_sha256"]
        entry["disposition"] = "transformed"
        entry["reason"] = override["reason"]

    additions = {
        (addition["target"]["package"], addition["target"]["path"]): addition
        for addition in result["additions"]
    }
    for override in overrides_document["additions"]:
        key = ("apple", override["path"])
        addition = additions.get(key)
        if addition is None:
            raise ContractError(f"Apple migration addition is missing from the audit: {override['path']}")
        addition["target"]["mode"] = override["mode"]
        addition["target"]["sha256"] = override["package_sha256"]
        addition["reason"] = override["reason"]
    return result


def _package_files(package_root: Path, roots: list[str]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for root_name in roots:
        relative_root = Path(root_name)
        if relative_root.is_absolute() or ".." in relative_root.parts or not relative_root.parts:
            raise ContractError(f"migration package root is unsafe: {root_name}")
        root = package_root / relative_root
        if root.is_symlink() or not root.is_dir():
            raise ContractError(f"migration package root is missing or unsafe: {root_name}")
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(package_root)
            if "__pycache__" in relative.parts or path.name == ".DS_Store" or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ContractError(f"migration package contains a symlink: {relative.as_posix()}")
            if path.is_file():
                files.append({
                    "mode": canonical_mode(path),
                    "path": relative.as_posix(),
                    "sha256": file_digest(path),
                })
    return files


def build_documents(audit_template: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    apple_root = ROOT / "platforms" / "apple"
    source = load(apple_root / "migration-source.json")
    overrides_document = load(apple_root / "migration-overrides.json")
    map_path = ROOT / "migration" / "ios-agent-skills-map-v2.json"
    if audit_template is None:
        audit_template = load(map_path) if map_path.is_file() else _initial_audit(source, overrides_document)
    audit_template = _apply_apple_override_records(audit_template, source, overrides_document)
    map_document = _with_content_digest({
        key: value for key, value in audit_template.items() if key != "content_sha256"
    })
    package_ids = {
        target["package"]
        for entry in map_document["entries"]
        for target in entry["targets"]
    } | {
        addition["target"]["package"] for addition in map_document["additions"]
    }
    catalog = _package_catalog()
    packages = []
    for package_id in sorted(package_ids):
        if package_id not in catalog:
            raise ContractError(f"migration target package is unknown: {package_id}")
        package_root, manifest = catalog[package_id]
        installation = manifest["installation"]
        roots = [*installation["asset_roots"], *installation["skill_roots"]]
        files = _package_files(package_root, roots)
        contract_manifest = manifest
        provider_relative = installation.get("provider_manifest")
        if provider_relative is not None:
            contract_manifest = load(package_root / provider_relative)
        capabilities = [
            {
                "binding_permission_profile": capability.get("binding_permission_profile"),
                "id": capability["id"],
                "permission_profile": capability.get("permission_profile"),
                "version": capability["version"],
            }
            for capability in contract_manifest.get("capabilities", [])
        ]
        packages.append({
            "capabilities": capabilities,
            "content_sha256": sha256(files),
            "files": files,
            "id": package_id,
            "permissions": {
                "binding_profiles": {
                    capability["id"]: capability["binding_permission_profile"]
                    for capability in capabilities
                    if capability["binding_permission_profile"] is not None
                },
                "capability_profiles": {
                    capability["id"]: capability["permission_profile"]
                    for capability in capabilities
                    if capability["permission_profile"] is not None
                },
                "package": manifest.get("permissions", {}),
            },
            "roots": roots,
            "version": manifest["version"],
        })
    inventory_document = _with_content_digest({
        "packages": packages,
        "schema_version": "1.0",
    })
    return map_document, inventory_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    migration_root = ROOT / "migration"
    map_path = migration_root / "ios-agent-skills-map-v2.json"
    inventory_path = migration_root / "current-package-inventory-v1.json"
    existing_map = load(map_path) if map_path.is_file() else None
    expected_map, expected_inventory = build_documents(existing_map)
    if args.check:
        if not map_path.is_file() or load(map_path) != expected_map:
            raise ContractError("migration relocation map is missing or stale")
        if not inventory_path.is_file() or load(inventory_path) != expected_inventory:
            raise ContractError("current package inventory is missing or stale")
        return 0
    migration_root.mkdir(parents=True, exist_ok=True)
    dump(expected_map, map_path)
    dump(expected_inventory, inventory_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
