from __future__ import annotations

import importlib.util
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
        with self.assertRaisesRegex(module.ContractError, "partial platform uninstall"):
            module._selected_platforms(lock, ["apple"])
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
            original_replace = os.replace
            injected_path = target / "agents" / "reviewer.toml"
            failure_injected = False

            def fail_on_reviewer(source, destination):
                nonlocal failure_injected
                source_path = Path(source)
                if (
                    not failure_injected
                    and source_path.name == "reviewer.toml"
                    and source_path.parent.name == "agents"
                ):
                    failure_injected = True
                    raise OSError("injected uninstall failure")
                return original_replace(source, destination)

            with mock.patch.object(module.os, "replace", side_effect=fail_on_reviewer):
                with self.assertRaisesRegex(OSError, "injected uninstall failure"):
                    module.run(arguments)

            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "skills" / "apple-verification" / "SKILL.md").is_file())
            self.assertTrue((target / ".agent-skills" / "install-lock.json").is_file())
            self.assertTrue(injected_path.is_file())
            module._managed_state(target)
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
