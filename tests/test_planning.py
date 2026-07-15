from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS

from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
from agent_workflow.registry import ManifestRegistry


class PlanCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = ManifestRegistry.from_directory(MANIFESTS)
        cls.discovery = DiscoveryEngine(cls.registry)
        cls.compiler = PlanCompiler(cls.registry)

    def test_apple_plan_is_ready_and_deterministic(self) -> None:
        profile = self.discovery.discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        first = self.compiler.compile(profile, policy)
        second = self.compiler.compile(profile, policy)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["missing_capabilities"], [])
        implementation = next(node for node in first["nodes"] if node["capability"] == "implementation.apple")
        self.assertEqual(implementation["resource_keys"], ["repository-write:{target-root}"])
        self.assertFalse(implementation["idempotent"])
        self.assertEqual(implementation["provider"], "ios-agent-skills")
        self.assertEqual(implementation["binding"]["name"], "ios-feature-implementation")
        self.assertRegex(implementation["provider_manifest_digest"], r"^[0-9a-f]{64}$")

    def test_apple_plan_uses_in_tree_provider_by_default(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        profile = DiscoveryEngine(registry).discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        plan = PlanCompiler(registry).compile(profile, policy)
        self.assertEqual(plan["status"], "ready")
        implementation = next(node for node in plan["nodes"] if node["capability"] == "implementation.apple")
        self.assertEqual(implementation["provider"], "ios-agent-skills")

    def test_disabled_apple_provider_is_blocked(self) -> None:
        registry = ManifestRegistry.from_directory(
            MANIFESTS, disabled_providers=["ios-agent-skills"]
        )
        profile = DiscoveryEngine(registry).discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        self.assertEqual(PlanCompiler(registry).compile(profile, policy)["status"], "blocked")

    def test_binding_change_invalidates_plan_fingerprint(self) -> None:
        profile = self.discovery.discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        baseline = self.compiler.compile(profile, policy)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "platforms"
            shutil.copytree(MANIFESTS, copied)
            path = copied / "apple" / "provider" / "manifest.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["bindings"]["implementation.apple"]["mode"] = "business"
            capability = next(
                item for item in value["capabilities"] if item["id"] == "implementation.apple"
            )
            capability["supported_modes"] = ["business"]
            path.write_text(json.dumps(value), encoding="utf-8")
            registry = ManifestRegistry.from_directory(copied)
            changed = PlanCompiler(registry).compile(profile, policy)
        self.assertNotEqual(baseline["fingerprint"], changed["fingerprint"])
        self.assertNotEqual(baseline["registry_fingerprint"], changed["registry_fingerprint"])

    def test_generic_review_is_shared_without_leaking_apple_extension_to_android(self) -> None:
        profile = self.discovery.discover(FIXTURES / "android-app")
        policy = PolicyResolver().resolve(profile, "实现 Android 功能")
        plan = self.compiler.compile(profile, policy)
        review = next(node for node in plan["nodes"] if node["id"] == "review")
        self.assertEqual(review["provider"], "review")
        self.assertNotIn("review.independent", plan["missing_capabilities"])
        self.assertNotIn("review.apple.static", [node["capability"] for node in plan["nodes"]])

    def test_unimplemented_platforms_are_bootstrap_required_without_phantom_bindings(self) -> None:
        profile = self.discovery.discover(FIXTURES / "unknown")
        tasks = {
            "code": "实现功能",
            "doc": "分析文档",
            "qa": "执行回归测试",
        }
        for platform in ("android", "backend", "desktop", "web"):
            for task_kind, task in tasks.items():
                with self.subTest(platform=platform, task=task_kind):
                    policy = PolicyResolver().resolve(profile, task, explicit_platforms=[platform])
                    plan = self.compiler.compile(profile, policy)
                    self.assertEqual(plan["status"], "blocked")
                    self.assertEqual(len(plan["bootstrap_required"]), 1)
                    requirement = plan["bootstrap_required"][0]
                    self.assertEqual(requirement["platform"], platform)
                    self.assertEqual(requirement["provider"], f"{platform}-agent-skills")
                    platform_nodes = [
                        node
                        for node in plan["nodes"]
                        if node["capability"].startswith((f"analysis.{platform}", f"implementation.{platform}", f"verification.{platform}"))
                    ]
                    self.assertTrue(platform_nodes)
                    self.assertTrue(all(node["provider"] is None for node in platform_nodes))
                    self.assertTrue(all(node["binding"] is None for node in platform_nodes))

    def test_review_only_plan_keeps_review_mandatory(self) -> None:
        profile = self.discovery.discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "审查 iOS 代码")
        plan = self.compiler.compile(profile, policy)
        review = next(node for node in plan["nodes"] if node["id"] == "review")
        self.assertTrue(review["mandatory"])

    def test_design_and_platform_verification_are_composed_without_duplicate_qa(self) -> None:
        profile = self.discovery.discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS UI 设计并补测试")
        plan = self.compiler.compile(profile, policy)
        capabilities = [node["capability"] for node in plan["nodes"]]
        self.assertIn("design.system", capabilities)
        self.assertIn("design.ir.compile", capabilities)
        self.assertIn("design.apple.binding", capabilities)
        self.assertIn("design.apple.source", capabilities)
        self.assertIn("verification.apple.affected-tests", capabilities)
        self.assertIn("verification.apple.auto", capabilities)
        self.assertNotIn("qa.targeted", capabilities)
        self.assertIn("review.independent", capabilities)
        self.assertEqual(plan["status"], "ready")

    def test_apple_implementation_with_tests_is_ready_without_shared_qa_provider(self) -> None:
        profile = self.discovery.discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能并补充测试")
        plan = self.compiler.compile(profile, policy)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["missing_capabilities"], [])
        self.assertIn("verification.apple.auto", [node["capability"] for node in plan["nodes"]])
        self.assertNotIn("qa.targeted", [node["capability"] for node in plan["nodes"]])

    def test_apple_specialized_recipes_use_capabilities_not_skill_names(self) -> None:
        profile = self.discovery.discover(FIXTURES / "apple-app")
        cases = {
            "生成 iOS HTML 文档": "documentation.html",
            "修改 Xcode build setting": "build.apple.configuration",
            "修复 iOS crash": "debugging.apple.execute",
            "分析 iOS crash": "debugging.apple.analysis",
            "分析 iOS 性能掉帧": "performance.apple",
            "执行 iOS 设备自动化": "automation.apple",
        }
        for task, expected in cases.items():
            with self.subTest(task=task):
                policy = PolicyResolver().resolve(profile, task)
                plan = self.compiler.compile(profile, policy)
                self.assertIn(expected, [node["capability"] for node in plan["nodes"]])

    def test_unknown_code_profile_is_blocked(self) -> None:
        profile = self.discovery.discover(FIXTURES / "unknown")
        policy = PolicyResolver().resolve(profile, "检查这个项目")
        plan = self.compiler.compile(profile, policy)
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("routing.platform-selection", plan["missing_capabilities"])

    def test_unresolved_ambiguity_is_blocked(self) -> None:
        profile = self.discovery.discover(FIXTURES / "ambiguous")
        policy = PolicyResolver().resolve(profile, "实现功能")
        plan = self.compiler.compile(profile, policy)
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("routing.ambiguity-resolution", plan["missing_capabilities"])

    def test_same_path_ambiguous_target_remains_blocked(self) -> None:
        profile = self.discovery.discover(FIXTURES / "ambiguous", target_files=["src/index.ts"])
        self.assertEqual(len(profile["target_modules"]), 2)
        policy = PolicyResolver().resolve(profile, "实现功能")
        plan = self.compiler.compile(profile, policy)
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("routing.ambiguity-resolution", plan["missing_capabilities"])

    def test_explicit_platform_resolves_ambiguity(self) -> None:
        profile = self.discovery.discover(FIXTURES / "ambiguous")
        policy = PolicyResolver().resolve(profile, "实现功能", explicit_platforms=["web"])
        plan = self.compiler.compile(profile, policy)
        self.assertNotIn("routing.ambiguity-resolution", plan["missing_capabilities"])
        self.assertNotIn("review.independent", plan["missing_capabilities"])
        self.assertIn("web", [item["platform"] for item in plan["bootstrap_required"]])


if __name__ == "__main__":
    unittest.main()
