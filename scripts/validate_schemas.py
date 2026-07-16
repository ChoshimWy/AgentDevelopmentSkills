#!/usr/bin/env python3
"""Validate local JSON Schema structure, references and golden failures offline."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.contracts import validate  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402


VALID_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


def main() -> int:
    failures: list[str] = []
    ids: dict[str, Path] = {}
    documents: dict[Path, dict[str, Any]] = {}
    files = sorted(
        [*(ROOT / "schemas").glob("*.schema.json")]
        + [
            path
            for collection in ("disciplines", "platforms", "stacks")
            for path in (ROOT / collection).glob("*/contracts/*.schema.json")
        ]
        + list((ROOT / "platforms").glob("*/config/*.schema.json"))
    )
    for path in files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            failures.append(f"{path}: {error}")
            continue
        documents[path] = value
        if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            failures.append(f"{path}: unsupported or missing $schema")
        schema_id = value.get("$id")
        if not schema_id or schema_id in ids:
            failures.append(f"{path}: missing or duplicate $id")
        else:
            ids[schema_id] = path
        if not value.get("title"):
            failures.append(f"{path}: missing title")
        _validate_node(value, path, "$", failures)

    for path, value in documents.items():
        _validate_references(value, path, documents, ids, failures)
    _validate_negative_samples(failures)

    if failures:
        print("\n".join(f"FAIL {item}" for item in failures))
        return 1
    invalid_count = len(list((ROOT / "tests" / "golden" / "invalid").glob("*.json")))
    print(f"PASS {len(files)} schema files and {invalid_count} negative contract samples")
    return 0


def _validate_node(node: Any, path: Path, pointer: str, failures: list[str]) -> None:
    if isinstance(node, list):
        for index, child in enumerate(node):
            _validate_node(child, path, f"{pointer}/{index}", failures)
        return
    if not isinstance(node, dict):
        return
    declared_type = node.get("type")
    types = {declared_type} if isinstance(declared_type, str) else set(declared_type) if isinstance(declared_type, list) else set()
    if types and not types <= VALID_TYPES:
        failures.append(f"{path}:{pointer}: invalid type {declared_type!r}")
    if "required" in node:
        required = node["required"]
        properties = node.get("properties")
        if not isinstance(required, list) or len(required) != len(set(required)):
            failures.append(f"{path}:{pointer}: required must contain unique names")
        elif not isinstance(properties, dict) or not set(required) <= set(properties):
            failures.append(f"{path}:{pointer}: required names must exist in properties")
    if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"] or len(node["enum"]) != len(set(node["enum"]))):
        failures.append(f"{path}:{pointer}: enum must be non-empty and unique")
    if declared_type == "array" and "items" not in node:
        failures.append(f"{path}:{pointer}: array schema must define items")
    for key, child in node.items():
        _validate_node(child, path, f"{pointer}/{key}", failures)


def _validate_references(
    node: Any, path: Path, documents: dict[Path, dict[str, Any]], ids: dict[str, Path], failures: list[str],
) -> None:
    if isinstance(node, list):
        for child in node:
            _validate_references(child, path, documents, ids, failures)
        return
    if not isinstance(node, dict):
        return
    reference = node.get("$ref")
    if isinstance(reference, str):
        base, _, fragment = reference.partition("#")
        target_path = path if not base else ids.get(base, (path.parent / base).resolve())
        target = documents.get(target_path)
        if target is None:
            failures.append(f"{path}: unresolved $ref {reference}")
        elif fragment and not _json_pointer_exists(target, fragment):
            failures.append(f"{path}: unresolved $ref fragment {reference}")
    for child in node.values():
        _validate_references(child, path, documents, ids, failures)


def _json_pointer_exists(value: Any, fragment: str) -> bool:
    current = value
    for raw in fragment.lstrip("/").split("/") if fragment else []:
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _validate_negative_samples(failures: list[str]) -> None:
    for path in sorted((ROOT / "tests" / "golden" / "invalid").glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        try:
            validate(case["kind"], case["artifact"])
        except (ContractError, KeyError, TypeError, ValueError):
            continue
        failures.append(f"{path}: invalid golden sample unexpectedly passed")


if __name__ == "__main__":
    sys.exit(main())
