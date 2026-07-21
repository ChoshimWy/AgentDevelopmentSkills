from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"


def load_uninstaller_module():
    spec = importlib.util.spec_from_file_location(
        "uninstall_local_under_test", ROOT / "scripts/uninstall_local.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load uninstall_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UninstallScriptTests(unittest.TestCase):
    def install(self, target: Path) -> dict[str, object]:
        completed = subprocess.run(
            [
                str(INSTALL),
                "--target-root",
                str(target),
                "--platform",
                "apple",
                "--json",
            ],
            cwd=ROOT,
            env={**os.environ, "AGENT_SKILLS_INSTALL_ENGINE": "python"},
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def uninstall(
        self,
        target: Path,
        *arguments: str,
        check: bool = True,
        json_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(UNINSTALL), "--target-root", str(target), *arguments]
        if json_output:
            command.append("--json")
        return subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "AGENT_SKILLS_UNINSTALL_ENGINE": "python"},
            check=check,
            capture_output=True,
            text=True,
        )

    def test_dry_run_then_uninstall_removes_only_managed_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            target.mkdir()
            (target / "config.toml").write_text(
                'model = "keep-me"\n\n[custom]\nvalue = 7\n', encoding="utf-8"
            )
            self.install(target)
            config_path = target / "config.toml"
            config_text = config_path.read_text(encoding="utf-8").replace(
                "model_instructions_file =", '"model_instructions_file" ='
            )
            config_path.write_text(
                "# user header comment\n" + config_text + "# trailing user note\n",
                encoding="utf-8",
            )
            (target / "config.toml").chmod(0o600)
            unowned_files = {
                target / "agents" / "local.toml": "local agent\n",
                target / "bin" / "local-tool": "#!/bin/sh\n",
                target / "templates" / "local.txt": "local template\n",
            }
            for path, content in unowned_files.items():
                path.write_text(content, encoding="utf-8")
            system_skill = target / "skills" / ".system" / "openai-docs" / "SKILL.md"
            system_skill.parent.mkdir(parents=True)
            system_skill.write_text("system\n", encoding="utf-8")

            human = self.uninstall(target, "--dry-run", json_output=False)
            preview = json.loads(self.uninstall(target, "--platform", "apple", "--dry-run").stdout)
            self.assertIn("Agent Development Skills 卸载预览", human.stdout)
            self.assertIn("未写入任何文件", human.stdout)
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["selected_platforms"], ["apple"])
            self.assertEqual(preview["config_action"], "removed-managed-instructions-path")
            self.assertTrue(preview["preserved_system_skills"])
            self.assertTrue((target / "AGENTS.md").is_file())

            result = json.loads(self.uninstall(target, "--platform", "all").stdout)
            self.assertEqual(result["status"], "uninstalled")
            self.assertFalse((target / "AGENTS.md").exists())
            self.assertFalse((target / ".agent-skills").exists())
            self.assertTrue(system_skill.is_file())
            for path in result["activated_files"]:
                self.assertFalse((target / path).exists(), path)
            for profile in result["preserved_profiles"]:
                self.assertTrue((target / profile).is_file(), profile)
            for path, content in unowned_files.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)
            config = (target / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "keep-me"', config)
            self.assertIn("[custom]", config)
            self.assertIn("# user header comment", config)
            self.assertIn("# trailing user note", config)
            self.assertNotIn("model_instructions_file", config)
            self.assertEqual((target / "config.toml").stat().st_mode & 0o777, 0o600)

            repeated = self.uninstall(target, check=False)
            self.assertEqual(repeated.returncode, 2)
            self.assertIn("unmanaged or modified install", repeated.stderr)

    def test_custom_model_instructions_path_is_preserved_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            config_path = target / "config.toml"
            config = config_path.read_text(encoding="utf-8").replace(
                str(target / "AGENTS.md"), "/custom/AGENTS.md"
            )
            config_path.write_text(config, encoding="utf-8")
            config_path.chmod(0o600)
            expected = config_path.read_bytes()

            result = json.loads(self.uninstall(target).stdout)

            self.assertEqual(result["config_action"], "preserved")
            self.assertEqual(config_path.read_bytes(), expected)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_managed_instruction_key_spellings_are_removed_without_reformatting(self) -> None:
        for key in (
            "model_instructions_file",
            '"model_instructions_file"',
            "'model_instructions_file'",
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / ".codex"
                self.install(target)
                config_path = target / "config.toml"
                config = config_path.read_text(encoding="utf-8").replace(
                    "model_instructions_file", key,
                    1,
                )
                config_path.write_text(
                    "# before managed key\n" + config + "# after all settings\n",
                    encoding="utf-8",
                )

                self.uninstall(target)

                result = config_path.read_text(encoding="utf-8")
                self.assertNotIn("model_instructions_file", result)
                self.assertTrue(result.startswith("# before managed key\n"))
                self.assertTrue(result.endswith("# after all settings\n"))

    def test_modified_install_or_activated_file_blocks_without_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            agents = target / "AGENTS.md"
            agents.write_text(agents.read_text(encoding="utf-8") + "modified\n", encoding="utf-8")

            blocked = self.uninstall(target, check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("unmanaged or modified install", blocked.stderr)
            self.assertTrue((target / ".agent-skills" / "install-lock.json").is_file())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            reviewer = target / "agents" / "reviewer.toml"
            reviewer.write_text("modified\n", encoding="utf-8")

            blocked = self.uninstall(target, check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("unmanaged or modified install", blocked.stderr)
            self.assertEqual(reviewer.read_text(encoding="utf-8"), "modified\n")
            self.assertTrue((target / "AGENTS.md").is_file())

    def test_platform_selection_and_target_symlink_fail_closed(self) -> None:
        module = load_uninstaller_module()
        lock = {"selected_platforms": ["apple", "web"]}
        self.assertEqual(module._selected_platforms(lock, ["apple"]), ("apple",))
        with self.assertRaisesRegex(module.ContractError, "platform is not installed"):
            module._selected_platforms(lock, ["android"])
        with self.assertRaisesRegex(module.ContractError, "cannot be combined"):
            module._selected_platforms(lock, ["all", "apple"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_target = root / "real"
            real_target.mkdir()
            linked_target = root / "linked"
            linked_target.symlink_to(real_target)
            blocked = self.uninstall(linked_target, check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("must not be a symlink", blocked.stderr)

    def test_partial_platform_uninstall_uses_the_guarded_upgrade_transaction(self) -> None:
        from agent_workflow.upgrade import make_upgrade_conformance_evidence

        def evidence(package_lock):
            return make_upgrade_conformance_evidence(
                package_lock,
                manifest_count=1,
                negative_contract_count=1,
                test_count=1,
                suite_definition_hash="1" * 64,
                runner_sha256="2" * 64,
                environment={"platform": "test", "python": "3.14.0"},
                command_results=[{
                    "command": "test",
                    "exit_code": 0,
                    "stdout_sha256": "3" * 64,
                    "stderr_sha256": "4" * 64,
                }],
            )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            # Reuse the public source installer module to add Desktop first.
            installer_spec = importlib.util.spec_from_file_location("install_local", ROOT / "scripts/install_local.py")
            assert installer_spec is not None and installer_spec.loader is not None
            installer_module = importlib.util.module_from_spec(installer_spec)
            installer_spec.loader.exec_module(installer_module)
            with mock.patch.object(installer_module, "run_upgrade_conformance", side_effect=lambda _, lock: evidence(lock)):
                installer_module.run(SimpleNamespace(
                    target_root=str(target), platform=["desktop"], dry_run=False, json=True
                ))

            module = load_uninstaller_module()
            with mock.patch.object(module, "run_upgrade_conformance", side_effect=lambda _, lock: evidence(lock)):
                result = module.run(SimpleNamespace(
                    target_root=str(target), platform=["desktop"], dry_run=False, json=True
                ))

            self.assertEqual(result["status"], "upgraded")
            self.assertEqual(result["operation"], "partial-uninstall")
            self.assertEqual(result["remaining_platforms"], ["apple"])

    def test_incomplete_activation_lock_is_rejected_without_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            lock_path = target / ".agent-skills" / "activation-lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["files"].pop()
            lock_path.write_text(
                json.dumps(lock, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )

            blocked = self.uninstall(target, check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("supported managed file set", blocked.stderr)
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "templates" / "ui-smoke.example.yml").is_file())

    def test_missing_activation_lock_is_allowed_only_for_non_activated_installs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            (target / ".agent-skills" / "activation-lock.json").unlink()

            blocked = self.uninstall(target, check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("missing its activation lock", blocked.stderr)
            self.assertTrue((target / "AGENTS.md").is_file())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            target.mkdir()
            config = target / "config.toml"
            config.write_text(
                f'model_instructions_file = "{target / "AGENTS.md"}"\nmodel = "keep"\n',
                encoding="utf-8",
            )
            config.chmod(0o600)
            expected_config = config.read_bytes()
            installed = subprocess.run(
                [
                    str(INSTALL), "--target-root", str(target),
                    "--platform", "desktop", "--json",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(installed.stdout)["selected_runtime_configs"], [])
            self.assertFalse((target / ".agent-skills" / "activation-lock.json").exists())

            result = json.loads(self.uninstall(target, "--platform", "all").stdout)
            self.assertEqual(result["status"], "uninstalled")
            self.assertEqual(result["activated_files"], [])
            self.assertFalse((target / ".agent-skills").exists())
            self.assertFalse((target / "AGENTS.md").exists())
            self.assertEqual(config.read_bytes(), expected_config)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_non_activated_install_rejects_injected_activation_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            activated_target = root / "activated"
            desktop_target = root / "desktop"
            self.install(activated_target)
            subprocess.run(
                [
                    str(INSTALL), "--target-root", str(desktop_target),
                    "--platform", "desktop", "--json",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            activation_lock = json.loads(
                (activated_target / ".agent-skills" / "activation-lock.json").read_text(
                    encoding="utf-8"
                )
            )
            shutil.copy2(
                activated_target / ".agent-skills" / "activation-lock.json",
                desktop_target / ".agent-skills" / "activation-lock.json",
            )
            for item in activation_lock["files"]:
                source = activated_target / item["path"]
                destination = desktop_target / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

            blocked = self.uninstall(desktop_target, check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("must not contain an activation lock", blocked.stderr)
            self.assertTrue((desktop_target / "AGENTS.md").is_file())
            self.assertTrue((desktop_target / "agents" / "reviewer.toml").is_file())

    def test_source_installer_activation_baseline_can_be_uninstalled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            lock_path = target / ".agent-skills" / "activation-lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            removed = [
                item
                for item in lock["files"]
                if item["path"] not in module.SOURCE_INSTALLER_ACTIVATION_BASELINE
            ]
            lock["files"] = [
                item
                for item in lock["files"]
                if item["path"] in module.SOURCE_INSTALLER_ACTIVATION_BASELINE
            ]
            lock_path.write_text(
                json.dumps(lock, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            for item in removed:
                (target / item["path"]).unlink()

            result = json.loads(self.uninstall(target).stdout)

            self.assertEqual(result["status"], "uninstalled")
            self.assertFalse((target / "AGENTS.md").exists())
            self.assertFalse((target / ".agent-skills").exists())

    def test_native_activation_lock_can_use_python_compatibility_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            session = target / "bin" / "agent-session"
            native_cli = target / "bin" / "agent-skills"
            shutil.copy2(session, native_cli)

            lock_path = target / ".agent-skills" / "activation-lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["files"].append(
                {
                    "mode": native_cli.stat().st_mode & 0o777,
                    "path": "bin/agent-skills",
                    "sha256": hashlib.sha256(native_cli.read_bytes()).hexdigest(),
                }
            )
            lock["files"].sort(key=lambda item: item["path"])
            lock_path.write_text(
                json.dumps(lock, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )

            preview = json.loads(self.uninstall(target, "--dry-run").stdout)
            self.assertEqual(preview["status"], "planned")
            self.assertIn("bin/agent-skills", preview["activated_files"])
            self.assertTrue(native_cli.is_file())

            result = json.loads(self.uninstall(target).stdout)
            self.assertEqual(result["status"], "uninstalled")
            self.assertFalse(native_cli.exists())
            self.assertFalse(session.exists())
            self.assertFalse((target / ".agent-skills").exists())

    def test_preexisting_empty_activation_directories_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            for name in ("agents", "bin", "templates"):
                (target / name).mkdir(parents=True, exist_ok=True)
            self.install(target)

            self.uninstall(target)

            for name in ("agents", "bin", "templates"):
                self.assertTrue((target / name).is_dir())
                self.assertEqual(list((target / name).iterdir()), [])

    def test_transaction_failure_restores_managed_roots_and_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )
            original_rename = os.rename
            injected_path = target / "agents" / "reviewer.toml"
            failure_injected = False

            def fail_on_reviewer(source, destination, *args, **kwargs):
                nonlocal failure_injected
                if (
                    not failure_injected
                    and source == "reviewer.toml"
                    and kwargs.get("src_dir_fd") is not None
                ):
                    failure_injected = True
                    raise OSError("injected uninstall failure")
                return original_rename(source, destination, *args, **kwargs)

            with mock.patch.object(module.os, "rename", side_effect=fail_on_reviewer):
                with self.assertRaisesRegex(OSError, "injected uninstall failure"):
                    module.run(arguments)

            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "skills" / "apple-verification" / "SKILL.md").is_file())
            self.assertTrue((target / ".agent-skills" / "install-lock.json").is_file())
            self.assertTrue(injected_path.is_file())
            module._managed_state(target)
            self.assertFalse(any(target.glob(".agent-skills-uninstall-backup-*")))

    def test_activation_toctou_is_rejected_and_concurrent_bytes_are_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )
            reviewer = target / "agents" / "reviewer.toml"
            concurrent = b"user-concurrent = true\n"
            original_rename = os.rename
            injected = False

            def replace_after_preflight(source, destination, *args, **kwargs):
                nonlocal injected
                if (
                    not injected
                    and source == "reviewer.toml"
                    and kwargs.get("src_dir_fd") is not None
                ):
                    injected = True
                    reviewer.write_bytes(concurrent)
                return original_rename(source, destination, *args, **kwargs)

            with mock.patch.object(module.os, "rename", side_effect=replace_after_preflight):
                with self.assertRaisesRegex(module.ContractError, "changed during uninstall"):
                    module.run(arguments)

            self.assertEqual(reviewer.read_bytes(), concurrent)
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertFalse(any(target.glob(".agent-skills-uninstall-backup-*")))

    def test_config_toctou_is_rejected_without_losing_concurrent_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )
            config = target / "config.toml"
            concurrent = b'model = "concurrent-user-value"\n'
            original_rename = os.rename
            injected = False

            def replace_after_preflight(source, destination, *args, **kwargs):
                nonlocal injected
                if (
                    not injected
                    and source == "config.toml"
                    and kwargs.get("src_dir_fd") is not None
                ):
                    injected = True
                    config.write_bytes(concurrent)
                return original_rename(source, destination, *args, **kwargs)

            with mock.patch.object(module.os, "rename", side_effect=replace_after_preflight):
                with self.assertRaisesRegex(module.ContractError, "config.toml changed"):
                    module.run(arguments)

            self.assertEqual(config.read_bytes(), concurrent)
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertFalse(any(target.glob(".agent-skills-uninstall-backup-*")))

    def test_config_recreation_before_publish_preserves_both_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )
            config = target / "config.toml"
            original = config.read_bytes()
            concurrent = b'model = "concurrent-recreated"\n'

            def recreate_before_link(source, destination, *args, **kwargs):
                config.write_bytes(concurrent)
                raise FileExistsError("injected concurrent config recreation")

            with mock.patch.object(module.os, "link", side_effect=recreate_before_link):
                with self.assertRaisesRegex(module.ContractError, "rollback incomplete"):
                    module.run(arguments)

            self.assertEqual(config.read_bytes(), concurrent)
            self.assertTrue((target / "AGENTS.md").is_file())
            backups = list(target.glob(".agent-skills-uninstall-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "config.toml").read_bytes(), original)

    def test_activation_parent_symlink_race_cannot_reach_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )
            templates = target.resolve() / "templates"
            held_templates = root / "held-templates"
            external = root / "external"
            external.mkdir()
            victim = external / "ui-smoke.example.yml"
            victim_content = (templates / "ui-smoke.example.yml").read_bytes()
            victim.write_bytes(victim_content)
            original_rename = os.rename
            injected = False

            def swap_parent_during_fd_rename(source, destination, *args, **kwargs):
                nonlocal injected
                if (
                    not injected
                    and source == "ui-smoke.example.yml"
                    and kwargs.get("src_dir_fd") is not None
                ):
                    injected = True
                    original_rename(templates, held_templates)
                    templates.symlink_to(external, target_is_directory=True)
                    try:
                        return original_rename(source, destination, *args, **kwargs)
                    finally:
                        templates.unlink()
                        original_rename(held_templates, templates)
                return original_rename(source, destination, *args, **kwargs)

            with mock.patch.object(module.os, "rename", side_effect=swap_parent_during_fd_rename):
                result = module.run(arguments)

            self.assertTrue(injected)
            self.assertEqual(result["status"], "uninstalled")
            self.assertEqual(victim.read_bytes(), victim_content)
            self.assertTrue(victim.is_file())

    def test_target_root_swap_cannot_redirect_managed_or_config_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )
            target = target.resolve()
            held = root / "held-target"
            replacement = root / "replacement-target"
            replacement.mkdir()
            replacement_agents = b"replacement user agents\n"
            replacement_config = b'model = "replacement-user-config"\n'
            (replacement / "AGENTS.md").write_bytes(replacement_agents)
            (replacement / "config.toml").write_bytes(replacement_config)
            original_rename = os.rename
            injected: set[str] = set()

            def swap_root_during_fd_rename(source, destination, *args, **kwargs):
                if (
                    source in {"AGENTS.md", "config.toml"}
                    and source not in injected
                    and kwargs.get("src_dir_fd") is not None
                ):
                    injected.add(source)
                    original_rename(target, held)
                    original_rename(replacement, target)
                    try:
                        return original_rename(source, destination, *args, **kwargs)
                    finally:
                        original_rename(target, replacement)
                        original_rename(held, target)
                return original_rename(source, destination, *args, **kwargs)

            with mock.patch.object(
                module.os,
                "rename",
                side_effect=swap_root_during_fd_rename,
            ):
                result = module.run(arguments)

            self.assertEqual(injected, {"AGENTS.md", "config.toml"})
            self.assertEqual(result["status"], "uninstalled")
            self.assertEqual((replacement / "AGENTS.md").read_bytes(), replacement_agents)
            self.assertEqual((replacement / "config.toml").read_bytes(), replacement_config)

    def test_preflight_root_swap_cannot_cross_bind_activation_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            replacement = root / "replacement"
            held = root / "held-target"
            subprocess.run(
                [
                    str(INSTALL), "--target-root", str(target),
                    "--platform", "desktop", "--json",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.install(replacement)
            victim = target / "agents" / "reviewer.toml"
            victim.parent.mkdir()
            victim_content = (replacement / "agents" / "reviewer.toml").read_bytes()
            victim.write_bytes(victim_content)
            victim.chmod((replacement / "agents" / "reviewer.toml").stat().st_mode & 0o777)

            module = load_uninstaller_module()
            original_is_managed = module._is_managed_install
            original_load = module.load
            original_rename = os.rename
            swapped = False
            restored = False

            def swap_during_validation(path):
                nonlocal swapped
                if not swapped:
                    original_rename(target, held)
                    original_rename(replacement, target)
                    swapped = True
                return original_is_managed(path)

            def restore_after_activation_load(path):
                nonlocal restored
                value = original_load(path)
                if (
                    swapped
                    and not restored
                    and Path(path).name == "activation-lock.json"
                ):
                    original_rename(target, replacement)
                    original_rename(held, target)
                    restored = True
                return value

            arguments = SimpleNamespace(
                target_root=str(target), platform=["all"], dry_run=False, json=True
            )
            with mock.patch.object(
                module,
                "_is_managed_install",
                side_effect=swap_during_validation,
            ), mock.patch.object(module, "load", side_effect=restore_after_activation_load):
                with self.assertRaisesRegex(module.ContractError, "install lock differs"):
                    module.run(arguments)

            self.assertTrue(swapped and restored)
            self.assertEqual(victim.read_bytes(), victim_content)
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((replacement / "AGENTS.md").is_file())
            self.assertFalse(any(target.glob(".agent-skills-uninstall-backup-*")))

    def test_late_transaction_failure_restores_config_system_skills_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            config = target / "config.toml"
            config.write_text(
                "# keep this comment\n" + config.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            config.chmod(0o600)
            system_skill = target / "skills" / ".system" / "local" / "SKILL.md"
            system_skill.parent.mkdir(parents=True)
            system_skill.write_text("system skill\n", encoding="utf-8")
            system_skill.chmod(0o640)
            reviewer = target / "agents" / "reviewer.toml"
            expected = {
                path: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in (config, system_skill, reviewer)
            }
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )

            with mock.patch.object(
                module,
                "_verify_uninstalled_state",
                side_effect=OSError("injected late uninstall failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected late uninstall failure"):
                    module.run(arguments)

            for path, (content, mode) in expected.items():
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(path.stat().st_mode & 0o777, mode)
            module._managed_state(target)
            self.assertFalse(any(target.glob(".agent-skills-uninstall-backup-*")))

    def test_backup_cleanup_failure_is_reported_with_residual_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.install(target)
            module = load_uninstaller_module()
            arguments = SimpleNamespace(
                target_root=str(target), platform=["apple"], dry_run=False, json=True
            )

            with mock.patch.object(
                module.shutil,
                "rmtree",
                side_effect=OSError("injected backup cleanup failure"),
            ):
                with self.assertRaisesRegex(
                    module.ContractError,
                    "managed files were removed, but temporary backup cleanup failed",
                ):
                    module.run(arguments)

            backups = list(target.glob(".agent-skills-uninstall-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertFalse((target / "AGENTS.md").exists())
            shutil.rmtree(backups[0])


if __name__ == "__main__":
    unittest.main()
