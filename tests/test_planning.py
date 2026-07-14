from __future__ import annotations

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
        self.assertEqual(implementation["resource_keys"], ["repository-write:apple"])
        self.assertFalse(implementation["idempotent"])

    def test_design_and_qa_nodes_are_composed(self) -> None:
        profile = self.discovery.discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS UI 设计并补测试")
        plan = self.compiler.compile(profile, policy)
        capabilities = [node["capability"] for node in plan["nodes"]]
        self.assertIn("design.context", capabilities)
        self.assertIn("qa.targeted", capabilities)
        self.assertIn("review.independent", capabilities)

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
        self.assertEqual(plan["status"], "ready")


if __name__ == "__main__":
    unittest.main()
