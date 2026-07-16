"""Deterministic Canonical UI IR compilation and task-scoped packet slicing."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..models import ContractError
from .contracts import (
    design_fingerprint,
    validate_canonical_ui_ir,
    validate_design_agent_packet,
    validate_design_evidence,
    validate_design_system_registry,
)


def compile_design_system_registry(
    *,
    registry_id: str,
    version: str,
    tokens: dict[str, Any],
    components: dict[str, dict[str, Any]],
    binding_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze a platform-neutral registry while keeping platform bindings external."""

    normalized_components: dict[str, dict[str, Any]] = {}
    for key in sorted(components):
        source = components[key]
        normalized_components[key] = {
            "motion": deepcopy(source.get("motion", {})),
            "slots": sorted(set(source.get("slots", []))),
            "states": sorted(set(source.get("states", []))),
            "token_refs": sorted(set(source.get("token_refs", []))),
            "variants": sorted(set(source.get("variants", []))),
        }
    body = {
        "binding_refs": sorted(set(binding_refs or [])),
        "components": normalized_components,
        "registry_id": registry_id,
        "schema_version": "1.0",
        "tokens": {key: deepcopy(tokens[key]) for key in sorted(tokens)},
        "version": version,
    }
    value = {**body, "fingerprint": design_fingerprint(body)}
    validate_design_system_registry(value)
    return value


def compile_canonical_ir(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Compile normalized screen slices; never infer missing platform-neutral facts."""

    if not evidence:
        raise ContractError("canonical-ui-ir compilation requires evidence")
    screens: dict[str, dict[str, Any]] = {}
    tokens: dict[str, Any] = {}
    unknowns: dict[str, dict[str, Any]] = {}
    interactions: dict[str, dict[str, Any]] = {}
    assets: dict[str, dict[str, Any]] = {}
    responsive: dict[str, Any] = {}
    accessibility: dict[str, Any] = {}
    refs: list[str] = []
    for item in evidence:
        validate_design_evidence(item)
        refs.append(item["evidence_id"])
        for unknown in item["unknowns"]:
            previous = unknowns.get(unknown["id"])
            if previous is not None and previous != unknown:
                raise ContractError(f"conflicting unknown contract: {unknown['id']}")
            unknowns[unknown["id"]] = deepcopy(unknown)
        for source_slice in item["slices"]:
            payload = deepcopy(source_slice["payload"])
            if source_slice["kind"] == "token":
                token_id = source_slice["id"]
                if token_id in tokens and tokens[token_id] != payload:
                    raise ContractError(f"conflicting token evidence: {token_id}")
                tokens[token_id] = payload
                continue
            if source_slice["kind"] != "screen":
                continue
            screen_id = source_slice["id"]
            screen = {
                "id": screen_id,
                "regions": sorted(payload.get("regions", []), key=lambda value: value["id"]),
                "states": sorted(set(payload.get("states", ["default"]))),
            }
            for field, destination in (("interactions", interactions), ("assets", assets)):
                for entry in payload.get(field, []):
                    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                        raise ContractError(f"canonical-ui-ir {field} entries require ids")
                    previous = destination.get(entry["id"])
                    if previous is not None and previous != entry:
                        raise ContractError(f"conflicting {field} evidence: {entry['id']}")
                    destination[entry["id"]] = deepcopy(entry)
            for field, destination in (("responsive", responsive), ("accessibility", accessibility)):
                contract = payload.get(field, {})
                if not isinstance(contract, dict):
                    raise ContractError(f"canonical-ui-ir {field} evidence must be an object")
                if screen_id in destination and destination[screen_id] != contract:
                    raise ContractError(f"conflicting {field} evidence: {screen_id}")
                destination[screen_id] = deepcopy(contract)
            for region in screen["regions"]:
                region["components"] = sorted(region.get("components", []), key=lambda value: value["id"])
                for component in region["components"]:
                    component.setdefault("provenance", deepcopy(source_slice["provenance"]))
                    for key in ("token_refs", "states"):
                        component[key] = sorted(set(component.get(key, [])))
                    component.setdefault("slots", {})
                    component.setdefault("variant", "default")
            if screen_id in screens and screens[screen_id] != screen:
                raise ContractError(f"conflicting screen evidence: {screen_id}")
            screens[screen_id] = screen
    if not screens:
        raise ContractError("canonical-ui-ir compilation requires at least one screen slice")
    body = {
        "accessibility": {key: accessibility[key] for key in sorted(accessibility)},
        "assets": [assets[key] for key in sorted(assets)],
        "evidence_refs": sorted(set(refs)),
        "interactions": [interactions[key] for key in sorted(interactions)],
        "responsive": {key: responsive[key] for key in sorted(responsive)},
        "schema_version": "1.0",
        "screens": [screens[key] for key in sorted(screens)],
        "tokens": {key: tokens[key] for key in sorted(tokens)},
        "unknowns": [unknowns[key] for key in sorted(unknowns)],
    }
    fingerprint = design_fingerprint(body)
    value = {"ir_id": f"ir-{fingerprint[-16:]}", **body, "fingerprint": fingerprint}
    validate_canonical_ui_ir(value)
    return value


def slice_agent_packet(
    ir: dict[str, Any],
    registry: dict[str, Any],
    *,
    scope_kind: str,
    scope_id: str,
    expected_inputs: list[str] | None = None,
) -> dict[str, Any]:
    """Create a minimal screen/region/component packet and mark stale input identities."""

    validate_canonical_ui_ir(ir)
    validate_design_system_registry(registry)
    blocking_unknowns = [item["id"] for item in ir["unknowns"] if item["blocking"]]
    if blocking_unknowns:
        raise ContractError(f"design packet refuses blocking unknowns: {', '.join(blocking_unknowns)}")
    selected = _select_ir_slice(ir, scope_kind, scope_id)
    component_types = sorted({
        component["type"]
        for region in selected.get("regions", [])
        for component in region.get("components", [])
    })
    registry_components = {
        key: deepcopy(registry["components"][key])
        for key in component_types
        if key in registry["components"]
    }
    token_refs = sorted({
        token
        for region in selected.get("regions", [])
        for component in region.get("components", [])
        for token in component.get("token_refs", [])
    })
    registry_slice = {
        "components": registry_components,
        "registry_id": registry["registry_id"],
        "tokens": {key: deepcopy(registry["tokens"][key]) for key in token_refs if key in registry["tokens"]},
        "version": registry["version"],
    }
    source_fingerprints = sorted([ir["fingerprint"], registry["fingerprint"]])
    stale_inputs = sorted(set(expected_inputs or []) - set(source_fingerprints))
    body = {
        "ir_slice": selected,
        "registry_slice": registry_slice,
        "schema_version": "1.0",
        "source_fingerprints": source_fingerprints,
        "stale_inputs": stale_inputs,
        "task_scope": {"id": scope_id, "kind": scope_kind},
        "unknowns": deepcopy(ir["unknowns"]),
    }
    fingerprint = design_fingerprint(body)
    value = {"packet_id": f"packet-{fingerprint[-16:]}", **body, "fingerprint": fingerprint}
    validate_design_agent_packet(value)
    return value


def validate_packet_freshness(
    packet: dict[str, Any],
    ir: dict[str, Any],
    registry: dict[str, Any],
) -> None:
    """Fail closed when a reusable Packet no longer matches current upstream identities."""

    validate_design_agent_packet(packet)
    validate_canonical_ui_ir(ir)
    validate_design_system_registry(registry)
    current = sorted([ir["fingerprint"], registry["fingerprint"]])
    if packet["source_fingerprints"] != current or packet["stale_inputs"]:
        raise ContractError("design-agent-packet is stale for current IR or Registry")


def _select_ir_slice(ir: dict[str, Any], kind: str, identity: str) -> dict[str, Any]:
    if kind == "screen":
        matches = [screen for screen in ir["screens"] if screen["id"] == identity]
        if len(matches) == 1:
            return deepcopy(matches[0])
    elif kind == "region":
        matches = [region for screen in ir["screens"] for region in screen["regions"] if region["id"] == identity]
        if len(matches) == 1:
            return {"id": identity, "regions": [deepcopy(matches[0])], "states": []}
    elif kind == "component":
        matches = [
            component
            for screen in ir["screens"]
            for region in screen["regions"]
            for component in region["components"]
            if component["id"] == identity
        ]
        if len(matches) == 1:
            return {"id": identity, "regions": [{"components": [deepcopy(matches[0])], "id": "task-slice", "layout": {"kind": "slice"}}], "states": []}
    raise ContractError(f"design packet scope is missing or ambiguous: {kind}:{identity}")
