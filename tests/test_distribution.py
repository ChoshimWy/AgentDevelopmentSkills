from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_script("bootstrap_install.py")
builder = load_script("build_release_bundle.py")


class DistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="agent-skills-distribution-tests-")
        cls.root = Path(cls.temporary.name)
        cls.release = cls.root / "release"
        cls.manifest = builder.build_release_bundle(
            ROOT,
            cls.release,
            allow_dirty=True,
            channel="development",
        )
        cls.fixture_release = cls.root / "fixture-release"
        cls.fixture_release.mkdir()
        cls._write_fixture_release(cls.fixture_release)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @classmethod
    def _write_fixture_release(cls, release: Path) -> None:
        artifact_name = "agent-development-skills-1.0.0.zip"
        artifact_path = release / artifact_name
        entrypoint = textwrap.dedent(
            """\
            import json
            import sys
            print(json.dumps({"arguments": sys.argv[1:], "status": "fixture-passed"}, sort_keys=True))
            """
        ).encode("utf-8")
        with zipfile.ZipFile(artifact_path, "w", compression=zipfile.ZIP_STORED) as archive:
            info = zipfile.ZipInfo("agent-development-skills-1.0.0/scripts/install_local.py")
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entrypoint)
        artifact_data = artifact_path.read_bytes()
        bootstrap_data = (ROOT / "scripts/bootstrap_install.py").read_bytes()
        (release / "bootstrap_install.py").write_bytes(bootstrap_data)
        manifest = {
            "asset_base_url": release.as_uri() + "/",
            "artifacts": [{
                "entrypoint": "scripts/install_local.py",
                "filename": artifact_name,
                "format": "zip",
                "host_os": ["darwin", "linux", "windows"],
                "id": "fixture",
                "root": "agent-development-skills-1.0.0",
                "sha256": hashlib.sha256(artifact_data).hexdigest(),
                "size": len(artifact_data),
            }],
            "bootstrap_assets": [{
                "filename": "bootstrap_install.py",
                "sha256": hashlib.sha256(bootstrap_data).hexdigest(),
                "size": len(bootstrap_data),
            }],
            "channel": "development",
            "minimum_python": "3.11",
            "product": "agent-development-skills",
            "schema_version": "1.0",
            "source": {"dirty": False, "repository": "fixture://local", "revision": "fixture"},
            "version": "1.0.0",
        }
        (release / "release-manifest.json").write_bytes(bootstrap._canonical_json(manifest))

    def run_bootstrap(
        self,
        release: Path,
        target: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/bootstrap_install.py"),
                "--manifest-url",
                (release / "release-manifest.json").as_uri(),
                "--artifact-base-url",
                release.as_uri() + "/",
                "--target-root",
                str(target),
                *arguments,
            ],
            cwd=ROOT,
            env={**os.environ, "AGENT_SKILLS_ALLOW_FILE_URL": "1"},
            check=False,
            capture_output=True,
            text=True,
        )

    def test_release_bundle_is_deterministic_and_manifest_hashes_every_asset(self) -> None:
        second = self.root / "release-second"
        second_manifest = builder.build_release_bundle(
            ROOT,
            second,
            allow_dirty=True,
            channel="development",
        )
        self.assertEqual(self.manifest, second_manifest)
        self.assertEqual(
            (self.release / "release-manifest.json").read_bytes(),
            (second / "release-manifest.json").read_bytes(),
        )
        artifact = self.manifest["artifacts"][0]
        self.assertEqual(
            hashlib.sha256((self.release / artifact["filename"]).read_bytes()).hexdigest(),
            artifact["sha256"],
        )
        for asset in self.manifest["bootstrap_assets"]:
            data = (self.release / asset["filename"]).read_bytes()
            self.assertEqual(len(data), asset["size"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), asset["sha256"])

    def test_default_release_hosts_are_posix_and_windows_remains_fail_closed(self) -> None:
        self.assertEqual(builder.DEFAULT_HOST_OS, ("darwin", "linux"))
        posix_manifest = {**self.manifest, "artifacts": [{
            **self.manifest["artifacts"][0], "host_os": ["darwin", "linux"]
        }]}
        with self.assertRaisesRegex(bootstrap.BootstrapError, "host_os=windows"):
            bootstrap.select_artifact(posix_manifest, host_os="windows")
        with self.assertRaisesRegex(builder.ReleaseBuildError, "Windows Conformance"):
            builder.build_release_bundle(
                ROOT,
                self.root / "windows-release",
                allow_dirty=True,
                channel="development",
                host_os=("windows",),
            )

    def test_verified_bundle_runs_shared_installer_dry_run(self) -> None:
        target = self.root / "dry-run-target"
        completed = self.run_bootstrap(
            self.fixture_release,
            target,
            "--platform",
            "apple",
            "--dry-run",
            "--json",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "fixture-passed")
        self.assertEqual(
            report["arguments"],
            ["--target-root", str(target), "--platform", "apple", "--dry-run", "--json"],
        )
        self.assertFalse(target.exists())

        help_completed = self.run_bootstrap(self.fixture_release, target, "--help")
        self.assertEqual(help_completed.returncode, 0, help_completed.stderr)
        self.assertEqual(
            json.loads(help_completed.stdout)["arguments"],
            ["--target-root", str(target), "--help"],
        )

    @unittest.skipIf(os.name == "nt", "production manifest intentionally excludes Windows")
    def test_built_release_bundle_runs_real_installer_dry_run(self) -> None:
        target = self.root / "real-dry-run-target"
        completed = self.run_bootstrap(
            self.release,
            target,
            "--platform",
            "apple",
            "--dry-run",
            "--json",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["selected_platforms"], ["apple"])
        self.assertFalse(target.exists())

    @unittest.skipIf(os.name == "nt", "production Windows install remains fail-closed")
    def test_built_release_bundle_completes_supported_install(self) -> None:
        target = self.root / "real-installed-target"
        completed = self.run_bootstrap(self.release, target, "--platform", "apple", "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "installed")
        self.assertEqual(report["post_install_smoke"]["status"], "passed")
        self.assertTrue((target / "skills/apple-orchestration/SKILL.md").is_file())

    def test_release_bundle_contains_shared_installer_and_runtime_roots(self) -> None:
        artifact = self.manifest["artifacts"][0]
        prefix = artifact["root"] + "/"
        with zipfile.ZipFile(self.release / artifact["filename"]) as archive:
            names = set(archive.namelist())
        self.assertIn(prefix + artifact["entrypoint"], names)
        self.assertTrue(any(name.startswith(prefix + "src/agent_workflow/") for name in names))
        self.assertTrue(any(name.startswith(prefix + "platforms/") for name in names))
        self.assertTrue(any(name.startswith(prefix + "disciplines/") for name in names))

    def test_tampered_artifact_is_rejected_before_target_write(self) -> None:
        tampered = self.root / "tampered-release"
        shutil.copytree(self.fixture_release, tampered)
        artifact = json.loads(
            (tampered / "release-manifest.json").read_text(encoding="utf-8")
        )["artifacts"][0]
        with (tampered / artifact["filename"]).open("ab") as stream:
            stream.write(b"tampered")
        target = self.root / "tampered-target"
        completed = self.run_bootstrap(
            tampered,
            target,
            "--platform",
            "desktop",
            "--dry-run",
            "--json",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("does not match manifest", completed.stderr)
        self.assertFalse(target.exists())

    def test_unsafe_archive_path_is_rejected(self) -> None:
        directory = self.root / "unsafe-extract"
        directory.mkdir()
        archive_path = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("bundle/../escape", b"escape")
        data = archive_path.read_bytes()
        artifact = {
            "entrypoint": "scripts/install_local.py",
            "filename": "unsafe.zip",
            "format": "zip",
            "host_os": ["darwin"],
            "id": "unsafe",
            "root": "bundle",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        with self.assertRaisesRegex(bootstrap.BootstrapError, "unsafe path"):
            bootstrap.extract_verified_artifact(data, artifact, directory)
        self.assertFalse((self.root / "escape").exists())

    def test_archive_normalization_aliases_and_unicode_collisions_are_rejected(self) -> None:
        cases = (
            ("bundle/a/b", "bundle/a//b"),
            ("bundle/a/b", "bundle/a/./b"),
            ("bundle/caf\u00e9", "bundle/cafe\u0301"),
        )
        for index, names in enumerate(cases):
            with self.subTest(names=names):
                directory = self.root / f"alias-extract-{index}"
                directory.mkdir()
                archive_path = self.root / f"alias-{index}.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name in names:
                        archive.writestr(name, b"content")
                data = archive_path.read_bytes()
                artifact = {
                    "entrypoint": "scripts/install_local.py",
                    "filename": archive_path.name,
                    "format": "zip",
                    "host_os": ["darwin"],
                    "id": "alias",
                    "root": "bundle",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
                with self.assertRaisesRegex(bootstrap.BootstrapError, "unsafe path|duplicate path"):
                    bootstrap.extract_verified_artifact(data, artifact, directory)

    def test_manifest_rejects_boolean_sizes(self) -> None:
        value = json.loads((self.release / "release-manifest.json").read_text(encoding="utf-8"))
        value["artifacts"][0]["size"] = True
        with self.assertRaisesRegex(bootstrap.BootstrapError, "artifact size"):
            bootstrap.parse_release_manifest(bootstrap._canonical_json(value))
        value = json.loads((self.release / "release-manifest.json").read_text(encoding="utf-8"))
        value["bootstrap_assets"][0]["size"] = True
        with self.assertRaisesRegex(bootstrap.BootstrapError, "bootstrap asset"):
            bootstrap.parse_release_manifest(bootstrap._canonical_json(value))

    def test_builder_never_reuses_or_deletes_existing_or_unsafe_output(self) -> None:
        existing = self.root / "existing-output"
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(builder.ReleaseBuildError, "must not already exist"):
            builder.build_release_bundle(
                ROOT, existing, allow_dirty=True, channel="development"
            )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        with self.assertRaisesRegex(builder.ReleaseBuildError, "source root or its ancestor"):
            builder.build_release_bundle(
                ROOT, ROOT, allow_dirty=True, channel="development"
            )
        if hasattr(os, "symlink"):
            link = self.root / "output-link"
            try:
                link.symlink_to(existing, target_is_directory=True)
            except OSError:
                return
            with self.assertRaisesRegex(builder.ReleaseBuildError, "symlink"):
                builder.build_release_bundle(
                    ROOT, link, allow_dirty=True, channel="development"
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_manifest_must_be_canonical_and_file_urls_are_test_only(self) -> None:
        raw = (self.release / "release-manifest.json").read_bytes()
        with self.assertRaisesRegex(bootstrap.BootstrapError, "canonical JSON"):
            bootstrap.parse_release_manifest(raw.replace(b"{", b"{ ", 1))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "insecure download URL"):
                bootstrap.fetch_bytes((self.release / "release-manifest.json").as_uri(), maximum=1024 * 1024)

    def test_platform_bootstraps_are_thin_and_share_python_core(self) -> None:
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("scripts/install_local.py", shell)
        self.assertIn("bootstrap_install.py", shell)
        self.assertNotIn("python3.14", shell)
        self.assertIn("bootstrap_install.py", powershell)
        self.assertIn("AGENT_SKILLS_RELEASE_MANIFEST_URL", powershell)
        self.assertIn("ResponseHeadersRead", powershell)
        self.assertIn("Get-FileHash", powershell)
        self.assertNotIn("return $LASTEXITCODE", powershell)
        self.assertNotIn("build_install_bundle", shell)
        self.assertNotIn("build_install_bundle", powershell)

    @unittest.skipIf(os.name == "nt", "POSIX pipe bootstrap is covered on macOS/Linux")
    def test_piped_posix_bootstrap_downloads_shared_core_and_forwards_arguments(self) -> None:
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            "output=''\n"
            "url=''\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = '-o' ]; then output=$2; shift 2; else shift; fi\n"
            "done\n"
            "case \"$output\" in\n"
            "  */release-manifest.json) cp \"$AGENT_SKILLS_TEST_MANIFEST\" \"$output\" ;;\n"
            "  *) cp \"$AGENT_SKILLS_TEST_BOOTSTRAP\" \"$output\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        target = self.root / "piped-target"
        completed = subprocess.run(
            ["/bin/bash", "-s", "--", "--target-root", str(target), "--platform", "apple", "--dry-run"],
            cwd=self.root,
            env={
                **os.environ,
                "AGENT_SKILLS_ALLOW_FILE_URL": "1",
                "AGENT_SKILLS_PYTHON": sys.executable,
                "AGENT_SKILLS_RELEASE_BASE_URL": "https://release.example.invalid",
                "AGENT_SKILLS_RELEASE_MANIFEST_URL": (self.fixture_release / "release-manifest.json").as_uri(),
                "AGENT_SKILLS_TEST_BOOTSTRAP": str(ROOT / "scripts/bootstrap_install.py"),
                "AGENT_SKILLS_TEST_MANIFEST": str(self.fixture_release / "release-manifest.json"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            },
            input=(ROOT / "install.sh").read_text(encoding="utf-8"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "fixture-passed")
        self.assertEqual(
            report["arguments"],
            ["--target-root", str(target), "--platform", "apple", "--dry-run"],
        )

    @unittest.skipIf(os.name == "nt", "POSIX pipe bootstrap is covered on macOS/Linux")
    def test_piped_posix_bootstrap_rejects_tampered_shared_core_before_execution(self) -> None:
        tampered_release = self.root / "tampered-bootstrap-release"
        shutil.copytree(self.fixture_release, tampered_release)
        (tampered_release / "bootstrap_install.py").write_text(
            "raise SystemExit('must not execute')\n", encoding="utf-8"
        )
        manifest_value = json.loads(
            (tampered_release / "release-manifest.json").read_text(encoding="utf-8")
        )
        manifest_value["asset_base_url"] = tampered_release.as_uri() + "/"
        (tampered_release / "release-manifest.json").write_bytes(
            bootstrap._canonical_json(manifest_value)
        )
        completed = subprocess.run(
            ["/bin/bash", "-s", "--", "--dry-run"],
            cwd=self.root,
            env={
                **os.environ,
                "AGENT_SKILLS_ALLOW_FILE_URL": "1",
                "AGENT_SKILLS_PYTHON": sys.executable,
                "AGENT_SKILLS_RELEASE_BASE_URL": "https://release.example.invalid",
                "AGENT_SKILLS_RELEASE_MANIFEST_URL": (tampered_release / "release-manifest.json").as_uri(),
                "PATH": "/usr/bin:/bin",
            },
            input=(ROOT / "install.sh").read_text(encoding="utf-8"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not match release manifest", completed.stderr)
        self.assertNotIn("must not execute", completed.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX pipe bootstrap is covered on macOS/Linux")
    def test_piped_posix_bootstrap_enforces_manifest_stream_limit(self) -> None:
        oversized = self.root / "oversized-manifest.json"
        oversized.write_bytes(b"x" * (1024 * 1024 + 1))
        completed = subprocess.run(
            ["/bin/bash", "-s", "--", "--dry-run"],
            cwd=self.root,
            env={
                **os.environ,
                "AGENT_SKILLS_ALLOW_FILE_URL": "1",
                "AGENT_SKILLS_PYTHON": sys.executable,
                "AGENT_SKILLS_RELEASE_BASE_URL": "https://release.example.invalid",
                "AGENT_SKILLS_RELEASE_MANIFEST_URL": oversized.as_uri(),
                "PATH": "/usr/bin:/bin",
            },
            input=(ROOT / "install.sh").read_text(encoding="utf-8"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("size limit", completed.stderr)


if __name__ == "__main__":
    unittest.main()
