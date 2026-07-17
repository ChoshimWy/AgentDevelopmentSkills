from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

from scripts.build_python_artifacts import build_python_artifacts


ROOT = Path(__file__).resolve().parents[1]


class PythonPackagingTests(unittest.TestCase):
    def test_release_artifacts_do_not_embed_reusable_private_key_material(self) -> None:
        forbidden = (
            b"-----BEGIN " + b"PRIVATE KEY-----",
            b"-----BEGIN RSA " + b"PRIVATE KEY-----",
            b"TEST_RSA_PRIVATE_" + b"EXPONENT_HEX",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = build_python_artifacts(ROOT, root)
            wheel = next(item for item in artifacts if item["kind"] == "wheel")
            with zipfile.ZipFile(root / wheel["filename"]) as archive:
                wheel_files = [archive.read(name) for name in archive.namelist()]
            sdist = next(item for item in artifacts if item["kind"] == "sdist")
            with tarfile.open(root / sdist["filename"], "r:gz") as archive:
                sdist_files = [
                    stream.read()
                    for member in archive.getmembers()
                    if member.isfile() and (stream := archive.extractfile(member)) is not None
                ]
            for marker in forbidden:
                self.assertFalse(any(marker in value for value in [*wheel_files, *sdist_files]))

    def test_wheel_and_sdist_are_byte_stable_and_sdist_rebuilds_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first_records = build_python_artifacts(ROOT, first)
            second_records = build_python_artifacts(ROOT, second)
            self.assertEqual(first_records, second_records)
            for record in first_records:
                self.assertEqual(
                    (first / record["filename"]).read_bytes(),
                    (second / record["filename"]).read_bytes(),
                )

            sdist = next(item for item in first_records if item["kind"] == "sdist")
            extracted = root / "extracted"
            with tarfile.open(first / sdist["filename"], "r:gz") as archive:
                for member in archive.getmembers():
                    self.assertTrue(member.isfile())
                    relative = Path(member.name)
                    self.assertNotIn("..", relative.parts)
                    source_stream = archive.extractfile(member)
                    self.assertIsNotNone(source_stream)
                    destination = extracted / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source_stream.read())  # type: ignore[union-attr]
                    destination.chmod(member.mode & 0o777)
            source = next(extracted.iterdir())
            self.assertTrue((source / ".github/workflows/conformance.yml").is_file())
            self.assertTrue((source / ".github/workflows/publish-release.yml").is_file())
            rebuilt = root / "rebuilt"
            completed = subprocess.run(
                [sys.executable, str(source / "scripts/build_python_artifacts.py"), "--output", str(rebuilt)],
                cwd=source,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            for record in first_records:
                self.assertEqual(
                    hashlib.sha256((first / record["filename"]).read_bytes()).hexdigest(),
                    hashlib.sha256((rebuilt / record["filename"]).read_bytes()).hexdigest(),
                )

    def test_clean_venv_exercises_install_matrix_route_doctor_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = build_python_artifacts(ROOT, root / "artifacts")
            wheel = next(item for item in artifacts if item["kind"] == "wheel")
            wheel_path = root / "artifacts" / wheel["filename"]
            with zipfile.ZipFile(wheel_path) as archive:
                record = next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
                self.assertIn("share/agent-workflow/schemas/activation-lock-v2.schema.json", archive.read(record).decode())

            venv = root / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
            executable = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
            subprocess.run(
                [str(executable), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            agent_skills = scripts / ("agent-skills.exe" if sys.platform == "win32" else "agent-skills")
            manifest_probe = subprocess.run(
                [
                    str(executable), "-c",
                    "from agent_workflow.worktree_sessions.cli import _manifest_root_default; "
                    "p=_manifest_root_default(); print(p); raise SystemExit(0 if p and (p/'apple/manifest.json').is_file() else 2)",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(manifest_probe.returncode, 0, manifest_probe.stderr)
            help_result = subprocess.run([str(agent_skills), "--help"], text=True, capture_output=True, check=False)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            share = Path(manifest_probe.stdout.strip()).parent
            uninstall_script = share / "scripts" / "uninstall_local.py"
            self.assertTrue(uninstall_script.is_file())
            selections = (
                ("core-only", ["--core-only"], []),
                ("apple", ["--platform", "apple"], ["apple"]),
                ("desktop", ["--platform", "desktop"], ["desktop"]),
                (
                    "multi-platform",
                    ["--platform", "apple", "--platform", "desktop"],
                    ["apple", "desktop"],
                ),
                ("all", ["--platform", "all"], ["apple", "desktop"]),
            )
            for name, selection, expected_platforms in selections:
                with self.subTest(selection=name):
                    target = root / f"target-{name}"
                    install = subprocess.run(
                        [str(agent_skills), "install", *selection, "--target-root", str(target)],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(install.returncode, 0, install.stderr)
                    install_report = json.loads(install.stdout)
                    self.assertEqual(install_report["status"], "installed")
                    self.assertEqual(install_report["selected_platforms"], expected_platforms)
                    self.assertTrue((target / "AGENTS.md").is_file())
                    self.assertFalse((target / ".agent-skills" / "activation-lock.json").exists())
                    installed_skills = {
                        item.name for item in (target / "skills").iterdir()
                        if item.is_dir() and item.name != ".system"
                    }
                    if name == "core-only":
                        self.assertEqual(installed_skills, set())
                    if "apple" in expected_platforms:
                        self.assertIn("ios-feature-implementation", installed_skills)
                    if "desktop" in expected_platforms:
                        self.assertIn("desktop-orchestration", installed_skills)

                    doctor = subprocess.run(
                        [str(agent_skills), "doctor", "--target-root", str(target)],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(doctor.returncode, 0, doctor.stderr)
                    self.assertEqual(json.loads(doctor.stdout)["status"], "passed")

                    route_platform = (
                        "apple" if "apple" in expected_platforms
                        else "desktop" if "desktop" in expected_platforms
                        else None
                    )
                    if route_platform is not None:
                        fixture = "apple-app" if route_platform == "apple" else "desktop-electron"
                        route = subprocess.run(
                            [
                                str(agent_skills), "route", str(share / "tests" / "fixtures" / fixture),
                                "--task", "实现平台功能", "--platform", route_platform,
                            ],
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(route.returncode, 0, route.stderr)
                        self.assertEqual(
                            json.loads(route.stdout)["selected_platforms"], [route_platform]
                        )

                    uninstall = subprocess.run(
                        [
                            str(executable), str(uninstall_script), "--target-root", str(target),
                            "--platform", "all", "--json",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
                    self.assertEqual(json.loads(uninstall.stdout)["status"], "uninstalled")
                    self.assertFalse((target / ".agent-skills").exists())
                    self.assertFalse((target / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
