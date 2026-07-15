from __future__ import annotations

import unittest

from tests.support import FIXTURES, MANIFESTS

from agent_workflow.canonical_json import load
from agent_workflow.compatibility import compare_apple_routes, run_apple_dual_route_smoke
from agent_workflow.registry import ManifestRegistry


class AppleCompatibilityTests(unittest.TestCase):
    def test_frozen_legacy_routes_match_core_plans(self) -> None:
        baseline = load(FIXTURES / "compatibility" / "apple-dual-route-v1.json")
        registry = ManifestRegistry.from_directory(MANIFESTS)
        report = compare_apple_routes(baseline, repository_fixtures=FIXTURES, registry=registry)
        self.assertEqual(report["status"], "matched")
        self.assertTrue(all(not case["differences"] for case in report["cases"]))

    def test_no_side_effect_dual_route_smoke_and_fallback(self) -> None:
        baseline = load(FIXTURES / "compatibility" / "apple-dual-route-smoke-v1.json")
        registry = ManifestRegistry.from_directory(MANIFESTS)
        disabled = ManifestRegistry.from_directory(
            MANIFESTS, disabled_providers=["ios-agent-skills"]
        )
        report = run_apple_dual_route_smoke(
            baseline,
            repository_fixtures=FIXTURES,
            registry=registry,
            disabled_registry=disabled,
        )
        self.assertEqual(report["status"], "matched")
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual(report["fallback"]["disabled_core_plan_status"], "blocked")
        self.assertEqual(report["fallback"]["legacy_independent_status"], "completed")


if __name__ == "__main__":
    unittest.main()
