from __future__ import annotations

from pathlib import Path
import json
import shutil
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS, PROVIDERS

from agent_workflow.canonical_json import dump, load
from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.models import ContractError
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
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
    def test_source_registry_loads_shared_discipline_capabilities(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        documentation = registry.resolve_binding("documentation.html")
        git = registry.resolve_binding("git.workflow")
        self.assertIsNotNone(documentation)
        self.assertIsNotNone(git)
        self.assertEqual(documentation.provider_id, "documentation")
        self.assertEqual(documentation.binding["name"], "html-docs")
        self.assertEqual(git.provider_id, "git")
        self.assertIsNone(registry.resolve_binding("report.apple.html", platform="apple"))

    def test_builtin_capability_contract_is_normalized(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        contract = registry.capability_contract("implementation.apple")
        self.assertIsNotNone(contract)
        self.assertFalse(contract["idempotent"])
        self.assertEqual(contract["concurrency_keys"], ["repository-write:{target-root}"])

    def test_provider_core_version_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "incompatible with core"):
            ManifestRegistry.from_directory(MANIFESTS, core_version="0.3.0")

    def test_provider_permission_expansion_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            path = root / "platforms" / "apple" / "provider" / "manifest.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["capabilities"][0]["permission_profile"] = "credential-admin"
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "expands permission"):
                ManifestRegistry.from_directory(root / "platforms")

    def test_legacy_apple_provider_without_auto_cannot_produce_ready_code_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            path = root / "platforms" / "apple" / "provider" / "manifest.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["capabilities"] = [
                item for item in value["capabilities"]
                if item["id"] != "verification.apple.auto"
            ]
            value["bindings"].pop("verification.apple.auto")
            dump(value, path)
            registry = ManifestRegistry.from_directory(root / "platforms")
            profile = DiscoveryEngine(registry).discover(FIXTURES / "apple-app")
            policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
            plan = PlanCompiler(registry).compile(profile, policy)
            self.assertEqual(plan["status"], "blocked")
            self.assertIn("verification.apple.auto", plan["missing_capabilities"])

    def test_provider_cannot_reuse_another_capability_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            path = root / "platforms" / "apple" / "provider" / "manifest.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            affected = next(item for item in value["capabilities"] if item["id"] == "verification.apple.affected-tests")
            affected["permission_profile"] = "release-signing-execute"
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "capability permission"):
                ManifestRegistry.from_directory(root / "platforms")

    def test_provider_package_version_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            path = root / "platforms" / "apple" / "provider" / "manifest.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["package"]["version"] = "0.3.0"
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "outside"):
                ManifestRegistry.from_directory(root / "platforms")

    def test_provider_targets_must_match_bootstrap_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            path = root / "platforms" / "apple" / "provider" / "manifest.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["targets"] = ["android"]
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "targets are outside"):
                ManifestRegistry.from_directory(root / "platforms")

    def test_apple_provider_conflict_never_uses_discovery_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            rogue_path = root / "platforms" / "rogue" / "manifest.json"
            rogue_path.parent.mkdir()
            dump(manifest("rogue", "implementation.apple"), rogue_path)
            registry = ManifestRegistry.from_directory(root / "platforms")
            with self.assertRaisesRegex(ContractError, "ambiguous capability provider"):
                registry.resolve_binding("implementation.apple", platform="apple")

    def test_platform_filter_resolves_generic_capability_between_platform_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = json.loads((MANIFESTS / "apple" / "manifest.json").read_text(encoding="utf-8"))
            provider = json.loads((PROVIDERS / "ios-agent-skills" / "manifest.json").read_text(encoding="utf-8"))
            bootstrap["id"] = "android-alt-provider-bootstrap"
            bootstrap["provider_contract"]["package_id"] = "android-alt-agent-skills"
            bootstrap["targets"] = ["android-alt"]
            provider["id"] = "android-alt-agent-skills"
            provider["targets"] = ["android-alt"]
            provider["bindings"]["review.apple.static"] = provider["bindings"].pop("review.independent")
            next(item for item in provider["capabilities"] if item["id"] == "review.independent")["id"] = "review.apple.static"
            for capability in provider["capabilities"]:
                capability["reachability"] = "manual-only"
            provider["manual_only_capabilities"] = [
                capability["id"]
                for capability in provider["capabilities"]
                if capability["reachability"] == "manual-only"
            ]
            provider["manual_only_metadata"] = {
                capability_id: {
                    "entrypoint": provider["bindings"][capability_id]["name"],
                    "reason": "Synthetic cross-platform provider fixture remains explicit-only.",
                    "review_by": "2026-10-14",
                }
                for capability_id in provider["manual_only_capabilities"]
            }
            (root / "android-bootstrap").mkdir()
            (root / "android-provider").mkdir()
            dump(bootstrap, root / "android-bootstrap" / "manifest.json")
            dump(provider, root / "android-provider" / "manifest.json")
            registry = ManifestRegistry.from_directory(MANIFESTS, provider_roots=[root])
            apple = registry.resolve_binding("review.apple.static", platform="apple")
            android = registry.resolve_binding("review.apple.static", platform="android-alt")
            self.assertEqual(apple.provider_id, "ios-agent-skills")
            self.assertEqual(android.provider_id, "android-alt-agent-skills")

    def test_unimplemented_platforms_only_advertise_bootstrap_contracts(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        for platform in ("android", "backend", "desktop", "web"):
            with self.subTest(platform=platform):
                manifest_value = registry.by_id(platform).value
                self.assertEqual(manifest_value["implementation_status"], "bootstrap-only")
                self.assertEqual(manifest_value["capabilities"], [])
                self.assertEqual(manifest_value["bindings"], {})
                requirement = registry.bootstrap_requirement(platform)
                self.assertEqual(requirement["platform"], platform)
                self.assertEqual(requirement["provider"], f"{platform}-agent-skills")

    def test_provider_capability_without_binding_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            path = root / "platforms" / "apple" / "provider" / "manifest.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            del value["bindings"]["implementation.apple"]
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "has no binding"):
                ManifestRegistry.from_directory(root / "platforms")

    def test_provider_mode_permission_and_reachability_metadata_fail_closed(self) -> None:
        mutations = {
            "mode": "binding mode is unsupported",
            "permission": "binding permission is incompatible",
            "reachability": "capabilities lack reachability",
        }
        for mutation, expected in mutations.items():
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(MANIFESTS, root / "platforms")
                shutil.copytree(MANIFESTS.parent / "disciplines", root / "disciplines")
                path = root / "platforms" / "apple" / "provider" / "manifest.json"
                value = load(path)
                capability = next(item for item in value["capabilities"] if item["id"] == "implementation.apple")
                if mutation == "mode":
                    value["bindings"]["implementation.apple"]["mode"] = "unsupported"
                elif mutation == "permission":
                    capability["binding_permission_profile"] = "repository-read-only"
                else:
                    capability.pop("reachability")
                dump(value, path)
                with self.assertRaisesRegex(ContractError, expected):
                    ManifestRegistry.from_directory(root / "platforms")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(MANIFESTS, root / "platforms")
            shutil.copytree(MANIFESTS.parent / "disciplines", root / "disciplines")
            path = root / "platforms" / "apple" / "provider" / "manifest.json"
            value = load(path)
            capability = next(
                item for item in value["capabilities"] if item["id"] == "verification.apple.digest"
            )
            capability["reachability"] = "recipe"
            value["manual_only_capabilities"].remove("verification.apple.digest")
            value["manual_only_metadata"].pop("verification.apple.digest")
            dump(value, path)
            with self.assertRaisesRegex(ContractError, "not reachable from a recipe"):
                ManifestRegistry.from_directory(root / "platforms")

    def test_provider_cannot_omit_all_reachability_or_forge_manual_metadata(self) -> None:
        mutations = {
            "all omitted": "capabilities lack reachability",
            "manual list": "manual-only capability list is inconsistent",
            "manual metadata": "manual-only metadata is inconsistent",
        }
        for mutation, expected in mutations.items():
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(MANIFESTS, root / "platforms")
                shutil.copytree(MANIFESTS.parent / "disciplines", root / "disciplines")
                path = root / "platforms" / "apple" / "provider" / "manifest.json"
                value = load(path)
                if mutation == "all omitted":
                    for capability in value["capabilities"]:
                        capability.pop("reachability")
                elif mutation == "manual list":
                    value["manual_only_capabilities"].pop()
                else:
                    value["manual_only_metadata"].pop(next(iter(value["manual_only_metadata"])))
                dump(value, path)
                with self.assertRaisesRegex(ContractError, expected):
                    ManifestRegistry.from_directory(root / "platforms")

    def test_external_provider_bridge_remains_supported_without_in_tree_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "platforms"
            for package in MANIFESTS.iterdir():
                source = package / "manifest.json"
                if source.is_file():
                    target = root / package.name / "manifest.json"
                    target.parent.mkdir(parents=True)
                    shutil.copy2(source, target)
            registry = ManifestRegistry.from_directory(root, provider_roots=[PROVIDERS])
            resolved = registry.resolve_binding("implementation.apple", platform="apple")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.provider_id, "ios-agent-skills")

    def test_external_provider_cannot_shadow_in_tree_provider(self) -> None:
        with self.assertRaisesRegex(ContractError, "manifest ids must be unique"):
            ManifestRegistry.from_directory(MANIFESTS, provider_roots=[PROVIDERS])

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
