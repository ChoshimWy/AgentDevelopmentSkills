from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from agent_workflow.canonical_json import dump, load
from agent_workflow.models import ContractError
from agent_workflow.registry.manifests import ManifestRegistry


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "disciplines" / "design" / "contracts" / "product-design-bootstrap" / "manifest.json"
PROVIDER = ROOT / "providers" / "product-design-provider" / "manifest.json"
FIGMA_BOOTSTRAP = ROOT / "disciplines" / "design" / "contracts" / "figma-design-source-bootstrap" / "manifest.json"
FIGMA_PROVIDER = ROOT / "providers" / "figma-design-source-provider" / "manifest.json"


class ProductDesignProviderTests(unittest.TestCase):
    def registry(self, provider: dict | None = None) -> ManifestRegistry:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bootstrap").mkdir()
            dump(load(BOOTSTRAP), root / "bootstrap" / "manifest.json")
            if provider is not None:
                (root / "provider").mkdir()
                dump(provider, root / "provider" / "manifest.json")
            return ManifestRegistry.from_directory(root)

    def test_missing_provider_is_explicitly_degraded(self) -> None:
        registry = self.registry()
        self.assertIsNone(registry.resolve_binding("design.product.context"))
        requirement = registry.bootstrap_requirement("product-design")
        self.assertIsNotNone(requirement)
        self.assertEqual(requirement["provider"], "product-design-provider")
        self.assertEqual(requirement["required_capabilities"], [])

    def test_provider_capabilities_are_manual_and_structured_read_is_excluded(self) -> None:
        provider = load(PROVIDER)
        registry = self.registry(provider)
        resolved = registry.resolve_binding("design.product.audit")
        self.assertEqual(resolved.provider_id, "product-design-provider")
        self.assertIn("design.source.read", provider["does_not_provide"])
        self.assertTrue(all(item["reachability"] == "manual-only" for item in provider["capabilities"]))

    def test_internal_skill_rename_only_changes_binding(self) -> None:
        provider = load(PROVIDER)
        renamed = deepcopy(provider)
        renamed["bindings"]["design.product.audit"]["name"] = "product-design:experience-audit"
        renamed["manual_only_metadata"]["design.product.audit"]["entrypoint"] = "product-design:experience-audit"
        registry = self.registry(renamed)
        self.assertEqual(
            registry.resolve_binding("design.product.audit").binding["name"],
            "product-design:experience-audit",
        )

    def test_incompatible_or_ambiguous_provider_fails_closed(self) -> None:
        provider = load(PROVIDER)
        incompatible = deepcopy(provider)
        incompatible["package"]["version"] = "1.0.0"
        with self.assertRaisesRegex(ContractError, "outside"):
            self.registry(incompatible)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("bootstrap", "one", "two"):
                (root / name).mkdir()
            dump(load(BOOTSTRAP), root / "bootstrap" / "manifest.json")
            dump(provider, root / "one" / "manifest.json")
            duplicate = deepcopy(provider)
            duplicate["id"] = "other-product-design-provider"
            dump(duplicate, root / "two" / "manifest.json")
            with self.assertRaisesRegex(ContractError, "exactly one bootstrap"):
                ManifestRegistry.from_directory(root)

    def test_figma_read_export_and_write_permissions_cannot_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("bootstrap", "provider"):
                (root / name).mkdir()
            dump(load(FIGMA_BOOTSTRAP), root / "bootstrap" / "manifest.json")
            dump(load(FIGMA_PROVIDER), root / "provider" / "manifest.json")
            registry = ManifestRegistry.from_directory(root)
            self.assertEqual(registry.resolve_binding("design.source.read").contract["permission_profile"], "design-source-read")
            self.assertEqual(registry.resolve_binding("design.source.export").contract["permission_profile"], "design-source-export")
            self.assertEqual(registry.resolve_binding("design.source.write").contract["permission_profile"], "design-source-write")

            expanded = load(FIGMA_PROVIDER)
            read = next(item for item in expanded["capabilities"] if item["id"] == "design.source.read")
            read["permission_profile"] = "design-source-write"
            read["binding_permission_profile"] = "design-source-write"
            dump(expanded, root / "provider" / "manifest.json")
            with self.assertRaisesRegex(ContractError, "expands capability permission"):
                ManifestRegistry.from_directory(root)

    def test_source_registry_keeps_optional_provider_bootstraps_unambiguous(self) -> None:
        registry = ManifestRegistry.from_directory(ROOT / "platforms")
        self.assertEqual(registry.bootstrap_requirement("product-design")["provider"], "product-design-provider")
        self.assertEqual(registry.bootstrap_requirement("figma-design-source")["provider"], "figma-design-source-provider")


if __name__ == "__main__":
    unittest.main()
