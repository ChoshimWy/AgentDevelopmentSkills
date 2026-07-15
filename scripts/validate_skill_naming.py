#!/usr/bin/env python3
"""Validate repository Skill names against the cross-platform naming policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FORBIDDEN_RUNTIME_PREFIXES = {"claude-", "codex-", "gemini-", "openai-"}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def read_skill_name(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return None
    for line in lines[1:]:
        if line == "---":
            break
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError(f"invalid semantic version: {value}")
    return tuple(int(part) for part in value.split("."))


def validate_policy_shape(policy: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    def exact(value: object, keys: set[str], label: str) -> bool:
        if not isinstance(value, dict) or set(value) != keys:
            failures.append(f"{label} must contain exactly {sorted(keys)}")
            return False
        return True

    if not exact(policy, {"schema_version", "shared_orchestration", "global", "platforms"}, "policy"):
        return failures
    global_policy = policy["global"]
    if exact(
        global_policy,
        {"name_pattern", "max_length", "folder_must_match_name", "forbidden_runtime_prefixes"},
        "policy.global",
    ):
        prefixes = global_policy["forbidden_runtime_prefixes"]
        if (
            not isinstance(global_policy["name_pattern"], str)
            or not isinstance(global_policy["max_length"], int)
            or global_policy["max_length"] < 1
            or not isinstance(global_policy["folder_must_match_name"], bool)
            or not isinstance(prefixes, list)
            or any(not isinstance(item, str) or not item for item in prefixes)
            or len(prefixes) != len(set(prefixes))
        ):
            failures.append("policy.global values are invalid")
    platforms = policy["platforms"]
    if not isinstance(platforms, dict):
        failures.append("policy.platforms must be an object")
        return failures
    platform_keys = {
        "allowed_prefixes", "canonical_orchestration", "deprecated", "grandfathered", "provider_binding"
    }
    for platform_id, config in platforms.items():
        label = f"policy.platforms.{platform_id}"
        if not isinstance(platform_id, str) or not platform_id or not exact(config, platform_keys, label):
            continue
        prefixes = config["allowed_prefixes"]
        if (
            not isinstance(config["canonical_orchestration"], str)
            or not isinstance(prefixes, list)
            or not prefixes
            or any(not isinstance(item, str) or not item for item in prefixes)
            or len(prefixes) != len(set(prefixes))
            or (config["provider_binding"] is not None and not isinstance(config["provider_binding"], str))
        ):
            failures.append(f"{label} scalar values are invalid")
        for collection, keys in (
            ("grandfathered", {"name", "reason", "review_by"}),
            ("deprecated", {"name", "replacement", "remove_in"}),
        ):
            records = config[collection]
            if not isinstance(records, list):
                failures.append(f"{label}.{collection} must be an array")
                continue
            names: list[str] = []
            for index, record in enumerate(records):
                if not exact(record, keys, f"{label}.{collection}[{index}]"):
                    continue
                if any(not isinstance(record[key], str) or not record[key] for key in keys):
                    failures.append(f"{label}.{collection}[{index}] values must be non-empty strings")
                names.append(record["name"])
            if len(names) != len(set(names)):
                failures.append(f"{label}.{collection} names must be unique")
    return failures


def validate_repository(root: Path, policy_path: Path) -> list[str]:
    failures: list[str] = []
    policy = load_json(policy_path)
    failures.extend(validate_policy_shape(policy))
    if failures:
        return failures
    global_policy = policy.get("global")
    platform_policy = policy.get("platforms")
    if policy.get("schema_version") != "1.0" or not isinstance(global_policy, dict) or not isinstance(platform_policy, dict):
        return ["skill naming policy must use schema_version 1.0 with global and platforms objects"]

    try:
        name_pattern = re.compile(str(global_policy["name_pattern"]))
        max_length = int(global_policy["max_length"])
        folder_must_match = global_policy["folder_must_match_name"] is True
        forbidden_prefixes = tuple(global_policy["forbidden_runtime_prefixes"])
    except (KeyError, TypeError, ValueError, re.error) as error:
        return [f"invalid global skill naming policy: {error}"]

    if policy.get("shared_orchestration") != "workflow-orchestration":
        failures.append("shared_orchestration must be workflow-orchestration")
    if not REQUIRED_FORBIDDEN_RUNTIME_PREFIXES <= set(forbidden_prefixes):
        failures.append("global forbidden_runtime_prefixes cannot weaken the required runtime/vendor baseline")

    manifests: dict[str, tuple[Path, dict[str, Any]]] = {}
    for collection in ("platforms", "disciplines", "stacks"):
        collection_root = root / collection
        if not collection_root.is_dir():
            continue
        for manifest_path in sorted(collection_root.glob("*/manifest.json")):
            manifest = load_json(manifest_path)
            package_id = manifest.get("id")
            if isinstance(package_id, str):
                manifests[package_id] = (manifest_path.parent, manifest)

    actual_platform_ids = {
        package_id for package_id, (_, manifest) in manifests.items() if manifest.get("kind") == "platform" and package_id != "core"
    }
    if set(platform_policy) != actual_platform_ids:
        failures.append(
            "skill naming policy platform ids must exactly match platform manifests: "
            f"expected={sorted(actual_platform_ids)} actual={sorted(platform_policy)}"
        )

    seen: dict[str, Path] = {}
    skills_by_package: dict[str, dict[str, Path]] = {}
    for package_id, (package_root, manifest) in sorted(manifests.items()):
        installation = manifest.get("installation")
        skill_roots = installation.get("skill_roots", []) if isinstance(installation, dict) else []
        scan_roots = list(skill_roots)
        if (package_root / "skills").is_dir() and "skills" not in scan_roots:
            scan_roots.append("skills")
            failures.append(f"{package_id}: physical skills root must be declared in installation.skill_roots")
        package_skills: dict[str, Path] = {}
        for root_name in scan_roots:
            skills_root = package_root / root_name
            if not skills_root.is_dir():
                failures.append(f"{package_id}: skill root is missing: {root_name}")
                continue
            for skill_file in sorted(skills_root.glob("*/SKILL.md")):
                name = read_skill_name(skill_file)
                relative = skill_file.relative_to(root)
                if not name:
                    failures.append(f"{relative}: missing frontmatter name")
                    continue
                if not name_pattern.fullmatch(name) or len(name) > max_length:
                    failures.append(f"{relative}: invalid skill name: {name}")
                if folder_must_match and skill_file.parent.name != name:
                    failures.append(f"{relative}: folder must match frontmatter name {name}")
                if name in seen:
                    failures.append(f"duplicate installable skill name {name}: {seen[name]} and {relative}")
                else:
                    seen[name] = relative
                package_skills[name] = skill_file.parent
        skills_by_package[package_id] = package_skills

    shared = policy.get("shared_orchestration")
    if shared not in seen:
        failures.append(f"shared orchestration skill is missing: {shared}")

    for package_id, package_skills in sorted(skills_by_package.items()):
        config = platform_policy.get(package_id, {})
        deprecated = {
            record.get("name") for record in config.get("deprecated", []) if isinstance(record, dict)
        } if isinstance(config, dict) else set()
        grandfathered = {
            record.get("name") for record in config.get("grandfathered", []) if isinstance(record, dict)
        } if isinstance(config, dict) else set()
        for name in package_skills:
            if name.startswith(forbidden_prefixes) and name not in deprecated and name not in grandfathered:
                failures.append(f"{package_id}: runtime/vendor-prefixed Skill is forbidden: {name}")

    for platform_id in sorted(actual_platform_ids):
        package_root, manifest = manifests[platform_id]
        config = platform_policy.get(platform_id)
        if not isinstance(config, dict):
            continue
        allowed_prefixes = tuple(config.get("allowed_prefixes", []))
        grandfathered_records = config.get("grandfathered", [])
        deprecated_records = config.get("deprecated", [])
        grandfathered = {record.get("name"): record for record in grandfathered_records if isinstance(record, dict)}
        deprecated = {record.get("name"): record for record in deprecated_records if isinstance(record, dict)}
        platform_skills = skills_by_package.get(platform_id, {})

        expected_canonical = f"{platform_id}-orchestration"
        if config.get("canonical_orchestration") != expected_canonical:
            failures.append(f"{platform_id}: canonical_orchestration must be {expected_canonical}")
        if f"{platform_id}-" not in allowed_prefixes:
            failures.append(f"{platform_id}: allowed_prefixes must include {platform_id}-")

        if manifest.get("implementation_status") == "bootstrap-only" and platform_skills:
            failures.append(f"{platform_id}: bootstrap-only platform must not ship Skills")

        for name, skill_root in platform_skills.items():
            if name in deprecated:
                agent_metadata = skill_root / "agents" / "openai.yaml"
                if not agent_metadata.is_file() or "allow_implicit_invocation: false" not in agent_metadata.read_text(encoding="utf-8"):
                    failures.append(f"{platform_id}: deprecated Skill {name} must disable implicit invocation")
                continue
            if name in grandfathered:
                record = grandfathered[name]
                if not record.get("reason") or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(record.get("review_by", ""))):
                    failures.append(f"{platform_id}: grandfathered Skill {name} requires reason and review_by")
                continue
            if not any(name.startswith(prefix) for prefix in allowed_prefixes):
                failures.append(f"{platform_id}: Skill {name} must use one of prefixes {list(allowed_prefixes)}")

        try:
            package_version = version_tuple(str(manifest["version"]))
        except (KeyError, TypeError, ValueError) as error:
            failures.append(f"{platform_id}: invalid package version for naming lifecycle: {error}")
            package_version = ()
        for name, record in deprecated.items():
            replacement = record.get("replacement")
            remove_in = record.get("remove_in")
            if replacement not in platform_skills and manifest.get("implementation_status") == "implemented":
                failures.append(f"{platform_id}: deprecated Skill {name} replacement is missing: {replacement}")
            try:
                removal_version = version_tuple(str(remove_in))
            except (TypeError, ValueError) as error:
                failures.append(f"{platform_id}: invalid deprecation version for {name}: {error}")
                continue
            if package_version and package_version < removal_version and name not in platform_skills:
                failures.append(f"{platform_id}: deprecated Skill {name} must remain until {remove_in}")
            if package_version and package_version >= removal_version and name in platform_skills:
                failures.append(f"{platform_id}: deprecated Skill {name} must be removed in {remove_in}")

        canonical = config.get("canonical_orchestration")
        if manifest.get("implementation_status") == "implemented":
            if canonical not in platform_skills:
                failures.append(f"{platform_id}: canonical orchestration Skill is missing: {canonical}")
            provider_binding = config.get("provider_binding")
            installation = manifest.get("installation")
            provider_relative = installation.get("provider_manifest") if isinstance(installation, dict) else None
            if provider_binding and provider_relative:
                provider = load_json(package_root / provider_relative)
                binding = provider.get("bindings", {}).get(provider_binding)
                if not isinstance(binding, dict) or binding.get("name") != canonical:
                    failures.append(f"{platform_id}: {provider_binding} must bind canonical orchestration {canonical}")
                bound_names = {
                    item.get("name") for item in provider.get("bindings", {}).values() if isinstance(item, dict)
                }
                for deprecated_name in deprecated:
                    if deprecated_name in bound_names:
                        failures.append(f"{platform_id}: deprecated Skill must not be a Provider binding: {deprecated_name}")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args(argv)
    root = args.repository_root.resolve()
    policy_path = args.policy.resolve() if args.policy else root / "skill-naming-policy.json"
    failures = validate_repository(root, policy_path)
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("PASS Skill naming policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
