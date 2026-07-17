from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

from tests.support import MANIFESTS, ROOT

from agent_workflow.canonical_json import dump, dumps, load, sha256
from agent_workflow.contracts import validate
from agent_workflow.doctor import diagnose_install
from agent_workflow.installation import build_install_bundle, install_bundle
from agent_workflow.models import ContractError
from agent_workflow.package_lock import install_plan_identity_hash, schema_inventory


def _filesystem_identity(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = path.lstat().st_mode & 0o777
        if path.is_symlink():
            entries.append({"kind": "symlink", "mode": mode, "path": relative, "target": os.readlink(path)})
        elif path.is_dir():
            entries.append({"kind": "directory", "mode": mode, "path": relative})
        else:
            entries.append({
                "kind": "file",
                "mode": mode,
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    return entries


def _check(report: dict[str, object], check_id: str) -> dict[str, object]:
    return next(item for item in report["checks"] if item["id"] == check_id)  # type: ignore[index, union-attr]


def _write_reanchored_locks(target: Path, package_lock: dict[str, object]) -> None:
    managed = target / ".agent-skills"
    install_lock_path = managed / "install-lock.json"
    package_lock_path = managed / "agent-skills.lock"
    package_lock["fingerprint"] = sha256({
        key: value for key, value in package_lock.items() if key != "fingerprint"
    })
    install_lock = load(install_lock_path)
    install_lock["package_lock_hash"] = package_lock["fingerprint"]
    install_lock["fingerprint"] = sha256({
        key: value
        for key, value in install_lock.items()
        if key not in {"fingerprint", "status"}
    })
    dump(package_lock, package_lock_path)
    dump(install_lock, install_lock_path)


def _write_semantically_forged_locks(
    target: Path,
    install_lock: dict[str, Any],
    package_lock: dict[str, Any],
) -> None:
    package_lock["install_plan_identity_hash"] = install_plan_identity_hash(install_lock)
    package_lock["fingerprint"] = sha256({
        key: value for key, value in package_lock.items() if key != "fingerprint"
    })
    install_lock["package_lock_hash"] = package_lock["fingerprint"]
    install_lock["fingerprint"] = sha256({
        key: value for key, value in install_lock.items() if key not in {"fingerprint", "status"}
    })
    validate("agent-skills-lock", package_lock)
    validate("install-plan", install_lock)
    managed = target / ".agent-skills"
    dump(package_lock, managed / "agent-skills.lock")
    dump(install_lock, managed / "install-lock.json")


class DoctorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = build_install_bundle(MANIFESTS, platforms=["apple"])

    def install(self, root: Path) -> Path:
        target = root / "codex"
        install_bundle(self.bundle, target)
        return target

    def run_cli(self, target: Path, *, schemas: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        command = [
            sys.executable,
            "-m",
            "agent_workflow.cli",
            "doctor",
            "--target-root",
            str(target),
        ]
        if schemas is not None:
            command.extend(("--schemas", str(schemas)))
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_healthy_install_passes_every_check_and_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.install(Path(directory))
            before = _filesystem_identity(target)
            first = diagnose_install(target, schema_root=ROOT / "schemas")
            second = diagnose_install(target, schema_root=ROOT / "schemas")
            after = _filesystem_identity(target)
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertEqual(first["status"], "passed")
            self.assertEqual(first["summary"]["failed"], 0)
            self.assertEqual(first["summary"]["skipped"], 0)
            self.assertEqual(first["install"]["package_lock_hash"], self.bundle.package_lock["fingerprint"])
            self.assertEqual(
                _check(first, "schema.inventory")["details"]["file_count"],
                len(schema_inventory(ROOT / "schemas")["files"]),
            )
            self.assertEqual(_check(first, "instructions.global")["status"], "passed")
            self.assertEqual(_check(first, "binding.freeze")["status"], "passed")
            self.assertEqual(_check(first, "permission.freeze")["status"], "passed")
            validate("doctor-report", first)
            cli = self.run_cli(target)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout), first)
            malformed = deepcopy(first)
            malformed["checks"][0]["status"] = []
            with self.assertRaises(ContractError):
                validate("doctor-report", malformed)

    def test_core_single_and_multi_platform_installations_pass(self) -> None:
        configurations = (
            ("core-only", {"core_only": True}),
            ("apple", {"platforms": ["apple"]}),
            ("desktop", {"platforms": ["desktop"]}),
            ("apple-desktop", {"platforms": ["apple", "desktop"]}),
        )
        for label, arguments in configurations:
            with self.subTest(configuration=label), tempfile.TemporaryDirectory() as directory:
                bundle = build_install_bundle(MANIFESTS, **arguments)
                target = Path(directory) / "codex"
                install_bundle(bundle, target)
                report = diagnose_install(target, schema_root=ROOT / "schemas")
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["install"]["selected_platforms"], bundle.plan["selected_platforms"])

    def test_cli_returns_canonical_report_and_blocked_exit_for_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.install(Path(directory))
            agents = target / "AGENTS.md"
            agents.write_text(agents.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
            result = self.run_cli(target)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "")
            report = json.loads(result.stdout)
            self.assertEqual(result.stdout, dumps(report))
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(_check(report, "instructions.global")["status"], "failed")
            validate("doctor-report", report)

    def test_package_skill_activation_and_layout_drift_are_attributed(self) -> None:
        mutations = (
            (
                "package.integrity",
                lambda target: (target / ".agent-skills" / "packages" / "core" / "manifest.json").write_text("{}\n"),
            ),
            (
                "skill.integrity",
                lambda target: (target / "skills" / "ios-feature-implementation" / "SKILL.md").write_text("changed\n"),
            ),
            (
                "filesystem.layout",
                lambda target: (target / ".agent-skills" / "unknown").write_text("unexpected\n"),
            ),
        )
        for expected_check, mutate in mutations:
            with self.subTest(check=expected_check), tempfile.TemporaryDirectory() as directory:
                target = self.install(Path(directory))
                mutate(target)
                report = diagnose_install(target, schema_root=ROOT / "schemas")
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(_check(report, expected_check)["status"], "failed")

        with tempfile.TemporaryDirectory() as directory:
            target = self.install(Path(directory))
            activated = target / "bin" / "managed-tool"
            activated.parent.mkdir()
            activated.write_text("managed\n", encoding="utf-8")
            activated.chmod(0o755)
            activation_lock = {
                "files": [{
                    "mode": 0o755,
                    "path": "bin/managed-tool",
                    "sha256": hashlib.sha256(activated.read_bytes()).hexdigest(),
                }],
                "manager": "agent-development-skills",
                "schema_version": "1.0",
            }
            lock_path = target / ".agent-skills" / "activation-lock.json"
            dump(activation_lock, lock_path)
            self.assertEqual(diagnose_install(target, schema_root=ROOT / "schemas")["status"], "passed")
            activated.write_text("tampered\n", encoding="utf-8")
            report = diagnose_install(target, schema_root=ROOT / "schemas")
            self.assertEqual(_check(report, "activation.integrity")["status"], "failed")

    def test_schema_drift_recovery_residue_and_symlink_target_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.install(root)
            schema_repository = root / "schema-repository"
            for entry in self.bundle.package_lock["schema_inventory"]["files"]:
                source = ROOT / entry["path"]
                destination = schema_repository / entry["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            schema_root = schema_repository / "schemas"
            self.assertEqual(diagnose_install(target, schema_root=schema_root)["status"], "passed")
            changed_schema = schema_root / "doctor-report-v1.schema.json"
            changed_schema.write_text(changed_schema.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            drifted = diagnose_install(target, schema_root=schema_root)
            self.assertEqual(_check(drifted, "schema.inventory")["status"], "failed")

            residue = target / ".agent-skills-backup-recovery"
            residue.mkdir()
            recovery = diagnose_install(target, schema_root=ROOT / "schemas")
            self.assertEqual(recovery["recovery"]["status"], "attention")
            self.assertEqual(_check(recovery, "recovery.residue")["status"], "failed")

            linked = root / "linked-codex"
            linked.symlink_to(target)
            linked_report = diagnose_install(linked, schema_root=ROOT / "schemas")
            self.assertEqual(linked_report["status"], "blocked")
            self.assertEqual(linked_report["recovery"]["status"], "unknown")
            self.assertEqual(_check(linked_report, "filesystem.target")["status"], "failed")
            validate("doctor-report", linked_report)

    def test_lock_anchor_and_legacy_install_without_persistent_lock_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self.install(Path(directory))
            package_lock_path = target / ".agent-skills" / "agent-skills.lock"
            package_lock_path.unlink()
            report = diagnose_install(target, schema_root=ROOT / "schemas")
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(_check(report, "filesystem.layout")["status"], "failed")
            self.assertEqual(_check(report, "lock.persistent")["status"], "failed")
            self.assertEqual(_check(report, "binding.freeze")["status"], "skipped")

        with tempfile.TemporaryDirectory() as directory:
            target = self.install(Path(directory))
            lock_path = target / ".agent-skills" / "agent-skills.lock"
            lock = deepcopy(load(lock_path))
            lock["lineage"]["previous_lock_hash"] = "a" * 64
            lock["fingerprint"] = hashlib.sha256(
                dumps({key: value for key, value in lock.items() if key != "fingerprint"}).encode("utf-8")
            ).hexdigest()
            dump(lock, lock_path)
            report = diagnose_install(target, schema_root=ROOT / "schemas")
            self.assertEqual(_check(report, "lock.persistent")["status"], "failed")

    def test_cross_lock_binding_and_permission_drift_are_attributed(self) -> None:
        mutations = ("binding", "permission")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                target = self.install(Path(directory))
                managed = target / ".agent-skills"
                package_lock_path = managed / "agent-skills.lock"
                package_lock = load(package_lock_path)
                capability = sorted(package_lock["capability_providers"])[0]
                if mutation == "binding":
                    package_lock["capability_providers"][capability]["binding"] = {
                        "kind": "skill",
                        "name": "forged-binding",
                    }
                    package_lock["bindings_sha256"] = sha256({
                        name: {"binding": provider["binding"], "package": provider["package"]}
                        for name, provider in sorted(package_lock["capability_providers"].items())
                    })
                else:
                    package_lock["permission_profiles"] = ["forged-permission"]
                    for provider in package_lock["capability_providers"].values():
                        provider["permission_profile"] = "forged-permission"
                _write_reanchored_locks(target, package_lock)
                report = diagnose_install(target, schema_root=ROOT / "schemas")
                self.assertEqual(report["status"], "blocked")
                expected_check = "binding.freeze" if mutation == "binding" else "permission.freeze"
                self.assertEqual(_check(report, "lock.persistent")["status"], "passed")
                self.assertEqual(_check(report, expected_check)["status"], "failed")

    def test_persistent_lock_package_closure_and_full_identity_drift_fail_closed(self) -> None:
        mutations = ("phantom-package", "provider-digest", "core-compatibility", "provider-version")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                target = self.install(Path(directory))
                lock = load(target / ".agent-skills" / "agent-skills.lock")
                if mutation == "phantom-package":
                    phantom = deepcopy(lock["packages"][-1])
                    phantom["id"] = "phantom"
                    phantom["source"]["uri"] = "registry://phantom"
                    lock["packages"].append(phantom)
                elif mutation == "provider-digest":
                    package = next(item for item in lock["packages"] if item["provider_manifest_sha256"] is not None)
                    package["provider_manifest_sha256"] = "f" * 64
                elif mutation == "core-compatibility":
                    package = lock["packages"][1]
                    package["core_compatibility"] = ">=0.0.0"
                else:
                    package = next(item for item in lock["packages"] if item["provider_version"] is not None)
                    package["provider_version"] = "0.2.1"
                    package["provider_compatibility"] = ">=0.0.0"
                _write_reanchored_locks(target, lock)
                report = diagnose_install(target, schema_root=ROOT / "schemas")
                self.assertEqual(report["status"], "blocked")
                if mutation == "phantom-package":
                    self.assertEqual(_check(report, "lock.persistent")["status"], "failed")
                else:
                    self.assertEqual(_check(report, "lock.persistent")["status"], "passed")
                    self.assertEqual(_check(report, "package.integrity")["status"], "failed")

    def test_double_lock_semantic_forgery_is_rebuilt_from_installed_manifests(self) -> None:
        for mutation in ("binding", "permission", "compatibility"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                target = self.install(Path(directory))
                managed = target / ".agent-skills"
                install_lock = load(managed / "install-lock.json")
                package_lock = load(managed / "agent-skills.lock")
                if mutation == "binding":
                    capability = sorted(install_lock["bindings"])[0]
                    forged = {"kind": "skill", "name": "forged-binding"}
                    install_lock["bindings"][capability]["binding"] = forged
                    install_lock["capability_providers"][capability]["binding"] = forged
                    package_lock["capability_providers"][capability]["binding"] = forged
                    package_lock["bindings_sha256"] = sha256({
                        name: {"binding": provider["binding"], "package": provider["package"]}
                        for name, provider in sorted(package_lock["capability_providers"].items())
                    })
                elif mutation == "permission":
                    capability = next(
                        name
                        for name, provider in install_lock["capability_providers"].items()
                        if any(
                            profile != provider["permission_profile"]
                            for profile in install_lock["permission_profiles"]
                        )
                    )
                    current = install_lock["capability_providers"][capability]["permission_profile"]
                    forged = next(profile for profile in install_lock["permission_profiles"] if profile != current)
                    install_lock["capability_providers"][capability]["permission_profile"] = forged
                    package_lock["capability_providers"][capability]["permission_profile"] = forged
                else:
                    selected = next(
                        item for item in install_lock["selected_packages"] if item["provider_version"] is not None
                    )
                    persistent = next(item for item in package_lock["packages"] if item["id"] == selected["id"])
                    for item in (selected, persistent):
                        item["core_compatibility"] = ">=0.0.0"
                        item["provider_version"] = "0.2.1"
                        item["provider_compatibility"] = ">=0.0.0"
                _write_semantically_forged_locks(target, install_lock, package_lock)
                report = diagnose_install(target, schema_root=ROOT / "schemas")
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(_check(report, "lock.persistent")["status"], "passed")
                expected = {
                    "binding": "binding.freeze",
                    "permission": "permission.freeze",
                    "compatibility": "package.integrity",
                }[mutation]
                self.assertEqual(_check(report, expected)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
