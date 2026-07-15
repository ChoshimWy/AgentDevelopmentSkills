from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS

from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.models import ContractError
from agent_workflow.policy import PolicyResolver, classify_task, merge_policy_layers
from agent_workflow.registry import ManifestRegistry


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = ManifestRegistry.from_directory(MANIFESTS)
        cls.engine = DiscoveryEngine(cls.registry)

    def test_apple_app(self) -> None:
        profile = self.engine.discover(FIXTURES / "apple-app")
        self.assertEqual(profile["platforms"], ["apple"])
        self.assertEqual(profile["repository"]["kind"], "single")

    def test_swift_cli_is_not_assumed_to_be_apple_platform(self) -> None:
        profile = self.engine.discover(FIXTURES / "swift-cli")
        self.assertEqual(profile["platforms"], [])
        self.assertEqual(profile["repository"]["kind"], "unknown")

    def test_apple_workspace_is_detected(self) -> None:
        profile = self.engine.discover(FIXTURES / "apple-workspace")
        self.assertEqual(profile["platforms"], ["apple"])

    def test_swift_package_requires_explicit_apple_workspace_signal(self) -> None:
        profile = self.engine.discover(FIXTURES / "apple-swift-package")
        self.assertEqual(profile["platforms"], ["apple"])

    def test_monorepo_includes_apple_module(self) -> None:
        profile = self.engine.discover(FIXTURES / "monorepo")
        self.assertIn("apple", profile["platforms"])
        self.assertTrue(any(item["platform"] == "apple" and item["path"] == "apps/ios" for item in profile["modules"]))

    def test_android_app(self) -> None:
        profile = self.engine.discover(FIXTURES / "android-app")
        self.assertEqual(profile["platforms"], ["android"])

    def test_backend_service_requires_server_signal(self) -> None:
        profile = self.engine.discover(FIXTURES / "backend-service")
        self.assertEqual(profile["platforms"], ["backend"])

    def test_tauri_combines_desktop_and_web(self) -> None:
        profile = self.engine.discover(FIXTURES / "tauri-app")
        self.assertEqual(profile["platforms"], ["desktop", "web"])
        self.assertEqual(profile["repository"]["kind"], "single")
        self.assertEqual({item["path"] for item in profile["modules"]}, {"."})
        self.assertEqual(profile["ambiguities"], [])

    def test_web_app_does_not_promote_source_files_to_modules(self) -> None:
        profile = self.engine.discover(FIXTURES / "web-app")
        self.assertEqual(profile["repository"]["kind"], "single")
        self.assertEqual([(item["path"], item["platform"]) for item in profile["modules"]], [(".", "web")])

    def test_monorepo_discovers_platforms(self) -> None:
        profile = self.engine.discover(FIXTURES / "monorepo")
        self.assertEqual(profile["platforms"], ["android", "apple", "backend", "web"])
        self.assertEqual(profile["repository"]["kind"], "monorepo")

    def test_nested_monorepo_cwd_falls_back_to_structural_root(self) -> None:
        profile = self.engine.discover(FIXTURES / "monorepo" / "apps" / "ios")
        self.assertEqual(Path(profile["repository"]["root"]), FIXTURES / "monorepo")
        self.assertEqual(profile["repository"]["kind"], "monorepo")
        self.assertEqual(
            [(item["path"], item["platform"]) for item in profile["target_modules"]],
            [("apps/ios", "apple")],
        )

    def test_structural_root_does_not_escape_unrelated_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory) / "outer"
            (outer / "apps" / "other-app").mkdir(parents=True)
            requested = outer / "misc" / "repo"
            requested.mkdir(parents=True)
            (requested / "README.md").write_text("fixture", encoding="utf-8")
            profile = self.engine.discover(requested)
            self.assertEqual(Path(profile["repository"]["root"]), requested.resolve())

    def test_root_aggregate_signal_preserves_nested_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Podfile").write_text("platform :ios", encoding="utf-8")
            project = root / "apps" / "ios" / "App.xcodeproj"
            project.mkdir(parents=True)
            (project / "project.pbxproj").write_text("// fixture", encoding="utf-8")
            profile = self.engine.discover(root, target_files=["apps/ios/Foo.swift"])
            self.assertEqual({item["path"] for item in profile["modules"]}, {".", "apps/ios"})
            self.assertEqual(
                [(item["path"], item["platform"]) for item in profile["target_modules"]],
                [("apps/ios", "apple")],
            )

    def test_target_file_selects_longest_matching_module(self) -> None:
        profile = self.engine.discover(
            FIXTURES / "monorepo", target_files=["apps/ios/Sources/Feature.swift"]
        )
        self.assertEqual(
            [(item["path"], item["platform"]) for item in profile["target_modules"]],
            [("apps/ios", "apple")],
        )
        policy = PolicyResolver().resolve(profile, "修复功能")
        self.assertEqual(policy["selected_platforms"], ["apple"])

    def test_shared_contract_selects_all_discovered_consumers_conservatively(self) -> None:
        profile = self.engine.discover(
            FIXTURES / "monorepo", target_files=["packages/api-schema/openapi.yaml"]
        )
        self.assertEqual(len(profile["shared_contracts"]), 1)
        self.assertEqual(
            sorted({item["platform"] for item in profile["target_modules"]}),
            ["android", "apple", "backend", "web"],
        )
        policy = PolicyResolver().resolve(profile, "更新共享接口")
        self.assertEqual(policy["selected_platforms"], ["android", "apple", "backend", "web"])

    def test_unknown_is_safe(self) -> None:
        profile = self.engine.discover(FIXTURES / "unknown")
        self.assertEqual(profile["platforms"], [])
        self.assertEqual(profile["repository"]["kind"], "unknown")

    def test_nested_test_fixtures_do_not_classify_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "tests" / "fixtures" / "App.xcodeproj"
            fixture.mkdir(parents=True)
            (fixture / "project.pbxproj").write_text("// fixture", encoding="utf-8")
            profile = self.engine.discover(root)
            self.assertEqual(profile["platforms"], [])

    def test_conflicting_strong_signals_are_reported(self) -> None:
        profile = self.engine.discover(FIXTURES / "ambiguous")
        self.assertEqual(profile["platforms"], ["backend", "web"])
        self.assertTrue(profile["ambiguities"])


class PolicyTests(unittest.TestCase):
    def test_explicit_platform_is_locked(self) -> None:
        profile = {"platforms": ["apple", "web"]}
        policy = PolicyResolver().resolve(profile, "修复页面", explicit_platforms=["apple"])
        self.assertEqual(policy["selected_platforms"], ["apple"])
        self.assertEqual(policy["decisions"][0]["merge_strategy"], "locked")

    def test_task_platform_precedes_discovery(self) -> None:
        profile = {"platforms": ["web"]}
        policy = PolicyResolver().resolve(profile, "修复 iOS 页面")
        self.assertEqual(policy["selected_platforms"], ["apple"])

    def test_task_classification_adds_design_and_qa(self) -> None:
        task = classify_task("实现 Figma 页面并补充 QA 测试")
        self.assertEqual(task["disciplines"], ["design", "development", "qa"])

    def test_risky_contract_terms_override_small_change_hint(self) -> None:
        task = classify_task("iOS 单文件 contract 小改动")
        self.assertEqual(task["type"], "code-risky")
        self.assertEqual(task["risk"], "high")

    def test_policy_merge_strategies_and_lock(self) -> None:
        merged, decisions = merge_policy_layers(
            [
                {"source": "core", "values": {"network": True, "tags": ["core"]}, "strategies": {"network": "deny-wins", "tags": "union"}},
                {"source": "project", "values": {"network": False, "tags": ["project"]}, "strategies": {"network": "deny-wins", "tags": "union"}},
            ]
        )
        self.assertEqual(merged, {"network": False, "tags": ["core", "project"]})
        self.assertEqual(len(decisions), 4)
        with self.assertRaises(ContractError):
            merge_policy_layers(
                [
                    {"source": "user", "values": {"device": "real"}, "strategies": {"device": "locked"}},
                    {"source": "project", "values": {"device": "simulator"}},
                ]
            )


if __name__ == "__main__":
    unittest.main()
