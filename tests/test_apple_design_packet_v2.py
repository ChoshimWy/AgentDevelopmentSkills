from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

from agent_workflow.design import (
    DesignSourceGateway,
    compile_canonical_ir,
    compile_design_system_registry,
    slice_agent_packet,
)
from agent_workflow.models import ContractError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "platforms/apple/skills/apple-design-context-compiler/scripts/compile_shared_agent_packet.py"
SPEC = importlib.util.spec_from_file_location("compile_shared_agent_packet", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class AppleDesignPacketV2Tests(unittest.TestCase):
    def packet(self) -> dict:
        evidence = DesignSourceGateway().normalize(
            source_kind="figma",
            document_id="settings-document",
            document_version="42",
            page_id="settings-page",
            node_slices=[{
                "id": "settings",
                "kind": "screen",
                "payload": {
                    "regions": [{
                        "components": [{
                            "id": "save-button", "slots": {"label": "Save"},
                            "states": ["default", "disabled"], "token_refs": ["color.action"],
                            "type": "button", "variant": "primary",
                        }],
                        "id": "footer", "layout": {"kind": "stack"},
                    }],
                    "states": ["default"],
                },
            }],
            parser_name="figma-adapter",
            parser_version="1.0.0",
        )
        ir = compile_canonical_ir([evidence])
        registry = compile_design_system_registry(
            registry_id="shared", version="1.0.0",
            tokens={"color.action": {"semantic": "action"}},
            components={"button": {"slots": ["label"], "states": ["default", "disabled"], "token_refs": ["color.action"], "variants": ["primary"], "motion": {}}},
        )
        return slice_agent_packet(ir, registry, scope_kind="component", scope_id="save-button")

    def registry(self) -> dict:
        return {"entries": [{
            "bindings": [{
                "availability": "iOS 17.0+", "declaration_hash": "sha256:" + "1" * 64,
                "framework": "SwiftUI", "id": "binding.save", "module": "SettingsUI",
                "source": "Sources/SettingsUI/SaveButton.swift", "symbol": "SaveButton",
            }],
            "design_id": "button", "provenance": {"confidence": "exact", "source": "manual-contract"},
            "status": "active",
        }]}

    def test_shared_slice_compiles_to_deterministic_apple_packet(self) -> None:
        environment = {"device": "iPhone", "minimum_os": "17.0", "platform": "iOS", "ui_framework": "SwiftUI"}
        first_packet = self.packet()
        second_packet = self.packet()
        first = MODULE.compile_apple_packet(
            first_packet, self.registry(), environment, first_packet["source_fingerprints"]
        )
        second = MODULE.compile_apple_packet(
            second_packet, self.registry(), environment, second_packet["source_fingerprints"]
        )
        self.assertEqual(first, second)
        self.assertEqual(first["bindings"][0]["symbol"], "SaveButton")
        self.assertEqual(first["task_scope"], {"id": "save-button", "kind": "component"})
        self.assertEqual(first["acceptance"]["required_appearances"], ["dark", "light"])
        tampered = deepcopy(first)
        tampered["bindings"][0]["symbol"] = "OtherButton"
        with self.assertRaisesRegex(ContractError, "identity"):
            MODULE.validate_apple_packet(tampered)

    def test_current_upstream_identities_are_required_and_declaration_hash_is_strict(self) -> None:
        packet = self.packet()
        environment = {"device": "iPhone", "minimum_os": "17.0", "platform": "iOS", "ui_framework": "SwiftUI"}
        with self.assertRaisesRegex(ContractError, "stale for current upstream"):
            MODULE.compile_apple_packet(
                packet,
                self.registry(),
                environment,
                sorted(["design-v1:" + "0" * 64, packet["source_fingerprints"][1]]),
            )
        registry = self.registry()
        registry["entries"][0]["bindings"][0]["declaration_hash"] = "not-a-hash"
        with self.assertRaisesRegex(ContractError, "declaration hash"):
            MODULE.compile_apple_packet(
                packet, registry, environment, packet["source_fingerprints"]
            )

    def test_heuristic_or_ambiguous_binding_fails_closed(self) -> None:
        registry = self.registry()
        registry["entries"][0]["provenance"]["confidence"] = "heuristic"
        packet = self.packet()
        with self.assertRaisesRegex(ContractError, "missing or ambiguous"):
            MODULE.compile_apple_packet(
                packet, registry,
                {"device": "iPhone", "minimum_os": "17.0", "platform": "iOS", "ui_framework": "SwiftUI"},
                packet["source_fingerprints"],
            )
        ambiguous = self.registry()
        ambiguous["entries"].append(deepcopy(ambiguous["entries"][0]))
        packet = self.packet()
        with self.assertRaisesRegex(ContractError, "missing or ambiguous"):
            MODULE.compile_apple_packet(
                packet, ambiguous,
                {"device": "iPhone", "minimum_os": "17.0", "platform": "iOS", "ui_framework": "SwiftUI"},
                packet["source_fingerprints"],
            )


if __name__ == "__main__":
    unittest.main()
