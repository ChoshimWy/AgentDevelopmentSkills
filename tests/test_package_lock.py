from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.support import FIXTURES, MANIFESTS, ROOT

from agent_workflow.canonical_json import dump, load, sha256
from agent_workflow.contracts import validate
from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.installation import PERSISTENT_PACKAGE_LOCK, build_install_bundle, install_bundle
from agent_workflow.models import ContractError
from agent_workflow.package_lock import (
    diff_package_locks,
    explain_package_lock,
    resolve_package_lock,
    schema_inventory,
    validate_package_lock,
    validate_plan_package_lock,
)
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
from agent_workflow.registry import ManifestRegistry
from agent_workflow.runtime import FakeAdapterExecutor


class PackageLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.apple = build_install_bundle(MANIFESTS, platforms=["apple"])

    def test_resolver_is_deterministic_and_freezes_schemas_without_absolute_paths(self) -> None:
        first = resolve_package_lock(self.apple.plan, schema_root=ROOT / "schemas")
        second = resolve_package_lock(self.apple.plan, schema_root=ROOT / "schemas")
        self.assertEqual(first, second)
        self.assertEqual(first, self.apple.package_lock)
        self.assertEqual(first["core"]["runtime_version"], "0.2.0")
        self.assertEqual(first["selection"]["platforms"], ["apple"])
        self.assertEqual(
            len(first["schema_inventory"]["files"]),
            len(schema_inventory(ROOT / "schemas")["files"]),
        )
        self.assertTrue(all(not Path(item["path"]).is_absolute() and ".." not in Path(item["path"]).parts for item in first["schema_inventory"]["files"]))
        validate("agent-skills-lock", first)

    def test_source_contract_supports_controlled_uris_and_rejects_escape(self) -> None:
        relative = resolve_package_lock(
            self.apple.plan,
            schema_root=ROOT / "schemas",
            package_sources={"apple": {"kind": "relative-path", "uri": "./platforms/apple"}},
        )
        self.assertEqual(next(item for item in relative["packages"] if item["id"] == "apple")["source"]["uri"], "./platforms/apple")
        remote = resolve_package_lock(
            self.apple.plan,
            schema_root=ROOT / "schemas",
            package_sources={"apple": {"kind": "https", "uri": "https://example.test/releases/apple.zip"}},
            package_source_artifact_hashes={"apple": "a" * 64},
        )
        validate_package_lock(remote)
        with self.assertRaisesRegex(ContractError, "artifact SHA-256"):
            resolve_package_lock(
                self.apple.plan,
                schema_root=ROOT / "schemas",
                package_sources={"apple": {"kind": "https", "uri": "https://example.test/releases/apple.zip"}},
            )
        with self.assertRaisesRegex(ContractError, "unsafe"):
            resolve_package_lock(
                self.apple.plan,
                schema_root=ROOT / "schemas",
                package_sources={"apple": {"kind": "relative-path", "uri": "./../apple"}},
            )
        with self.assertRaisesRegex(ContractError, "missing or unsafe"):
            resolve_package_lock(
                self.apple.plan,
                schema_root=ROOT / "schemas",
                package_sources={"apple": {"kind": "relative-path", "uri": "./missing/apple"}},
            )
        with self.assertRaisesRegex(ContractError, "unknown packages"):
            resolve_package_lock(
                self.apple.plan,
                schema_root=ROOT / "schemas",
                package_sources={"unknown": {"kind": "local-registry", "uri": "registry://unknown"}},
            )

    def test_tamper_and_cross_identity_mismatch_fail_closed(self) -> None:
        tampered = deepcopy(self.apple.package_lock)
        tampered["packages"][-1]["source"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "identity is stale|core identity|fingerprint"):
            validate_package_lock(tampered)

        forged = deepcopy(self.apple.package_lock)
        forged["bindings_sha256"] = "0" * 64
        forged["fingerprint"] = sha256({key: value for key, value in forged.items() if key != "fingerprint"})
        with self.assertRaisesRegex(ContractError, "bindings digest"):
            validate_package_lock(forged)

        malformed = deepcopy(self.apple.package_lock)
        malformed["schema_inventory"]["files"] = 1
        with self.assertRaisesRegex(ContractError, "schema files"):
            validate_package_lock(malformed)

        malformed_fields = (
            ("package kind", lambda value: value["packages"][0].__setitem__("kind", [])),
            (
                "provider package",
                lambda value: next(iter(value["capability_providers"].values())).__setitem__("package", []),
            ),
            ("dependency source", lambda value: value["dependencies"][0].__setitem__("from", [])),
        )
        for label, mutate in malformed_fields:
            with self.subTest(label=label):
                invalid = deepcopy(self.apple.package_lock)
                mutate(invalid)
                with self.assertRaises(ContractError):
                    validate_package_lock(invalid)

        incompatible = deepcopy(self.apple.package_lock)
        design = next(item for item in incompatible["packages"] if item["id"] == "design")
        design["version"] = "9.0.0"
        for provider in incompatible["capability_providers"].values():
            if provider["package"] == "design":
                provider["package_version"] = "9.0.0"
        incompatible["bindings_sha256"] = sha256({
            capability: {"binding": provider["binding"], "package": provider["package"]}
            for capability, provider in sorted(incompatible["capability_providers"].items())
        })
        incompatible["fingerprint"] = sha256({key: value for key, value in incompatible.items() if key != "fingerprint"})
        with self.assertRaisesRegex(ContractError, "version is not satisfied"):
            validate_package_lock(incompatible)

        incompatible_provider = deepcopy(self.apple.package_lock)
        apple = next(item for item in incompatible_provider["packages"] if item["id"] == "apple")
        apple["provider_version"] = "9.0.0"
        incompatible_provider["fingerprint"] = sha256({
            key: value for key, value in incompatible_provider.items() if key != "fingerprint"
        })
        with self.assertRaisesRegex(ContractError, "provider compatibility"):
            validate_package_lock(incompatible_provider)

    def test_diff_explain_and_lineage_are_deterministic(self) -> None:
        expanded = build_install_bundle(MANIFESTS, platforms=["apple", "desktop"]).package_lock
        diff = diff_package_locks(self.apple.package_lock, expanded)
        self.assertEqual(diff["status"], "changed")
        self.assertEqual(diff["packages"]["added"], ["desktop", "qa"])
        self.assertTrue(diff["bindings"]["added"])
        permission_changed = deepcopy(self.apple.package_lock)
        capability = next(
            name for name, provider in permission_changed["capability_providers"].items()
            if provider["permission_profile"] != permission_changed["permission_profiles"][0]
        )
        permission_changed["capability_providers"][capability]["permission_profile"] = permission_changed["permission_profiles"][0]
        permission_changed["fingerprint"] = sha256({
            key: value for key, value in permission_changed.items() if key != "fingerprint"
        })
        validate_package_lock(permission_changed)
        permission_diff = diff_package_locks(self.apple.package_lock, permission_changed)
        self.assertEqual(permission_diff["permissions"]["added"], [])
        self.assertEqual(permission_diff["permissions"]["removed"], [])
        self.assertEqual(permission_diff["permissions"]["changed_capabilities"], [capability])
        explanation = explain_package_lock(expanded)
        self.assertEqual(explanation["package_count"], len(expanded["packages"]))
        successor = resolve_package_lock(
            self.apple.plan,
            schema_root=ROOT / "schemas",
            previous_lock=self.apple.package_lock,
        )
        self.assertEqual(successor["lineage"]["previous_lock_hash"], self.apple.package_lock["fingerprint"])

    def test_install_persists_lock_and_modified_lock_blocks_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(self.apple, target)
            lock_path = target / ".agent-skills" / PERSISTENT_PACKAGE_LOCK
            self.assertEqual(load(lock_path), self.apple.package_lock)
            tampered = load(lock_path)
            tampered["lineage"]["previous_lock_hash"] = "a" * 64
            tampered["fingerprint"] = sha256({
                key: value for key, value in tampered.items() if key != "fingerprint"
            })
            validate_package_lock(tampered)
            dump(tampered, lock_path)
            with self.assertRaisesRegex(ContractError, "modified"):
                install_bundle(self.apple, target)

    def test_legacy_install_lock_without_persistent_lock_can_upgrade(self) -> None:
        legacy_fixture = load(FIXTURES / "phase6" / "install-lock-v2-legacy.json")
        validate("install-plan", legacy_fixture)
        self.assertNotIn("package_lock_hash", legacy_fixture)
        self.assertNotIn("core_compatibility", legacy_fixture["selected_packages"][0])

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(self.apple, target)
            managed = target / ".agent-skills"
            legacy_lock = load(managed / "install-lock.json")
            legacy_lock.pop("package_lock_hash")
            for package in legacy_lock["selected_packages"]:
                package.pop("core_compatibility")
                package.pop("provider_compatibility")
                package.pop("provider_version")
            for provider in legacy_lock["capability_providers"].values():
                provider.pop("permission_profile")
            legacy_lock["fingerprint"] = sha256({
                key: value for key, value in legacy_lock.items() if key not in {"fingerprint", "status"}
            })
            validate("install-plan", legacy_lock)
            dump(legacy_lock, managed / "install-lock.json")
            (managed / PERSISTENT_PACKAGE_LOCK).unlink()
            result = install_bundle(self.apple, target)
            self.assertEqual(result["status"], "installed")
            self.assertTrue((managed / PERSISTENT_PACKAGE_LOCK).is_file())

    def test_lock_hash_invalidates_plan_and_is_recorded_in_ledger(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        profile = DiscoveryEngine(registry).discover(FIXTURES / "apple-app")
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        baseline = PlanCompiler(registry).compile(profile, policy)
        locked = PlanCompiler(registry).compile(
            profile,
            policy,
            package_lock=self.apple.package_lock,
        )
        self.assertNotEqual(baseline["fingerprint"], locked["fingerprint"])
        self.assertEqual(locked["package_lock_hash"], self.apple.package_lock["fingerprint"])
        with self.assertRaisesRegex(ContractError, "current package Lockfile"):
            FakeAdapterExecutor().run(locked)
        ledger = FakeAdapterExecutor().run(locked, package_lock=self.apple.package_lock)
        self.assertEqual(ledger["package_lock_hash"], self.apple.package_lock["fingerprint"])
        invalid_ledger = deepcopy(ledger)
        invalid_ledger["package_lock_hash"] = "stale"
        with self.assertRaisesRegex(ContractError, "package_lock_hash"):
            validate("run-ledger", invalid_ledger)
        validate_plan_package_lock(locked, self.apple.package_lock)

        successor = resolve_package_lock(
            self.apple.plan,
            schema_root=ROOT / "schemas",
            previous_lock=self.apple.package_lock,
        )
        forged_plan = deepcopy(locked)
        forged_plan["package_lock_hash"] = successor["fingerprint"]
        with self.assertRaisesRegex(ContractError, "fingerprint mismatch"):
            validate("workflow-plan", forged_plan)
        with self.assertRaisesRegex(ContractError, "fingerprint mismatch"):
            FakeAdapterExecutor().run(forged_plan, package_lock=successor)

        forged_id = deepcopy(locked)
        forged_id["plan_id"] = "plan-forged"
        with self.assertRaisesRegex(ContractError, "id mismatch"):
            validate("workflow-plan", forged_id)

        unrelated = build_install_bundle(MANIFESTS, core_only=True).package_lock
        with self.assertRaisesRegex(ContractError, "not frozen"):
            PlanCompiler(registry).compile(profile, policy, package_lock=unrelated)
        with self.assertRaisesRegex(ContractError, "does not match Lockfile"):
            FakeAdapterExecutor().run(locked, package_lock=unrelated)
        permission_drift = deepcopy(self.apple.package_lock)
        permission_drift["capability_providers"]["implementation.apple"]["permission_profile"] = "repository-read-only"
        permission_drift["fingerprint"] = sha256({
            key: value for key, value in permission_drift.items() if key != "fingerprint"
        })
        validate_package_lock(permission_drift)
        with self.assertRaisesRegex(ContractError, "permission differs"):
            PlanCompiler(registry).compile(profile, policy, package_lock=permission_drift)
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            FakeAdapterExecutor().run(
                locked,
                ledger_path=ledger_path,
                package_lock=self.apple.package_lock,
            )
            with self.assertRaisesRegex(ContractError, "does not match Lockfile"):
                FakeAdapterExecutor().run(
                    locked,
                    ledger_path=ledger_path,
                    package_lock=unrelated,
                    resume=True,
                )


if __name__ == "__main__":
    unittest.main()
