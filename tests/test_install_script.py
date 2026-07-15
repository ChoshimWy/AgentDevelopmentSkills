from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import subprocess
import tempfile
import termios
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"


def load_installer_module():
    spec = importlib.util.spec_from_file_location("install_local_under_test", ROOT / "scripts/install_local.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load install_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallScriptTests(unittest.TestCase):
    def run_install(
        self,
        target: Path,
        *arguments: str,
        check: bool = True,
        json_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(INSTALL), "--target-root", str(target), *arguments]
        if "--platform" not in arguments:
            command.extend(["--platform", "apple"])
        if json_output:
            command.append("--json")
        return subprocess.run(
            command,
            cwd=ROOT,
            check=check,
            capture_output=True,
            text=True,
        )

    def test_dry_run_is_source_checkout_only_and_does_not_create_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing" / ".codex"
            human = self.run_install(target, "--dry-run", json_output=False)
            result = self.run_install(target, "--dry-run")
            report = json.loads(result.stdout)

            self.assertIn("Apple / iOS 平台安装预览", human.stdout)
            self.assertNotIn("目标目录：", human.stdout)
            self.assertNotIn("已选择平台：", human.stdout)
            self.assertNotIn("规划平台：", human.stdout)
            self.assertNotIn("Runtime Config：", human.stdout)
            self.assertNotIn("安装包（", human.stdout)
            self.assertNotIn("  • Skills：", human.stdout)
            self.assertNotIn("旧 iOSAgentSkills 软链：", human.stdout)
            self.assertIn("未写入任何文件", human.stdout)
            self.assertIn("移除 --dry-run，重新执行原命令", human.stdout)
            self.assertNotIn("确认后运行：./install.sh", human.stdout)
            self.assertIn("持久备份：不创建", human.stdout)
            self.assertIn("添加 --json", human.stdout)
            self.assertNotIn("Agent Development Skills", human.stdout)
            self.assertNotIn("────────────────────────", human.stdout)
            self.assertTrue(human.stdout.startswith("◇ Apple / iOS 平台安装预览"))
            self.assertNotIn("\033[", human.stdout)
            self.assertEqual(report["status"], "planned")
            self.assertEqual(report["target_root"], str(target.resolve()))
            self.assertEqual(report["selected_platforms"], ["apple"])
            self.assertEqual(report["selected_runtime_configs"], ["codex"])
            self.assertEqual(
                report["selected_packages"],
                [
                    "core",
                    "design",
                    "documentation",
                    "git",
                    "review",
                    "workflow",
                    "apple",
                    "codex",
                ],
            )
            self.assertIsInstance(report["skill_count"], int)
            self.assertGreater(report["skill_count"], 0)
            self.assertEqual(report["persistent_backup"], False)
            self.assertEqual(
                [item["id"] for item in report["platform_options"]],
                ["apple", "android", "web", "backend", "desktop"],
            )
            self.assertFalse(target.exists())

            all_report = json.loads(
                self.run_install(target, "--platform", "all", "--dry-run").stdout
            )
            self.assertEqual(all_report["selected_platforms"], ["apple"])

            missing_selection = subprocess.run(
                [str(INSTALL), "--target-root", str(target), "--dry-run", "--json"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(missing_selection.returncode, 2)
            self.assertIn("--json requires an explicit --platform", missing_selection.stderr)
            self.assertFalse(target.exists())

            module = load_installer_module()
            multi_platform_report = {**report, "selected_platforms": ["apple", "web"]}
            self.assertIn(
                "Apple / iOS、Web 平台安装预览",
                module._human_report(multi_platform_report, color=False),
            )

            class TTYBuffer(io.StringIO):
                def isatty(self) -> bool:
                    return True

            prompt_output = TTYBuffer()
            selected, options = module._select_platforms(
                SimpleNamespace(platform=[], json=False),
                input_stream=TTYBuffer("\x1b[B \x1b[A\n"),
                output_stream=prompt_output,
            )
            self.assertEqual(selected, ("apple",))
            self.assertEqual([item["id"] for item in options], ["apple", "android", "web", "backend", "desktop"])
            plain_prompt = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", prompt_output.getvalue())
            self.assertTrue(plain_prompt.startswith("› [x] Apple / iOS"))
            self.assertNotIn("选择安装平台", plain_prompt)
            self.assertIn("\n  [ ] Android", plain_prompt)
            self.assertIn("[x] Apple / iOS", prompt_output.getvalue())
            self.assertIn("[ ] Android", prompt_output.getvalue())
            self.assertIn("↑/↓ 移动", prompt_output.getvalue())
            self.assertIn("Space 选择/取消", prompt_output.getvalue())
            self.assertIn("Android", prompt_output.getvalue())
            self.assertIn("bootstrap-only", prompt_output.getvalue())
            self.assertIn("尚不可安装", prompt_output.getvalue())
            self.assertTrue(prompt_output.getvalue().endswith("\033[8F\033[J"))

            toggle_output = TTYBuffer()
            toggled, _ = module._select_platforms(
                SimpleNamespace(platform=[], json=False),
                input_stream=TTYBuffer(" \n \n"),
                output_stream=toggle_output,
            )
            self.assertEqual(toggled, ("apple",))
            self.assertIn("[ ] Apple / iOS", toggle_output.getvalue())
            self.assertIn("请至少选择一个可安装平台", toggle_output.getvalue())

            future_web = dict(options[2])
            future_web.update(
                availability="ready",
                handler={"activation": "web-v1", "smoke": "web-smoke-v1"},
                selectable=True,
                status="implemented",
            )
            combined = module._prompt_for_platforms(
                [options[0], future_web],
                input_stream=TTYBuffer("\x1b[B \n"),
                output_stream=TTYBuffer(),
            )
            self.assertEqual(combined, ("apple", "web"))

            master, slave = pty.openpty()
            terminal_before = termios.tcgetattr(slave)
            process = subprocess.Popen(
                [str(INSTALL), "--target-root", str(target), "--dry-run"],
                cwd=ROOT,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
            )
            output = bytearray()

            def read_until(needle: bytes, timeout: float) -> None:
                deadline = time.monotonic() + timeout
                while needle not in output and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if not ready:
                        if process.poll() is not None:
                            break
                        continue
                    try:
                        output.extend(os.read(master, 4096))
                    except OSError:
                        break

            try:
                read_until(b"Space", 5)
                self.assertIn(b"[x] Apple / iOS", output)
                os.write(master, b"\x1b[B \x1b[A \r \r")
                read_until("未写入任何文件".encode("utf-8"), 10)
                process.wait(timeout=10)
                while select.select([master], [], [], 0.05)[0]:
                    try:
                        output.extend(os.read(master, 4096))
                    except OSError:
                        break
                terminal_after = termios.tcgetattr(slave)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                os.close(master)
                os.close(slave)
            rendered = output.decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 0, rendered)
            self.assertNotIn("选择安装平台", rendered)
            self.assertIn("Android (bootstrap-only) 尚不可安装", rendered)
            self.assertIn("请至少选择一个可安装平台", rendered)
            self.assertIn("Apple / iOS 平台安装预览", rendered)
            final_screen = rendered.rsplit("\033[J", 1)[-1]
            self.assertNotIn("[x] Apple / iOS", final_screen)
            self.assertNotIn("↑/↓ 移动", final_screen)
            self.assertNotIn("Agent Development Skills", final_screen)
            self.assertNotIn("────────────────────────", final_screen)
            self.assertIn("Apple / iOS 平台安装预览", final_screen)
            for flag in (termios.ECHO, termios.ICANON, termios.ISIG, termios.IEXTEN):
                self.assertEqual(terminal_after[3] & flag, terminal_before[3] & flag)
            self.assertEqual(terminal_after[0] & termios.ICRNL, terminal_before[0] & termios.ICRNL)
            self.assertEqual(terminal_after[1] & termios.OPOST, terminal_before[1] & termios.OPOST)

            signal_master, signal_slave = pty.openpty()
            signal_before = termios.tcgetattr(signal_slave)
            signal_process = subprocess.Popen(
                [str(INSTALL), "--target-root", str(target), "--dry-run"],
                cwd=ROOT,
                stdin=signal_slave,
                stdout=signal_slave,
                stderr=signal_slave,
                close_fds=True,
            )
            signal_output = bytearray()
            deadline = time.monotonic() + 5
            try:
                while b"Space" not in signal_output and time.monotonic() < deadline:
                    ready, _, _ = select.select([signal_master], [], [], 0.1)
                    if ready:
                        signal_output.extend(os.read(signal_master, 4096))
                self.assertIn(b"Space", signal_output)
                signal_process.terminate()
                signal_process.wait(timeout=5)
                signal_after = termios.tcgetattr(signal_slave)
            finally:
                if signal_process.poll() is None:
                    signal_process.kill()
                    signal_process.wait(timeout=5)
                os.close(signal_master)
                os.close(signal_slave)
            self.assertEqual(signal_process.returncode, 128 + signal.SIGTERM)
            for flag in (termios.ECHO, termios.ICANON, termios.ISIG, termios.IEXTEN):
                self.assertEqual(signal_after[3] & flag, signal_before[3] & flag)
            self.assertEqual(signal_after[0] & termios.ICRNL, signal_before[0] & termios.ICRNL)
            self.assertEqual(signal_after[1] & termios.OPOST, signal_before[1] & termios.OPOST)

            catalog_root = Path(directory) / "future-catalog"
            future_root = catalog_root / "platforms" / "future"
            future_root.mkdir(parents=True)
            future_manifest = json.loads(
                (ROOT / "platforms" / "apple" / "manifest.json").read_text(encoding="utf-8")
            )
            future_manifest["id"] = "future"
            (future_root / "manifest.json").write_text(
                json.dumps(future_manifest), encoding="utf-8"
            )
            with mock.patch.object(module, "ROOT", catalog_root):
                future_options = module._platform_inventory()
            self.assertEqual(future_options[0]["status"], "implemented")
            self.assertEqual(
                future_options[0]["availability"], "source-installer-handler-missing"
            )
            self.assertFalse(future_options[0]["selectable"])
            with self.assertRaisesRegex(
                module.ContractError, "source-installer-handler-missing"
            ):
                module._validated_platform_selection(["future"], future_options)

            blocked = self.run_install(
                target,
                "--platform",
                "android",
                "--dry-run",
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("Android (bootstrap-only)", blocked.stderr)
            self.assertFalse(target.exists())

    def test_fresh_install_activates_codex_assets_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            first = self.run_install(target, json_output=False)
            second = json.loads(self.run_install(target).stdout)

            self.assertIn("Apple / iOS 平台安装完成", first.stdout)
            self.assertNotIn("目标目录：", first.stdout)
            self.assertNotIn("已选择平台：", first.stdout)
            self.assertNotIn("规划平台：", first.stdout)
            self.assertNotIn("Runtime Config：", first.stdout)
            self.assertNotIn("安装包（", first.stdout)
            self.assertNotIn("  • Skills：", first.stdout)
            self.assertNotIn("旧 iOSAgentSkills 软链：", first.stdout)
            self.assertNotIn("Agent Development Skills", first.stdout)
            self.assertNotIn("────────────────────────", first.stdout)
            self.assertTrue(first.stdout.startswith("✓ Apple / iOS 平台安装完成"))
            self.assertIn("Installed workflow smoke：passed", first.stdout)
            self.assertIn("Plan / Review / Final：ready / passed / completed", first.stdout)
            multi_platform_report = {**second, "selected_platforms": ["apple", "web"]}
            self.assertIn(
                "Apple / iOS、Web 平台安装完成",
                load_installer_module()._human_report(multi_platform_report, color=False),
            )
            self.assertEqual(second["activation"]["managed_file_updates"], [])
            self.assertEqual(len(second["activation"]["managed_files_unchanged"]), 13)
            self.assertTrue((target / "skills" / "apple-verification" / "SKILL.md").is_file())
            self.assertTrue((target / "agents" / "reviewer.toml").is_file())
            self.assertTrue((target / "bin" / "codex_verify").is_file())
            self.assertEqual((target / "bin" / "codex_verify").stat().st_mode & 0o777, 0o755)
            self.assertEqual((target / "bin" / "agent-session").stat().st_mode & 0o777, 0o755)
            isolated_environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            subprocess.run(
                [str(target / "bin" / "agent-session"), "--help"],
                cwd=directory,
                env=isolated_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            wrapper_help = subprocess.run(
                [str(target / "bin" / "codex_verify"), "--help"],
                cwd=directory,
                env=isolated_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("--worktree-session-request", wrapper_help.stdout)
            subprocess.run(
                [str(target / "skills" / "apple-verification" / "scripts" / "worktree_session.py"), "--help"],
                cwd=directory,
                env=isolated_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            repository = Path(directory) / "session-repo"
            repository.mkdir()
            for command in (
                ("init", "-q"),
                ("config", "user.name", "Installed Session Smoke"),
                ("config", "user.email", "installed-session@example.invalid"),
                ("config", "core.hooksPath", "/dev/null"),
            ):
                subprocess.run(["git", *command], cwd=repository, check=True, capture_output=True)
            (repository / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "test(session): [HUMAN] 创建安装态基线"], cwd=repository, check=True)
            session = subprocess.run(
                [
                    str(target / "bin" / "agent-session"), "create", "installed-smoke",
                    "--repository", str(repository), "--project-id", "installed-smoke",
                    "--platform", "apple", "--worktree-root", str(Path(directory) / "worktrees"),
                ],
                cwd=directory,
                env=isolated_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(session.stdout)["session"]["selected_platforms"], ["apple"])
            self.assertTrue((target / ".agent-skills" / "activation-lock.json").is_file())

            activation_lock = target / ".agent-skills" / "activation-lock.json"
            legacy_lock = json.loads(activation_lock.read_text(encoding="utf-8"))
            legacy_lock["files"] = [
                item for item in legacy_lock["files"] if item["path"] != "bin/agent-session"
            ]
            activation_lock.write_text(json.dumps(legacy_lock), encoding="utf-8")
            (target / "bin" / "agent-session").unlink()
            upgraded = json.loads(self.run_install(target).stdout)
            self.assertIn("bin/agent-session", upgraded["activation"]["managed_file_updates"])
            self.assertTrue((target / "bin" / "agent-session").is_file())

            agents = target / "AGENTS.md"
            original_agents = agents.read_bytes()
            agents.write_bytes(original_agents + b"\nmodified\n")
            blocked_preview = self.run_install(target, "--dry-run", check=False)
            self.assertEqual(blocked_preview.returncode, 2)
            self.assertIn("modified install roots", blocked_preview.stderr)
            agents.write_bytes(original_agents)

            (target / "skills" / ".DS_Store").write_bytes(b"Finder metadata")
            metadata_preview = json.loads(self.run_install(target, "--dry-run").stdout)
            self.assertEqual(metadata_preview["status"], "planned")
            metadata_reinstall = json.loads(self.run_install(target).stdout)
            self.assertEqual(metadata_reinstall["status"], "installed")
            self.assertFalse((target / "skills" / ".DS_Store").exists())

    def test_recognized_legacy_links_are_replaced_without_backup_and_system_skills_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "iOSAgentSkills"
            target = root / "home" / ".codex"
            (legacy / "skills" / ".system" / "openai-docs").mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "AGENTS.md").write_text("legacy\n", encoding="utf-8")
            (legacy / "skills" / ".system" / "openai-docs" / "SKILL.md").write_text(
                "system\n", encoding="utf-8"
            )
            (target / "AGENTS.md").symlink_to(legacy / "AGENTS.md")
            (target / "skills").symlink_to(legacy / "skills")
            (target / "config.toml").write_text(
                'model = "keep-me"\n\n[custom]\nvalue = 7\n', encoding="utf-8"
            )

            human_dry_run = self.run_install(target, "--dry-run", json_output=False)
            self.assertNotIn("旧 iOSAgentSkills 软链：", human_dry_run.stdout)
            dry_run = json.loads(self.run_install(target, "--dry-run").stdout)
            self.assertEqual(dry_run["would_remove_legacy_symlinks"], ["AGENTS.md", "skills"])
            self.assertTrue((target / "AGENTS.md").is_symlink())

            installed = json.loads(self.run_install(target).stdout)
            self.assertEqual(installed["removed_legacy_symlinks"], ["AGENTS.md", "skills"])
            self.assertTrue(installed["preserved_system_skills"])
            self.assertFalse((target / "AGENTS.md").is_symlink())
            self.assertTrue((target / "skills" / ".system" / "openai-docs" / "SKILL.md").is_file())
            config = (target / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "keep-me"', config)
            self.assertIn("[custom]", config)
            self.assertFalse((root / "home" / ".agent-skills-backups").exists())

            reinstalled = json.loads(self.run_install(target).stdout)
            self.assertEqual(reinstalled["status"], "installed")
            self.assertTrue((target / "skills" / ".system" / "openai-docs" / "SKILL.md").is_file())

    def test_unknown_unmanaged_roots_are_rejected_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            target.mkdir()
            agents = target / "AGENTS.md"
            agents.write_text("user-owned\n", encoding="utf-8")

            human = self.run_install(target, check=False, json_output=False)
            result = self.run_install(target, check=False)
            self.assertIn("Apple 工作流安装未完成", human.stderr)
            self.assertIn("unknown unmanaged", human.stderr)
            self.assertIn("添加 --json", human.stderr)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown unmanaged", result.stderr)
            self.assertEqual(agents.read_text(encoding="utf-8"), "user-owned\n")
            self.assertFalse((target / ".agent-skills").exists())

    def test_nested_directory_named_ios_agent_skills_is_not_treated_as_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "iOSAgentSkills"
            target = root / ".codex"
            (legacy / "nested").mkdir(parents=True)
            (legacy / "skills").mkdir()
            target.mkdir()
            (legacy / "nested" / "AGENTS.md").write_text("not-root\n", encoding="utf-8")
            (target / "AGENTS.md").symlink_to(legacy / "nested" / "AGENTS.md")
            (target / "skills").symlink_to(legacy / "skills")

            result = self.run_install(target, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("non-iOSAgentSkills", result.stderr)
            self.assertTrue((target / "AGENTS.md").is_symlink())
            self.assertTrue((target / "skills").is_symlink())

    def test_modified_activated_file_blocks_reinstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.run_install(target)
            reviewer = target / "agents" / "reviewer.toml"
            reviewer.write_text("modified\n", encoding="utf-8")

            result = self.run_install(target, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("activated file was modified", result.stderr)
            self.assertEqual(reviewer.read_text(encoding="utf-8"), "modified\n")

    def test_activation_parent_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".codex"
            external = root / "external-agents"
            target.mkdir()
            external.mkdir()
            (target / "agents").symlink_to(external)

            result = self.run_install(target, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("activation directory must be a regular directory", result.stderr)
            self.assertEqual(list(external.iterdir()), [])
            self.assertFalse((target / ".agent-skills").exists())

    def test_incomplete_activation_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.run_install(target)
            lock = target / ".agent-skills" / "activation-lock.json"
            lock.write_text(
                '{"files":[],"manager":"agent-development-skills","schema_version":"1.0"}\n',
                encoding="utf-8",
            )

            result = self.run_install(target, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("does not cover the managed file set", result.stderr)

            lock.unlink()
            lock.symlink_to(target / "missing-activation-lock.json")
            dry_run = self.run_install(target, "--dry-run", check=False)
            reinstall = self.run_install(target, check=False)
            self.assertEqual(dry_run.returncode, 2)
            self.assertEqual(reinstall.returncode, 2)
            self.assertIn("activation lock must be a regular file", dry_run.stderr)
            self.assertIn("activation lock must be a regular file", reinstall.stderr)
            self.assertTrue(lock.is_symlink())
            self.assertFalse(lock.exists())

    def test_activation_failure_restores_files_from_temporary_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.run_install(target)
            module = load_installer_module()
            config = target / "config.toml"
            activation_lock = target / ".agent-skills" / "activation-lock.json"
            original_config = config.read_bytes()
            original_lock = activation_lock.read_bytes()
            original_atomic_write = module._atomic_write
            call_count = 0

            def fail_once_on_second_write(path, data, mode):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("injected activation failure")
                return original_atomic_write(path, data, mode)

            with mock.patch.object(module, "_atomic_write", side_effect=fail_once_on_second_write):
                with self.assertRaisesRegex(OSError, "injected activation failure"):
                    module._activate(target, original_config + b"\n# changed\n")

            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual(activation_lock.read_bytes(), original_lock)
            self.assertTrue(module._validate_activation_lock(target))

    def test_fresh_install_smoke_failure_removes_new_managed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            module = load_installer_module()
            arguments = SimpleNamespace(target_root=str(target), dry_run=False, platform=["apple"], json=True)

            with mock.patch.object(module, "parse_args", return_value=arguments):
                with mock.patch.object(
                    module,
                    "_run_target_smoke",
                    side_effect=OSError("injected smoke failure"),
                ):
                    with self.assertRaisesRegex(OSError, "injected smoke failure"):
                        module.run()

            for name in module.MANAGED_ROOTS:
                self.assertFalse(module._path_exists(target / name))
            self.assertFalse((target / ".agent-skills-backups").exists())

    def test_managed_reinstall_activation_failure_restores_previous_managed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".codex"
            self.run_install(target)
            module = load_installer_module()
            arguments = SimpleNamespace(target_root=str(target), dry_run=False, platform=["apple"], json=True)
            original_inodes = {
                name: (target / name).stat().st_ino
                for name in module.MANAGED_ROOTS
            }

            with mock.patch.object(module, "parse_args", return_value=arguments):
                with mock.patch.object(
                    module,
                    "_activate",
                    side_effect=OSError("injected activation failure"),
                ):
                    with self.assertRaisesRegex(OSError, "injected activation failure"):
                        module.run()

            self.assertEqual(
                {name: (target / name).stat().st_ino for name in module.MANAGED_ROOTS},
                original_inodes,
            )
            self.assertTrue(module._validate_activation_lock(target))


if __name__ == "__main__":
    unittest.main()
