#!/usr/bin/env python3
"""Build or check credential-free Phase 3 design source-to-Apple fixtures."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.canonical_json import dump, load, sha256  # noqa: E402
from agent_workflow.design import (  # noqa: E402
    DesignSourceGateway,
    build_ui_validation_report,
    compile_canonical_ir,
    compile_design_system_registry,
    slice_agent_packet,
)
from agent_workflow.models import ContractError  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures" / "design-phase3"
APPLE_SCRIPT = ROOT / "platforms/apple/skills/apple-design-context-compiler/scripts/compile_shared_agent_packet.py"


def build() -> dict[str, dict]:
    source = {
        "document_id": "settings-document",
        "document_version": "42",
        "node_slices": [{
            "id": "settings",
            "kind": "screen",
            "payload": {
                "accessibility": {"dynamic_type": True, "reduce_motion": True, "voice_over": True},
                "assets": [{"id": "icon.save", "semantic": "save"}],
                "interactions": [{"event": "tap", "from": "default", "id": "save", "to": "loading"}],
                "regions": [{
                    "components": [{
                        "id": "save-button",
                        "slots": {"label": "Save"},
                        "states": ["default", "disabled", "pressed"],
                        "token_refs": ["color.action.primary"],
                        "type": "button",
                        "variant": "primary",
                    }],
                    "id": "footer",
                    "layout": {"alignment": "trailing", "kind": "stack"},
                }],
                "responsive": {"compact": {"footer": "fill"}},
                "states": ["default", "loading"],
            },
        }],
        "page_id": "settings-page",
        "parser_version": "1.0.0",
    }
    gateway = DesignSourceGateway()
    evidences = {
        kind: gateway.normalize(
            source_kind=kind,
            document_id=source["document_id"],
            document_version=source["document_version"],
            page_id=source["page_id"],
            node_slices=source["node_slices"],
            parser_name=f"{kind}-adapter",
            parser_version=source["parser_version"],
        )
        for kind in ("figma", "sketch", "manual", "screenshot")
    }
    ir = compile_canonical_ir([evidences["figma"]])
    registry = compile_design_system_registry(
        registry_id="shared-default",
        version="1.0.0",
        tokens={"color.action.primary": {"semantic": "action-primary"}},
        components={"button": {
            "motion": {}, "slots": ["label"], "states": ["default", "disabled", "pressed"],
            "token_refs": ["color.action.primary"], "variants": ["primary"],
        }},
        binding_refs=["apple-settings-v1"],
    )
    packet = slice_agent_packet(ir, registry, scope_kind="component", scope_id="save-button")
    apple_registry = {"entries": [{
        "bindings": [{
            "availability": "iOS 17.0+", "declaration_hash": "sha256:" + "1" * 64,
            "framework": "SwiftUI", "id": "binding.save-button.swiftui", "module": "SettingsUI",
            "source": "Sources/SettingsUI/SaveButton.swift", "symbol": "SaveButton",
        }],
        "design_id": "button", "provenance": {"confidence": "exact", "source": "manual-contract"},
        "status": "active",
    }]}
    environment = {"device": "iPhone", "minimum_os": "17.0", "platform": "iOS", "ui_framework": "SwiftUI"}
    spec = importlib.util.spec_from_file_location("phase3_apple_packet", APPLE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    apple_packet = module.compile_apple_packet(
        packet, apple_registry, environment, packet["source_fingerprints"]
    )
    report = build_ui_validation_report(
        packet,
        environment={
            "build_fingerprint": "fixture-build-1", "locale": "en-US", "os_version": "17.0",
            "platform": "iOS", "viewport": {"height": 844, "width": 390},
        },
        checks=[{
            "classification": "none", "evidence_refs": [{
                "kind": "semantic-tree", "sha256": "2" * 64,
                "uri": "artifact://semantic/save-button",
            }],
            "kind": "semantic", "status": "passed", "target_id": "save-button",
        }, {
            "classification": "none", "evidence_refs": [{
                "kind": "screenshot", "sha256": "3" * 64,
                "uri": "artifact://screenshot/save-button",
            }, {
                "kind": "visual-diff", "sha256": "4" * 64,
                "uri": "artifact://visual-diff/save-button",
            }],
            "kind": "visual", "status": "passed", "target_id": "save-button",
        }],
    )
    graph = {
        "artifacts": {
            "apple_packet": {"fingerprint": apple_packet["fingerprint"]},
            "canonical_ir": {"fingerprint": ir["fingerprint"]},
            "design_evidence": {"content_sha256": evidences["figma"]["source"]["content_sha256"]},
            "design_registry": {"fingerprint": registry["fingerprint"]},
            "shared_packet": {"fingerprint": packet["fingerprint"]},
            "ui_report": {"content_sha256": sha256(report)},
        },
        "edges": [
            ["figma_source_slice", "design_evidence"], ["design_evidence", "canonical_ir"],
            ["canonical_ir", "shared_packet"], ["design_registry", "shared_packet"],
            ["shared_packet", "apple_packet"], ["shared_packet", "ui_report"],
        ],
        "schema_version": "1.0",
    }
    return {
        "apple-agent-packet-v2.json": apple_packet,
        "canonical-ui-ir-v1.json": ir,
        "design-agent-packet-v1.json": packet,
        "design-system-registry-v1.json": registry,
        "figma-evidence-v1.json": evidences["figma"],
        "manual-evidence-v1.json": evidences["manual"],
        "provenance-graph-v1.json": graph,
        "screenshot-evidence-v1.json": evidences["screenshot"],
        "sketch-evidence-v1.json": evidences["sketch"],
        "ui-validation-report-v1.json": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build()
    if args.check:
        failures = []
        actual_names = {path.name for path in FIXTURES.glob("*.json")} if FIXTURES.is_dir() else set()
        if actual_names != set(expected):
            failures.append("fixture file set is stale")
        for name, value in expected.items():
            path = FIXTURES / name
            if not path.is_file() or load(path) != value:
                failures.append(f"fixture differs: {name}")
        if failures:
            raise ContractError("; ".join(failures))
        print(f"PASS {len(expected)} Phase 3 design fixtures")
        return 0
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for path in FIXTURES.glob("*.json"):
        path.unlink()
    for name, value in expected.items():
        dump(value, FIXTURES / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
