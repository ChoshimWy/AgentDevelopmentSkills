from __future__ import annotations

import json
import math
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile
import unittest

from agent_workflow import __version__ as PYTHON_CORE_VERSION
from agent_workflow.canonical_json import dumps, sha256
from agent_workflow.canonical_json import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
)
from agent_workflow.models import ContractError
from agent_workflow.recipes import automatic_recipe_capabilities
from agent_workflow.registry import ManifestRegistry


ROOT = Path(__file__).resolve().parents[1]
RUST_COMPATIBILITY_ENABLED = (
    os.environ.get("AGENT_SKILLS_RUST_COMPATIBILITY") == "1"
    and shutil.which("cargo") is not None
)


@unittest.skipUnless(
    RUST_COMPATIBILITY_ENABLED,
    "set AGENT_SKILLS_RUST_COMPATIBILITY=1 and install cargo",
)
class RustCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build = subprocess.run(
            ["cargo", "build", "--quiet", "--locked", "-p", "agent-skills-rs"],
            cwd=ROOT,
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
            text=True,
            capture_output=True,
            check=False,
        )
        if build.returncode != 0:
            raise AssertionError(build.stderr)
        executable = "agent-skills-rs.exe" if os.name == "nt" else "agent-skills-rs"
        cls.rust_cli = ROOT / "target" / "debug" / executable

    def run_rust(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.rust_cli), *arguments],
            cwd=ROOT,
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_canonical_json_and_hash_match_python(self) -> None:
        value = {
            "z": [3, 2, 1],
            "a": "中文",
            "nested": {"b": True, "a": None},
            "number_boundaries": [
                1.0,
                1e-7,
                1e-5,
                0.0001,
                1e15,
                1e16,
                -0.0,
            ],
            "large_integer": 123456789012345678901234567890,
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "contract.json"
            artifact.write_text(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            canonical = self.run_rust("canonicalize", str(artifact))
            self.assertEqual(canonical.returncode, 0, canonical.stderr)
            self.assertEqual(canonical.stdout, dumps(value))
            identity = self.run_rust("hash", str(artifact))
            self.assertEqual(identity.returncode, 0, identity.stderr)
            self.assertEqual(identity.stdout, sha256(value) + "\n")

    def test_version_boundary_matches_fail_closed_exit_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "contract.json"
            artifact.write_text('{"schema_version":"2.0"}', encoding="utf-8")
            result = self.run_rust("validate-version", str(artifact))
            self.assertEqual(result.returncode, 2)
            self.assertIn("unsupported schema_version", result.stderr)

    def test_tracked_json_corpus_matches_python(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.json"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        checked = 0
        for relative_bytes in tracked:
            if not relative_bytes:
                continue
            relative = os.fsdecode(relative_bytes)
            artifact = ROOT / relative
            with self.subTest(artifact=relative):
                value = json.loads(artifact.read_text(encoding="utf-8"))
                result = self.run_rust("canonicalize", str(artifact))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, dumps(value))
            checked += 1
        self.assertGreater(checked, 100)

    def test_float_corpus_matches_python(self) -> None:
        generator = random.Random(0xA63E17)
        values: list[float] = []
        while len(values) < 4096:
            bits = generator.getrandbits(64)
            value = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
            if math.isfinite(value):
                values.append(value)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "float-corpus.json"
            artifact.write_text(
                json.dumps(values, allow_nan=False, separators=(",", ":")),
                encoding="utf-8",
            )
            result = self.run_rust("canonicalize", str(artifact))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(values))

    def test_integer_digit_limit_matches_python_and_rejects_overwritten_value(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            accepted = Path(directory) / "accepted.json"
            accepted.write_text(
                '{"value":' + "9" * MAX_CANONICAL_INTEGER_DIGITS + "}",
                encoding="utf-8",
            )
            result = self.run_rust("canonicalize", str(accepted))
            self.assertEqual(result.returncode, 0, result.stderr)

            for token in (
                "9" * (MAX_CANONICAL_INTEGER_DIGITS + 1),
                "-" + "9" * (MAX_CANONICAL_INTEGER_DIGITS + 1),
            ):
                rejected = Path(directory) / "rejected.json"
                rejected.write_text(
                    '{"value":' + token + ',"value":0}',
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    json.loads(rejected.read_text(encoding="utf-8"))
                result = self.run_rust("canonicalize", str(rejected))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("maximum", result.stderr)

    def test_nesting_limit_is_shared_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for depth, expected_returncode in (
                (MAX_CANONICAL_JSON_DEPTH, 0),
                (MAX_CANONICAL_JSON_DEPTH + 1, 2),
            ):
                artifact = Path(directory) / f"depth-{depth}.json"
                artifact.write_text(
                    "[" * depth + "0" + "]" * depth,
                    encoding="utf-8",
                )
                result = self.run_rust("canonicalize", str(artifact))
                self.assertEqual(result.returncode, expected_returncode, result.stderr)
                if expected_returncode == 0:
                    self.assertEqual(
                        result.stdout,
                        dumps(json.loads(artifact.read_text(encoding="utf-8"))),
                    )
                else:
                    self.assertEqual(result.stdout, "")
                    self.assertIn("nesting depth", result.stderr)

    def test_failure_paths_keep_stdout_empty_and_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "malformed": (b'{"value":', "invalid"),
                "invalid-utf8": (b'{"value":"\xff"}', "invalid"),
                "nan": (b'{"value":NaN}', "invalid"),
                "infinity": (b'{"value":Infinity}', "invalid"),
            }
            for name, (payload, expected_error) in cases.items():
                with self.subTest(case=name):
                    artifact = root / f"{name}.json"
                    artifact.write_bytes(payload)
                    result = self.run_rust("canonicalize", str(artifact))
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(expected_error, result.stderr)

            missing = self.run_rust("canonicalize", str(root / "missing.json"))
            self.assertEqual(missing.returncode, 2)
            self.assertEqual(missing.stdout, "")
            self.assertIn("cannot be read", missing.stderr)

            for payload in ({}, {"schema_version": 1}):
                artifact = root / "version.json"
                artifact.write_text(json.dumps(payload), encoding="utf-8")
                result = self.run_rust("validate-version", str(artifact))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("unsupported schema_version", result.stderr)

    def test_clap_surface_has_stable_success_and_usage_exits(self) -> None:
        for arguments in (("--help",), ("--version",)):
            with self.subTest(arguments=arguments):
                result = self.run_rust(*arguments)
                self.assertEqual(result.returncode, 0)
                self.assertNotEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
        version = self.run_rust("--version")
        self.assertIn(PYTHON_CORE_VERSION, version.stdout)

        for arguments in ((), ("unknown",), ("canonicalize",)):
            with self.subTest(arguments=arguments):
                result = self.run_rust(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Usage:", result.stderr)

    def registry_snapshot(
        self,
        root: Path,
        *,
        disabled_providers: tuple[str, ...] = (),
        provider_roots: tuple[Path, ...] = (),
    ) -> dict:
        registry = ManifestRegistry.from_directory(
            root,
            disabled_providers=disabled_providers,
            provider_roots=provider_roots,
        )
        bindings = {}
        for registered in registry.manifests:
            for capability in registered.value["capabilities"]:
                capability_id = capability["id"]
                resolved = registry.resolve_binding(capability_id)
                if resolved is not None:
                    bindings[capability_id] = {
                        "binding": resolved.binding,
                        "capability_id": resolved.capability_id,
                        "contract": resolved.contract,
                        "manifest_digest": resolved.manifest_digest,
                        "provider_id": resolved.provider_id,
                    }
        bootstrap_requirements = {}
        for registered in registry.manifests:
            if registered.value.get("role") != "bootstrap":
                continue
            manifest_id = registered.value["id"]
            requirement = registry.bootstrap_requirement(manifest_id)
            if requirement is not None:
                bootstrap_requirements[manifest_id] = requirement
        return {
            "bindings": bindings,
            "bootstrap_requirements": bootstrap_requirements,
            "digest": registry.digest(),
            "manifests": [
                {"id": registered.value["id"], "sha256": registered.digest}
                for registered in registry.manifests
            ],
            "schema_version": "1.0",
        }

    def test_registry_snapshot_matches_python_with_provider_controls(self) -> None:
        for disabled in ((), ("ios-agent-skills",)):
            with self.subTest(disabled=disabled):
                arguments = ["registry-snapshot", str(ROOT / "platforms")]
                for provider in disabled:
                    arguments.extend(["--disable-provider", provider])
                result = self.run_rust(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = self.registry_snapshot(
                    ROOT / "platforms",
                    disabled_providers=disabled,
                )
                self.assertEqual(result.stdout, dumps(expected))

    def test_registry_rejects_non_string_provider_role_before_all_controls(
        self,
    ) -> None:
        for invalid_role in (None, 7, [], {}):
            with (
                self.subTest(role=invalid_role),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                shutil.copytree(ROOT / "platforms", root / "platforms")
                shutil.copytree(ROOT / "disciplines", root / "disciplines")
                shutil.copytree(ROOT / "runtime-configs", root / "runtime-configs")
                provider = root / "platforms/apple/provider/manifest.json"
                value = json.loads(provider.read_text(encoding="utf-8"))
                value["role"] = invalid_role
                value["capabilities"][0]["permission_profile"] = "credential-admin"
                value["capabilities"][0]["side_effects"] = ["credentials"]
                provider.write_text(dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(
                    ContractError,
                    "plugin-manifest role is invalid",
                ):
                    ManifestRegistry.from_directory(root / "platforms")
                commands = (
                    (
                        "registry-snapshot",
                        str(root / "platforms"),
                        "--disable-provider",
                        "ios-agent-skills",
                    ),
                    (
                        "registry-resolve",
                        str(root / "platforms"),
                        "implementation.apple",
                        "--platform",
                        "android",
                    ),
                )
                for arguments in commands:
                    result = self.run_rust(*arguments)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("plugin-manifest role is invalid", result.stderr)

    def test_registry_optional_shape_missing_root_and_symlinks_match_python(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_root = root / "registry"
            manifest_root.mkdir()
            value = {
                "bindings": {"analysis.fixture": "fixture"},
                "capabilities": [{"id": "analysis.fixture"}],
                "conflicts": ["missing", "missing"],
                "detection": {"medium": [], "strong": ["", ""], "weak": []},
                "id": "fixture",
                "kind": "adapter",
                "requires": ["analysis.base", "analysis.base"],
                "schema_version": "1.0",
            }
            (manifest_root / "manifest.json").write_text(
                dumps(value),
                encoding="utf-8",
            )
            base_root = manifest_root / "base"
            base_root.mkdir()
            (base_root / "manifest.json").write_text(
                dumps(
                    {
                        "bindings": {"analysis.base": "base"},
                        "capabilities": [{"id": "analysis.base"}],
                        "detection": {"medium": [], "strong": [], "weak": []},
                        "id": "base",
                        "kind": "adapter",
                        "schema_version": "1.0",
                    }
                ),
                encoding="utf-8",
            )
            unrelated = root / "unrelated"
            unrelated.write_text("not a manifest", encoding="utf-8")
            (manifest_root / "unrelated-link").symlink_to(unrelated)
            linked_manifest = root / "linked-manifest.json"
            linked_manifest.write_text(
                dumps(
                    {
                        "bindings": {"analysis.linked": "linked"},
                        "capabilities": [{"id": "analysis.linked"}],
                        "detection": {"medium": [], "strong": [], "weak": []},
                        "id": "linked",
                        "kind": "adapter",
                        "schema_version": "1.0",
                    }
                ),
                encoding="utf-8",
            )
            link_root = manifest_root / "linked"
            link_root.mkdir()
            (link_root / "manifest.json").symlink_to(linked_manifest)
            expected = self.registry_snapshot(manifest_root)
            result = self.run_rust("registry-snapshot", str(manifest_root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))

            missing = root / "missing"
            expected = self.registry_snapshot(missing)
            result = self.run_rust("registry-snapshot", str(missing))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))

    def test_registry_contract_normalization_corpus_matches_python(self) -> None:
        accepted_mutations = {
            "permission-null": {"permission_profile": None},
            "permission-empty": {"permission_profile": ""},
            "permission-false": {"permission_profile": False},
            "permission-zero": {"permission_profile": 0},
            "side-effects-null": {"side_effects": None},
            "concurrency-null": {"concurrency_keys": None},
            "empty-schema-strings": {
                "input_schema": "",
                "output_schema": "",
                "version": "",
            },
        }
        rejected_mutations = {
            "permission-object": {"permission_profile": {"admin": True}},
            "permission-huge-number": {
                "permission_profile": int("9" * 400),
            },
            "side-effects-string": {"side_effects": "project-files"},
            "concurrency-number": {"concurrency_keys": 1},
            "failure-codes-null": {"failure_codes": None},
            "idempotent-number": {"idempotent": 1},
            "input-schema-null": {"input_schema": None},
            "output-schema-number": {"output_schema": 1},
            "version-null": {"version": None},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "bindings": {"analysis.fixture": "fixture"},
                "capabilities": [{"id": "analysis.fixture"}],
                "detection": {"medium": [], "strong": [], "weak": []},
                "id": "fixture",
                "kind": "adapter",
                "permissions": {"detection": "repository-read-only"},
                "schema_version": "1.0",
            }
            artifact = root / "manifest.json"
            for name, mutation in accepted_mutations.items():
                with self.subTest(accepted=name):
                    value = json.loads(json.dumps(base))
                    value["capabilities"][0].update(mutation)
                    artifact.write_text(dumps(value), encoding="utf-8")
                    expected = self.registry_snapshot(root)
                    result = self.run_rust("registry-snapshot", str(root))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, dumps(expected))
            for name, mutation in rejected_mutations.items():
                with self.subTest(rejected=name):
                    value = json.loads(json.dumps(base))
                    value["capabilities"][0].update(mutation)
                    artifact.write_text(dumps(value), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ContractError,
                        "capability-contract",
                    ):
                        ManifestRegistry.from_directory(root)
                    result = self.run_rust("registry-snapshot", str(root))
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("capability-contract", result.stderr)

            value = json.loads(json.dumps(base))
            value["capabilities"][0]["id"] = "中文"
            value["bindings"] = {"中文": "fixture"}
            artifact.write_text(dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "capability-contract id"):
                ManifestRegistry.from_directory(root)
            result = self.run_rust("registry-snapshot", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("capability-contract id", result.stderr)

    def test_registry_conflict_failure_message_matches_python_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for identifier in ("a", "z", "fixture"):
                package = root / identifier
                package.mkdir()
                value = {
                    "bindings": {},
                    "capabilities": [],
                    "detection": {"medium": [], "strong": [], "weak": []},
                    "id": identifier,
                    "kind": "adapter",
                    "schema_version": "1.0",
                }
                if identifier == "fixture":
                    value["conflicts"] = ["z", "a", "a"]
                (package / "manifest.json").write_text(
                    dumps(value),
                    encoding="utf-8",
                )
            with self.assertRaises(ContractError) as raised:
                ManifestRegistry.from_directory(root)
            expected = str(raised.exception)
            self.assertEqual(expected, "manifest fixture conflicts with: a, z")
            result = self.run_rust("registry-snapshot", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, expected + "\n")

    def test_registry_huge_semver_matches_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "platforms", root / "platforms")
            shutil.copytree(ROOT / "disciplines", root / "disciplines")
            shutil.copytree(ROOT / "runtime-configs", root / "runtime-configs")
            huge = "999999999999999999999999999999.2.3"
            provider = root / "platforms/apple/provider/manifest.json"
            provider_value = json.loads(provider.read_text(encoding="utf-8"))
            provider_value["package"]["version"] = huge
            provider.write_text(dumps(provider_value), encoding="utf-8")
            bootstrap = root / "platforms/apple/manifest.json"
            bootstrap_value = json.loads(bootstrap.read_text(encoding="utf-8"))
            bootstrap_value["provider_contract"]["package_compatibility"] = (
                ">=999999999999999999999999999998.0 "
                "<1000000000000000000000000000000.0"
            )
            bootstrap.write_text(dumps(bootstrap_value), encoding="utf-8")
            expected = self.registry_snapshot(root / "platforms")
            result = self.run_rust("registry-snapshot", str(root / "platforms"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))

    def test_registry_deep_directory_fails_without_native_abort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory)
            for _ in range(130):
                current /= "d"
                current.mkdir()
            result = self.run_rust("registry-snapshot", directory)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("maximum directory depth", result.stderr)

    def test_automatic_recipe_capability_closure_matches_python(self) -> None:
        for targets in ((), ("apple",), ("desktop",), ("apple", "desktop")):
            with self.subTest(targets=targets):
                result = self.run_rust("recipe-capabilities", *targets)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = sorted(automatic_recipe_capabilities(targets))
                self.assertEqual(result.stdout, dumps(expected))

    def test_registry_provider_mutations_fail_closed_like_python(self) -> None:
        mutations = {
            "permission": (
                lambda value: value["capabilities"][0].update(
                    {"permission_profile": "credential-admin"}
                ),
                "expands permission",
            ),
            "missing-binding": (
                lambda value: value["bindings"].pop("implementation.apple"),
                "has no binding",
            ),
            "package-version": (
                lambda value: value["package"].update({"version": "0.3.0"}),
                "outside",
            ),
        }
        for name, (mutate, expected) in mutations.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(ROOT / "platforms", root / "platforms")
                shutil.copytree(ROOT / "disciplines", root / "disciplines")
                shutil.copytree(ROOT / "runtime-configs", root / "runtime-configs")
                provider = root / "platforms/apple/provider/manifest.json"
                value = json.loads(provider.read_text(encoding="utf-8"))
                mutate(value)
                provider.write_text(dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ContractError, expected):
                    ManifestRegistry.from_directory(root / "platforms")
                result = self.run_rust(
                    "registry-snapshot",
                    str(root / "platforms"),
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(expected, result.stderr)

    def test_registry_external_provider_roots_match_python(self) -> None:
        external = ROOT / "tests/fixtures/providers"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platforms = root / "platforms"
            for package in (ROOT / "platforms").iterdir():
                source = package / "manifest.json"
                if source.is_file():
                    target = platforms / package.name / "manifest.json"
                    target.parent.mkdir(parents=True)
                    shutil.copy2(source, target)
            shutil.copytree(ROOT / "disciplines", root / "disciplines")
            shutil.copytree(ROOT / "runtime-configs", root / "runtime-configs")
            registry = ManifestRegistry.from_directory(
                platforms,
                provider_roots=(external,),
            )
            expected = registry.resolve_binding(
                "implementation.apple",
                platform="apple",
            )
            result = self.run_rust(
                "registry-resolve",
                str(platforms),
                "implementation.apple",
                "--platform",
                "apple",
                "--provider-root",
                str(external),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "binding": expected.binding,
                    "capability_id": expected.capability_id,
                    "contract": expected.contract,
                    "manifest_digest": expected.manifest_digest,
                    "provider_id": expected.provider_id,
                },
            )

        with self.assertRaisesRegex(ContractError, "ids must be unique"):
            ManifestRegistry.from_directory(
                ROOT / "platforms",
                provider_roots=(external,),
            )
        result = self.run_rust(
            "registry-snapshot",
            str(ROOT / "platforms"),
            "--provider-root",
            str(external),
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("ids must be unique", result.stderr)


if __name__ == "__main__":
    unittest.main()
