from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.support import MANIFESTS, ROOT

from agent_workflow.canonical_json import dump, dumps, load, sha256
from agent_workflow.contracts import (
    validate,
    validate_migration_report,
    validate_upgrade_conformance_evidence,
)
from agent_workflow.cli import (
    _partial_uninstall_external_context,
    _partial_uninstall_selection,
    _source_upgrade_context,
)
from agent_workflow.doctor import diagnose_install
from agent_workflow.installation import (
    build_install_bundle,
    install_bundle,
    target_lifecycle_lock,
)
from agent_workflow.models import ContractError
from agent_workflow.upgrade import (
    apply_upgrade,
    make_upgrade_conformance_evidence,
    plan_upgrade,
    prepare_upgrade_candidate,
    rollback_upgrade,
)


def _copy_registry(root: Path) -> Path:
    repository = root / "repository"
    for name in ("platforms", "disciplines", "runtime-configs", "schemas"):
        shutil.copytree(ROOT / name, repository / name)
    return repository / "platforms"


def _evidence(candidate_lock: dict[str, object], *, stdout_sha256: str = "2" * 64) -> dict[str, object]:
    return make_upgrade_conformance_evidence(
        candidate_lock,
        manifest_count=19,
        negative_contract_count=13,
        test_count=353,
        suite_definition_hash=sha256(["test-suite"]),
        runner_sha256="1" * 64,
        environment={"platform": "unit-test", "python": "3.11.0"},
        command_results=[
            {
                "command": "unit-test-suite",
                "exit_code": 0,
                "stdout_sha256": stdout_sha256,
                "stderr_sha256": "3" * 64,
            }
        ],
    )


class UpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.current_bundle = build_install_bundle(MANIFESTS, platforms=["apple"])

    def install(self, root: Path) -> Path:
        target = root / "codex"
        install_bundle(self.current_bundle, target)
        return target

    def changed_registry(self, root: Path) -> Path:
        manifests = _copy_registry(root)
        skill = root / "repository" / "disciplines" / "documentation" / "skills" / "html-docs" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nUpgrade fixture.\n", encoding="utf-8")
        return manifests

    def operation(self, manifests: Path, target: Path):
        candidate = prepare_upgrade_candidate(manifests, target)
        return plan_upgrade(
            manifests,
            target,
            _evidence(candidate.bundle.package_lock),
            schema_root=ROOT / "schemas",
        )

    def run_cli(self, manifests: Path, *args: str) -> subprocess.CompletedProcess[str]:
        from agent_workflow import cli

        argv = ["agent-skills", "--manifests", str(manifests), *args]
        self._conformance_run_count = getattr(self, "_conformance_run_count", 0) + 1
        output_digest = f"{(self._conformance_run_count % 14) + 1:x}" * 64
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch(
                "agent_workflow.cli.run_upgrade_conformance",
                side_effect=lambda _, package_lock: _evidence(
                    package_lock,
                    stdout_sha256=output_digest,
                ),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            returncode = cli.main()
        return subprocess.CompletedProcess(argv, returncode, stdout.getvalue(), stderr.getvalue())

    def test_upgrade_contracts_reject_schema_incompatible_scalar_types(self) -> None:
        evidence = _evidence(self.current_bundle.package_lock)
        for invalid_exit_code in (False, 0.0, -0.0):
            with self.subTest(invalid_exit_code=repr(invalid_exit_code)):
                invalid = deepcopy(evidence)
                invalid["command_results"][0]["exit_code"] = invalid_exit_code
                stable = {
                    key: value
                    for key, value in invalid.items()
                    if key not in {"attestation_key", "fingerprint"}
                }
                stable["command_results"] = [
                    {
                        "command": item["command"],
                        "exit_code": item["exit_code"],
                    }
                    for item in invalid["command_results"]
                ]
                invalid["attestation_key"] = sha256(stable)
                invalid["fingerprint"] = sha256({
                    key: value
                    for key, value in invalid.items()
                    if key != "fingerprint"
                })
                with self.assertRaisesRegex(ContractError, "command result"):
                    validate_upgrade_conformance_evidence(invalid)

        migration = {
            "after_sha256": "4" * 64,
            "artifact": "activation-lock",
            "before_sha256": "5" * 64,
            "from_version": "1.0",
            "lossless": True,
            "schema_version": "1.0",
            "status": "planned",
            "steps": [
                {
                    "changes": ["temporary-version"],
                    "from_version": "1.0",
                    "lossless": True,
                    "to_version": 7,
                },
                {
                    "changes": ["final-version"],
                    "from_version": 7,
                    "lossless": True,
                    "to_version": "2.0",
                },
            ],
            "to_version": "2.0",
        }
        migration["fingerprint"] = sha256(migration)
        with self.assertRaisesRegex(ContractError, "step chain"):
            validate_migration_report(migration)

    def test_no_change_plan_is_deterministic_and_apply_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.install(Path(directory))
            before = dumps(load(target / ".agent-skills" / "install-lock.json"))
            candidate = prepare_upgrade_candidate(MANIFESTS, target)
            evidence = _evidence(candidate.bundle.package_lock)
            first = plan_upgrade(MANIFESTS, target, evidence, schema_root=ROOT / "schemas")
            second = plan_upgrade(MANIFESTS, target, evidence, schema_root=ROOT / "schemas")
            self.assertEqual(first.plan, second.plan)
            self.assertEqual(first.plan["status"], "no-change")
            self.assertEqual(first.plan["upgrade_steps"], [])
            self.assertEqual(first.plan["compatibility"]["mode"], "identity-only")
            validate("upgrade-plan", first.plan)
            result = apply_upgrade(
                first,
                target,
                approve_plan=first.plan["fingerprint"],
            )
            self.assertEqual(result["status"], "no-change")
            self.assertEqual(before, dumps(load(target / ".agent-skills" / "install-lock.json")))
            self.assertFalse((target / ".agent-skills" / "rollback-point").exists())

            variable_output = plan_upgrade(
                MANIFESTS,
                target,
                _evidence(candidate.bundle.package_lock, stdout_sha256="4" * 64),
                schema_root=ROOT / "schemas",
            )
            self.assertEqual(first.plan, variable_output.plan)

    def test_upgrade_persists_rollback_point_and_rollback_restores_exact_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            old_lock = load(target / ".agent-skills" / "agent-skills.lock")
            manifests = self.changed_registry(root)
            operation = self.operation(manifests, target)
            self.assertEqual(operation.plan["status"], "planned")
            self.assertEqual(operation.plan["rollback"]["previous_lock_hash"], old_lock["fingerprint"])
            self.assertTrue(operation.plan["upgrade_steps"])
            self.assertEqual(operation.plan["upgrade_steps"][-1]["kind"], "lock")
            result = apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            self.assertEqual(result["status"], "upgraded")
            upgraded_lock = load(target / ".agent-skills" / "agent-skills.lock")
            self.assertEqual(upgraded_lock["lineage"]["previous_lock_hash"], old_lock["fingerprint"])
            point = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")
            self.assertEqual(point["package_lock_hash"], old_lock["fingerprint"])
            validate("rollback-point", point)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

            rolled_back = rollback_upgrade(
                target,
                approve_current_lock=upgraded_lock["fingerprint"],
                approve_rollback_point=point["fingerprint"],
            )
            self.assertEqual(rolled_back["status"], "rolled-back")
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), old_lock)
            reverse = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")
            self.assertEqual(reverse["package_lock_hash"], upgraded_lock["fingerprint"])
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

    def test_permission_change_requires_exact_scoped_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            manifests = _copy_registry(root)
            provider_path = root / "repository" / "platforms" / "apple" / "provider" / "manifest.json"
            provider = json.loads(provider_path.read_text(encoding="utf-8"))
            capability = next(item for item in provider["capabilities"] if item["id"] == "analysis.apple")
            capability["permission_profile"] = "project-write"
            capability["binding_permission_profile"] = "project-write"
            provider_path.write_text(dumps(provider), encoding="utf-8")
            bootstrap_path = root / "repository" / "platforms" / "apple" / "manifest.json"
            bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
            bootstrap["provider_contract"]["capability_permissions"]["analysis.apple"] = "project-write"
            bootstrap_path.write_text(dumps(bootstrap), encoding="utf-8")
            operation = self.operation(manifests, target)
            expected = "permission:analysis.apple:repository-read-only->project-write"
            self.assertIn(expected, operation.plan["approvals_required"])
            with self.assertRaisesRegex(ContractError, "missing"):
                apply_upgrade(
                    operation,
                    target,
                    approve_plan=operation.plan["fingerprint"],
                )
            result = apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            self.assertEqual(result["status"], "upgraded")
            installed = load(target / ".agent-skills" / "install-lock.json")
            self.assertEqual(
                installed["capability_providers"]["analysis.apple"]["permission_profile"],
                "project-write",
            )

    def test_stale_evidence_plan_and_rollback_approval_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            manifests = self.changed_registry(root)
            candidate = prepare_upgrade_candidate(manifests, target)
            evidence = _evidence(candidate.bundle.package_lock)
            stale = deepcopy(evidence)
            stale["candidate_package_lock_hash"] = "0" * 64
            stale["fingerprint"] = "1" * 64
            with self.assertRaises(ContractError):
                plan_upgrade(manifests, target, stale, schema_root=ROOT / "schemas")
            operation = plan_upgrade(manifests, target, evidence, schema_root=ROOT / "schemas")
            with self.assertRaisesRegex(ContractError, "exact planned fingerprint"):
                apply_upgrade(operation, target, approve_plan="0" * 64)
            apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            with self.assertRaisesRegex(ContractError, "exact current Lockfile"):
                rollback_upgrade(
                    target,
                    approve_current_lock="0" * 64,
                    approve_rollback_point="0" * 64,
                )
            upgraded = load(target / ".agent-skills" / "agent-skills.lock")
            with self.assertRaisesRegex(ContractError, "exact rollback point"):
                rollback_upgrade(
                    target,
                    approve_current_lock=upgraded["fingerprint"],
                    approve_rollback_point="0" * 64,
                )

    def test_upgrade_failure_restores_current_install_and_leaves_no_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            current_lock = load(target / ".agent-skills" / "agent-skills.lock")
            manifests = self.changed_registry(root)
            operation = self.operation(manifests, target)
            real_replace = os.replace
            failed = False

            def fail_once(source, destination):
                nonlocal failed
                source_path = Path(source)
                if not failed and source_path.name == "skills" and source_path.parent.name.startswith(".agent-skills-stage-"):
                    failed = True
                    raise OSError("injected upgrade swap failure")
                return real_replace(source, destination)

            with patch("agent_workflow.installation.os.replace", side_effect=fail_once):
                with self.assertRaisesRegex(OSError, "injected upgrade swap failure"):
                    apply_upgrade(
                        operation,
                        target,
                        approve_plan=operation.plan["fingerprint"],
                        approvals=operation.plan["approvals_required"],
                    )
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), current_lock)
            self.assertFalse((target / ".agent-skills" / "rollback-point").exists())
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")
            self.assertFalse(any(target.glob(".agent-skills-stage-*")))
            self.assertFalse(any(target.glob(".agent-skills-backup-*")))

    def test_cli_dry_run_apply_and_rollback_require_frozen_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            manifests = self.changed_registry(root)
            candidate = prepare_upgrade_candidate(manifests, target)
            plan_path = root / "upgrade-plan.json"
            preview = self.run_cli(
                manifests,
                "upgrade",
                "--target-root",
                str(target),
                "--dry-run",
                "--output",
                str(plan_path),
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            self.assertEqual(load(plan_path), plan)
            apply = self.run_cli(
                manifests,
                "upgrade",
                "--target-root",
                str(target),
                "--plan",
                str(plan_path),
                "--approve-plan",
                plan["fingerprint"],
            )
            self.assertEqual(apply.returncode, 0, apply.stderr)
            upgraded = json.loads(apply.stdout)
            rollback = self.run_cli(
                manifests,
                "rollback",
                "--target-root",
                str(target),
                "--approve-current-lock",
                upgraded["package_lock_hash"],
                "--approve-rollback-point",
                load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")["fingerprint"],
            )
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertEqual(json.loads(rollback.stdout)["status"], "rolled-back")

    def test_tampered_rollback_point_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            operation = self.operation(self.changed_registry(root), target)
            apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            current = load(target / ".agent-skills" / "agent-skills.lock")
            snapshot_agents = target / ".agent-skills" / "rollback-point" / "AGENTS.md"
            snapshot_agents.write_text(snapshot_agents.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "rollback"):
                rollback_upgrade(
                    target,
                    approve_current_lock=current["fingerprint"],
                    approve_rollback_point=load(
                        target / ".agent-skills" / "rollback-point" / "rollback-point.json"
                    )["fingerprint"],
                )
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), current)

    def test_upgrade_source_change_after_plan_is_rejected_and_old_install_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            manifests = self.changed_registry(root)
            operation = self.operation(manifests, target)
            current = load(target / ".agent-skills" / "agent-skills.lock")
            skill = root / "repository" / "disciplines" / "documentation" / "skills" / "html-docs" / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "\nTOCTOU mutation.\n", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "differs from install plan"):
                apply_upgrade(
                    operation,
                    target,
                    approve_plan=operation.plan["fingerprint"],
                    approvals=operation.plan["approvals_required"],
                )
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), current)
            self.assertFalse((target / ".agent-skills" / "rollback-point").exists())
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

    def test_regular_install_cannot_discard_an_existing_rollback_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            operation = self.operation(self.changed_registry(root), target)
            apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            upgraded = load(target / ".agent-skills" / "agent-skills.lock")
            point = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")

            with self.assertRaisesRegex(ContractError, "persistent rollback point"):
                install_bundle(self.current_bundle, target)
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), upgraded)
            self.assertEqual(
                load(target / ".agent-skills" / "rollback-point" / "rollback-point.json"),
                point,
            )

    def test_source_activation_upgrade_requires_trusted_external_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            activated = target / "agents" / "managed.toml"
            activated.parent.mkdir()
            activated.write_text("managed = true\n", encoding="utf-8")
            activated.chmod(0o644)
            dump(
                {
                    "files": [
                        {
                            "mode": 0o644,
                            "path": "agents/managed.toml",
                            "sha256": hashlib.sha256(activated.read_bytes()).hexdigest(),
                        }
                    ],
                    "manager": "agent-development-skills",
                    "schema_version": "1.0",
                },
                target / ".agent-skills" / "activation-lock.json",
            )
            (target / ".agent-skills" / "activation-lock.json").chmod(0o644)

            manifests = self.changed_registry(root)
            candidate = prepare_upgrade_candidate(manifests, target)
            with self.assertRaisesRegex(ContractError, "source-installer activation"):
                plan_upgrade(
                    manifests,
                    target,
                    _evidence(candidate.bundle.package_lock),
                    schema_root=ROOT / "schemas",
                )

    def test_external_snapshot_restores_file_and_ancestor_directory_preimages(self) -> None:
        from agent_workflow.installation import _restore_external_state, _write_rollback_point

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            config = target / "config.toml"
            config.write_text("old-config\n", encoding="utf-8")
            config.chmod(0o600)
            empty_user_directory = target / "user-empty"
            empty_user_directory.mkdir()
            empty_user_directory.chmod(0o700)
            snapshot = root / "external-snapshot"
            _write_rollback_point(
                target,
                snapshot,
                external_paths=("config.toml", "new.config.toml", "user-empty/created.toml"),
            )

            config.write_text("new-config\n", encoding="utf-8")
            config.chmod(0o644)
            (target / "new.config.toml").write_text("created\n", encoding="utf-8")
            (empty_user_directory / "created.toml").write_text("created\n", encoding="utf-8")
            empty_user_directory.chmod(0o755)
            _restore_external_state(snapshot, target)

            self.assertEqual(config.read_text(encoding="utf-8"), "old-config\n")
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertFalse((target / "new.config.toml").exists())
            self.assertTrue(empty_user_directory.is_dir())
            self.assertEqual(empty_user_directory.stat().st_mode & 0o777, 0o700)
            self.assertFalse((empty_user_directory / "created.toml").exists())

    def test_real_source_activation_asset_upgrade_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            installed = subprocess.run(
                [
                    str(ROOT / "install.sh"),
                    "--target-root", str(target),
                    "--platform", "apple",
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            activated = target / "agents" / "reviewer.toml"
            original = activated.read_bytes()
            (target / "config.toml").chmod(0o600)
            retired = target / "bin" / "retired-tool"
            retired.write_text("legacy\n", encoding="utf-8")
            retired.chmod(0o755)
            activation_lock_path = target / ".agent-skills" / "activation-lock.json"
            activation_lock = load(activation_lock_path)
            activation_lock["files"].append({
                "mode": 0o755,
                "path": "bin/retired-tool",
                "sha256": hashlib.sha256(retired.read_bytes()).hexdigest(),
            })
            activation_lock["files"] = sorted(activation_lock["files"], key=lambda item: item["path"])
            dump(activation_lock, activation_lock_path)
            activation_lock_path.chmod(0o644)
            manifests = _copy_registry(root)
            source = root / "repository" / "disciplines" / "review" / "assets" / "codex" / "agents" / "reviewer.toml"
            source.write_bytes(source.read_bytes() + b"\n# upgraded activation fixture\n")
            candidate = prepare_upgrade_candidate(manifests, target)
            external_paths, external_handler, external_handler_sha256 = _source_upgrade_context(
                manifests,
                target,
                candidate.selection,
            )
            operation = plan_upgrade(
                manifests,
                target,
                _evidence(candidate.bundle.package_lock),
                schema_root=root / "repository" / "schemas",
                external_paths=external_paths,
                external_handler=external_handler,
                external_handler_sha256=external_handler_sha256,
            )
            old_lock = load(target / ".agent-skills" / "agent-skills.lock")
            with patch(
                "agent_workflow.upgrade.run_installed_workflow_smoke",
                side_effect=ContractError("injected installed smoke failure"),
            ):
                with self.assertRaisesRegex(ContractError, "injected installed smoke failure"):
                    apply_upgrade(
                        operation,
                        target,
                        approve_plan=operation.plan["fingerprint"],
                        approvals=operation.plan["approvals_required"],
                    )
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), old_lock)
            self.assertEqual(activated.read_bytes(), original)
            self.assertTrue(retired.is_file())
            self.assertFalse((target / ".agent-skills" / "rollback-point").exists())

            from agent_workflow.activation import apply_source_activation as real_activation

            def fail_after_bound_activation(installed_target: Path, *, selected_platforms):
                real_activation(installed_target, selected_platforms=selected_platforms)
                raise OSError("injected bound activation failure")

            with patch(
                "agent_workflow.upgrade.apply_source_activation",
                side_effect=fail_after_bound_activation,
            ):
                with self.assertRaisesRegex(OSError, "injected bound activation failure"):
                    apply_upgrade(
                        operation,
                        target,
                        approve_plan=operation.plan["fingerprint"],
                        approvals=operation.plan["approvals_required"],
                    )
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), old_lock)
            self.assertEqual(activated.read_bytes(), original)
            self.assertTrue(retired.is_file())
            self.assertFalse((target / ".agent-skills" / "rollback-point").exists())

            apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            upgraded = load(target / ".agent-skills" / "agent-skills.lock")
            point = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")
            self.assertIn(b"upgraded activation fixture", activated.read_bytes())
            self.assertEqual((target / "config.toml").stat().st_mode & 0o777, 0o600)
            self.assertFalse(retired.exists())
            agent_session = subprocess.run(
                [str(target / "bin" / "agent-session"), "--help"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(agent_session.returncode, 0, agent_session.stderr)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

            rollback_upgrade(
                target,
                approve_current_lock=upgraded["fingerprint"],
                approve_rollback_point=point["fingerprint"],
            )
            self.assertEqual(activated.read_bytes(), original)
            self.assertEqual(retired.read_text(encoding="utf-8"), "legacy\n")
            self.assertEqual(retired.stat().st_mode & 0o777, 0o755)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

    def test_activation_lock_v1_migration_is_planned_applied_and_exactly_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            installed = subprocess.run(
                [
                    str(ROOT / "install.sh"), "--target-root", str(target),
                    "--platform", "apple", "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            activation_lock_path = target / ".agent-skills" / "activation-lock.json"
            legacy = load(activation_lock_path)
            legacy.pop("handler")
            legacy["schema_version"] = "1.0"
            dump(legacy, activation_lock_path)
            activation_lock_path.chmod(0o644)
            legacy_bytes = activation_lock_path.read_bytes()
            legacy_doctor = diagnose_install(target, schema_root=ROOT / "schemas")
            self.assertEqual(legacy_doctor["status"], "passed")
            activation_check = next(
                item for item in legacy_doctor["checks"] if item["id"] == "activation.integrity"
            )
            self.assertEqual(activation_check["details"]["deprecation"], "blocked-new-use")

            candidate = prepare_upgrade_candidate(MANIFESTS, target)
            external_paths, external_handler, handler_hash = _source_upgrade_context(
                MANIFESTS, target, candidate.selection
            )
            operation = plan_upgrade(
                MANIFESTS,
                target,
                _evidence(candidate.bundle.package_lock),
                schema_root=ROOT / "schemas",
                external_paths=external_paths,
                external_handler=external_handler,
                external_handler_sha256=handler_hash,
            )
            self.assertEqual(operation.plan["status"], "planned")
            self.assertEqual(len(operation.plan["migrations"]), 1)
            migration = operation.plan["migrations"][0]
            self.assertEqual(migration["artifact"], "activation-lock")
            self.assertEqual((migration["from_version"], migration["to_version"]), ("1.0", "2.0"))
            self.assertTrue(migration["lossless"])

            reordered = load(activation_lock_path)
            reordered["files"] = list(reversed(reordered["files"]))
            dump(reordered, activation_lock_path)
            activation_lock_path.chmod(0o644)
            with self.assertRaisesRegex(ContractError, "rollback point differs from the approved upgrade plan"):
                apply_upgrade(
                    operation,
                    target,
                    approve_plan=operation.plan["fingerprint"],
                    approvals=operation.plan["approvals_required"],
                )
            activation_lock_path.write_bytes(legacy_bytes)
            activation_lock_path.chmod(0o644)

            result = apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            migrated = load(activation_lock_path)
            self.assertEqual(migrated["schema_version"], "2.0")
            self.assertEqual(migrated["handler"], "core.source-activation.apple-codex-v1")
            point = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")
            rollback_upgrade(
                target,
                approve_current_lock=result["package_lock_hash"],
                approve_rollback_point=point["fingerprint"],
            )
            self.assertEqual(activation_lock_path.read_bytes(), legacy_bytes)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

    def test_new_activation_destination_cannot_overwrite_unmanaged_user_file(self) -> None:
        from agent_workflow.activation import apply_source_activation

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            installed = subprocess.run(
                [
                    str(ROOT / "install.sh"), "--target-root", str(target),
                    "--platform", "apple", "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            destination = target / "agents" / "reviewer.toml"
            activation_lock_path = target / ".agent-skills" / "activation-lock.json"
            activation_lock = load(activation_lock_path)
            activation_lock["files"] = [
                item for item in activation_lock["files"]
                if item["path"] != "agents/reviewer.toml"
            ]
            dump(activation_lock, activation_lock_path)
            activation_lock_path.chmod(0o644)
            destination.write_text("user-owned = true\n", encoding="utf-8")
            destination.chmod(0o644)

            with self.assertRaisesRegex(ContractError, "unmanaged activation destination"):
                apply_source_activation(target, selected_platforms=["apple"])
            self.assertEqual(destination.read_text(encoding="utf-8"), "user-owned = true\n")

    def test_partial_platform_uninstall_recomposes_closure_and_is_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            installed = subprocess.run(
                [
                    str(ROOT / "install.sh"), "--target-root", str(target),
                    "--platform", "apple", "--platform", "desktop", "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            old_lock = load(target / ".agent-skills" / "agent-skills.lock")
            activated = target / "agents" / "reviewer.toml"
            activated_value = activated.read_bytes()
            profile = target / "readonly.config.toml"
            profile_value = profile.read_bytes()

            preview = self.run_cli(
                MANIFESTS,
                "uninstall", "--target-root", str(target),
                "--platform", "apple", "--dry-run",
                "--output", str(Path(directory) / "uninstall-plan.json"),
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            self.assertEqual(plan["action"], "partial-uninstall")
            self.assertEqual(plan["selection"]["platforms"], ["desktop"])
            self.assertNotIn("codex", plan["selection"]["runtime_configs"])
            mismatched_apply = self.run_cli(
                MANIFESTS,
                "uninstall", "--target-root", str(target),
                "--platform", "desktop",
                "--plan", str(Path(directory) / "uninstall-plan.json"),
                "--approve-plan", plan["fingerprint"],
            )
            self.assertEqual(mismatched_apply.returncode, 2)
            self.assertIn("platform request differs", mismatched_apply.stderr)
            apply = self.run_cli(
                MANIFESTS,
                "uninstall", "--target-root", str(target),
                "--platform", "apple",
                "--plan", str(Path(directory) / "uninstall-plan.json"),
                "--approve-plan", plan["fingerprint"],
            )
            self.assertEqual(apply.returncode, 0, apply.stderr)
            result = json.loads(apply.stdout)
            self.assertEqual(result["status"], "partially-uninstalled")
            installed_lock = load(target / ".agent-skills" / "install-lock.json")
            self.assertEqual(installed_lock["selected_platforms"], ["desktop"])
            package_ids = [item["id"] for item in installed_lock["selected_packages"]]
            self.assertNotIn("apple", package_ids)
            self.assertNotIn("codex", package_ids)
            self.assertFalse(activated.exists())
            self.assertNotIn("model_instructions_file", (target / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(profile.read_bytes(), profile_value)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

            point = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")
            rollback_upgrade(
                target,
                approve_current_lock=result["package_lock_hash"],
                approve_rollback_point=point["fingerprint"],
            )
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), old_lock)
            self.assertEqual(activated.read_bytes(), activated_value)
            self.assertEqual(profile.read_bytes(), profile_value)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

            desktop_plan_path = Path(directory) / "desktop-uninstall-plan.json"
            preview_desktop = self.run_cli(
                MANIFESTS,
                "uninstall", "--target-root", str(target),
                "--platform", "desktop", "--dry-run", "--output", str(desktop_plan_path),
            )
            self.assertEqual(preview_desktop.returncode, 0, preview_desktop.stderr)
            desktop_plan = json.loads(preview_desktop.stdout)
            self.assertEqual(desktop_plan["selection"]["platforms"], ["apple"])
            remove_desktop = self.run_cli(
                MANIFESTS,
                "uninstall", "--target-root", str(target),
                "--platform", "desktop", "--plan", str(desktop_plan_path),
                "--approve-plan", desktop_plan["fingerprint"],
            )
            self.assertEqual(remove_desktop.returncode, 0, remove_desktop.stderr)
            apple_lock = load(target / ".agent-skills" / "install-lock.json")
            self.assertEqual(apple_lock["selected_platforms"], ["apple"])
            self.assertIn("codex", apple_lock["selected_runtime_configs"])
            self.assertEqual(activated.read_bytes(), activated_value)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")


    def test_partial_uninstall_selection_rejects_unknown_duplicate_and_mixed_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.install(Path(directory))
            with self.assertRaisesRegex(ContractError, "not installed"):
                _partial_uninstall_selection(target, ["desktop"])
            with self.assertRaisesRegex(ContractError, "unique"):
                _partial_uninstall_selection(target, ["apple", "apple"])
            with self.assertRaisesRegex(ContractError, "cannot be combined"):
                _partial_uninstall_selection(target, ["all", "apple"])
            selection = _partial_uninstall_selection(target, ["all"])
            self.assertTrue(selection["core_only"])
            self.assertEqual(selection["platforms"], [])

    def test_partial_uninstall_blocks_remaining_package_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            bundle = build_install_bundle(
                MANIFESTS,
                platforms=["apple", "desktop"],
                schema_root=ROOT / "schemas",
            )
            install_bundle(bundle, target)
            manifests = self.changed_registry(root)
            candidate = prepare_upgrade_candidate(
                manifests,
                target,
                platforms=["apple"],
                disciplines=[],
                runtime_configs=[],
                core_only=False,
            )
            with self.assertRaisesRegex(ContractError, "remaining packages"):
                plan_upgrade(
                    manifests,
                    target,
                    _evidence(candidate.bundle.package_lock),
                    schema_root=root / "repository" / "schemas",
                    platforms=["apple"],
                    disciplines=[],
                    runtime_configs=[],
                    core_only=False,
                    action="partial-uninstall",
                    removed_platforms=["desktop"],
                )

    def test_partial_uninstall_planner_enforces_runtime_ownership_and_handler_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            installed = subprocess.run(
                [
                    str(ROOT / "install.sh"), "--target-root", str(target),
                    "--platform", "apple", "--platform", "desktop", "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            apple_without_runtime = prepare_upgrade_candidate(
                MANIFESTS,
                target,
                platforms=["apple"],
                disciplines=[],
                runtime_configs=[],
                core_only=False,
            )
            deactivation_paths, deactivation_handler, handler_hash = (
                _partial_uninstall_external_context(target, ["apple"])
            )
            with self.assertRaisesRegex(ContractError, "activation-owned codex"):
                plan_upgrade(
                    MANIFESTS,
                    target,
                    _evidence(apple_without_runtime.bundle.package_lock),
                    schema_root=ROOT / "schemas",
                    platforms=["apple"],
                    disciplines=[],
                    runtime_configs=[],
                    core_only=False,
                    external_paths=deactivation_paths,
                    external_handler=deactivation_handler,
                    external_handler_sha256=handler_hash,
                    action="partial-uninstall",
                    removed_platforms=["desktop"],
                    removed_runtime_configs=["codex"],
                )

            apple_preserved = prepare_upgrade_candidate(
                MANIFESTS,
                target,
                platforms=["apple"],
                disciplines=[],
                runtime_configs=["codex"],
                core_only=False,
            )
            activation_paths, activation_handler, handler_hash = _source_upgrade_context(
                MANIFESTS, target, apple_preserved.selection
            )
            with self.assertRaisesRegex(ContractError, "handler differs"):
                plan_upgrade(
                    MANIFESTS,
                    target,
                    _evidence(apple_preserved.bundle.package_lock),
                    schema_root=ROOT / "schemas",
                    platforms=["apple"],
                    disciplines=[],
                    runtime_configs=["codex"],
                    core_only=False,
                    external_paths=activation_paths,
                    external_handler=activation_handler,
                    external_handler_sha256=handler_hash,
                    action="partial-uninstall",
                    removed_platforms=["desktop"],
                )

            desktop_preserved = prepare_upgrade_candidate(
                MANIFESTS,
                target,
                platforms=["desktop"],
                disciplines=[],
                runtime_configs=[],
                core_only=False,
            )
            with self.assertRaisesRegex(ContractError, "handler differs"):
                plan_upgrade(
                    MANIFESTS,
                    target,
                    _evidence(desktop_preserved.bundle.package_lock),
                    schema_root=ROOT / "schemas",
                    platforms=["desktop"],
                    disciplines=[],
                    runtime_configs=[],
                    core_only=False,
                    action="partial-uninstall",
                    removed_platforms=["apple"],
                    removed_runtime_configs=["codex"],
                )

    def test_lifecycle_lock_serializes_apply_and_doctor_reports_crash_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            operation = self.operation(self.changed_registry(root), target)
            with target_lifecycle_lock(target):
                with self.assertRaisesRegex(ContractError, "already active"):
                    apply_upgrade(
                        operation,
                        target,
                        approve_plan=operation.plan["fingerprint"],
                        approvals=operation.plan["approvals_required"],
                    )
                self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "blocked")
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")

    def test_conformance_runner_rejects_candidate_from_a_different_schema_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = deepcopy(self.current_bundle.package_lock)
            candidate["schema_inventory"]["files"][0]["sha256"] = "0" * 64
            candidate["schema_inventory"]["content_sha256"] = sha256(
                candidate["schema_inventory"]["files"]
            )
            candidate["fingerprint"] = sha256(
                {key: value for key, value in candidate.items() if key != "fingerprint"}
            )
            path = Path(directory) / "candidate.lock"
            dump(candidate, path)
            completed = subprocess.run(
                [sys.executable, "scripts/run_conformance.py", "--upgrade-lock", str(path)],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("schema inventory differs", completed.stderr)

    def test_rollback_swap_failure_restores_upgraded_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            operation = self.operation(self.changed_registry(root), target)
            apply_upgrade(
                operation,
                target,
                approve_plan=operation.plan["fingerprint"],
                approvals=operation.plan["approvals_required"],
            )
            upgraded = load(target / ".agent-skills" / "agent-skills.lock")
            point = load(target / ".agent-skills" / "rollback-point" / "rollback-point.json")
            real_replace = os.replace
            failed = False

            def fail_once(source, destination):
                nonlocal failed
                source_path = Path(source)
                if not failed and source_path.name == "skills" and source_path.parent.name.startswith(".agent-skills-stage-"):
                    failed = True
                    raise OSError("injected rollback swap failure")
                return real_replace(source, destination)

            with patch("agent_workflow.installation.os.replace", side_effect=fail_once):
                with self.assertRaisesRegex(OSError, "injected rollback swap failure"):
                    rollback_upgrade(
                        target,
                        approve_current_lock=upgraded["fingerprint"],
                        approve_rollback_point=point["fingerprint"],
                    )
            self.assertEqual(load(target / ".agent-skills" / "agent-skills.lock"), upgraded)
            self.assertEqual(
                load(target / ".agent-skills" / "rollback-point" / "rollback-point.json"),
                point,
            )
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")
            self.assertFalse(any(target.glob(".agent-skills-stage-*")))
            self.assertFalse(any(target.glob(".agent-skills-backup-*")))


if __name__ == "__main__":
    unittest.main()
