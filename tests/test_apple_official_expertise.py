from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import re

from support import ROOT
from agent_workflow.registry import ManifestRegistry


SCRIPT = ROOT / "platforms" / "apple" / "scripts" / "apple_official_expertise.py"
SPEC = importlib.util.spec_from_file_location("apple_official_expertise", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
PREVIOUS_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(MODULE)
finally:
    sys.dont_write_bytecode = PREVIOUS_DONT_WRITE_BYTECODE


def assert_schema(test: unittest.TestCase, value: object, schema: dict[str, object], pointer: str = "$") -> None:
    declared = schema.get("type")
    declared_types = [declared] if isinstance(declared, str) else declared or []
    type_checks = {
        "array": lambda item: isinstance(item, list),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "null": lambda item: item is None,
        "object": lambda item: isinstance(item, dict),
        "string": lambda item: isinstance(item, str),
    }
    if declared_types:
        test.assertTrue(any(type_checks[item](value) for item in declared_types), pointer)
    if "const" in schema:
        test.assertEqual(value, schema["const"], pointer)
    if "enum" in schema:
        test.assertIn(value, schema["enum"], pointer)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            test.assertIn(required, value, f"{pointer}/{required}")
        if schema.get("additionalProperties") is False:
            test.assertEqual(set(value) - set(properties), set(), pointer)
        for key, child in value.items():
            if key in properties:
                assert_schema(test, child, properties[key], f"{pointer}/{key}")
    elif isinstance(value, list):
        test.assertGreaterEqual(len(value), schema.get("minItems", 0), pointer)
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            test.assertEqual(len(encoded), len(set(encoded)), pointer)
        for index, child in enumerate(value):
            assert_schema(test, child, schema["items"], f"{pointer}/{index}")
    elif isinstance(value, str):
        test.assertGreaterEqual(len(value), schema.get("minLength", 0), pointer)
        if "pattern" in schema:
            test.assertIsNotNone(re.fullmatch(schema["pattern"], value), pointer)
    elif isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema:
        test.assertGreaterEqual(value, schema["minimum"], pointer)


class AppleOfficialExpertiseTests(unittest.TestCase):
    def make_skill(self, root: Path, name: str, body: str = "Use this guidance.") -> None:
        skill = root / name
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Fixture skill.\n---\n\n{body}\n",
            encoding="utf-8",
        )
        (skill / "references" / "detail.md").write_text("Fixture reference.\n", encoding="utf-8")

    def test_ready_packet_routes_known_skills_without_copying_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-specialist", "SECRET APPLE GUIDANCE")
            self.make_skill(root, "uikit-app-modernization")
            packet = MODULE.build_packet(
                root,
                xcode_version="27.0",
                xcode_build="18A123",
                sdk_major=27,
                attest_xcode_export=True,
            )
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["next_action"], "route-existing-entry")
        self.assertEqual(
            packet["capabilities"],
            ["implementation.apple.modernization", "implementation.apple.swiftui-guidance"],
        )
        self.assertNotIn("SECRET APPLE GUIDANCE", MODULE.canonical_dumps(packet))
        self.assertEqual(packet["source"]["redistribution"], "local-export-only")
        self.assertEqual(packet["source"]["kind"], "local-skill-export")
        self.assertEqual(packet["source"]["trust"]["status"], "explicit-local-attestation")
        schema = json.loads(
            (ROOT / "platforms" / "apple" / "config" / "apple-official-expertise-packet-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        assert_schema(self, packet, schema)

    def test_sdk_specific_skill_is_ineligible_without_sdk_27(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-whats-new-27")
            packet = MODULE.build_packet(
                root,
                xcode_version="27.0",
                xcode_build="18A123",
                sdk_major=26,
                attest_xcode_export=True,
            )
        self.assertEqual(packet["status"], "partial")
        self.assertEqual(packet["capabilities"], [])
        self.assertEqual(packet["skills"][0]["activation"]["reasons"], ["requires-sdk-major-27"])

    def test_unknown_skill_is_reported_but_not_activated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "test-modernizer")
            self.make_skill(root, "future-apple-specialist")
            packet = MODULE.build_packet(
                root,
                xcode_version="27.0",
                xcode_build="18A123",
                attest_xcode_export=True,
            )
        self.assertEqual(packet["status"], "partial")
        self.assertEqual(packet["next_action"], "update-routing-map")
        self.assertEqual(packet["unknown_skills"], ["future-apple-specialist"])
        self.assertEqual(packet["capabilities"], [])

    def test_missing_identity_keeps_discovered_guidance_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "c-bounds-safety")
            packet = MODULE.build_packet(root, attest_xcode_export=True)
        self.assertEqual(packet["status"], "partial")
        self.assertEqual(packet["capabilities"], [])
        self.assertEqual(packet["skills"][0]["activation"]["reasons"], ["missing-xcode-source-identity"])

    def test_content_change_invalidates_source_and_skill_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-specialist")
            before = MODULE.build_packet(
                root, xcode_version="27.0", xcode_build="18A123", attest_xcode_export=True
            )
            (root / "swiftui-specialist" / "references" / "detail.md").write_text(
                "Changed fixture.\n", encoding="utf-8"
            )
            after = MODULE.build_packet(
                root, xcode_version="27.0", xcode_build="18A123", attest_xcode_export=True
            )
        self.assertNotEqual(before["source"]["content_sha256"], after["source"]["content_sha256"])
        self.assertNotEqual(before["skills"][0]["content_sha256"], after["skills"][0]["content_sha256"])

    def test_frozen_source_hash_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-specialist")
            with self.assertRaisesRegex(MODULE.ExpertiseError, "differs from the frozen source"):
                MODULE.build_packet(
                    root,
                    xcode_version="27.0",
                    xcode_build="18A123",
                    attest_xcode_export=True,
                    expected_source_sha256="0" * 64,
                )

    def test_invalid_routing_contract_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-specialist")
            routing = json.loads(MODULE.DEFAULT_ROUTING.read_text(encoding="utf-8"))
            routing["skills"]["swiftui-specialist"]["local_routes"][0]["skill"] = "../unsafe"
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps(routing), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ExpertiseError, "local route is invalid"):
                MODULE.build_packet(
                    root,
                    routing_path=routing_path,
                    xcode_version="27.0",
                    xcode_build="18A123",
                    attest_xcode_export=True,
                )

    def test_unattested_nondefault_source_cannot_be_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-specialist")
            packet = MODULE.build_packet(root, xcode_version="27.0", xcode_build="18A123")
        self.assertEqual(packet["status"], "partial")
        self.assertEqual(packet["capabilities"], [])
        self.assertEqual(packet["source"]["trust"]["status"], "unverified")
        self.assertFalse(packet["source"]["trust"]["trusted_for_activation"])
        self.assertEqual(
            packet["skills"][0]["activation"]["reasons"],
            ["unverified-xcode-export-source"],
        )

    def test_malformed_xcode_identity_and_sdk_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-specialist")
            invalid_cases = [
                ({"xcode_version": "foo27bar", "xcode_build": "18A123"}, "Xcode version is invalid"),
                ({"xcode_version": "27.0", "xcode_build": "anything"}, "Xcode build is invalid"),
                ({"xcode_version": "27.0", "xcode_build": "18A123", "sdk_major": -1}, "SDK major is invalid"),
            ]
            for arguments, message in invalid_cases:
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(MODULE.ExpertiseError, message):
                        MODULE.build_packet(root, attest_xcode_export=True, **arguments)

    def test_symlink_in_export_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-specialist")
            link = root / "swiftui-specialist" / "references" / "unsafe.md"
            try:
                link.symlink_to(root / "swiftui-specialist" / "SKILL.md")
            except OSError as error:
                self.skipTest(f"symlink unavailable: {error}")
            with self.assertRaisesRegex(MODULE.ExpertiseError, "contains a symlink"):
                MODULE.build_packet(
                    root,
                    xcode_version="27.0",
                    xcode_build="18A123",
                    attest_xcode_export=True,
                )

    def test_cli_emits_canonical_json_and_require_ready_fails_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_skill(root, "swiftui-whats-new-27")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--source-dir", str(root), "--require-ready"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        packet = json.loads(result.stdout)
        self.assertEqual(packet["status"], "partial")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stdout, MODULE.canonical_dumps(packet))

    def test_federated_capabilities_resolve_to_existing_apple_entries(self) -> None:
        registry = ManifestRegistry.from_directory(ROOT / "platforms")
        expected = {
            "analysis.apple.official-expertise": ("scripts/apple_official_expertise.py", "inspect"),
            "automation.apple.interaction-evidence": ("ios-automation", "interaction-evidence"),
            "build.apple.security-hardening": ("xcode-build", "security-hardening"),
            "implementation.apple.c-bounds-safety": ("ios-feature-implementation", "c-bounds-safety"),
            "implementation.apple.modernization": ("ios-feature-implementation", "modernization"),
            "implementation.apple.swiftui-guidance": ("ios-feature-implementation", "swiftui-guidance"),
            "implementation.apple.test-modernization": ("ios-feature-implementation", "test-modernization"),
        }
        for capability, (name, mode) in expected.items():
            with self.subTest(capability=capability):
                resolved = registry.resolve_binding(capability, platform="apple")
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved.provider_id, "ios-agent-skills")
                self.assertEqual(resolved.binding["name"], name)
                self.assertEqual(resolved.binding["mode"], mode)

        routing = json.loads(MODULE.DEFAULT_ROUTING.read_text(encoding="utf-8"))
        provider = json.loads(
            (ROOT / "platforms" / "apple" / "provider" / "manifest.json").read_text(encoding="utf-8")
        )
        bindings = list(provider["bindings"].values())
        for official_skill, route in routing["skills"].items():
            for local_route in route["local_routes"]:
                with self.subTest(official_skill=official_skill, local_route=local_route):
                    self.assertTrue(
                        any(
                            binding.get("kind") == "skill"
                            and binding.get("name") == local_route["skill"]
                            and binding.get("mode", "default") == local_route["mode"]
                            for binding in bindings
                        ),
                        f"unbound local route: {official_skill} -> {local_route}",
                    )


if __name__ == "__main__":
    unittest.main()
