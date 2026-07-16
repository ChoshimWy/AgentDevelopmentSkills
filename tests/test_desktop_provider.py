from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tests.support import FIXTURES, MANIFESTS

from agent_workflow.adapters import build_adapter_request, validate_adapter_result
from agent_workflow.installation import build_install_bundle
from agent_workflow.models import ContractError
from agent_workflow.registry import ManifestRegistry
from platforms.desktop.scripts import desktop_adapter
from platforms.desktop.scripts.desktop_discovery import (
    build_environment_profile,
    inspect_repository,
    validate_environment_profile,
    validate_project_profile,
)


class DesktopDiscoveryTests(unittest.TestCase):
    def test_native_electron_and_tauri_have_stable_strong_routes(self) -> None:
        expectations = {
            "desktop-native": "native-windows",
            "desktop-electron": "electron",
            "desktop-tauri": "tauri",
        }
        for fixture, framework in expectations.items():
            with self.subTest(fixture=fixture):
                first = inspect_repository(FIXTURES / fixture)
                second = inspect_repository(FIXTURES / fixture)
                self.assertEqual(first, second)
                self.assertEqual(first["status"], "supported")
                self.assertEqual(first["selected_framework"], framework)
                self.assertEqual(first["module_root"], ".")
                validate_project_profile(first)

    def test_multiple_strong_frameworks_and_weak_only_signal_are_ambiguous(self) -> None:
        ambiguous = inspect_repository(FIXTURES / "desktop-ambiguous")
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertIsNone(ambiguous["selected_framework"])
        self.assertEqual(ambiguous["ambiguities"], ["multiple-strong-frameworks:electron,tauri"])

        weak = inspect_repository(FIXTURES / "desktop-weak")
        self.assertEqual(weak["status"], "ambiguous")
        self.assertEqual(weak["ambiguities"], ["no-strong-framework-signal"])

    def test_environment_profile_freezes_compatibility_dimensions_and_permissions(self) -> None:
        facts = json.loads((FIXTURES / "desktop-environment.json").read_text(encoding="utf-8"))
        first = build_environment_profile(facts)
        second = build_environment_profile(facts)
        self.assertEqual(first, second)
        validate_environment_profile(first)
        self.assertEqual(first["matrix_dimensions"]["display_scales"], [2])
        self.assertEqual(first["matrix_dimensions"]["input_kinds"], ["keyboard", "mouse"])
        self.assertEqual(
            first["matrix_dimensions"]["permission_names"],
            ["automation", "filesystem", "installer-elevation", "network", "notification"],
        )

        changed = deepcopy(facts)
        changed["dpi"]["scale_factor"] = 1
        self.assertNotEqual(build_environment_profile(changed)["fingerprint"], first["fingerprint"])


class DesktopProviderTests(unittest.TestCase):
    def test_desktop_goldens_freeze_routes_environment_and_cp1_anchor(self) -> None:
        golden_root = Path(__file__).resolve().parent / "golden" / "phase4-desktop"
        index = json.loads((golden_root / "golden-index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["entries"]), 6)
        for entry in index["entries"]:
            path = golden_root / entry["path"]
            artifact = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
            self.assertEqual(artifact["fingerprint"], entry["artifact_fingerprint"])
            self.assertNotIn(str(Path.cwd()), path.read_text(encoding="utf-8"))
        anchor = json.loads((golden_root / "cp1-anchor.json").read_text(encoding="utf-8"))
        self.assertEqual(anchor["status"], "passed")
        self.assertEqual(anchor["adapter"]["execution_kind"], "controlled-conformance-runner")
        self.assertEqual(anchor["adapter"]["evidence_kinds"], ["validation"])
        self.assertEqual(anchor["qa"]["defect_status"], "closed")
        self.assertEqual(anchor["qa"]["regression_status"], "current")
        self.assertEqual(anchor["qa"]["status"], "passed")
        self.assertTrue(anchor["qa"]["environment_linked"])
        self.assertEqual(anchor["qa"]["result_environment_fingerprints"], [anchor["environment_fingerprint"]])
        self.assertEqual(anchor["qa"]["defect_environment_fingerprint"], anchor["environment_fingerprint"])
        self.assertEqual(anchor["qa"]["regression_environment_fingerprints"], [anchor["environment_fingerprint"]])
        self.assertEqual(anchor["qa"]["environment_stale_status"], "stale")
        self.assertEqual(anchor["qa"]["environment_stale_reasons"], ["environment-fingerprint-changed"])

    def test_install_bundle_is_explicit_and_contains_only_desktop_assets(self) -> None:
        bundle = build_install_bundle(MANIFESTS, platforms=["desktop"])
        self.assertEqual(bundle.plan["status"], "planned")
        self.assertEqual(bundle.plan["selected_platforms"], ["desktop"])
        self.assertEqual(
            [item["id"] for item in bundle.plan["selected_packages"]],
            ["core", "git", "qa", "review", "workflow", "desktop"],
        )
        self.assertEqual(
            [item["name"] for item in bundle.plan["skills"]],
            ["gh-pr-flow", "git-workflow", "session-worktree", "qa-workflow", "code-review", "workflow-orchestration", "desktop-orchestration"],
        )
        self.assertIn("qa.plan.compile", bundle.plan["bindings"])
        self.assertEqual(bundle.plan["bindings"]["build.desktop"]["binding"]["name"], "scripts/desktop_adapter.py")

    def test_provider_capability_permissions_and_resource_keys_are_frozen(self) -> None:
        registry = ManifestRegistry.from_directory(MANIFESTS)
        affected = registry.resolve_binding("verification.desktop.affected-tests", platform="desktop")
        smoke = registry.resolve_binding("verification.desktop.ui-smoke", platform="desktop")
        self.assertEqual(affected.contract["permission_profile"], "project-read-execute")
        self.assertEqual(affected.contract["concurrency_keys"], ["build-queue:{target-root}"])
        self.assertEqual(
            smoke.contract["concurrency_keys"],
            ["build-queue:{target-root}", "desktop-session:{target-root}"],
        )

    def test_adapter_plans_and_normalizes_a_controlled_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                json.dumps({"devDependencies": {"electron": "1"}, "scripts": {"build": "echo build", "test": "echo test", "ui-smoke": "echo smoke"}}),
                encoding="utf-8",
            )
            profile = inspect_repository(root)
            environment = build_environment_profile(json.loads((FIXTURES / "desktop-environment.json").read_text(encoding="utf-8")))
            request = _request(profile, environment, "verification.desktop.affected-tests", "affected-tests")
            plan = desktop_adapter.plan_command(request, "affected-tests")
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["argv"], ["npm", "run", "test"])
            self.assertEqual(plan["resource_keys"], [f"build-queue:{root.resolve()}"])

            completed = SimpleNamespace(returncode=0, stdout="1 passed", stderr="")
            with patch.object(desktop_adapter.shutil, "which", return_value="/usr/bin/npm"):
                result = desktop_adapter.run_adapter(
                    request,
                    "affected-tests",
                    execute=True,
                    runner=lambda *args, **kwargs: completed,
                )
            validate_adapter_result(request, result)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["evidence"][0]["kind"], "validation")
            self.assertEqual(result["evidence"][0]["data"]["retention"], "task-scoped")
            self.assertEqual(result["cleanup"][0]["status"], "completed")
            self.assertTrue((root / ".agent-workflow" / "desktop-artifacts").is_dir())

            def cancel(*args, **kwargs):
                raise KeyboardInterrupt()

            with patch.object(desktop_adapter.shutil, "which", return_value="/usr/bin/npm"):
                cancelled = desktop_adapter.run_adapter(
                    request,
                    "affected-tests",
                    execute=True,
                    runner=cancel,
                )
            self.assertEqual(cancelled["status"], "blocked")
            self.assertEqual(cancelled["cleanup"][0]["status"], "completed")
            self.assertEqual(cancelled["failure_attribution"]["category"], "environment")

    def test_adapter_blocks_missing_script_stale_environment_and_interaction(self) -> None:
        profile = inspect_repository(FIXTURES / "desktop-electron")
        environment = build_environment_profile(json.loads((FIXTURES / "desktop-environment.json").read_text(encoding="utf-8")))
        request = _request(profile, environment, "verification.desktop.affected-tests", "affected-tests")
        blocked = desktop_adapter.run_adapter(request, "affected-tests", execute=True)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("required package script is missing", blocked["no_test_reason"])

        stale = deepcopy(request)
        stale["task_context"]["environment_fingerprint"] = "desktop-v1:" + "0" * 64
        stale = _rebuild_request(stale)
        with self.assertRaisesRegex(ContractError, "environment fingerprint is stale"):
            desktop_adapter.plan_command(stale, "affected-tests")

        interaction_facts = json.loads((FIXTURES / "desktop-environment.json").read_text(encoding="utf-8"))
        for permission in interaction_facts["permissions"]:
            if permission["name"] == "automation":
                permission["status"] = "granted"
        interaction_environment = build_environment_profile(interaction_facts)
        interaction = _request(profile, interaction_environment, "automation.desktop.interaction", "interaction")
        result = desktop_adapter.run_adapter(interaction, "interaction", execute=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("framework-specific session adapter", result["failure_attribution"]["summary"])

    def test_adapter_fails_closed_when_required_permissions_are_denied_or_unknown(self) -> None:
        profile = inspect_repository(FIXTURES / "desktop-electron")
        facts = json.loads((FIXTURES / "desktop-environment.json").read_text(encoding="utf-8"))

        denied = build_environment_profile(facts)
        ui_request = _request(profile, denied, "verification.desktop.ui-smoke", "ui-smoke")
        ui_plan = desktop_adapter.plan_command(ui_request, "ui-smoke")
        self.assertEqual(ui_plan["status"], "blocked")
        self.assertIn("automation:denied", ui_plan["reason"])

        unknown_facts = deepcopy(facts)
        for permission in unknown_facts["permissions"]:
            if permission["name"] == "filesystem":
                permission["status"] = "unknown"
        unknown = build_environment_profile(unknown_facts)
        test_request = _request(profile, unknown, "verification.desktop.affected-tests", "affected-tests")
        test_plan = desktop_adapter.plan_command(test_request, "affected-tests")
        self.assertEqual(test_plan["status"], "blocked")
        self.assertIn("filesystem:unknown", test_plan["reason"])


def _request(profile: dict, environment: dict, capability: str, mode: str) -> dict:
    plan = {
        "fingerprint": "workflow-plan-desktop-fixture",
        "nodes": [{
            "binding": {"kind": "script", "mode": mode, "name": "scripts/desktop_adapter.py"},
            "capability": capability,
            "id": "desktop-node",
            "provider": "desktop-agent-skills",
        }],
        "plan_id": "desktop-plan",
        "schema_version": "1.0",
    }
    return build_adapter_request(
        plan,
        "desktop-node",
        context={
            "checkpoints": {"CP1": "required"},
            "desktop_environment_profile": environment,
            "desktop_project_profile": profile,
            "environment_fingerprint": environment["fingerprint"],
        },
        invocation_id=f"invoke-{mode}",
    )


def _rebuild_request(request: dict) -> dict:
    plan = {
        "fingerprint": request["plan_fingerprint"],
        "nodes": [{
            "binding": request["binding"],
            "capability": request["capability"],
            "id": request["node_id"],
            "provider": request["provider"],
        }],
        "plan_id": request["plan_id"],
        "schema_version": "1.0",
    }
    return build_adapter_request(plan, request["node_id"], context=request["task_context"], invocation_id=request["invocation_id"])


if __name__ == "__main__":
    unittest.main()
