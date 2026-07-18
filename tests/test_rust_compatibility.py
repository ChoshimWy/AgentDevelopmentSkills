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
from copy import deepcopy

from agent_workflow import __version__ as PYTHON_CORE_VERSION
from agent_workflow.canonical_json import dump, dumps, sha256
from agent_workflow.canonical_json import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
)
from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.installation import build_install_bundle
from agent_workflow.models import ContractError
from agent_workflow.package_lock import (
    MAX_LOCK_PACKAGES,
    diff_package_locks,
    explain_package_lock,
    resolve_package_lock,
    validate_package_lock,
)
from agent_workflow.planning import PlanCompiler
from agent_workflow.policy import PolicyResolver
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

    def test_policy_resolution_corpus_matches_python_byte_for_byte(self) -> None:
        cases = (
            (
                {"platforms": ["web"]},
                "修复 iOS 页面",
                (),
                None,
                (),
            ),
            (
                {"platforms": ["apple", "web"]},
                "修复页面",
                ("apple",),
                None,
                (),
            ),
            (
                {
                    "platforms": ["apple", "desktop"],
                    "target_modules": [
                        {"path": "apps/desktop", "platform": "desktop"}
                    ],
                },
                "实现 Figma 页面并补充 QA 测试",
                (),
                {"network": False},
                (
                    {
                        "source": "core",
                        "strategies": {
                            "network": "deny-wins",
                            "tags": "union",
                        },
                        "values": {"network": True, "tags": ["core"]},
                    },
                    {
                        "source": "project",
                        "strategies": {
                            "network": "deny-wins",
                            "tags": "union",
                        },
                        "values": {"network": False, "tags": ["project"]},
                    },
                ),
            ),
            (
                {
                    "ambiguities": [
                        {
                            "candidates": ["backend", "web"],
                            "path": ".",
                            "reason": "multiple-platform-signals",
                        }
                    ],
                    "platforms": ["backend", "web"],
                    "target_modules": [],
                },
                "修复功能",
                (),
                None,
                (),
            ),
            (
                {"platforms": []},
                "只做 QA 回归",
                (),
                None,
                (),
            ),
            (
                {"platforms": ["apple"]},
                "iOS 单文件 contract 小改动",
                (),
                None,
                (),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_path = root / "profile.json"
            constraints_path = root / "constraints.json"
            layers_path = root / "layers.json"
            for index, (
                profile,
                task,
                explicit,
                constraints,
                layers,
            ) in enumerate(cases):
                with self.subTest(case=index):
                    profile_path.write_text(dumps(profile), encoding="utf-8")
                    arguments = [
                        "policy-resolve",
                        str(profile_path),
                        task,
                    ]
                    for platform in explicit:
                        arguments.extend(["--explicit-platform", platform])
                    if constraints is not None:
                        constraints_path.write_text(
                            dumps(constraints),
                            encoding="utf-8",
                        )
                        arguments.extend(["--constraints", str(constraints_path)])
                    if layers:
                        layers_path.write_text(dumps(list(layers)), encoding="utf-8")
                        arguments.extend(["--policy-layers", str(layers_path)])
                    result = self.run_rust(*arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected = PolicyResolver().resolve(
                        profile,
                        task,
                        explicit_platforms=explicit,
                        constraints=constraints,
                        policy_layers=layers,
                    )
                    self.assertEqual(result.stdout, dumps(expected))

    def test_policy_negative_and_numeric_set_cases_match_python(self) -> None:
        profile = {"platforms": []}
        cases = (
            (
                [{
                    "source": "project",
                    "strategies": {"values": "bogus"},
                    "values": {"values": ["project"]},
                }],
                False,
            ),
            (
                [
                    {
                        "source": "core",
                        "strategies": {"values": "union"},
                        "values": {"values": [2, 1]},
                    },
                    {
                        "source": "project",
                        "strategies": {"values": "union"},
                        "values": {"values": [3, 2]},
                    },
                ],
                True,
            ),
            ([{"source": "empty"}] * 1_025, False),
            (
                [{
                    "source": "project",
                    "strategies": {"values": "replace"},
                    "values": {"values": [0] * 16_385},
                }],
                False,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_path = root / "profile.json"
            layers_path = root / "layers.json"
            profile_path.write_text(dumps(profile), encoding="utf-8")
            for index, (layers, succeeds) in enumerate(cases):
                with self.subTest(case=index):
                    layers_path.write_text(dumps(layers), encoding="utf-8")
                    result = self.run_rust(
                        "policy-resolve",
                        str(profile_path),
                        "实现功能",
                        "--policy-layers",
                        str(layers_path),
                    )
                    if succeeds:
                        expected = PolicyResolver().resolve(
                            profile,
                            "实现功能",
                            policy_layers=layers,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, dumps(expected))
                    else:
                        with self.assertRaises(ContractError):
                            PolicyResolver().resolve(
                                profile,
                                "实现功能",
                                policy_layers=layers,
                            )
                        self.assertEqual(result.returncode, 2)
                        self.assertEqual(result.stdout, "")

            layers_path.write_text(
                '[{"source":"core","strategies":{"values":"union"},'
                '"values":{"values":[0.1]}},{"source":"project",'
                '"strategies":{"values":"union"},'
                '"values":{"values":[0.10000000000000001]}}]\n',
                encoding="utf-8",
            )
            raw_layers = json.loads(layers_path.read_text(encoding="utf-8"))
            expected = PolicyResolver().resolve(
                profile,
                "实现功能",
                policy_layers=raw_layers,
            )
            result = self.run_rust(
                "policy-resolve",
                str(profile_path),
                "实现功能",
                "--policy-layers",
                str(layers_path),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))
            self.assertEqual(expected["constraints"]["values"], [0.1])

            constraints = {f"field-{index}": index for index in range(16_385)}
            constraints_path = root / "constraints.json"
            constraints_path.write_text(dumps(constraints), encoding="utf-8")
            with self.assertRaises(ContractError):
                PolicyResolver().resolve(
                    profile,
                    "实现功能",
                    constraints=constraints,
                )
            result = self.run_rust(
                "policy-resolve",
                str(profile_path),
                "实现功能",
                "--constraints",
                str(constraints_path),
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")

            large_profile = {
                "platforms": [f"platform-{index}" for index in range(16_385)]
            }
            profile_path.write_text(dumps(large_profile), encoding="utf-8")
            with self.assertRaises(ContractError):
                PolicyResolver().resolve(large_profile, "实现功能")
            result = self.run_rust(
                "policy-resolve",
                str(profile_path),
                "实现功能",
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")

    def test_repository_discovery_fixture_corpus_matches_python(self) -> None:
        registry = ManifestRegistry.from_directory(ROOT / "platforms")
        engine = DiscoveryEngine(registry)
        fixtures = ROOT / "tests/fixtures"
        cases = (
            (fixtures / "apple-app", (), (), None),
            (fixtures / "swift-cli", (), (), None),
            (fixtures / "apple-workspace", (), (), None),
            (fixtures / "apple-swift-package", (), (), None),
            (fixtures / "android-app", (), (), None),
            (fixtures / "backend-service", (), (), None),
            (fixtures / "tauri-app", (), (), None),
            (fixtures / "web-app", (), (), None),
            (fixtures / "unknown", (), (), None),
            (fixtures / "ambiguous", (), (), None),
            (fixtures / "monorepo", (), (), None),
            (
                fixtures / "monorepo/apps/ios",
                (),
                (),
                None,
            ),
            (
                fixtures / "monorepo",
                ("apps/ios/Sources/Feature.swift",),
                (),
                None,
            ),
            (
                fixtures / "monorepo",
                ("packages/api-schema/openapi.yaml",),
                (),
                None,
            ),
            (
                fixtures / "monorepo",
                (),
                ("apps/android/app/src/main/AndroidManifest.xml",),
                fixtures / "monorepo",
            ),
        )
        for index, (repository, target_files, changed_files, cwd) in enumerate(
            cases
        ):
            with self.subTest(case=index, repository=repository.name):
                arguments = [
                    "repository-discover",
                    str(repository),
                    "--manifests",
                    str(ROOT / "platforms"),
                ]
                for target in target_files:
                    arguments.extend(["--target-file", target])
                for changed in changed_files:
                    arguments.extend(["--changed-file", changed])
                if cwd is not None:
                    arguments.extend(["--cwd", str(cwd)])
                result = self.run_rust(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = engine.discover(
                    repository,
                    target_files=target_files,
                    changed_files=changed_files,
                    cwd=cwd,
                )
                self.assertEqual(result.stdout, dumps(expected))

    def test_repository_glob_classes_and_target_path_boundaries_match_python(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "platforms", root / "platforms")
            shutil.copytree(ROOT / "disciplines", root / "disciplines")
            shutil.copytree(ROOT / "runtime-configs", root / "runtime-configs")
            manifest_path = root / "platforms/apple/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["detection"] = {
                "strong": ["App[0-9].xcodeproj"],
                "medium": [],
                "weak": [],
            }
            manifest_path.write_text(dumps(manifest), encoding="utf-8")
            repository = root / "repository"
            (repository / "App1.xcodeproj").mkdir(parents=True)
            registry = ManifestRegistry.from_directory(root / "platforms")
            expected = DiscoveryEngine(registry).discover(repository)
            result = self.run_rust(
                "repository-discover",
                str(repository),
                "--manifests",
                str(root / "platforms"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))
            self.assertEqual(expected["platforms"], ["apple"])

            unicode_relative = "é:module/file.swift"
            expected = DiscoveryEngine(registry).discover(
                repository,
                target_files=[unicode_relative],
            )
            result = self.run_rust(
                "repository-discover",
                str(repository),
                "--manifests",
                str(root / "platforms"),
                "--target-file",
                unicode_relative,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))

            for candidate in (
                "../../apps/ios/Foo.swift",
                "/apps/ios/Foo.swift",
                r"C:\apps\ios\Foo.swift",
            ):
                with self.subTest(candidate=candidate):
                    with self.assertRaises(ContractError):
                        DiscoveryEngine(registry).discover(
                            repository,
                            target_files=[candidate],
                        )
                    result = self.run_rust(
                        "repository-discover",
                        str(repository),
                        "--manifests",
                        str(root / "platforms"),
                        "--target-file",
                        candidate,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")

    def test_repository_discovery_edge_cases_match_python(self) -> None:
        registry = ManifestRegistry.from_directory(ROOT / "platforms")
        engine = DiscoveryEngine(registry)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate = root / "aggregate"
            project = aggregate / "apps/ios/App.xcodeproj"
            project.mkdir(parents=True)
            (aggregate / "Podfile").write_text(
                "platform :ios",
                encoding="utf-8",
            )
            (project / "project.pbxproj").write_text(
                "// fixture",
                encoding="utf-8",
            )
            ignored = root / "ignored"
            fixture = ignored / "tests/fixtures/App.xcodeproj"
            fixture.mkdir(parents=True)
            (fixture / "project.pbxproj").write_text(
                "// fixture",
                encoding="utf-8",
            )
            outer = root / "outer"
            (outer / "apps/other-app").mkdir(parents=True)
            unrelated = outer / "misc/repo"
            unrelated.mkdir(parents=True)
            (unrelated / "README.md").write_text(
                "fixture",
                encoding="utf-8",
            )
            cases = (
                (aggregate, ("apps/ios/Foo.swift",)),
                (ignored, ()),
                (unrelated, ()),
            )
            for index, (repository, target_files) in enumerate(cases):
                with self.subTest(case=index):
                    arguments = [
                        "repository-discover",
                        str(repository),
                        "--manifests",
                        str(ROOT / "platforms"),
                    ]
                    for target in target_files:
                        arguments.extend(["--target-file", target])
                    result = self.run_rust(*arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected = engine.discover(
                        repository,
                        target_files=target_files,
                    )
                    self.assertEqual(result.stdout, dumps(expected))

    def test_plan_empty_ambiguity_and_policy_tamper_are_fail_closed(self) -> None:
        registry = ManifestRegistry.from_directory(ROOT / "platforms")
        profile = DiscoveryEngine(registry).discover(
            ROOT / "tests/fixtures/apple-app"
        )
        policy = PolicyResolver().resolve(
            profile,
            "实现 iOS 功能",
            constraints={"routing_ambiguities": []},
        )
        expected = PlanCompiler(registry).compile(profile, policy)
        self.assertEqual(expected["status"], "ready")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_path = root / "profile.json"
            policy_path = root / "policy.json"
            profile_path.write_text(dumps(profile), encoding="utf-8")
            policy_path.write_text(dumps(policy), encoding="utf-8")
            arguments = [
                "plan-compile",
                str(profile_path),
                str(policy_path),
                "--manifests",
                str(ROOT / "platforms"),
            ]
            result = self.run_rust(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))

            stale = json.loads(dumps(policy))
            stale["selected_platforms"] = ["desktop"]
            policy_path.write_text(dumps(stale), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "fingerprint mismatch"):
                PlanCompiler(registry).compile(profile, stale)
            result = self.run_rust(*arguments)
            self.assertEqual(result.returncode, 2)
            self.assertIn("fingerprint mismatch", result.stderr)

            duplicate = json.loads(dumps(policy))
            duplicate["selected_platforms"] = ["apple", "apple"]
            duplicate["fingerprint"] = sha256(
                {
                    key: value
                    for key, value in duplicate.items()
                    if key != "fingerprint"
                }
            )
            policy_path.write_text(dumps(duplicate), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "must be unique"):
                PlanCompiler(registry).compile(profile, duplicate)
            result = self.run_rust(*arguments)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be unique", result.stderr)

            boolean_confidence = json.loads(dumps(policy))
            boolean_confidence["decisions"][0]["confidence"] = True
            boolean_confidence["fingerprint"] = sha256(
                {
                    key: value
                    for key, value in boolean_confidence.items()
                    if key != "fingerprint"
                }
            )
            policy_path.write_text(dumps(boolean_confidence), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "confidence is invalid"):
                PlanCompiler(registry).compile(profile, boolean_confidence)
            result = self.run_rust(*arguments)
            self.assertEqual(result.returncode, 2)
            self.assertIn("confidence is invalid", result.stderr)

    def test_plan_compiler_fixture_corpus_matches_python_byte_for_byte(
        self,
    ) -> None:
        fixtures = ROOT / "tests/fixtures"
        cases = (
            ("apple-app", "实现 iOS 功能", ()),
            ("apple-app", "实现 Figma 页面并补充 QA 测试", ()),
            ("apple-app", "审查 iOS 代码", ()),
            ("apple-app", "只做 iOS QA 回归", ()),
            ("apple-app", "更新 iOS 文档", ()),
            ("apple-app", "调查 iOS crash 性能", ()),
            ("desktop-electron", "实现桌面功能", ()),
            ("android-app", "实现 Android 功能", ()),
            ("unknown", "实现功能", ()),
            ("ambiguous", "修复功能", ()),
            ("apple-app", "实现 iOS 功能", ("ios-agent-skills",)),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_path = root / "profile.json"
            policy_path = root / "policy.json"
            for fixture_name, task, disabled in cases:
                with self.subTest(
                    fixture=fixture_name,
                    task=task,
                    disabled=disabled,
                ):
                    registry = ManifestRegistry.from_directory(
                        ROOT / "platforms",
                        disabled_providers=disabled,
                    )
                    profile = DiscoveryEngine(registry).discover(
                        fixtures / fixture_name
                    )
                    policy = PolicyResolver().resolve(profile, task)
                    expected = PlanCompiler(registry).compile(profile, policy)
                    profile_path.write_text(
                        dumps(profile),
                        encoding="utf-8",
                    )
                    policy_path.write_text(
                        dumps(policy),
                        encoding="utf-8",
                    )
                    arguments = [
                        "plan-compile",
                        str(profile_path),
                        str(policy_path),
                        "--manifests",
                        str(ROOT / "platforms"),
                    ]
                    for provider in disabled:
                        arguments.extend(["--disable-provider", provider])
                    result = self.run_rust(*arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, dumps(expected))

    def test_package_lock_lifecycle_and_locked_plan_match_python_byte_for_byte(
        self,
    ) -> None:
        apple = build_install_bundle(ROOT / "platforms", platforms=["apple"])
        expanded = build_install_bundle(
            ROOT / "platforms",
            platforms=["apple", "desktop"],
        ).package_lock
        registry = ManifestRegistry.from_directory(ROOT / "platforms")
        profile = DiscoveryEngine(registry).discover(
            ROOT / "tests/fixtures/apple-app"
        )
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        locked_plan = PlanCompiler(registry).compile(
            profile,
            policy,
            package_lock=apple.package_lock,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_plan_path = root / "install-plan.json"
            lock_path = root / "agent-skills.lock"
            expanded_path = root / "expanded.lock"
            profile_path = root / "profile.json"
            policy_path = root / "policy.json"
            for value, path in (
                (apple.plan, install_plan_path),
                (apple.package_lock, lock_path),
                (expanded, expanded_path),
                (profile, profile_path),
                (policy, policy_path),
            ):
                dump(value, path)

            resolved = self.run_rust(
                "lock-resolve",
                str(install_plan_path),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(resolved.stdout, dumps(apple.package_lock))

            validated = self.run_rust("lock-validate", str(lock_path))
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(
                validated.stdout,
                dumps(
                    {
                        "lock_hash": apple.package_lock["fingerprint"],
                        "status": "passed",
                    }
                ),
            )
            explained = self.run_rust("lock-explain", str(expanded_path))
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertEqual(
                explained.stdout,
                dumps(explain_package_lock(expanded)),
            )
            diffed = self.run_rust(
                "lock-diff",
                str(lock_path),
                str(expanded_path),
            )
            self.assertEqual(diffed.returncode, 0, diffed.stderr)
            self.assertEqual(
                diffed.stdout,
                dumps(diff_package_locks(apple.package_lock, expanded)),
            )
            compiled = self.run_rust(
                "plan-compile",
                str(profile_path),
                str(policy_path),
                "--manifests",
                str(ROOT / "platforms"),
                "--lock",
                str(lock_path),
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertEqual(compiled.stdout, dumps(locked_plan))

            def mutate_tree_drive(value: dict) -> None:
                package = next(
                    item
                    for item in value["packages"]
                    if item["id"] == "apple"
                )
                package["files"][0]["path"] = "C:x"
                package["files_sha256"] = sha256(package["files"])
                selected = next(
                    item
                    for item in value["selected_packages"]
                    if item["id"] == "apple"
                )
                selected["source_sha256"] = package["files_sha256"]
                for provider in value["capability_providers"].values():
                    if provider["package"] == "apple":
                        provider["source_sha256"] = package["files_sha256"]
                value["assets"] = [
                    {
                        "mode": entry["mode"],
                        "package": package_record["id"],
                        "path": entry["path"],
                        "sha256": entry["sha256"],
                    }
                    for package_record in value["packages"]
                    for entry in package_record["files"]
                ]
                value["asset_summary"]["content_sha256"] = sha256(
                    value["assets"]
                )

            install_plan_mutations = {
                "stale-fingerprint": lambda value: value.__setitem__(
                    "fingerprint",
                    "0" * 64,
                ),
                "invalid-status": lambda value: value.__setitem__(
                    "status",
                    "evil",
                ),
                "managed-roots": lambda value: value.__setitem__(
                    "managed_roots",
                    ["evil"],
                ),
                "selection-reason": lambda value: next(
                    item
                    for item in value["selected_packages"]
                    if item["id"] == "apple"
                ).__setitem__("selection_reasons", ["core"]),
                "package-file-count": lambda value: value["packages"][
                    0
                ].__setitem__("file_count", 999_999),
                "asset-summary": lambda value: value[
                    "asset_summary"
                ].__setitem__("content_sha256", "0" * 64),
                "fragment-drive": lambda value: value["instructions"][
                    "fragments"
                ][0].__setitem__("path", "C:x"),
                "tree-drive": mutate_tree_drive,
            }
            for name, mutate in install_plan_mutations.items():
                with self.subTest(install_plan_tamper=name):
                    invalid = deepcopy(apple.plan)
                    if name not in {"stale-fingerprint", "invalid-status"}:
                        invalid.pop("package_lock_hash", None)
                    mutate(invalid)
                    if name not in {"stale-fingerprint", "invalid-status"}:
                        invalid["fingerprint"] = sha256({
                            key: value
                            for key, value in invalid.items()
                            if key not in {"fingerprint", "status"}
                        })
                    with self.assertRaises(ContractError):
                        resolve_package_lock(
                            invalid,
                            schema_root=ROOT / "schemas",
                        )
                    invalid_path = root / f"install-{name}.json"
                    dump(invalid, invalid_path)
                    rejected = self.run_rust(
                        "lock-resolve",
                        str(invalid_path),
                        "--schemas",
                        str(ROOT / "schemas"),
                    )
                    self.assertEqual(rejected.returncode, 2)
                    self.assertEqual(rejected.stdout, "")

    def test_package_lock_sources_lineage_and_tamper_boundaries_match_python(
        self,
    ) -> None:
        apple = build_install_bundle(ROOT / "platforms", platforms=["apple"])
        artifact_hash = "a" * 64
        source_cases = (
            (
                ("--source", "apple=./platforms/apple", "--source-base", str(ROOT)),
                resolve_package_lock(
                    apple.plan,
                    schema_root=ROOT / "schemas",
                    package_sources={
                        "apple": {
                            "kind": "relative-path",
                            "uri": "./platforms/apple",
                        }
                    },
                    source_base=ROOT,
                ),
            ),
            (
                (
                    "--source",
                    "apple=https://example.test/releases/apple.zip",
                    "--source-sha256",
                    f"apple={artifact_hash}",
                ),
                resolve_package_lock(
                    apple.plan,
                    schema_root=ROOT / "schemas",
                    package_sources={
                        "apple": {
                            "kind": "https",
                            "uri": "https://example.test/releases/apple.zip",
                        }
                    },
                    package_source_artifact_hashes={
                        "apple": artifact_hash,
                    },
                ),
            ),
            *tuple(
                (
                    (
                        "--source",
                        (
                            "apple=https://example.test/releases/apple.zip"
                            f"{suffix}"
                        ),
                        "--source-sha256",
                        f"apple={artifact_hash}",
                    ),
                    resolve_package_lock(
                        apple.plan,
                        schema_root=ROOT / "schemas",
                        package_sources={
                            "apple": {
                                "kind": "https",
                                "uri": (
                                    "https://example.test/releases/apple.zip"
                                    f"{suffix}"
                                ),
                            }
                        },
                        package_source_artifact_hashes={
                            "apple": artifact_hash,
                        },
                    ),
                )
                for suffix in ("?", "#", "?#")
            ),
            *tuple(
                (
                    (
                        "--source",
                        f"apple={uri}",
                        "--source-sha256",
                        f"apple={artifact_hash}",
                    ),
                    resolve_package_lock(
                        apple.plan,
                        schema_root=ROOT / "schemas",
                        package_sources={
                            "apple": {
                                "kind": "https",
                                "uri": uri,
                            }
                        },
                        package_source_artifact_hashes={
                            "apple": artifact_hash,
                        },
                    ),
                )
                for uri in (
                    "https://[::1]:/apple.zip",
                    "https://[v1.test]/apple.zip",
                    "https://[fe80::1%25eth0]/apple.zip",
                )
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_plan_path = root / "install-plan.json"
            lock_path = root / "agent-skills.lock"
            dump(apple.plan, install_plan_path)
            dump(apple.package_lock, lock_path)
            for arguments, expected in source_cases:
                with self.subTest(arguments=arguments):
                    result = self.run_rust(
                        "lock-resolve",
                        str(install_plan_path),
                        "--schemas",
                        str(ROOT / "schemas"),
                        *arguments,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, dumps(expected))

            successor = resolve_package_lock(
                apple.plan,
                schema_root=ROOT / "schemas",
                previous_lock=apple.package_lock,
            )
            result = self.run_rust(
                "lock-resolve",
                str(install_plan_path),
                "--schemas",
                str(ROOT / "schemas"),
                "--previous",
                str(lock_path),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(successor))

            tampered = deepcopy(apple.package_lock)
            tampered["packages"][-1]["source"]["sha256"] = "0" * 64
            with self.assertRaises(ContractError):
                validate_package_lock(tampered)
            tampered_path = root / "tampered.lock"
            dump(tampered, tampered_path)
            rejected = self.run_rust("lock-validate", str(tampered_path))
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")

            uppercase = deepcopy(source_cases[1][1])
            apple_source = next(
                item["source"]
                for item in uppercase["packages"]
                if item["id"] == "apple"
            )
            apple_source["uri"] = apple_source["uri"].replace(
                "https://",
                "HTTPS://",
            )
            uppercase["fingerprint"] = sha256({
                key: value
                for key, value in uppercase.items()
                if key != "fingerprint"
            })
            validate_package_lock(uppercase)
            uppercase_path = root / "uppercase.lock"
            dump(uppercase, uppercase_path)
            accepted = self.run_rust("lock-validate", str(uppercase_path))
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            invalid_cases = {}
            for unsafe_path in (
                r"schemas\escaped.schema.json",
                "C:/escaped.schema.json",
                "C:escaped.schema.json",
            ):
                invalid = deepcopy(apple.package_lock)
                invalid["schema_inventory"]["files"][0]["path"] = unsafe_path
                invalid["schema_inventory"]["content_sha256"] = sha256(
                    invalid["schema_inventory"]["files"]
                )
                invalid["fingerprint"] = sha256({
                    key: value
                    for key, value in invalid.items()
                    if key != "fingerprint"
                })
                invalid_cases[f"schema-{unsafe_path[0]}"] = invalid
            excessive = deepcopy(apple.package_lock)
            excessive["selection"]["platforms"] = [
                f"platform-{index}"
                for index in range(MAX_LOCK_PACKAGES + 1)
            ]
            excessive["fingerprint"] = sha256({
                key: value
                for key, value in excessive.items()
                if key != "fingerprint"
            })
            invalid_cases["excessive-selection"] = excessive
            wrong_core_kind = deepcopy(apple.package_lock)
            wrong_core_kind["packages"][0]["kind"] = "platform"
            wrong_core_kind["fingerprint"] = sha256({
                key: value
                for key, value in wrong_core_kind.items()
                if key != "fingerprint"
            })
            invalid_cases["wrong-core-kind"] = wrong_core_kind
            cyclic = deepcopy(apple.package_lock)
            cyclic["dependencies"].append({
                "from": "design",
                "required_capabilities": ["implementation.apple"],
                "requirement": "optional",
                "to": "apple",
                "version": ">=0.2.0 <0.3.0",
            })
            cyclic["dependencies"].sort(
                key=lambda dependency: (
                    dependency["from"],
                    dependency["to"],
                )
            )
            cyclic["fingerprint"] = sha256({
                key: value
                for key, value in cyclic.items()
                if key != "fingerprint"
            })
            invalid_cases["dependency-cycle"] = cyclic
            for name, invalid in invalid_cases.items():
                with self.subTest(invalid=name):
                    with self.assertRaises(ContractError):
                        validate_package_lock(invalid)
                    invalid_path = root / f"{name}.lock"
                    dump(invalid, invalid_path)
                    result = self.run_rust(
                        "lock-validate",
                        str(invalid_path),
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")

            for malformed in (
                "./../apple",
                "./C:/apple",
                "./C:",
                "./C:apple",
                "https://[bad/a",
                "https://]/a",
                "https://example.test/a\tbad",
                r"https://example.test\evil/a",
            ):
                with self.subTest(malformed_source=malformed):
                    arguments = [
                        "lock-resolve",
                        str(install_plan_path),
                        "--schemas",
                        str(ROOT / "schemas"),
                        "--source",
                        f"apple={malformed}",
                    ]
                    if malformed.startswith("https://"):
                        arguments.extend(
                            [
                                "--source-sha256",
                                f"apple={artifact_hash}",
                            ]
                        )
                    unsafe = self.run_rust(*arguments)
                    self.assertEqual(unsafe.returncode, 2)
                    self.assertEqual(unsafe.stdout, "")

            schema_repository = root / "schema-repository"
            schemas = schema_repository / "schemas"
            schemas.mkdir(parents=True)
            schema = {
                "$schema": (
                    "https://json-schema.org/draft/2020-12/schema"
                ),
                "type": "object",
            }
            dump(schema, schemas / "root.schema.json")
            external = schema_repository / "external"
            external.mkdir()
            dump(schema, external / "linked.schema.json")
            contracts = schema_repository / "platforms/apple/contracts"
            contracts.parent.mkdir(parents=True)
            contracts.symlink_to(external, target_is_directory=True)
            unanchored_plan = deepcopy(apple.plan)
            unanchored_plan.pop("package_lock_hash")
            unanchored_plan["fingerprint"] = sha256({
                key: value
                for key, value in unanchored_plan.items()
                if key not in {"fingerprint", "status"}
            })
            unanchored_plan_path = root / "unanchored-install-plan.json"
            dump(unanchored_plan, unanchored_plan_path)
            expected = resolve_package_lock(
                unanchored_plan,
                schema_root=schemas,
            )
            result = self.run_rust(
                "lock-resolve",
                str(unanchored_plan_path),
                "--schemas",
                str(schemas),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))

            source_root = root / "source-root"
            apple_source = source_root / "platforms/apple"
            shutil.copytree(ROOT / "platforms/apple", apple_source)
            external_skills = source_root / "external-skills"
            (apple_source / "skills").rename(external_skills)
            (apple_source / "skills").symlink_to(
                external_skills,
                target_is_directory=True,
            )
            with self.assertRaises(ContractError):
                resolve_package_lock(
                    apple.plan,
                    schema_root=ROOT / "schemas",
                    package_sources={
                        "apple": {
                            "kind": "relative-path",
                            "uri": "./platforms/apple",
                        }
                    },
                    source_base=source_root,
                )
            unsafe_symlink = self.run_rust(
                "lock-resolve",
                str(install_plan_path),
                "--schemas",
                str(ROOT / "schemas"),
                "--source",
                "apple=./platforms/apple",
                "--source-base",
                str(source_root),
            )
            self.assertEqual(unsafe_symlink.returncode, 2)
            self.assertEqual(unsafe_symlink.stdout, "")

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
