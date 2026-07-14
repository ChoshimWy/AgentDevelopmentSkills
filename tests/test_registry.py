from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.support import MANIFESTS

from agent_workflow.canonical_json import dump
from agent_workflow.models import ContractError
from agent_workflow.registry import ManifestRegistry


def manifest(manifest_id: str, capability: str, *, requires: list[str] | None = None, conflicts: list[str] | None = None) -> dict:
    return {
        "bindings": {capability: "fixture"},
        "capabilities": [{"id": capability, "version": "1.0"}],
        "conflicts": conflicts or [],
        "detection": {"medium": [], "strong": [], "weak": []},
        "id": manifest_id,
        "kind": "adapter",
        "optional_requires": [],
        "permissions": {"detection": "repository-read-only"},
        "requires": requires or [],
        "schema_version": "1.0",
        "targets": [],
        "version": "1.0.0",
    }


class RegistryTests(unittest.TestCase):
    def test_builtin_capability_contract_is_normalized(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        contract = registry.capability_contract("implementation.apple")
        self.assertIsNotNone(contract)
        self.assertFalse(contract["idempotent"])
        self.assertEqual(contract["concurrency_keys"], ["repository-write:apple"])

    def test_missing_required_capability_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin" / "manifest.json"
            path.parent.mkdir()
            dump(manifest("plugin", "fixture.one", requires=["fixture.missing"]), path)
            with self.assertRaises(ContractError):
                ManifestRegistry.from_directory(directory)

    def test_manifest_conflict_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for manifest_id, capability, conflicts in (
                ("one", "fixture.one", ["two"]),
                ("two", "fixture.two", []),
            ):
                path = root / manifest_id / "manifest.json"
                path.parent.mkdir()
                dump(manifest(manifest_id, capability, conflicts=conflicts), path)
            with self.assertRaises(ContractError):
                ManifestRegistry.from_directory(root)

    def test_manifest_capability_dependency_cycle_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = (
                manifest("one", "fixture.one", requires=["fixture.two"]),
                manifest("two", "fixture.two", requires=["fixture.one"]),
            )
            for value in fixtures:
                path = root / value["id"] / "manifest.json"
                path.parent.mkdir()
                dump(value, path)
            with self.assertRaisesRegex(ContractError, "dependency cycle"):
                ManifestRegistry.from_directory(root)

    def test_ambiguous_provider_fails_on_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for manifest_id in ("one", "two"):
                path = root / manifest_id / "manifest.json"
                path.parent.mkdir()
                dump(manifest(manifest_id, "fixture.shared"), path)
            registry = ManifestRegistry.from_directory(root)
            with self.assertRaises(ContractError):
                registry.capability_contract("fixture.shared")


if __name__ == "__main__":
    unittest.main()
