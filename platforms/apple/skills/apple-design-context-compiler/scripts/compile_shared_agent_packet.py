#!/usr/bin/env python3
"""Bind a shared task-scoped Design Agent Packet to exact Apple symbols."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dump, load, sha256  # noqa: E402
from agent_workflow.design.contracts import validate_design_agent_packet  # noqa: E402
from agent_workflow.models import ContractError  # noqa: E402


def compile_apple_packet(
    shared_packet: dict[str, Any],
    registry: dict[str, Any],
    environment: dict[str, Any],
    current_source_fingerprints: list[str],
) -> dict[str, Any]:
    validate_design_agent_packet(shared_packet)
    if current_source_fingerprints != sorted(set(current_source_fingerprints)):
        raise ContractError("Apple binding current source fingerprints must be sorted and unique")
    if current_source_fingerprints != shared_packet["source_fingerprints"]:
        raise ContractError("Apple binding refuses a packet that is stale for current upstream identities")
    if shared_packet["stale_inputs"]:
        raise ContractError("Apple binding refuses a stale shared packet")
    blocking = [item["id"] for item in shared_packet["unknowns"] if item["blocking"]]
    if blocking:
        raise ContractError(f"Apple binding refuses blocking unknowns: {', '.join(blocking)}")
    _validate_environment(environment)
    entries = registry.get("entries")
    if not isinstance(entries, list):
        raise ContractError("Apple component registry entries must be an array")
    by_design: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("design_id"), str):
            raise ContractError("Apple component registry entry is invalid")
        by_design.setdefault(entry["design_id"], []).append(entry)

    bindings = []
    components = _components(shared_packet["ir_slice"])
    for component in components:
        candidates = [
            entry
            for entry in by_design.get(component["type"], [])
            if entry.get("status") == "active"
            and entry.get("provenance") == {"confidence": "exact", "source": "manual-contract"}
        ]
        if len(candidates) != 1:
            raise ContractError(f"Apple binding is missing or ambiguous for component type: {component['type']}")
        platform_bindings = [
            binding
            for binding in candidates[0].get("bindings", [])
            if binding.get("framework") == environment["ui_framework"]
        ]
        if len(platform_bindings) != 1:
            raise ContractError(f"Apple framework binding is missing or ambiguous for component type: {component['type']}")
        binding = platform_bindings[0]
        required = {"id", "framework", "symbol", "module", "source", "availability", "declaration_hash"}
        if not required <= set(binding) or any(not isinstance(binding[key], str) or not binding[key] for key in required):
            raise ContractError("Apple framework binding fields are invalid")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", binding["declaration_hash"]) is None:
            raise ContractError("Apple framework binding declaration hash is invalid")
        bindings.append({
            "availability": binding["availability"],
            "binding_id": binding["id"],
            "component_id": component["id"],
            "declaration_hash": binding["declaration_hash"],
            "framework": binding["framework"],
            "module": binding["module"],
            "source": binding["source"],
            "symbol": binding["symbol"],
        })
    body = {
        "acceptance": {
            "required_appearances": ["dark", "light"],
            "required_accessibility": ["dynamic-type", "reduce-motion", "voice-over"],
            "required_input_modes": ["focus", "keyboard", "touch"],
        },
        "bindings": sorted(bindings, key=lambda item: item["component_id"]),
        "apple_registry_fingerprint": f"apple-registry-v1:{sha256(registry)}",
        "current_source_fingerprints": deepcopy(current_source_fingerprints),
        "environment": deepcopy(environment),
        "packet_version": "2.0.0",
        "shared_packet": deepcopy(shared_packet),
        "shared_packet_fingerprint": shared_packet["fingerprint"],
        "task_scope": deepcopy(shared_packet["task_scope"]),
    }
    fingerprint = f"apple-design-v2:{sha256(body)}"
    value = {
        **body,
        "fingerprint": fingerprint,
        "packet_id": f"apple-packet-{fingerprint[-16:]}",
    }
    validate_apple_packet(value)
    return value


def validate_apple_packet(value: dict[str, Any]) -> None:
    fields = {
        "acceptance", "apple_registry_fingerprint", "bindings", "current_source_fingerprints",
        "environment", "fingerprint", "packet_id", "packet_version", "shared_packet",
        "shared_packet_fingerprint", "task_scope",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError("Apple Agent Packet v2 fields are invalid")
    if value["packet_version"] != "2.0.0":
        raise ContractError("Apple Agent Packet version is invalid")
    validate_design_agent_packet(value["shared_packet"])
    if value["shared_packet_fingerprint"] != value["shared_packet"]["fingerprint"]:
        raise ContractError("Apple Agent Packet shared fingerprint is stale")
    if value["current_source_fingerprints"] != value["shared_packet"]["source_fingerprints"]:
        raise ContractError("Apple Agent Packet current upstream identities are stale")
    if value["current_source_fingerprints"] != sorted(set(value["current_source_fingerprints"])):
        raise ContractError("Apple Agent Packet current upstream identities are invalid")
    if not isinstance(value["apple_registry_fingerprint"], str) or re.fullmatch(
        r"apple-registry-v1:[0-9a-f]{64}", value["apple_registry_fingerprint"]
    ) is None:
        raise ContractError("Apple Agent Packet registry fingerprint is invalid")
    if value["task_scope"] != value["shared_packet"]["task_scope"]:
        raise ContractError("Apple Agent Packet task scope differs from the shared Packet")
    _validate_environment(value["environment"])
    expected_components = [item["id"] for item in _components(value["shared_packet"]["ir_slice"])]
    bindings = value["bindings"]
    if not isinstance(bindings, list) or [item.get("component_id") for item in bindings] != expected_components:
        raise ContractError("Apple Agent Packet bindings do not cover the shared component slice")
    required_binding_fields = {
        "availability", "binding_id", "component_id", "declaration_hash",
        "framework", "module", "source", "symbol",
    }
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != required_binding_fields:
            raise ContractError("Apple Agent Packet binding fields are invalid")
        if binding["framework"] != value["environment"]["ui_framework"]:
            raise ContractError("Apple Agent Packet binding framework differs from environment")
        if any(not isinstance(binding[key], str) or not binding[key] for key in required_binding_fields):
            raise ContractError("Apple Agent Packet binding values are invalid")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", binding["declaration_hash"]) is None:
            raise ContractError("Apple Agent Packet declaration hash is invalid")
    acceptance = value["acceptance"]
    if acceptance != {
        "required_appearances": ["dark", "light"],
        "required_accessibility": ["dynamic-type", "reduce-motion", "voice-over"],
        "required_input_modes": ["focus", "keyboard", "touch"],
    }:
        raise ContractError("Apple Agent Packet acceptance matrix is invalid")
    body = {key: deepcopy(value[key]) for key in fields - {"fingerprint", "packet_id"}}
    expected_fingerprint = f"apple-design-v2:{sha256(body)}"
    if value["fingerprint"] != expected_fingerprint or value["packet_id"] != f"apple-packet-{expected_fingerprint[-16:]}":
        raise ContractError("Apple Agent Packet identity is invalid")


def _components(ir_slice: dict[str, Any]) -> list[dict[str, Any]]:
    result = [
        component
        for region in ir_slice.get("regions", [])
        for component in region.get("components", [])
    ]
    if not result:
        raise ContractError("Apple binding requires components in the shared packet slice")
    ids = [item.get("id") for item in result]
    if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
        raise ContractError("Apple packet component ids are invalid or ambiguous")
    return sorted(result, key=lambda item: item["id"])


def _validate_environment(value: Any) -> None:
    required = {"platform", "ui_framework", "minimum_os", "device"}
    if not isinstance(value, dict) or set(value) != required:
        raise ContractError("Apple binding environment fields are invalid")
    if value["platform"] not in {"iOS", "iPadOS", "macOS", "tvOS", "watchOS", "visionOS"}:
        raise ContractError("Apple binding platform is invalid")
    if value["ui_framework"] not in {"UIKit", "SwiftUI"}:
        raise ContractError("Apple binding framework is invalid")
    if any(not isinstance(value[key], str) or not value[key] for key in required):
        raise ContractError("Apple binding environment values are invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--environment", required=True, help="JSON file")
    parser.add_argument("--current-source-fingerprints", required=True, help="JSON array file")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = compile_apple_packet(
            load(args.packet),
            load(args.registry),
            load(args.environment),
            load(args.current_source_fingerprints),
        )
        dump(result, args.output)
    except (ContractError, OSError, json.JSONDecodeError) as error:
        print(f"BLOCKED {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
