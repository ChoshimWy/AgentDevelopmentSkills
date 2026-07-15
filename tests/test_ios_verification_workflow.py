from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "platforms/apple/skills/ios-verification/scripts"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fingerprint = load_module("fingerprint")
session_store = load_module("session_store")
evidence_cache = load_module("evidence_cache")
affected_tests = load_module("affected_tests")
verification_coordinator = load_module("verification_coordinator")


class FingerprintTests(unittest.TestCase):
    def test_target_fingerprint_is_stable_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Sources/Feature.swift"
            source.parent.mkdir(parents=True)
            source.write_text("let value = 1\n", encoding="utf-8")
            first = fingerprint.target_source_fingerprint(root, "Feature", ["Sources/Feature.swift"])
            second = fingerprint.target_source_fingerprint(root, "Feature", ["Sources/Feature.swift"])
            self.assertEqual(first, second)
            source.write_text("let value = 2\n", encoding="utf-8")
            self.assertNotEqual(first, fingerprint.target_source_fingerprint(root, "Feature", ["Sources/Feature.swift"]))

    def test_fingerprint_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                fingerprint.file_inventory(Path(temporary), ["../outside"])


class SessionTests(unittest.TestCase):
    def test_session_is_canonical_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = session_store.SessionStore(Path(temporary), "feature-1")
            created = store.create(
                base_commit="abc",
                current_diff_hash="diff",
                environment_fingerprint="env:123",
                project={"scheme": "App"},
            )
            self.assertEqual(created, store.load())
            raw = store.path.read_text(encoding="utf-8")
            self.assertTrue(raw.endswith("\n"))
            self.assertEqual(raw, json.dumps(created, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def test_session_id_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                session_store.SessionStore(Path(temporary), "../escape")


class EvidenceTests(unittest.TestCase):
    def test_same_or_stronger_evidence_requires_fresh_identity_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.json"
            report.write_text("{}\n", encoding="utf-8")
            artifact = {"uri": "report.json", "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
            requirement = {
                "environment_fingerprint": "env:1",
                "current_diff_hash": "diff:1",
                "source_fingerprints": ["target:1"],
                "minimum_capabilities": ["compile", "test"],
                "identity": {"selectors": ["FeatureTests"]},
            }
            weaker = {**requirement, "status": "passed", "capabilities": ["compile"], "artifacts": [artifact]}
            stronger = {
                **requirement,
                "identity": {"selectors": ["FeatureTests", "OtherTests"]},
                "status": "passed",
                "capabilities": ["compile", "test", "consumer"],
                "artifacts": [artifact],
            }
            self.assertEqual(stronger, evidence_cache.reusable_evidence(requirement, [weaker, stronger], root))
            self.assertIsNone(evidence_cache.reusable_evidence({**requirement, "current_diff_hash": "diff:2"}, [stronger], root))
            report.write_text("changed\n", encoding="utf-8")
            self.assertIsNone(evidence_cache.reusable_evidence(requirement, [stronger], root))

    def test_only_deterministic_failures_are_reusable(self) -> None:
        self.assertTrue(
            evidence_cache.deterministic_failure_reusable(
                {"fingerprint": "f", "classification": "compile", "retryable": False}, "f"
            )
        )
        self.assertFalse(
            evidence_cache.deterministic_failure_reusable(
                {"fingerprint": "f", "classification": "environment", "retryable": True}, "f"
            )
        )


class PlannerTests(unittest.TestCase):
    def test_changed_paths_combines_worktree_index_and_untracked_names(self) -> None:
        outputs = {
            ("diff", "--name-only"): "Sources/Renamed.swift\n",
            ("diff", "--name-only", "--cached"): "Tests/RenamedTests.swift\n",
            ("ls-files", "--others", "--exclude-standard"): "Fixtures/new.json\nSources/Renamed.swift\n",
        }
        with mock.patch.object(verification_coordinator, "git_output", side_effect=lambda _root, *args: outputs[args]):
            self.assertEqual(
                ["Fixtures/new.json", "Sources/Renamed.swift", "Tests/RenamedTests.swift"],
                verification_coordinator.changed_paths(Path(".")),
            )

    def test_affected_tests_use_basename_and_domain_rules(self) -> None:
        result = affected_tests.affected_tests(
            ["Sources/DeviceControlViewModel.swift", "Sources/Subscription/ReceiptService.swift"]
        )
        self.assertIn("DeviceControlViewModelTests", result["selectors"])
        self.assertIn("ReceiptServiceTests", result["selectors"])
        self.assertIn("EntitlementTests", result["selectors"])

    def test_ui_diff_requires_compile_and_ui_evidence(self) -> None:
        plan = verification_coordinator.evidence_plan(["Sources/DeviceControlView.swift"])
        ids = {item["evidence_id"] for item in plan["required_evidence"]}
        self.assertIn("compile:affected-target", ids)
        self.assertIn("ui:scenario-required", ids)

    def test_asset_dependency_release_and_unknown_diffs_fail_closed(self) -> None:
        asset = verification_coordinator.evidence_plan(["Assets/fixture.json"])
        self.assertIn("resource:integrity", {item["evidence_id"] for item in asset["required_evidence"]})
        dependency = verification_coordinator.evidence_plan(["Package.swift"])
        self.assertEqual("checkpoint", dependency["lane"])
        self.assertIn("dependency:resolve", {item["evidence_id"] for item in dependency["required_evidence"]})
        release = verification_coordinator.evidence_plan(["App/App.entitlements"])
        self.assertEqual("final", release["lane"])
        self.assertIn("release:configuration", {item["evidence_id"] for item in release["required_evidence"]})
        unknown = verification_coordinator.evidence_plan(["Scripts/generated.opaque"])
        self.assertEqual("blocked", unknown["status"])
        self.assertTrue(unknown["required_evidence"])
        combined = verification_coordinator.evidence_plan(["App.xcodeproj/project.pbxproj", "Package.resolved"])
        ids = [item["evidence_id"] for item in combined["required_evidence"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rule_diff_routes_to_policy_lint(self) -> None:
        plan = verification_coordinator.evidence_plan(["skills/ios-verification/SKILL.md"])
        self.assertEqual("dev", plan["lane"])
        self.assertEqual("policy-lint:current-diff", plan["required_evidence"][0]["evidence_id"])

    def test_source_below_a_directory_named_skills_is_not_rule_only(self) -> None:
        self.assertEqual("swift-small", verification_coordinator.classify("Sources/Skills/Feature.swift"))


class WrapperPolicyTests(unittest.TestCase):
    def test_wrapper_contains_fingerprint_dedupe_and_cache_controls(self) -> None:
        wrapper = (ROOT / "platforms/apple/config/codex/templates/codex_verify.example.sh").read_text(encoding="utf-8")
        for marker in (
            "compute_request_fingerprint",
            "request_fingerprint",
            "queue_or_reuse_job",
            "MATCHING_JOB_KIND='attached'",
            "MATCHING_JOB_KIND='cached'",
            "--force",
            "--no-cache",
        ):
            self.assertIn(marker, wrapper)


if __name__ == "__main__":
    unittest.main()
