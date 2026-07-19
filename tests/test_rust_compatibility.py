from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from unittest import mock

from agent_workflow import __version__ as PYTHON_CORE_VERSION
from agent_workflow.adapters import (
    build_adapter_request,
    validate_adapter_request,
    validate_adapter_result,
    validate_provider_invocation_plan,
)
from agent_workflow.adapters.invocations import (
    _claim_provider_invocation_at as claim_provider_invocation,
)
from agent_workflow.adapters.invocations import (
    _collect_submitted_results_at as collect_submitted_results,
)
from agent_workflow.adapters.invocations import (
    _inspect_provider_invocation_at as inspect_provider_invocation,
)
from agent_workflow.adapters.invocations import (
    _prepare_provider_invocation_at as prepare_provider_invocation,
)
from agent_workflow.adapters.invocations import (
    _submit_provider_invocation_at as submit_provider_invocation,
)
from agent_workflow.activation import (
    ACTIVATION_HANDLER_ID,
    DEACTIVATION_HANDLER_ID,
    PRESERVE_HANDLER_ID,
    activation_handler_sha256,
    apply_source_activation,
    deactivation_external_paths,
    external_paths as activation_external_paths,
)
from agent_workflow.canonical_json import dump, dumps, sha256
from agent_workflow.canonical_json import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
)
from agent_workflow.discovery import DiscoveryEngine
from agent_workflow.contracts import (
    validate_doctor_report,
    validate_upgrade_conformance_evidence,
    validate_upgrade_plan,
    validate_worktree_session_context,
)
from agent_workflow.doctor import diagnose_install
from agent_workflow.installation import (
    _load_package,
    _resolve_packages,
    build_install_bundle,
    install_bundle,
    resolve_platform_selection,
)
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
from agent_workflow.runtime import FakeAdapterExecutor, RecordedAdapterExecutor, RunLedger
from agent_workflow.upgrade import (
    make_upgrade_conformance_evidence,
    plan_upgrade,
    prepare_upgrade_candidate,
)
if os.name != "nt":
    from agent_workflow.worktree_sessions.git_workspace import (
        create_session_worktree,
        freeze_checkpoint,
        inspect_repository,
        refresh_session_source_identity,
        remove_created_session_worktree,
        repository_patch,
        session_source_identity,
        worktree_status,
    )
    from agent_workflow.worktree_sessions.gate import (
        attach_adapter_result,
        evaluate_session_gate,
    )
    from agent_workflow.worktree_sessions.registry import (
        SessionRegistry,
        new_session_context,
    )


ROOT = Path(__file__).resolve().parents[1]
RUST_COMPATIBILITY_ENABLED = (
    os.environ.get("AGENT_SKILLS_RUST_COMPATIBILITY") == "1"
    and shutil.which("cargo") is not None
)


def _normalize_runtime_ledger(value: dict) -> dict:
    """Remove runtime-generated identities and timestamps for semantic parity."""

    normalized = deepcopy(value)
    attempt_ids = {
        attempt["attempt_id"]: (
            f"attempt:{attempt['node_id']}:{attempt['attempt_number']}"
        )
        for attempt in normalized["node_attempts"]
    }
    normalized["run_id"] = "run"
    for attempt in normalized["node_attempts"]:
        attempt["attempt_id"] = attempt_ids[attempt["attempt_id"]]
        attempt["deadline"] = "deadline"
        for event in attempt["events"]:
            event["at"] = "time"
    for collection in (
        "resource_events",
        "approval_records",
        "artifact_hashes",
        "adapter_outcomes",
        "evidence",
    ):
        for item in normalized.get(collection, []):
            if "attempt_id" in item:
                item["attempt_id"] = attempt_ids[item["attempt_id"]]
            if (
                collection == "adapter_outcomes"
                and item.get("invocation_id", "").startswith(
                    "contract-failure-attempt-"
                )
            ):
                item["invocation_id"] = (
                    f"contract-failure-{item['attempt_id']}"
                )
                item["request_id"] = "adapter-request:contract-failure"
    return normalized


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
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def run_rust_bytes(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(self.rust_cli), *arguments],
            cwd=ROOT,
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
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

    def test_source_package_selection_matches_python(self) -> None:
        cases = (
            (["all"], [], [], False),
            (["apple"], [], [], False),
            ([], ["qa"], [], False),
            ([], [], ["codex"], False),
            (["apple"], [], ["codex"], False),
            ([], [], [], True),
        )
        for platforms, disciplines, runtime_configs, core_only in cases:
            with self.subTest(
                platforms=platforms,
                disciplines=disciplines,
                runtime_configs=runtime_configs,
                core_only=core_only,
            ):
                if (disciplines or runtime_configs) and not platforms and not core_only:
                    selected: tuple[str, ...] = ()
                else:
                    selected = resolve_platform_selection(
                        ROOT / "platforms",
                        platforms=platforms,
                        core_only=core_only,
                    )
                source = _resolve_packages(
                    (ROOT / "platforms").resolve(),
                    selected_platforms=selected,
                    disciplines=disciplines,
                    runtime_configs=runtime_configs,
                )
                expected = {
                    "package_roots": [
                        {"id": identifier, "path": str(path)}
                        for identifier, path in source.package_roots
                    ],
                    "resolved_dependencies": list(source.dependencies),
                    "selected_disciplines": list(source.selected_disciplines),
                    "selected_platforms": list(selected),
                    "selected_runtime_configs": list(source.selected_runtime_configs),
                    "selection_reasons": {
                        identifier: list(reasons)
                        for identifier, reasons in source.selection_reasons.items()
                    },
                }
                arguments = ["install-selection", str(ROOT / "platforms")]
                for platform in platforms:
                    arguments.extend(("--platform", platform))
                for discipline in disciplines:
                    arguments.extend(("--discipline", discipline))
                for runtime_config in runtime_configs:
                    arguments.extend(("--runtime-config", runtime_config))
                if core_only:
                    arguments.append("--core-only")
                native = self.run_rust(*arguments)
                self.assertEqual(native.returncode, 0, native.stderr)
                self.assertEqual(json.loads(native.stdout), expected)

        rejected = (
            ([], "select --core-only"),
            (["web"], "not installable"),
            (["all", "apple"], "cannot be combined"),
            (["apple", "apple"], "must be unique"),
        )
        for platforms, message in rejected:
            with self.subTest(rejected=platforms):
                arguments = ["install-selection", str(ROOT / "platforms")]
                for platform in platforms:
                    arguments.extend(("--platform", platform))
                native = self.run_rust(*arguments)
                self.assertEqual(native.returncode, 2)
                self.assertIn(message, native.stderr)

    def test_source_package_selection_accepts_omitted_package_requires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platforms = Path(directory) / "platforms"
            shutil.copytree(ROOT / "platforms" / "core", platforms / "core")
            manifest_path = platforms / "core" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("package_requires")
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            source = _resolve_packages(
                platforms.resolve(),
                selected_platforms=(),
                disciplines=(),
                runtime_configs=(),
            )
            native = self.run_rust(
                "install-selection",
                str(platforms),
                "--core-only",
            )
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(
                [item["id"] for item in json.loads(native.stdout)["package_roots"]],
                [identifier for identifier, _ in source.package_roots],
            )

    def test_source_package_selection_ignores_nested_manifests_like_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platforms = Path(directory) / "platforms"
            shutil.copytree(ROOT / "platforms" / "core", platforms / "core")
            nested = platforms / "core" / "assets" / "nested"
            nested.mkdir(parents=True)
            (nested / "manifest.json").write_text("{}\n", encoding="utf-8")

            source = _resolve_packages(
                platforms.resolve(),
                selected_platforms=(),
                disciplines=(),
                runtime_configs=(),
            )
            native = self.run_rust(
                "install-selection",
                str(platforms),
                "--core-only",
            )
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(
                [item["id"] for item in json.loads(native.stdout)["package_roots"]],
                [identifier for identifier, _ in source.package_roots],
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support is required")
    def test_source_package_selection_symlinks_fail_closed_like_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platforms = root / "platforms"
            shutil.copytree(ROOT / "platforms" / "core", platforms / "core")
            external = root / "external"
            external.mkdir()
            unsafe = platforms / "unsafe"
            try:
                unsafe.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlink is unavailable: {error}")

            with self.assertRaisesRegex(ContractError, "package candidate is unsafe"):
                _resolve_packages(
                    platforms.resolve(),
                    selected_platforms=(),
                    disciplines=(),
                    runtime_configs=(),
                )
            native_candidate = self.run_rust(
                "install-selection",
                str(platforms),
                "--core-only",
            )
            self.assertEqual(native_candidate.returncode, 2)
            self.assertIn("package candidate is unsafe", native_candidate.stderr)

            unsafe.unlink()
            unsafe.mkdir()
            (unsafe / "manifest.json").symlink_to(
                platforms / "core" / "manifest.json"
            )
            with self.assertRaisesRegex(ContractError, "package candidate is unsafe"):
                _resolve_packages(
                    platforms.resolve(),
                    selected_platforms=(),
                    disciplines=(),
                    runtime_configs=(),
                )
            native_manifest = self.run_rust(
                "install-selection",
                str(platforms),
                "--core-only",
            )
            self.assertEqual(native_manifest.returncode, 2)
            self.assertIn("package candidate is unsafe", native_manifest.stderr)

    def test_codex_config_renderer_matches_installed_source_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.toml"
            shared = root / "shared.toml"
            agents = root / "代理" / "AGENTS.md"
            existing.write_text(
                """
service_tier = "fast"
model = "local"
unicode = "中文"
memories.enabled = true

[features]
legacy = true

[plugins.local]
enabled = true
path = "/tmp/local"

[mcp_servers.codegraph]
command = "codegraph"
args = ["serve", "--mcp"]
""",
                encoding="utf-8",
            )
            shared.write_text(
                """
model = "shared"
service_tier = "flex"
date = 2026-07-18
float = 1.0
stamp = 2026-07-18T12:00:00+00:00
fractional = 2026-07-18T12:00:00.1-07:30
local_time = 12:00:00.123

[features]
shared = true

[plugins.managed]
enabled = true

[mcp_servers.shared]
url = "https://example.com"

[[agents.entries]]
name = "one"
""",
                encoding="utf-8",
            )
            source = subprocess.run(
                [
                    sys.executable,
                    str(
                        ROOT
                        / "runtime-configs/codex/assets/scripts/"
                        "sync_codex_shared_config.py"
                    ),
                    "--shared-config",
                    str(shared),
                    "--existing-config",
                    str(existing),
                    "--agents-path",
                    str(agents),
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                source.returncode,
                0,
                source.stderr.decode("utf-8", errors="replace"),
            )
            native = self.run_rust_bytes(
                "codex-config-render",
                str(shared),
                str(agents),
                "--existing-config",
                str(existing),
            )
            self.assertEqual(
                native.returncode,
                0,
                native.stderr.decode("utf-8", errors="replace"),
            )
            self.assertEqual(native.stdout, source.stdout)

    def test_codex_config_renderer_rejects_oversized_cli_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared.toml"
            with shared.open("wb") as stream:
                stream.truncate(64 * 1024 * 1024 + 1)
            result = self.run_rust(
                "codex-config-render",
                str(shared),
                str(Path(directory) / "AGENTS.md"),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("shared Codex config has more than", result.stderr)

    @unittest.skipIf(
        os.name == "nt",
        "Python source installation mode contract is POSIX-only",
    )
    def test_full_uninstall_report_and_filesystem_match_python(self) -> None:
        def snapshot(root: Path) -> list[tuple[str, str, int, str | None]]:
            records: list[tuple[str, str, int, str | None]] = []
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                mode = path.lstat().st_mode & 0o777
                if path.is_symlink():
                    records.append((relative, "symlink", mode, os.readlink(path)))
                elif path.is_dir():
                    records.append((relative, "directory", mode, None))
                else:
                    records.append(
                        (
                            relative,
                            "file",
                            mode,
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                        )
                    )
            return records

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python_target = root / "python" / ".codex"
            rust_target = root / "rust" / ".codex"
            for target in (python_target, rust_target):
                installed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/install_local.py"),
                        "--target-root",
                        str(target),
                        "--platform",
                        "apple",
                        "--json",
                    ],
                    cwd=ROOT,
                    encoding="utf-8",
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(installed.returncode, 0, installed.stderr)

            before_preview = snapshot(rust_target)
            source_preview = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/uninstall_local.py"),
                    "--target-root",
                    str(rust_target),
                    "--platform",
                    "all",
                    "--dry-run",
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            native_preview = self.run_rust_bytes(
                "lifecycle-uninstall",
                str(rust_target),
                "--platform",
                "all",
                "--dry-run",
                "--json",
            )
            self.assertEqual(source_preview.returncode, 0, source_preview.stderr)
            self.assertEqual(native_preview.returncode, 0, native_preview.stderr)
            self.assertEqual(native_preview.stdout, source_preview.stdout)
            self.assertEqual(snapshot(rust_target), before_preview)

            source_human = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/uninstall_local.py"),
                    "--target-root",
                    str(rust_target),
                    "--platform",
                    "all",
                    "--dry-run",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            native_human = self.run_rust_bytes(
                "lifecycle-uninstall",
                str(rust_target),
                "--platform",
                "all",
                "--dry-run",
            )
            self.assertEqual(source_human.returncode, 0, source_human.stderr)
            self.assertEqual(native_human.returncode, 0, native_human.stderr)
            self.assertEqual(native_human.stdout, source_human.stdout)
            self.assertEqual(snapshot(rust_target), before_preview)

            source_blocked = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/uninstall_local.py"),
                    "--target-root",
                    str(rust_target),
                    "--platform",
                    "missing",
                    "--dry-run",
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            native_blocked = self.run_rust_bytes(
                "lifecycle-uninstall",
                str(rust_target),
                "--platform",
                "missing",
                "--dry-run",
                "--json",
            )
            self.assertEqual(source_blocked.returncode, 2)
            self.assertEqual(native_blocked.returncode, 2)
            self.assertEqual(native_blocked.stdout, b"")
            self.assertEqual(native_blocked.stderr, source_blocked.stderr)
            self.assertEqual(snapshot(rust_target), before_preview)

            source = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/uninstall_local.py"),
                    "--target-root",
                    str(python_target),
                    "--platform",
                    "all",
                    "--json",
                ],
                cwd=ROOT,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            native = self.run_rust(
                "lifecycle-uninstall",
                str(rust_target),
                "--platform",
                "all",
                "--json",
            )
            self.assertEqual(source.returncode, 0, source.stderr)
            self.assertEqual(native.returncode, 0, native.stderr)
            source_report = json.loads(source.stdout)
            native_report = json.loads(native.stdout)
            source_report["target_root"] = "<target>"
            native_report["target_root"] = "<target>"
            self.assertEqual(native_report, source_report)
            self.assertEqual(snapshot(rust_target), snapshot(python_target))

            missing = root / "missing" / ".codex"
            blocked = self.run_rust("lifecycle-uninstall", str(missing), "--json")
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(blocked.stdout, "")
            self.assertEqual(json.loads(blocked.stderr)["status"], "blocked")
            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())

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

    def test_source_package_snapshot_matches_python(self) -> None:
        cases = (
            (["apple"], [], [], False),
            ([], ["qa"], [], False),
            ([], [], ["codex"], False),
            ([], [], [], True),
        )
        for platforms, disciplines, runtime_configs, core_only in cases:
            with self.subTest(
                platforms=platforms,
                disciplines=disciplines,
                runtime_configs=runtime_configs,
                core_only=core_only,
            ):
                if (disciplines or runtime_configs) and not platforms and not core_only:
                    selected: tuple[str, ...] = ()
                else:
                    selected = resolve_platform_selection(
                        ROOT / "platforms",
                        platforms=platforms,
                        core_only=core_only,
                    )
                source = _resolve_packages(
                    (ROOT / "platforms").resolve(),
                    selected_platforms=selected,
                    disciplines=disciplines,
                    runtime_configs=runtime_configs,
                )
                packages = []
                for identifier, path in source.package_roots:
                    package = _load_package(path, identifier)
                    packages.append({
                        "directories": list(package.directories),
                        "files": list(package.files),
                        "fragments": list(package.fragments),
                        "id": package.package_id,
                        "manifest": package.manifest,
                        "manifest_sha256": package.manifest_digest,
                        "provider": package.provider,
                        "provider_manifest_sha256": package.provider_digest,
                        "skills": [
                            {
                                "directories": list(skill.directories),
                                "files": list(skill.files),
                                "name": skill.name,
                            }
                            for skill in package.skills
                        ],
                    })
                arguments = [
                    "install-source-snapshot",
                    str(ROOT / "platforms"),
                ]
                for platform in platforms:
                    arguments.extend(("--platform", platform))
                for discipline in disciplines:
                    arguments.extend(("--discipline", discipline))
                for runtime_config in runtime_configs:
                    arguments.extend(("--runtime-config", runtime_config))
                if core_only:
                    arguments.append("--core-only")
                result = self.run_rust(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, dumps({"packages": packages}))

    def test_source_install_bundle_matches_python(self) -> None:
        cases = (
            (["apple"], [], [], False),
            ([], ["qa"], [], False),
            ([], [], ["codex"], False),
            ([], [], [], True),
        )
        for platforms, disciplines, runtime_configs, core_only in cases:
            with self.subTest(
                platforms=platforms,
                disciplines=disciplines,
                runtime_configs=runtime_configs,
                core_only=core_only,
            ):
                expected = build_install_bundle(
                    ROOT / "platforms",
                    platforms=platforms,
                    disciplines=disciplines,
                    runtime_configs=runtime_configs,
                    core_only=core_only,
                    schema_root=ROOT / "schemas",
                )
                arguments = [
                    "install-bundle",
                    str(ROOT / "platforms"),
                    "--schemas",
                    str(ROOT / "schemas"),
                ]
                for platform in platforms:
                    arguments.extend(("--platform", platform))
                for discipline in disciplines:
                    arguments.extend(("--discipline", discipline))
                for runtime_config in runtime_configs:
                    arguments.extend(("--runtime-config", runtime_config))
                if core_only:
                    arguments.append("--core-only")
                result = self.run_rust(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout,
                    dumps({
                        "instructions": expected.instructions,
                        "package_lock": expected.package_lock,
                        "plan": expected.plan,
                    }),
                )

    def test_source_install_bundle_previous_lock_matches_python(self) -> None:
        initial = build_install_bundle(
            ROOT / "platforms",
            platforms=["apple"],
            schema_root=ROOT / "schemas",
        )
        expected = build_install_bundle(
            ROOT / "platforms",
            platforms=["apple"],
            previous_lock=initial.package_lock,
            schema_root=ROOT / "schemas",
        )
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / "agent-skills.lock"
            dump(initial.package_lock, previous)
            result = self.run_rust(
                "install-bundle",
                str(ROOT / "platforms"),
                "--platform",
                "apple",
                "--schemas",
                str(ROOT / "schemas"),
                "--previous",
                str(previous),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            dumps({
                "instructions": expected.instructions,
                "package_lock": expected.package_lock,
                "plan": expected.plan,
            }),
        )

    def test_native_source_install_dry_run_matches_python_without_writes(self) -> None:
        expected = build_install_bundle(
            ROOT / "platforms",
            platforms=["apple"],
            schema_root=ROOT / "schemas",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing-target"
            result = self.run_rust(
                "lifecycle-install",
                str(ROOT / "platforms"),
                str(target),
                "--platform",
                "apple",
                "--schemas",
                str(ROOT / "schemas"),
                "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected.plan))
            self.assertFalse(target.exists())

    @unittest.skipIf(os.name == "nt", "production POSIX bootstrap preview is not a Windows route")
    def test_native_production_install_preview_matches_python_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing-target"
            python = subprocess.run(
                [
                    str(ROOT / "install.sh"),
                    "--target-root",
                    str(target),
                    "--platform",
                    "apple",
                    "--dry-run",
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            native = self.run_rust(
                "install",
                "--source-root",
                str(ROOT),
                "--target-root",
                str(target),
                "--platform",
                "apple",
                "--session-launcher",
                "/bin/echo",
                "--dry-run",
                "--json",
            )
            self.assertEqual(python.returncode, 0, python.stderr)
            self.assertEqual(native.returncode, 0, native.stderr)
            expected = json.loads(python.stdout)
            actual = json.loads(native.stdout)
            expected_activation = expected.pop("activation")
            actual_activation = actual.pop("activation")
            expected["engine"] = "rust"
            self.assertEqual(actual, expected)
            self.assertEqual(
                set(actual_activation["managed_file_updates"]),
                set(expected_activation["managed_file_updates"]) | {"bin/agent-skills"},
            )
            for field in (
                "config_changed",
                "managed_files_unchanged",
                "profile_creates",
                "profile_preserves",
            ):
                self.assertEqual(actual_activation[field], expected_activation[field])
            self.assertFalse(target.exists())

    @unittest.skipIf(os.name == "nt", "Python source installation mode contract is POSIX-only")
    def test_native_fresh_source_install_matches_python_filesystem(self) -> None:
        def snapshot(root: Path) -> list[tuple[str, str, int, str | None]]:
            records = []
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                mode = path.stat().st_mode & 0o777
                if path.is_symlink():
                    records.append((relative, "symlink", mode, os.readlink(path)))
                elif path.is_dir():
                    records.append((relative, "directory", mode, None))
                else:
                    records.append((
                        relative,
                        "file",
                        mode,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    ))
            return records

        for platforms, core_only in (([], True), (["apple"], False)):
            with self.subTest(platforms=platforms, core_only=core_only):
                expected = build_install_bundle(
                    ROOT / "platforms",
                    platforms=platforms,
                    core_only=core_only,
                    schema_root=ROOT / "schemas",
                )
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    python_target = root / "python"
                    native_target = root / "native"
                    expected_result = install_bundle(expected, python_target)
                    arguments = [
                        "lifecycle-install",
                        str(ROOT / "platforms"),
                        str(native_target),
                        "--schemas",
                        str(ROOT / "schemas"),
                    ]
                    for platform in platforms:
                        arguments.extend(("--platform", platform))
                    if core_only:
                        arguments.append("--core-only")
                    result = self.run_rust(*arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, dumps(expected_result))
                    self.assertEqual(snapshot(native_target), snapshot(python_target))

    def test_native_source_install_rejects_occupied_target_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            unmanaged = target / "AGENTS.md"
            unmanaged.write_text("unmanaged\n", encoding="utf-8")
            result = self.run_rust(
                "lifecycle-install",
                str(ROOT / "platforms"),
                str(target),
                "--core-only",
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("refusing to overwrite", result.stderr)
            self.assertEqual(unmanaged.read_text(encoding="utf-8"), "unmanaged\n")
            self.assertEqual([path.name for path in target.iterdir()], ["AGENTS.md"])

    @unittest.skipIf(os.name == "nt", "Python source installation mode contract is POSIX-only")
    def test_native_fresh_install_normalizes_crlf_instruction_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platforms = root / "platforms"
            shutil.copytree(ROOT / "platforms" / "core", platforms / "core")
            fragment = platforms / "core" / "agent-instructions" / "global.md"
            content = fragment.read_text(encoding="utf-8")
            fragment.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
            expected = build_install_bundle(
                platforms,
                core_only=True,
                schema_root=ROOT / "schemas",
            )
            python_target = root / "python"
            native_target = root / "native"
            expected_result = install_bundle(expected, python_target)
            result = self.run_rust(
                "lifecycle-install",
                str(platforms),
                str(native_target),
                "--core-only",
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected_result))
            self.assertEqual(
                (native_target / "AGENTS.md").read_bytes(),
                (python_target / "AGENTS.md").read_bytes(),
            )
            self.assertEqual(
                (native_target / ".agent-skills" / "install-lock.json").read_bytes(),
                (python_target / ".agent-skills" / "install-lock.json").read_bytes(),
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support is required")
    def test_source_package_snapshot_rejects_symlink_assets_like_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platforms = root / "platforms"
            shutil.copytree(ROOT / "platforms" / "core", platforms / "core")
            fragment = platforms / "core" / "agent-instructions" / "global.md"
            external = root / "global.md"
            external.write_text(fragment.read_text(encoding="utf-8"), encoding="utf-8")
            fragment.unlink()
            try:
                fragment.symlink_to(external)
            except OSError as error:
                self.skipTest(f"file symlink is unavailable: {error}")

            with self.assertRaises(ContractError):
                _load_package(platforms / "core", "core")
            native = self.run_rust(
                "install-source-snapshot",
                str(platforms),
                "--core-only",
            )
            self.assertEqual(native.returncode, 2)
            self.assertEqual(native.stdout, "")
            self.assertIn("missing or unsafe", native.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX source modes are required")
    def test_source_package_snapshot_normalizes_private_mode_like_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platforms = Path(directory) / "platforms"
            shutil.copytree(ROOT / "platforms" / "core", platforms / "core")
            manifest_path = platforms / "core" / "manifest.json"
            manifest_path.chmod(0o600)
            package = _load_package((platforms / "core").resolve(), "core")
            expected = {
                "packages": [{
                    "directories": list(package.directories),
                    "files": list(package.files),
                    "fragments": list(package.fragments),
                    "id": package.package_id,
                    "manifest": package.manifest,
                    "manifest_sha256": package.manifest_digest,
                    "provider": package.provider,
                    "provider_manifest_sha256": package.provider_digest,
                    "skills": [],
                }]
            }
            native = self.run_rust(
                "install-source-snapshot",
                str(platforms),
                "--core-only",
            )
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(native.stdout, dumps(expected))

    def test_source_package_snapshot_normalizes_crlf_fragments_like_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platforms = Path(directory) / "platforms"
            shutil.copytree(ROOT / "platforms" / "core", platforms / "core")
            fragment = platforms / "core" / "agent-instructions" / "global.md"
            content = fragment.read_text(encoding="utf-8")
            fragment.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
            package = _load_package((platforms / "core").resolve(), "core")
            expected = {
                "packages": [{
                    "directories": list(package.directories),
                    "files": list(package.files),
                    "fragments": list(package.fragments),
                    "id": package.package_id,
                    "manifest": package.manifest,
                    "manifest_sha256": package.manifest_digest,
                    "provider": package.provider,
                    "provider_manifest_sha256": package.provider_digest,
                    "skills": [],
                }]
            }
            native = self.run_rust(
                "install-source-snapshot",
                str(platforms),
                "--core-only",
            )
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(native.stdout, dumps(expected))

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

    @unittest.skipIf(
        os.name == "nt",
        "Python installation mode contract is POSIX-only",
    )
    def test_doctor_report_matches_python_exactly(self) -> None:
        python_version = (
            f"{sys.version_info.major}."
            f"{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            expected = diagnose_install(
                target,
                schema_root=ROOT / "schemas",
            )
            result = self.run_rust(
                "doctor-report",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
                "--python-version",
                python_version,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, dumps(expected))
            native = self.run_rust(
                "doctor",
                "--target-root",
                str(target),
            )
            self.assertEqual(native.returncode, 0, native.stderr)
            native_report = json.loads(native.stdout)
            validate_doctor_report(native_report)
            self.assertEqual(native_report["schema_version"], "2.0")
            self.assertEqual(native_report["status"], "passed")
            self.assertNotIn("python_version", native_report["environment"])
            self.assertEqual(
                native_report["environment"]["implementation"]["name"],
                "agent-skills-rs",
            )
            invalid_native = deepcopy(native_report)
            invalid_native["environment"]["schema_inventory"]["file_count"] = 0
            invalid_native["fingerprint"] = sha256({
                key: value
                for key, value in invalid_native.items()
                if key != "fingerprint"
            })
            with self.assertRaises(ContractError):
                validate_doctor_report(invalid_native)
            inconsistent_native = deepcopy(native_report)
            inconsistent_native["environment"]["schema_inventory"][
                "content_sha256"
            ] = "0" * 64
            inconsistent_native["fingerprint"] = sha256({
                key: value
                for key, value in inconsistent_native.items()
                if key != "fingerprint"
            })
            with self.assertRaises(ContractError):
                validate_doctor_report(inconsistent_native)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "empty"
            target.mkdir()
            expected = diagnose_install(
                target,
                schema_root=ROOT / "schemas",
            )
            result = self.run_rust(
                "doctor-report",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
                "--python-version",
                python_version,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual = json.loads(result.stdout)
            validate_doctor_report(actual)
            for report in (expected, actual):
                report.pop("fingerprint")
                for check in report["checks"]:
                    if check["status"] == "failed":
                        check["details"] = {"errors": ["failed"]}
            self.assertEqual(actual, expected)

            unsupported = self.run_rust(
                "doctor-report",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
                "--python-version",
                "3.10.9",
            )
            self.assertEqual(unsupported.returncode, 2, unsupported.stderr)
            unsupported_report = json.loads(unsupported.stdout)
            self.assertEqual(
                unsupported_report["checks"][0],
                {
                    "category": "environment",
                    "details": {
                        "actual": "3.10.9",
                        "required": ">=3.11",
                    },
                    "id": "environment.python",
                    "status": "failed",
                    "summary": (
                        "Python runtime does not satisfy "
                        "the supported baseline"
                    ),
                },
            )
            self.assertEqual(unsupported_report["status"], "blocked")

            huge = self.run_rust(
                "doctor-report",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
                "--python-version",
                f"{'9' * 100}.0.0",
            )
            self.assertEqual(huge.returncode, 2, huge.stderr)
            huge_report = json.loads(huge.stdout)
            validate_doctor_report(huge_report)
            self.assertEqual(
                huge_report["checks"][0]["status"],
                "passed",
            )

            invalid = self.run_rust(
                "doctor-report",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
                "--python-version",
                "03.11.0",
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(invalid.stdout, "")

    @unittest.skipIf(
        os.name == "nt",
        "Python installation mode contract is POSIX-only",
    )
    def test_upgrade_control_plane_contracts_match_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def native_upgrade_evidence(lock: dict) -> dict:
                return make_upgrade_conformance_evidence(
                    lock,
                    manifest_count=19,
                    negative_contract_count=16,
                    test_count=531,
                    suite_definition_hash=sha256(
                        ["native-upgrade-contract"]
                    ),
                    runner_sha256="1" * 64,
                    environment={
                        "platform": "compatibility-test",
                        "python": "3.11.0",
                    },
                    command_results=[
                        {
                            "command": "compatibility-suite",
                            "exit_code": 0,
                            "stderr_sha256": "2" * 64,
                            "stdout_sha256": "3" * 64,
                        }
                    ],
                )

            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["desktop"],
                ),
                target,
            )
            candidate = prepare_upgrade_candidate(ROOT / "platforms", target)
            evidence = make_upgrade_conformance_evidence(
                candidate.bundle.package_lock,
                manifest_count=19,
                negative_contract_count=16,
                test_count=531,
                suite_definition_hash=sha256(["native-upgrade-contract"]),
                runner_sha256="1" * 64,
                environment={"platform": "compatibility-test", "python": "3.11.0"},
                command_results=[
                    {
                        "command": "compatibility-suite",
                        "exit_code": 0,
                        "stderr_sha256": "2" * 64,
                        "stdout_sha256": "3" * 64,
                    }
                ],
            )
            operation = plan_upgrade(
                ROOT / "platforms",
                target,
                evidence,
                schema_root=ROOT / "schemas",
            )
            evidence_path = root / "upgrade-evidence.json"
            plan_path = root / "upgrade-plan.json"
            candidate_plan_path = root / "candidate-install-plan.json"
            candidate_lock_path = root / "candidate-package-lock.json"
            dump(evidence, evidence_path)
            dump(operation.plan, plan_path)
            dump(operation.candidate.bundle.plan, candidate_plan_path)
            dump(operation.candidate.bundle.package_lock, candidate_lock_path)

            rust_compiled_plan = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                operation.plan["target_root"],
            )
            self.assertEqual(
                rust_compiled_plan.returncode,
                0,
                rust_compiled_plan.stderr,
            )
            self.assertEqual(rust_compiled_plan.stdout, dumps(operation.plan))

            installed_candidate_plan = deepcopy(
                operation.candidate.bundle.plan
            )
            installed_candidate_plan["status"] = "installed"
            installed_candidate_plan["fingerprint"] = sha256({
                key: value
                for key, value in installed_candidate_plan.items()
                if key not in {"fingerprint", "status"}
            })
            dump(installed_candidate_plan, candidate_plan_path)
            rejected_installed_candidate = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                operation.plan["target_root"],
            )
            self.assertEqual(rejected_installed_candidate.returncode, 2)
            self.assertEqual(rejected_installed_candidate.stdout, "")
            dump(operation.candidate.bundle.plan, candidate_plan_path)

            rejected_raw_external_scope = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                operation.plan["target_root"],
                "--external-handler",
                "evil-handler",
                "--external-path",
                "user-state.txt",
            )
            self.assertEqual(rejected_raw_external_scope.returncode, 2)
            self.assertEqual(rejected_raw_external_scope.stdout, "")

            rejected_raw_current_state = self.run_rust(
                "upgrade-plan-build",
                "--current-install-plan",
                str(target / ".agent-skills" / "install-lock.json"),
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                operation.plan["target_root"],
            )
            self.assertEqual(rejected_raw_current_state.returncode, 2)
            self.assertEqual(rejected_raw_current_state.stdout, "")

            changed_repository = root / "changed-repository"
            for source in (
                "disciplines",
                "platforms",
                "runtime-configs",
                "schemas",
            ):
                shutil.copytree(ROOT / source, changed_repository / source)
            changed_skill = (
                changed_repository
                / "disciplines"
                / "review"
                / "skills"
                / "code-review"
                / "SKILL.md"
            )
            changed_skill.write_text(
                changed_skill.read_text(encoding="utf-8")
                + "\nNative upgrade compiler fixture.\n",
                encoding="utf-8",
            )
            changed_candidate = prepare_upgrade_candidate(
                changed_repository / "platforms",
                target,
            )
            changed_evidence = make_upgrade_conformance_evidence(
                changed_candidate.bundle.package_lock,
                manifest_count=19,
                negative_contract_count=16,
                test_count=531,
                suite_definition_hash=sha256(
                    ["native-upgrade-contract"]
                ),
                runner_sha256="1" * 64,
                environment={
                    "platform": "compatibility-test",
                    "python": "3.11.0",
                },
                command_results=[
                    {
                        "command": "compatibility-suite",
                        "exit_code": 0,
                        "stderr_sha256": "2" * 64,
                        "stdout_sha256": "3" * 64,
                    }
                ],
            )
            changed_operation = plan_upgrade(
                changed_repository / "platforms",
                target,
                changed_evidence,
                schema_root=changed_repository / "schemas",
            )
            dump(changed_candidate.bundle.plan, candidate_plan_path)
            dump(
                changed_candidate.bundle.package_lock,
                candidate_lock_path,
            )
            dump(changed_evidence, evidence_path)
            changed_rust_plan = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                changed_operation.plan["target_root"],
            )
            self.assertEqual(
                changed_rust_plan.returncode,
                0,
                changed_rust_plan.stderr,
            )
            self.assertEqual(
                changed_rust_plan.stdout,
                dumps(changed_operation.plan),
            )
            self.assertEqual(changed_operation.plan["status"], "planned")
            self.assertTrue(changed_operation.plan["upgrade_steps"])

            activated_target = root / "activated-codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple"],
                    runtime_configs=["codex"],
                ),
                activated_target,
            )
            activation_lock_path = (
                activated_target
                / ".agent-skills"
                / "activation-lock.json"
            )
            dump(
                {
                    "files": [],
                    "handler": (
                        "core.source-activation.apple-codex-v1"
                    ),
                    "manager": "agent-development-skills",
                    "schema_version": "2.0",
                },
                activation_lock_path,
            )
            activation_lock_path.chmod(0o644)
            apply_source_activation(
                activated_target,
                selected_platforms=["apple"],
            )
            activated_candidate = prepare_upgrade_candidate(
                changed_repository / "platforms",
                activated_target,
            )
            activated_evidence = make_upgrade_conformance_evidence(
                activated_candidate.bundle.package_lock,
                manifest_count=19,
                negative_contract_count=16,
                test_count=531,
                suite_definition_hash=sha256(
                    ["native-upgrade-contract"]
                ),
                runner_sha256="1" * 64,
                environment={
                    "platform": "compatibility-test",
                    "python": "3.11.0",
                },
                command_results=[
                    {
                        "command": "compatibility-suite",
                        "exit_code": 0,
                        "stderr_sha256": "2" * 64,
                        "stdout_sha256": "3" * 64,
                    }
                ],
            )
            dump(activated_candidate.bundle.plan, candidate_plan_path)
            dump(
                activated_candidate.bundle.package_lock,
                candidate_lock_path,
            )
            dump(activated_evidence, evidence_path)
            rejected_missing_launcher = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                str(activated_target.resolve()),
            )
            self.assertEqual(rejected_missing_launcher.returncode, 2)
            self.assertEqual(rejected_missing_launcher.stdout, "")

            activated_operation = plan_upgrade(
                changed_repository / "platforms",
                activated_target,
                activated_evidence,
                schema_root=changed_repository / "schemas",
                external_paths=activation_external_paths(activated_target),
                external_handler=ACTIVATION_HANDLER_ID,
                external_handler_sha256=activation_handler_sha256(),
            )
            activated_rust_result = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                str(activated_target.resolve()),
                "--session-launcher",
                str(self.rust_cli),
            )
            self.assertEqual(
                activated_rust_result.returncode,
                0,
                activated_rust_result.stderr,
            )
            activated_rust_plan = json.loads(activated_rust_result.stdout)
            validate_upgrade_plan(activated_rust_plan)
            activated_expected = deepcopy(activated_operation.plan)
            self.assertEqual(
                activated_rust_plan["external"]["path_count"],
                activated_expected["external"]["path_count"] + 1,
            )
            for plan in (activated_rust_plan, activated_expected):
                plan.pop("fingerprint")
                plan["rollback"].pop("point_fingerprint")
                for field in (
                    "handler_sha256",
                    "path_count",
                    "paths_sha256",
                ):
                    plan["external"].pop(field)
            self.assertEqual(activated_rust_plan, activated_expected)

            legacy_activation = json.loads(
                activation_lock_path.read_text(encoding="utf-8")
            )
            legacy_activation.pop("handler")
            legacy_activation["schema_version"] = "1.0"
            dump(legacy_activation, activation_lock_path)
            activation_lock_path.chmod(0o644)
            migration_operation = plan_upgrade(
                changed_repository / "platforms",
                activated_target,
                activated_evidence,
                schema_root=changed_repository / "schemas",
                external_paths=activation_external_paths(activated_target),
                external_handler=ACTIVATION_HANDLER_ID,
                external_handler_sha256=activation_handler_sha256(),
            )
            migration_rust_result = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                str(activated_target.resolve()),
                "--session-launcher",
                str(self.rust_cli),
            )
            self.assertEqual(
                migration_rust_result.returncode,
                0,
                migration_rust_result.stderr,
            )
            migration_rust_plan = json.loads(migration_rust_result.stdout)
            validate_upgrade_plan(migration_rust_plan)
            self.assertEqual(
                migration_rust_plan["migrations"],
                migration_operation.plan["migrations"],
            )
            self.assertEqual(
                migration_rust_plan["changes"]["status"],
                "changed",
            )

            scoped_target = root / "scoped-partial-codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                    runtime_configs=["codex"],
                ),
                scoped_target,
            )
            scoped_activation_lock = (
                scoped_target
                / ".agent-skills"
                / "activation-lock.json"
            )
            dump(
                {
                    "files": [],
                    "handler": ACTIVATION_HANDLER_ID,
                    "manager": "agent-development-skills",
                    "schema_version": "2.0",
                },
                scoped_activation_lock,
            )
            scoped_activation_lock.chmod(0o644)
            apply_source_activation(
                scoped_target,
                selected_platforms=["apple"],
            )
            scoped_paths = deactivation_external_paths(scoped_target)

            preserve_candidate = prepare_upgrade_candidate(
                ROOT / "platforms",
                scoped_target,
                platforms=["apple"],
                disciplines=[],
                runtime_configs=["codex"],
                core_only=False,
            )
            preserve_evidence = native_upgrade_evidence(
                preserve_candidate.bundle.package_lock
            )
            preserve_operation = plan_upgrade(
                ROOT / "platforms",
                scoped_target,
                preserve_evidence,
                schema_root=ROOT / "schemas",
                platforms=["apple"],
                disciplines=[],
                runtime_configs=["codex"],
                core_only=False,
                external_paths=scoped_paths,
                external_handler=PRESERVE_HANDLER_ID,
                external_handler_sha256=activation_handler_sha256(),
                action="partial-uninstall",
                removed_platforms=["desktop"],
            )
            dump(preserve_candidate.bundle.plan, candidate_plan_path)
            dump(
                preserve_candidate.bundle.package_lock,
                candidate_lock_path,
            )
            dump(preserve_evidence, evidence_path)
            preserve_rust_result = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                str(scoped_target.resolve()),
                "--action",
                "partial-uninstall",
                "--removed-platform",
                "desktop",
            )
            self.assertEqual(
                preserve_rust_result.returncode,
                0,
                preserve_rust_result.stderr,
            )
            preserve_rust_plan = json.loads(preserve_rust_result.stdout)
            preserve_expected = deepcopy(preserve_operation.plan)
            preserve_expected["external"]["handler_sha256"] = (
                preserve_rust_plan["external"]["handler_sha256"]
            )
            preserve_expected["fingerprint"] = sha256({
                key: value
                for key, value in preserve_expected.items()
                if key != "fingerprint"
            })
            self.assertEqual(preserve_rust_plan, preserve_expected)

            deactivation_candidate = prepare_upgrade_candidate(
                ROOT / "platforms",
                scoped_target,
                platforms=["desktop"],
                disciplines=[],
                runtime_configs=[],
                core_only=False,
            )
            deactivation_evidence = native_upgrade_evidence(
                deactivation_candidate.bundle.package_lock
            )
            deactivation_operation = plan_upgrade(
                ROOT / "platforms",
                scoped_target,
                deactivation_evidence,
                schema_root=ROOT / "schemas",
                platforms=["desktop"],
                disciplines=[],
                runtime_configs=[],
                core_only=False,
                external_paths=scoped_paths,
                external_handler=DEACTIVATION_HANDLER_ID,
                external_handler_sha256=activation_handler_sha256(),
                action="partial-uninstall",
                removed_platforms=["apple"],
                removed_runtime_configs=["codex"],
            )
            dump(deactivation_candidate.bundle.plan, candidate_plan_path)
            dump(
                deactivation_candidate.bundle.package_lock,
                candidate_lock_path,
            )
            dump(deactivation_evidence, evidence_path)
            deactivation_rust_result = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                str(scoped_target.resolve()),
                "--action",
                "partial-uninstall",
                "--removed-platform",
                "apple",
                "--removed-runtime-config",
                "codex",
            )
            self.assertEqual(
                deactivation_rust_result.returncode,
                0,
                deactivation_rust_result.stderr,
            )
            deactivation_rust_plan = json.loads(
                deactivation_rust_result.stdout
            )
            deactivation_expected = deepcopy(deactivation_operation.plan)
            deactivation_expected["external"]["handler_sha256"] = (
                deactivation_rust_plan["external"]["handler_sha256"]
            )
            deactivation_expected["fingerprint"] = sha256({
                key: value
                for key, value in deactivation_expected.items()
                if key != "fingerprint"
            })
            self.assertEqual(
                deactivation_rust_plan,
                deactivation_expected,
            )

            partial_target = root / "partial-codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["desktop"],
                ),
                partial_target,
            )
            partial_candidate = prepare_upgrade_candidate(
                ROOT / "platforms",
                partial_target,
                platforms=[],
                disciplines=[],
                runtime_configs=[],
                core_only=True,
            )
            partial_evidence = make_upgrade_conformance_evidence(
                partial_candidate.bundle.package_lock,
                manifest_count=19,
                negative_contract_count=16,
                test_count=531,
                suite_definition_hash=sha256(
                    ["native-upgrade-contract"]
                ),
                runner_sha256="1" * 64,
                environment={
                    "platform": "compatibility-test",
                    "python": "3.11.0",
                },
                command_results=[
                    {
                        "command": "compatibility-suite",
                        "exit_code": 0,
                        "stderr_sha256": "2" * 64,
                        "stdout_sha256": "3" * 64,
                    }
                ],
            )
            partial_operation = plan_upgrade(
                ROOT / "platforms",
                partial_target,
                partial_evidence,
                schema_root=ROOT / "schemas",
                platforms=[],
                disciplines=[],
                runtime_configs=[],
                core_only=True,
                action="partial-uninstall",
                removed_platforms=["desktop"],
            )
            dump(partial_candidate.bundle.plan, candidate_plan_path)
            dump(
                partial_candidate.bundle.package_lock,
                candidate_lock_path,
            )
            dump(partial_evidence, evidence_path)
            partial_rust_plan = self.run_rust(
                "upgrade-plan-build",
                "--candidate-install-plan",
                str(candidate_plan_path),
                "--candidate-package-lock",
                str(candidate_lock_path),
                "--evidence",
                str(evidence_path),
                "--target-root",
                partial_operation.plan["target_root"],
                "--action",
                "partial-uninstall",
                "--removed-platform",
                "desktop",
            )
            self.assertEqual(
                partial_rust_plan.returncode,
                0,
                partial_rust_plan.stderr,
            )
            self.assertEqual(
                partial_rust_plan.stdout,
                dumps(partial_operation.plan),
            )

            dump(evidence, evidence_path)
            dump(operation.plan, plan_path)
            rust_evidence = self.run_rust(
                "upgrade-evidence-validate",
                str(evidence_path),
            )
            self.assertEqual(rust_evidence.returncode, 0, rust_evidence.stderr)
            self.assertEqual(rust_evidence.stdout, dumps(evidence))
            rust_plan = self.run_rust(
                "upgrade-plan-validate",
                str(plan_path),
            )
            self.assertEqual(rust_plan.returncode, 0, rust_plan.stderr)
            self.assertEqual(rust_plan.stdout, dumps(operation.plan))

            invalid_evidence = deepcopy(evidence)
            invalid_evidence["command_results"].append(
                deepcopy(invalid_evidence["command_results"][0])
            )
            stable_evidence = {
                key: value
                for key, value in invalid_evidence.items()
                if key not in {"attestation_key", "fingerprint"}
            }
            stable_evidence["command_results"] = [
                {
                    "command": item["command"],
                    "exit_code": item["exit_code"],
                }
                for item in invalid_evidence["command_results"]
            ]
            invalid_evidence["attestation_key"] = sha256(stable_evidence)
            invalid_evidence["fingerprint"] = sha256({
                key: value
                for key, value in invalid_evidence.items()
                if key != "fingerprint"
            })
            with self.assertRaises(ContractError):
                validate_upgrade_conformance_evidence(invalid_evidence)
            dump(invalid_evidence, evidence_path)
            rejected_evidence = self.run_rust(
                "upgrade-evidence-validate",
                str(evidence_path),
            )
            self.assertEqual(rejected_evidence.returncode, 2)
            self.assertEqual(rejected_evidence.stdout, "")

            for invalid_exit_code in (False, 0.0, -0.0):
                with self.subTest(invalid_exit_code=repr(invalid_exit_code)):
                    invalid_evidence = deepcopy(evidence)
                    invalid_evidence["command_results"][0][
                        "exit_code"
                    ] = invalid_exit_code
                    stable_evidence = {
                        key: value
                        for key, value in invalid_evidence.items()
                        if key not in {"attestation_key", "fingerprint"}
                    }
                    stable_evidence["command_results"] = [
                        {
                            "command": item["command"],
                            "exit_code": item["exit_code"],
                        }
                        for item in invalid_evidence["command_results"]
                    ]
                    invalid_evidence["attestation_key"] = sha256(
                        stable_evidence
                    )
                    invalid_evidence["fingerprint"] = sha256({
                        key: value
                        for key, value in invalid_evidence.items()
                        if key != "fingerprint"
                    })
                    with self.assertRaises(ContractError):
                        validate_upgrade_conformance_evidence(
                            invalid_evidence
                        )
                    dump(invalid_evidence, evidence_path)
                    rejected_evidence = self.run_rust(
                        "upgrade-evidence-validate",
                        str(evidence_path),
                    )
                    self.assertEqual(rejected_evidence.returncode, 2)
                    self.assertEqual(rejected_evidence.stdout, "")

            invalid_plan = deepcopy(operation.plan)
            invalid_plan["current_selection"]["core_only"] = True
            invalid_plan["fingerprint"] = sha256({
                key: value
                for key, value in invalid_plan.items()
                if key != "fingerprint"
            })
            with self.assertRaises(ContractError):
                validate_upgrade_plan(invalid_plan)
            dump(invalid_plan, plan_path)
            rejected_plan = self.run_rust(
                "upgrade-plan-validate",
                str(plan_path),
            )
            self.assertEqual(rejected_plan.returncode, 2)
            self.assertEqual(rejected_plan.stdout, "")

            invalid_plan = deepcopy(operation.plan)
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
            invalid_plan["migrations"] = [migration]
            invalid_plan["fingerprint"] = sha256({
                key: value
                for key, value in invalid_plan.items()
                if key != "fingerprint"
            })
            with self.assertRaises(ContractError):
                validate_upgrade_plan(invalid_plan)
            dump(invalid_plan, plan_path)
            rejected_plan = self.run_rust(
                "upgrade-plan-validate",
                str(plan_path),
            )
            self.assertEqual(rejected_plan.returncode, 2)
            self.assertEqual(rejected_plan.stdout, "")

    @unittest.skipIf(
        os.name == "nt",
        "Python installation mode contract is POSIX-only",
    )
    def test_doctor_baseline_matches_python_and_remains_read_only(self) -> None:
        check_ids = {
            "filesystem.target",
            "recovery.residue",
            "filesystem.layout",
            "install.lock",
            "lock.persistent",
            "recovery.rollback-point",
            "environment.core",
            "schema.inventory",
            "package.integrity",
            "skill.integrity",
            "instructions.global",
            "binding.freeze",
            "permission.freeze",
            "activation.integrity",
        }

        def python_projection(target: Path) -> dict:
            report = diagnose_install(target, schema_root=ROOT / "schemas")
            return {
                "checks": [
                    check
                    for check in report["checks"]
                    if check["id"] in check_ids
                ],
                "install": report["install"],
                "recovery": report["recovery"],
                "target_root": report["target_root"],
            }

        def normalize_failures(value: dict) -> dict:
            normalized = deepcopy(value)
            normalized.pop("fingerprint", None)
            for check in normalized["checks"]:
                if check["status"] == "failed":
                    check["details"] = {"errors": ["failed"]}
            return normalized

        def filesystem_identity(root: Path) -> list[tuple]:
            if root.is_symlink():
                return [("symlink", ".", os.readlink(root))]
            if not root.exists():
                return []
            entries: list[tuple] = []
            for path in [root, *sorted(root.rglob("*"))]:
                relative = (
                    "."
                    if path == root
                    else path.relative_to(root).as_posix()
                )
                mode = path.lstat().st_mode & 0o777
                if path.is_symlink():
                    entries.append(
                        ("symlink", relative, mode, os.readlink(path))
                    )
                elif path.is_dir():
                    entries.append(("directory", relative, mode))
                else:
                    entries.append(
                        (
                            "file",
                            relative,
                            mode,
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                        )
                    )
            return entries

        def forge_installed_package_file(
            target: Path,
            package_id: str,
            relative: str,
            value: dict,
        ) -> None:
            managed = target / ".agent-skills"
            install_lock_path = managed / "install-lock.json"
            package_lock_path = managed / "agent-skills.lock"
            install_lock = json.loads(
                install_lock_path.read_text(encoding="utf-8")
            )
            package_lock = json.loads(
                package_lock_path.read_text(encoding="utf-8")
            )
            installed_path = (
                managed / "packages" / package_id / relative
            )
            dump(value, installed_path)
            file_sha256 = hashlib.sha256(
                installed_path.read_bytes()
            ).hexdigest()
            record = next(
                item
                for item in install_lock["packages"]
                if item["id"] == package_id
            )
            file_record = next(
                item
                for item in record["files"]
                if item["path"] == relative
            )
            file_record["sha256"] = file_sha256
            record["files_sha256"] = sha256(record["files"])
            if relative == "manifest.json":
                record["manifest_sha256"] = sha256(value)
            else:
                record["provider_manifest_sha256"] = sha256(value)
            selected = next(
                item
                for item in install_lock["selected_packages"]
                if item["id"] == package_id
            )
            selected["source_sha256"] = record["files_sha256"]
            for asset in install_lock["assets"]:
                if (
                    asset["package"] == package_id
                    and asset["path"] == relative
                ):
                    asset["sha256"] = file_sha256
            for provider in install_lock["capability_providers"].values():
                if provider["package"] == package_id:
                    provider["source_sha256"] = record["files_sha256"]
            install_lock["asset_summary"]["content_sha256"] = sha256(
                install_lock["assets"]
            )
            persistent = next(
                item
                for item in package_lock["packages"]
                if item["id"] == package_id
            )
            persistent["manifest_sha256"] = record["manifest_sha256"]
            persistent["provider_manifest_sha256"] = (
                record["provider_manifest_sha256"]
            )
            persistent["source"]["sha256"] = record["files_sha256"]
            if package_id == "core":
                package_lock["core"]["source_sha256"] = (
                    record["files_sha256"]
                )
            for provider in package_lock["capability_providers"].values():
                if provider["package"] == package_id:
                    provider["source_sha256"] = record["files_sha256"]
            package_lock["assets_sha256"] = sha256(
                install_lock["assets"]
            )
            package_lock["install_plan_identity_hash"] = sha256({
                key: item
                for key, item in install_lock.items()
                if key
                not in {
                    "fingerprint",
                    "package_lock_hash",
                    "status",
                }
            })
            package_lock["fingerprint"] = sha256({
                key: item
                for key, item in package_lock.items()
                if key != "fingerprint"
            })
            install_lock["package_lock_hash"] = (
                package_lock["fingerprint"]
            )
            install_lock["fingerprint"] = sha256({
                key: item
                for key, item in install_lock.items()
                if key not in {"fingerprint", "status"}
            })
            dump(package_lock, package_lock_path)
            dump(install_lock, install_lock_path)

        def refingerprint_locks(
            target: Path,
            install_lock: dict,
            package_lock: dict,
        ) -> None:
            managed = target / ".agent-skills"
            package_lock["install_plan_identity_hash"] = sha256({
                key: value
                for key, value in install_lock.items()
                if key
                not in {
                    "fingerprint",
                    "package_lock_hash",
                    "status",
                }
            })
            package_lock["fingerprint"] = sha256({
                key: value
                for key, value in package_lock.items()
                if key != "fingerprint"
            })
            install_lock["package_lock_hash"] = (
                package_lock["fingerprint"]
            )
            install_lock["fingerprint"] = sha256({
                key: value
                for key, value in install_lock.items()
                if key not in {"fingerprint", "status"}
            })
            dump(package_lock, managed / "agent-skills.lock")
            dump(install_lock, managed / "install-lock.json")

        def assert_doctor_failure(
            target: Path,
            check_id: str,
            message: str,
        ) -> None:
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(
                normalize_failures(actual),
                normalize_failures(expected),
            )
            actual_check = next(
                check
                for check in actual["checks"]
                if check["id"] == check_id
            )
            expected_check = next(
                check
                for check in expected["checks"]
                if check["id"] == check_id
            )
            self.assertEqual(actual_check, expected_check)
            self.assertEqual(actual_check["details"], {
                "errors": [message],
            })
            self.assertEqual(filesystem_identity(target), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )

            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            self.assertEqual(filesystem_identity(target), before)

            residue = target / ".agent-skills-stage-interrupted"
            residue.mkdir()
            residue_before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                normalize_failures(json.loads(result.stdout)),
                normalize_failures(expected),
            )
            self.assertEqual(filesystem_identity(target), residue_before)
            residue.rmdir()

            package_lock = (
                target / ".agent-skills" / "agent-skills.lock"
            )
            package_lock.write_text("{}\n", encoding="utf-8")
            tampered_before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                normalize_failures(json.loads(result.stdout)),
                normalize_failures(expected),
            )
            self.assertEqual(filesystem_identity(target), tampered_before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "platforms", root / "platforms")
            shutil.copytree(ROOT / "disciplines", root / "disciplines")
            shutil.copytree(
                ROOT / "runtime-configs",
                root / "runtime-configs",
            )
            core_manifest_path = root / "platforms/core/manifest.json"
            core_manifest = json.loads(
                core_manifest_path.read_text(encoding="utf-8")
            )
            core_manifest[
                "installation"
            ]["instruction_fragments"][0]["order"] = (
                9_223_372_036_854_775_808
            )
            dump(core_manifest, core_manifest_path)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    root / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            self.assertEqual(filesystem_identity(target), before)

        from agent_workflow.installation import _write_rollback_point

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            point = _write_rollback_point(
                target,
                target / ".agent-skills/rollback-point",
            )
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            rollback_details = next(
                check["details"]
                for check in actual["checks"]
                if check["id"] == "recovery.rollback-point"
            )
            self.assertEqual(rollback_details, {
                "available": True,
                "package_lock_hash": point["package_lock_hash"],
                "point_id": point["point_id"],
            })
            self.assertEqual(filesystem_identity(target), before)

            managed = target / ".agent-skills"
            managed.chmod(0o700)
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            expected_by_id = {
                check["id"]: check
                for check in expected["checks"]
            }
            actual_by_id = {
                check["id"]: check
                for check in actual["checks"]
            }
            self.assertEqual(
                actual_by_id["recovery.rollback-point"],
                expected_by_id["recovery.rollback-point"],
            )
            self.assertEqual(
                actual_by_id["filesystem.layout"]["status"],
                "failed",
            )
            self.assertEqual(
                actual_by_id["recovery.rollback-point"]["status"],
                "passed",
            )
            self.assertEqual(filesystem_identity(target), before)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            activated = target / "bin/managed-tool"
            activated.parent.mkdir()
            activated.write_bytes(b"managed\n")
            activated.chmod(0o755)
            activation_lock = {
                "files": [{
                    "mode": 0o755,
                    "path": "bin/managed-tool",
                    "sha256": hashlib.sha256(
                        activated.read_bytes()
                    ).hexdigest(),
                }],
                "manager": "agent-development-skills",
                "schema_version": "1.0",
            }
            dump(
                activation_lock,
                target / ".agent-skills/activation-lock.json",
            )
            _write_rollback_point(
                target,
                target / ".agent-skills/rollback-point",
                external_paths=[
                    "bin/managed-tool",
                    "config/missing.json",
                ],
            )
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            self.assertEqual(filesystem_identity(target), before)

            snapshot = (
                target
                / ".agent-skills/rollback-point"
                / "external-files/bin/managed-tool"
            )
            snapshot.write_bytes(b"tampered\n")
            assert_doctor_failure(
                target,
                "recovery.rollback-point",
                "external snapshot file differs from state: "
                "bin/managed-tool",
            )

        for mutation in (
            "unknown-entry",
            "agents-content",
            "package-content",
            "external-state",
            "snapshot-contract",
            "external-state-symlink",
        ):
            with self.subTest(
                rollback_mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "codex"
                install_bundle(
                    build_install_bundle(
                        ROOT / "platforms",
                        platforms=["apple", "desktop"],
                    ),
                    target,
                )
                rollback_root = target / ".agent-skills/rollback-point"
                _write_rollback_point(target, rollback_root)
                if mutation == "unknown-entry":
                    (rollback_root / "unknown").write_text(
                        "unexpected\n",
                        encoding="utf-8",
                    )
                    message = (
                        "rollback point contains missing or unknown entries"
                    )
                elif mutation == "agents-content":
                    agents = rollback_root / "AGENTS.md"
                    agents.write_bytes(
                        agents.read_bytes() + b"\n# tampered\n"
                    )
                    message = (
                        "rollback point AGENTS.md differs from Install Lock"
                    )
                elif mutation == "package-content":
                    manifest = (
                        rollback_root / "packages/core/manifest.json"
                    )
                    manifest.write_bytes(
                        manifest.read_bytes() + b"\n"
                    )
                    message = (
                        "rollback point package differs from Install Lock: "
                        "core"
                    )
                elif mutation == "external-state":
                    dump({}, rollback_root / "external-state.json")
                    message = (
                        "rollback point external state shape is invalid"
                    )
                elif mutation == "snapshot-contract":
                    point_path = rollback_root / "rollback-point.json"
                    point = json.loads(
                        point_path.read_text(encoding="utf-8")
                    )
                    point["snapshot_sha256"] = "0" * 64
                    point["fingerprint"] = sha256({
                        key: value
                        for key, value in point.items()
                        if key != "fingerprint"
                    })
                    dump(point, point_path)
                    message = (
                        "rollback point snapshot digest is invalid"
                    )
                else:
                    state = rollback_root / "external-state.json"
                    real_state = target / "real-external-state.json"
                    state.rename(real_state)
                    state.symlink_to(real_state)
                    message = (
                        "rollback point external state is missing or unsafe"
                    )
                assert_doctor_failure(
                    target,
                    "recovery.rollback-point",
                    message,
                )

        for mutation in (
            "skill-content",
            "agents-content",
            "binding-cross-lock",
            "permission-cross-lock",
            "binding-semantic-forgery",
            "permission-semantic-forgery",
        ):
            with self.subTest(
                post_install_mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "codex"
                install_bundle(
                    build_install_bundle(
                        ROOT / "platforms",
                        platforms=["apple", "desktop"],
                    ),
                    target,
                )
                managed = target / ".agent-skills"
                install_lock = json.loads(
                    (managed / "install-lock.json").read_text(
                        encoding="utf-8"
                    )
                )
                package_lock = json.loads(
                    (managed / "agent-skills.lock").read_text(
                        encoding="utf-8"
                    )
                )
                if mutation == "skill-content":
                    skill = install_lock["skills"][0]
                    skill_file = target / "skills" / skill["name"]
                    skill_file /= skill["files"][0]["path"]
                    skill_file.write_bytes(
                        skill_file.read_bytes() + b"\n# tampered\n"
                    )
                    check_id = "skill.integrity"
                    message = (
                        "installed Skill content differs: "
                        f"{skill['name']}"
                    )
                elif mutation == "agents-content":
                    agents = target / "AGENTS.md"
                    agents.write_bytes(
                        agents.read_bytes() + b"\n# tampered\n"
                    )
                    check_id = "instructions.global"
                    message = (
                        "global AGENTS.md content differs from Install Lock"
                    )
                elif mutation == "binding-cross-lock":
                    capability = next(iter(
                        package_lock["capability_providers"]
                    ))
                    provider = package_lock[
                        "capability_providers"
                    ][capability]
                    replacement = next(
                        candidate["binding"]
                        for candidate in package_lock[
                            "capability_providers"
                        ].values()
                        if candidate["package"] == provider["package"]
                        and candidate["binding"] != provider["binding"]
                    )
                    provider["binding"] = deepcopy(replacement)
                    package_lock["bindings_sha256"] = sha256({
                        name: {
                            "binding": candidate["binding"],
                            "package": candidate["package"],
                        }
                        for name, candidate in sorted(
                            package_lock[
                                "capability_providers"
                            ].items()
                        )
                    })
                    refingerprint_locks(
                        target,
                        install_lock,
                        package_lock,
                    )
                    check_id = "binding.freeze"
                    message = (
                        "Capability Binding digest differs from Install Lock"
                    )
                elif mutation == "permission-cross-lock":
                    package_lock["permission_profiles"].append(
                        "zz-fixture-unused"
                    )
                    refingerprint_locks(
                        target,
                        install_lock,
                        package_lock,
                    )
                    check_id = "permission.freeze"
                    message = (
                        "permission profile set differs between Lockfiles"
                    )
                elif mutation == "binding-semantic-forgery":
                    capability = next(iter(
                        install_lock["capability_providers"]
                    ))
                    provider = install_lock[
                        "capability_providers"
                    ][capability]
                    replacement = next(
                        candidate["binding"]
                        for candidate in install_lock[
                            "capability_providers"
                        ].values()
                        if candidate["package"] == provider["package"]
                        and candidate["binding"] != provider["binding"]
                    )
                    provider["binding"] = deepcopy(replacement)
                    install_lock["bindings"][capability]["binding"] = (
                        deepcopy(replacement)
                    )
                    package_lock[
                        "capability_providers"
                    ][capability]["binding"] = deepcopy(replacement)
                    package_lock["bindings_sha256"] = sha256(
                        install_lock["bindings"]
                    )
                    refingerprint_locks(
                        target,
                        install_lock,
                        package_lock,
                    )
                    check_id = "binding.freeze"
                    message = (
                        "Capability Binding semantics differ from installed "
                        "Manifests"
                    )
                else:
                    install_lock["permission_profiles"].append(
                        "zz-fixture-unused"
                    )
                    package_lock["permission_profiles"].append(
                        "zz-fixture-unused"
                    )
                    refingerprint_locks(
                        target,
                        install_lock,
                        package_lock,
                    )
                    check_id = "permission.freeze"
                    message = (
                        "Capability permission semantics differ from installed "
                        "Manifests"
                    )
                assert_doctor_failure(target, check_id, message)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            managed = target / ".agent-skills"
            install_lock_path = managed / "install-lock.json"
            package_lock_path = managed / "agent-skills.lock"
            install_lock = json.loads(
                install_lock_path.read_text(encoding="utf-8")
            )
            package_lock = json.loads(
                package_lock_path.read_text(encoding="utf-8")
            )
            major, minor, patch = (
                int(part)
                for part in PYTHON_CORE_VERSION.split(".")
            )
            mismatched_core_version = f"{major}.{minor}.{patch + 1}"
            install_lock["core_version"] = mismatched_core_version
            for package in install_lock["selected_packages"]:
                package["core_compatibility"] = (
                    f"=={mismatched_core_version}"
                )
            package_lock["install_plan_identity_hash"] = sha256({
                key: value
                for key, value in install_lock.items()
                if key
                not in {
                    "fingerprint",
                    "package_lock_hash",
                    "status",
                }
            })
            package_lock["fingerprint"] = sha256({
                key: value
                for key, value in package_lock.items()
                if key != "fingerprint"
            })
            install_lock["package_lock_hash"] = package_lock["fingerprint"]
            install_lock["fingerprint"] = sha256({
                key: value
                for key, value in install_lock.items()
                if key not in {"fingerprint", "status"}
            })
            dump(package_lock, package_lock_path)
            dump(install_lock, install_lock_path)

            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "")
            actual = json.loads(result.stdout)
            self.assertEqual(
                normalize_failures(actual),
                normalize_failures(expected),
            )
            self.assertEqual(
                next(
                    check
                    for check in actual["checks"]
                    if check["id"] == "install.lock"
                )["status"],
                "passed",
            )
            self.assertEqual(
                next(
                    check
                    for check in actual["checks"]
                    if check["id"] == "lock.persistent"
                )["status"],
                "passed",
            )
            self.assertEqual(
                next(
                    check
                    for check in actual["checks"]
                    if check["id"] == "environment.core"
                )["status"],
                "failed",
            )
            self.assertEqual(
                [
                    check["id"]
                    for check in actual["checks"]
                    if check["status"] == "failed"
                ],
                [
                    "environment.core",
                    "package.integrity",
                    "skill.integrity",
                    "instructions.global",
                    "binding.freeze",
                    "permission.freeze",
                ],
            )
            self.assertEqual(filesystem_identity(target), before)

        for mutation in (
            "missing-asset-root",
            "missing-skill-and-asset-root",
            "provider-role",
            "provider-invalid-role",
        ):
            with self.subTest(
                package_semantic_mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "codex"
                install_bundle(
                    build_install_bundle(
                        ROOT / "platforms",
                        platforms=["apple", "desktop"],
                    ),
                    target,
                )
                if mutation in {
                    "missing-asset-root",
                    "missing-skill-and-asset-root",
                }:
                    package_id = "core"
                    relative = "manifest.json"
                    installed_path = (
                        target
                        / ".agent-skills"
                        / "packages"
                        / package_id
                        / relative
                    )
                    value = json.loads(
                        installed_path.read_text(encoding="utf-8")
                    )
                    value["installation"]["asset_roots"] = [
                        "missing-assets"
                    ]
                    if mutation == "missing-skill-and-asset-root":
                        value["installation"]["skill_roots"] = [
                            "missing-skills"
                        ]
                        expected_error = (
                            "skill root is missing: missing-skills"
                        )
                    else:
                        expected_error = (
                            "installation asset path is missing: "
                            "missing-assets"
                        )
                else:
                    package_id = "apple"
                    package_manifest_path = (
                        target
                        / ".agent-skills"
                        / "packages"
                        / package_id
                        / "manifest.json"
                    )
                    package_manifest = json.loads(
                        package_manifest_path.read_text(encoding="utf-8")
                    )
                    relative = package_manifest["installation"][
                        "provider_manifest"
                    ]
                    installed_path = (
                        target
                        / ".agent-skills"
                        / "packages"
                        / package_id
                        / relative
                    )
                    value = json.loads(
                        installed_path.read_text(encoding="utf-8")
                    )
                    if mutation == "provider-role":
                        value["role"] = "builtin"
                        expected_error = (
                            "installation provider is not a provider "
                            "manifest: apple"
                        )
                    else:
                        value["role"] = 7
                        expected_error = (
                            "plugin-manifest role is invalid"
                        )
                forge_installed_package_file(
                    target,
                    package_id,
                    relative,
                    value,
                )
                before = filesystem_identity(target)
                expected = python_projection(target)
                result = self.run_rust(
                    "doctor-baseline",
                    str(target),
                    "--schemas",
                    str(ROOT / "schemas"),
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                actual = json.loads(result.stdout)
                self.assertEqual(
                    next(
                        check["status"]
                        for check in actual["checks"]
                        if check["id"] == "install.lock"
                    ),
                    "passed",
                    actual["checks"],
                )
                self.assertEqual(
                    next(
                        check["status"]
                        for check in actual["checks"]
                        if check["id"] == "lock.persistent"
                    ),
                    "passed",
                    actual["checks"],
                )
                self.assertEqual(
                    normalize_failures(actual),
                    normalize_failures(expected),
                )
                actual_package = next(
                    check
                    for check in actual["checks"]
                    if check["id"] == "package.integrity"
                )
                expected_package = next(
                    check
                    for check in expected["checks"]
                    if check["id"] == "package.integrity"
                )
                self.assertEqual(actual_package, expected_package)
                self.assertEqual(
                    actual_package["details"],
                    {"errors": [expected_error]},
                )
                self.assertEqual(filesystem_identity(target), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            manifest_path = (
                target
                / ".agent-skills"
                / "packages"
                / "apple"
                / "manifest.json"
            )
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["installation"]["provider_manifest"] = (
                "./provider//manifest.json/"
            )
            forge_installed_package_file(
                target,
                "apple",
                "manifest.json",
                manifest,
            )
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            self.assertEqual(filesystem_identity(target), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            manifest_path = (
                target
                / ".agent-skills"
                / "packages"
                / "core"
                / "manifest.json"
            )
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["installation"]["asset_roots"] = [
                "missing-assets"
            ]
            forge_installed_package_file(
                target,
                "core",
                "manifest.json",
                manifest,
            )
            managed = target / ".agent-skills"
            install_lock_path = managed / "install-lock.json"
            package_lock_path = managed / "agent-skills.lock"
            install_lock = json.loads(
                install_lock_path.read_text(encoding="utf-8")
            )
            package_lock = json.loads(
                package_lock_path.read_text(encoding="utf-8")
            )
            persistent = next(
                item
                for item in package_lock["packages"]
                if item["id"] == "apple"
            )
            persistent["core_compatibility"] = ">=0.0.0"
            package_lock["fingerprint"] = sha256({
                key: value
                for key, value in package_lock.items()
                if key != "fingerprint"
            })
            install_lock["package_lock_hash"] = (
                package_lock["fingerprint"]
            )
            install_lock["fingerprint"] = sha256({
                key: value
                for key, value in install_lock.items()
                if key not in {"fingerprint", "status"}
            })
            dump(package_lock, package_lock_path)
            dump(install_lock, install_lock_path)
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual_package = next(
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "package.integrity"
            )
            expected_package = next(
                check
                for check in expected["checks"]
                if check["id"] == "package.integrity"
            )
            self.assertEqual(actual_package, expected_package)
            self.assertEqual(
                actual_package["details"],
                {
                    "errors": [
                        "installed package identity differs from "
                        "persistent Lockfile: apple"
                    ]
                },
            )
            self.assertEqual(filesystem_identity(target), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            managed = target / ".agent-skills"
            install_lock_path = managed / "install-lock.json"
            package_lock_path = managed / "agent-skills.lock"
            install_lock = json.loads(
                install_lock_path.read_text(encoding="utf-8")
            )
            package_lock = json.loads(
                package_lock_path.read_text(encoding="utf-8")
            )
            selected = next(
                item
                for item in install_lock["selected_packages"]
                if item["provider_version"] is not None
            )
            persistent = next(
                item
                for item in package_lock["packages"]
                if item["id"] == selected["id"]
            )
            for item in (selected, persistent):
                item["core_compatibility"] = ">=0.0.0"
                item["provider_compatibility"] = ">=0.0.0"
            package_lock["install_plan_identity_hash"] = sha256({
                key: value
                for key, value in install_lock.items()
                if key
                not in {
                    "fingerprint",
                    "package_lock_hash",
                    "status",
                }
            })
            package_lock["fingerprint"] = sha256({
                key: value
                for key, value in package_lock.items()
                if key != "fingerprint"
            })
            install_lock["package_lock_hash"] = package_lock["fingerprint"]
            install_lock["fingerprint"] = sha256({
                key: value
                for key, value in install_lock.items()
                if key not in {"fingerprint", "status"}
            })
            dump(package_lock, package_lock_path)
            dump(install_lock, install_lock_path)
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(
                normalize_failures(actual),
                normalize_failures(expected),
            )
            actual_package = next(
                check
                for check in actual["checks"]
                if check["id"] == "package.integrity"
            )
            expected_package = next(
                check
                for check in expected["checks"]
                if check["id"] == "package.integrity"
            )
            self.assertEqual(actual_package, expected_package)
            self.assertEqual(
                actual_package["details"],
                {
                    "errors": [
                        "Lockfile package semantics differ from installed "
                        f"Manifests: {selected['id']}"
                    ]
                },
            )
            self.assertEqual(filesystem_identity(target), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            package_details = next(
                check["details"]
                for check in actual["checks"]
                if check["id"] == "package.integrity"
            )
            self.assertEqual(
                package_details["package_count"],
                len(package_details["packages"]),
            )
            self.assertEqual(package_details["packages"], sorted(
                package_details["packages"]
            ))
            self.assertEqual(filesystem_identity(target), before)

            manifest_path = (
                target
                / ".agent-skills"
                / "packages"
                / "core"
                / "manifest.json"
            )
            manifest_path.chmod(0o755)
            mode_before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual_package = next(
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "package.integrity"
            )
            expected_package = next(
                check
                for check in expected["checks"]
                if check["id"] == "package.integrity"
            )
            self.assertEqual(actual_package, expected_package)
            self.assertEqual(
                actual_package["details"],
                {
                    "errors": [
                        "installed package content differs: core"
                    ]
                },
            )
            self.assertEqual(filesystem_identity(target), mode_before)
            manifest_path.chmod(0o644)

            manifest_path.write_text("{}\n", encoding="utf-8")
            tampered_before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(
                normalize_failures(actual),
                normalize_failures(expected),
            )
            actual_package = next(
                check
                for check in actual["checks"]
                if check["id"] == "package.integrity"
            )
            expected_package = next(
                check
                for check in expected["checks"]
                if check["id"] == "package.integrity"
            )
            self.assertEqual(actual_package, expected_package)
            self.assertEqual(
                actual_package["details"],
                {
                    "errors": [
                        "installed package content differs: core"
                    ]
                },
            )
            self.assertEqual(filesystem_identity(target), tampered_before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            manifest_path = (
                target
                / ".agent-skills"
                / "packages"
                / "core"
                / "manifest.json"
            )
            real_manifest = manifest_path.with_name("real-manifest.json")
            manifest_path.rename(real_manifest)
            manifest_path.symlink_to(real_manifest.name)
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual_package = next(
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "package.integrity"
            )
            expected_package = next(
                check
                for check in expected["checks"]
                if check["id"] == "package.integrity"
            )
            self.assertEqual(actual_package, expected_package)
            self.assertEqual(
                actual_package["details"],
                {
                    "errors": [
                        "install tree must not contain symlinks: "
                        "manifest.json"
                    ]
                },
            )
            self.assertEqual(filesystem_identity(target), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            activated = target / "bin" / "managed-tool"
            activated.parent.mkdir()
            activated.write_bytes(b"managed\n")
            activated.chmod(0o755)
            activation_lock = {
                "files": [{
                    "mode": 0o755,
                    "path": "bin/managed-tool",
                    "sha256": hashlib.sha256(
                        activated.read_bytes()
                    ).hexdigest(),
                }],
                "manager": "agent-development-skills",
                "schema_version": "1.0",
            }
            activation_lock_path = (
                target / ".agent-skills" / "activation-lock.json"
            )
            dump(activation_lock, activation_lock_path)
            activation_lock_path.chmod(0o644)

            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            self.assertEqual(
                next(
                    check
                    for check in actual["checks"]
                    if check["id"] == "activation.integrity"
                )["details"],
                {
                    "deprecation": "blocked-new-use",
                    "file_count": 1,
                    "managed": True,
                    "schema_version": "1.0",
                },
            )
            self.assertEqual(filesystem_identity(target), before)

            activation_lock["handler"] = (
                "core.source-activation.apple-codex-v1"
            )
            activation_lock["schema_version"] = "2.0"
            dump(activation_lock, activation_lock_path)
            activation_lock_path.chmod(0o644)
            before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            actual.pop("fingerprint")
            self.assertEqual(actual, expected)
            self.assertEqual(
                next(
                    check
                    for check in actual["checks"]
                    if check["id"] == "activation.integrity"
                )["details"]["deprecation"],
                "current",
            )
            self.assertEqual(filesystem_identity(target), before)

            invalid_version = deepcopy(activation_lock)
            invalid_version["schema_version"] = 1
            invalid_version.pop("handler")
            dump(invalid_version, activation_lock_path)
            activation_lock_path.chmod(0o644)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual = json.loads(result.stdout)
            expected_activation = next(
                check
                for check in expected["checks"]
                if check["id"] == "activation.integrity"
            )
            actual_activation = next(
                check
                for check in actual["checks"]
                if check["id"] == "activation.integrity"
            )
            self.assertEqual(actual_activation, expected_activation)
            self.assertEqual(
                actual_activation["details"],
                {
                    "errors": [
                        "unsupported activation-lock schema_version: 1"
                    ]
                },
            )

            for invalid, error in (
                (
                    "a'b",
                    'unsupported activation-lock schema_version: "a\'b"',
                ),
                (
                    "a\u2028b",
                    'unsupported activation-lock schema_version: "a\u2028b"',
                ),
                (
                    "a\u034fb",
                    'unsupported activation-lock schema_version: "a\u034fb"',
                ),
            ):
                invalid_version["schema_version"] = invalid
                dump(invalid_version, activation_lock_path)
                activation_lock_path.chmod(0o644)
                expected = python_projection(target)
                result = self.run_rust(
                    "doctor-baseline",
                    str(target),
                    "--schemas",
                    str(ROOT / "schemas"),
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                actual_activation = next(
                    check
                    for check in json.loads(result.stdout)["checks"]
                    if check["id"] == "activation.integrity"
                )
                expected_activation = next(
                    check
                    for check in expected["checks"]
                    if check["id"] == "activation.integrity"
                )
                self.assertEqual(actual_activation, expected_activation)
                self.assertEqual(
                    actual_activation["details"],
                    {"errors": [error]},
                )

            invalid_path = deepcopy(activation_lock)
            invalid_path["files"][0]["path"] = "."
            dump(invalid_path, activation_lock_path)
            activation_lock_path.chmod(0o644)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual_activation = next(
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "activation.integrity"
            )
            expected_activation = next(
                check
                for check in expected["checks"]
                if check["id"] == "activation.integrity"
            )
            self.assertEqual(actual_activation, expected_activation)
            self.assertEqual(
                actual_activation["details"],
                {
                    "errors": [
                        "activated file must be a package-relative path"
                    ]
                },
            )

            dump(activation_lock, activation_lock_path)
            activation_lock_path.chmod(0o644)
            activated.chmod(0o644)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual_activation = next(
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "activation.integrity"
            )
            expected_activation = next(
                check
                for check in expected["checks"]
                if check["id"] == "activation.integrity"
            )
            self.assertEqual(actual_activation, expected_activation)
            self.assertEqual(
                actual_activation["details"],
                {"errors": ["activated file differs: bin/managed-tool"]},
            )

            activated.chmod(0o755)
            real_activated = activated.with_name("real-managed-tool")
            activated.rename(real_activated)
            activated.symlink_to(real_activated.name)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual_activation = next(
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "activation.integrity"
            )
            expected_activation = next(
                check
                for check in expected["checks"]
                if check["id"] == "activation.integrity"
            )
            self.assertEqual(actual_activation, expected_activation)
            self.assertEqual(
                actual_activation["details"],
                {
                    "errors": [
                        "activated file must not traverse a symlink: "
                        "bin/managed-tool"
                    ]
                },
            )
            activated.unlink()
            real_activated.rename(activated)

            activated.write_bytes(b"tampered\n")
            tampered_before = filesystem_identity(target)
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "")
            actual = json.loads(result.stdout)
            self.assertEqual(
                normalize_failures(actual),
                normalize_failures(expected),
            )
            self.assertEqual(
                [
                    check["id"]
                    for check in actual["checks"]
                    if check["status"] == "failed"
                ],
                ["activation.integrity"],
            )
            self.assertEqual(
                filesystem_identity(target),
                tampered_before,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex"
            install_bundle(
                build_install_bundle(
                    ROOT / "platforms",
                    platforms=["apple", "desktop"],
                ),
                target,
            )
            activation_lock_path = (
                target / ".agent-skills" / "activation-lock.json"
            )
            activation_lock_path.symlink_to("missing-activation-lock.json")
            expected = python_projection(target)
            result = self.run_rust(
                "doctor-baseline",
                str(target),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            actual_activation = next(
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "activation.integrity"
            )
            expected_activation = next(
                check
                for check in expected["checks"]
                if check["id"] == "activation.integrity"
            )
            self.assertEqual(actual_activation, expected_activation)
            self.assertEqual(
                actual_activation["details"],
                {"errors": ["activation Lock is missing or unsafe"]},
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            before = filesystem_identity(linked)
            expected = python_projection(linked)
            result = self.run_rust(
                "doctor-baseline",
                str(linked),
                "--schemas",
                str(ROOT / "schemas"),
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                normalize_failures(json.loads(result.stdout)),
                normalize_failures(expected),
            )
            self.assertEqual(filesystem_identity(linked), before)

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

    def test_adapter_request_and_result_contracts_match_python(self) -> None:
        from tests.test_adapters import StructuredAdapterContractTests

        fixture = StructuredAdapterContractTests()
        fixture.setUp()
        fixture.plan["edges"] = []
        fixture.plan["status"] = "ready"
        fixture.plan["nodes"][0].update(
            {
                "mandatory": True,
                "max_retries": 0,
                "status": "ready",
                "timeout_seconds": 300,
            }
        )
        adapter_plan_content = {
            key: value
            for key, value in fixture.plan.items()
            if key not in {"fingerprint", "plan_id"}
        }
        fixture.plan["fingerprint"] = sha256(adapter_plan_content)
        fixture.plan["plan_id"] = f"plan-{fixture.plan['fingerprint'][:12]}"
        fixture.request = build_adapter_request(
            fixture.plan,
            "apple-verify",
            context=fixture.context,
            invocation_id="verify-invocation-1",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            context_path = root / "context.json"
            request_path = root / "request.json"
            result_path = root / "result.json"
            dump(fixture.plan, plan_path)
            dump(fixture.context, context_path)

            built = self.run_rust(
                "adapter-request-build",
                str(plan_path),
                "apple-verify",
                str(context_path),
                "verify-invocation-1",
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(json.loads(built.stdout), fixture.request)
            dump(fixture.request, request_path)
            validated = self.run_rust(
                "adapter-request-validate",
                str(request_path),
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(json.loads(validated.stdout), fixture.request)

            request_cases = []
            tampered = deepcopy(fixture.request)
            tampered["request_id"] = "adapter-request-tampered"
            request_cases.append(tampered)
            mismatched_checkpoints = deepcopy(fixture.request)
            mismatched_checkpoints["checkpoints"]["CP0"] = "pending"
            request_cases.append(mismatched_checkpoints)
            invalid_binding = deepcopy(fixture.request)
            invalid_binding["binding"]["mode"] = " "
            request_cases.append(invalid_binding)
            unknown_field = deepcopy(fixture.request)
            unknown_field["unexpected"] = True
            request_cases.append(unknown_field)
            for index, request in enumerate(request_cases):
                with self.subTest(request_case=index):
                    with self.assertRaises(ContractError):
                        validate_adapter_request(request)
                    dump(request, request_path)
                    rejected = self.run_rust(
                        "adapter-request-validate",
                        str(request_path),
                    )
                    self.assertEqual(rejected.returncode, 2)
                    self.assertEqual(rejected.stdout, "")

            base_result = fixture.result()
            result_cases: list[tuple[dict, dict, bool]] = [
                (fixture.request, base_result, True),
            ]
            unknown_result = deepcopy(base_result)
            unknown_result["unexpected"] = True
            result_cases.append((fixture.request, unknown_result, False))
            invalid_hash = deepcopy(base_result)
            invalid_hash["artifacts"][0]["sha256"] = "not-a-hash"
            result_cases.append((fixture.request, invalid_hash, False))
            duplicate_artifact = deepcopy(base_result)
            duplicate_artifact["artifacts"].append(
                deepcopy(duplicate_artifact["artifacts"][0])
            )
            result_cases.append((fixture.request, duplicate_artifact, False))
            unknown_artifact = deepcopy(base_result)
            unknown_artifact["evidence"][0]["artifact_ids"] = ["missing"]
            result_cases.append((fixture.request, unknown_artifact, False))
            empty_data = deepcopy(base_result)
            empty_data["evidence"][0]["data"] = {}
            result_cases.append((fixture.request, empty_data, False))
            failed_without_evidence = deepcopy(base_result)
            failed_without_evidence["status"] = "failed"
            failed_without_evidence["failure_attribution"] = {
                "category": "code",
                "summary": "failure",
            }
            result_cases.append((fixture.request, failed_without_evidence, False))
            failed_cleanup = deepcopy(base_result)
            failed_cleanup["cleanup"] = [
                {
                    "resource": "device",
                    "status": "failed",
                    "detail": "release failed",
                }
            ]
            result_cases.append((fixture.request, failed_cleanup, False))
            incomplete_gap = deepcopy(base_result)
            incomplete_gap["no_test_reason"] = "no target"
            result_cases.append((fixture.request, incomplete_gap, False))
            null_gap = deepcopy(base_result)
            null_gap["no_test_reason"] = None
            null_gap["suggested_validation"] = None
            result_cases.append((fixture.request, null_gap, True))
            no_evidence = deepcopy(base_result)
            no_evidence["evidence"] = []
            no_evidence["artifacts"] = []
            result_cases.append((fixture.request, no_evidence, False))

            auto_plan = deepcopy(fixture.plan)
            auto_plan["nodes"][0]["capability"] = "verification.apple.auto"
            auto_plan["nodes"][0]["binding"]["mode"] = "auto"
            auto_request = build_adapter_request(
                auto_plan,
                "apple-verify",
                context=fixture.context,
                invocation_id="verify-auto-invocation-1",
            )
            auto_result = fixture.result()
            for field in (
                "request_id",
                "invocation_id",
                "plan_fingerprint",
                "node_id",
                "capability",
                "provider",
                "binding",
            ):
                auto_result[field] = auto_request[field]
            missing_execution = deepcopy(auto_result)
            result_cases.append((auto_request, missing_execution, False))
            auto_result["evidence"][0]["data"] = {
                "level": "unit",
                "executed_validation": [
                    {"kind": "quick-verify", "status": "passed"}
                ],
            }
            result_cases.append((auto_request, auto_result, True))

            review_plan = deepcopy(fixture.plan)
            review_plan["nodes"][0]["capability"] = "review.independent"
            review_plan["nodes"][0]["provider"] = "core"
            review_request = build_adapter_request(
                review_plan,
                "apple-verify",
                context=fixture.context,
                invocation_id="review-invocation-1",
            )
            review_result = fixture.result()
            for field in (
                "request_id",
                "invocation_id",
                "plan_fingerprint",
                "node_id",
                "capability",
                "provider",
                "binding",
            ):
                review_result[field] = review_request[field]
            review_result["artifacts"] = []
            review_result["evidence"] = [
                {
                    "kind": "review",
                    "status": "passed",
                    "summary": "review passed",
                    "data": {
                        "blocking_issues": [],
                        "implementation_actor": "builder-1",
                        "reviewer_actor": "reviewer-1",
                    },
                    "artifact_ids": [],
                }
            ]
            result_cases.append((review_request, review_result, True))
            same_actor = deepcopy(review_result)
            same_actor["evidence"][0]["data"]["reviewer_actor"] = "builder-1"
            result_cases.append((review_request, same_actor, False))
            unblocked_issue = deepcopy(review_result)
            unblocked_issue["evidence"][0]["data"]["blocking_issues"] = ["P1"]
            result_cases.append((review_request, unblocked_issue, False))

            for index, (request, result, expected) in enumerate(result_cases):
                with self.subTest(result_case=index):
                    python_accepted = True
                    try:
                        validate_adapter_result(request, result)
                    except ContractError:
                        python_accepted = False
                    self.assertEqual(python_accepted, expected)
                    dump(request, request_path)
                    dump(result, result_path)
                    native = self.run_rust(
                        "adapter-result-validate",
                        str(request_path),
                        str(result_path),
                    )
                    self.assertEqual(native.returncode == 0, expected, native.stderr)
                    if expected:
                        self.assertEqual(json.loads(native.stdout), result)
                    else:
                        self.assertEqual(native.stdout, "")

    def test_recorded_adapter_runtime_matches_python(self) -> None:
        from tests.test_adapters import AppleProviderAnchorSliceTests

        fixture = AppleProviderAnchorSliceTests()
        fixture.setUp()

        def complete_results(
            *,
            context: dict | None = None,
            suffix: str = "1",
        ) -> dict:
            active_context = context or fixture.context
            return {
                **fixture._supporting_results(
                    context=active_context,
                    invocation_suffix=suffix,
                ),
                "apple-1": fixture._result(
                    "apple-1",
                    "delivery",
                    {"changed_files": ["Fixture.swift"]},
                    context=active_context,
                    invocation_id=f"apple-1-invocation-{suffix}",
                ),
                "apple-2": fixture._result(
                    "apple-2",
                    "validation",
                    {"level": "affected-tests", "tests": 1},
                    context=active_context,
                    invocation_id=f"apple-2-invocation-{suffix}",
                ),
                "review": fixture._result(
                    "review",
                    "review",
                    {
                        "blocking_issues": [],
                        "implementation_actor": "builder-1",
                        "reviewer_actor": "reviewer-1",
                    },
                    context=active_context,
                    invocation_id=f"review-invocation-{suffix}",
                ),
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            results_path = root / "results.json"
            context_path = root / "context.json"
            dump(fixture.plan, plan_path)

            def native(
                results: dict,
                context: dict,
                *,
                ledger: Path | None = None,
                resume: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                dump(results, results_path)
                dump(context, context_path)
                arguments = [
                    "runtime-execute-recorded",
                    str(plan_path),
                    str(results_path),
                    str(context_path),
                    "--identity-seed",
                    "0123456789abcdef",
                ]
                if ledger is not None:
                    arguments.extend(["--ledger", str(ledger)])
                if resume:
                    arguments.append("--resume")
                return self.run_rust(*arguments)

            results = complete_results()
            expected = RecordedAdapterExecutor(
                results,
                context=fixture.context,
            ).run(fixture.plan)
            actual = native(results, fixture.context)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual.stdout)),
                _normalize_runtime_ledger(expected),
            )

            null_gap_results = complete_results()
            null_gap_results["apple-2"]["no_test_reason"] = None
            null_gap_results["apple-2"]["suggested_validation"] = None
            expected_null_gap = RecordedAdapterExecutor(
                null_gap_results,
                context=fixture.context,
            ).run(fixture.plan)
            actual_null_gap = native(null_gap_results, fixture.context)
            self.assertEqual(
                actual_null_gap.returncode,
                0,
                actual_null_gap.stderr,
            )
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual_null_gap.stdout)),
                _normalize_runtime_ledger(expected_null_gap),
            )

            null_auto_results = complete_results()
            null_auto_results["apple-3"].update(
                {
                    "status": "partial",
                    "no_test_reason": None,
                    "suggested_validation": None,
                }
            )
            null_auto_results["apple-3"]["evidence"][0]["status"] = "partial"
            expected_null_auto = RecordedAdapterExecutor(
                null_auto_results,
                context=fixture.context,
            ).run(fixture.plan)
            actual_null_auto = native(null_auto_results, fixture.context)
            self.assertEqual(
                actual_null_auto.returncode,
                0,
                actual_null_auto.stderr,
            )
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual_null_auto.stdout)),
                _normalize_runtime_ledger(expected_null_auto),
            )

            expected = RecordedAdapterExecutor(
                {},
                context=fixture.context,
            ).run(fixture.plan)
            actual = native({}, fixture.context)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual.stdout)),
                _normalize_runtime_ledger(expected),
            )

            failed_results = {
                **fixture._supporting_results(include_downstream=False),
                "apple-1": fixture._result(
                    "apple-1",
                    "delivery",
                    {"changed_files": ["Fixture.swift"]},
                ),
                "apple-2": fixture._result(
                    "apple-2",
                    "validation",
                    {"level": "affected-tests", "tests": 1},
                ),
            }
            failed_results["apple-2"].update(
                {
                    "status": "failed",
                    "failure_attribution": {
                        "category": "code",
                        "summary": "test failed",
                    },
                }
            )
            failed_results["apple-2"]["evidence"][0]["status"] = "failed"
            expected = RecordedAdapterExecutor(
                failed_results,
                context=fixture.context,
            ).run(fixture.plan)
            actual = native(failed_results, fixture.context)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            actual_ledger = json.loads(actual.stdout)
            self.assertEqual(
                _normalize_runtime_ledger(actual_ledger),
                _normalize_runtime_ledger(expected),
            )
            self.assertEqual(
                len(
                    [
                        item
                        for item in actual_ledger["adapter_outcomes"]
                        if item["node_id"] == "apple-2"
                    ]
                ),
                1,
            )

            partial_results = complete_results()
            partial_results["apple-2"].update(
                {
                    "status": "partial",
                    "evidence": [],
                    "artifacts": [],
                    "no_test_reason": "fixture has no executable test target",
                    "suggested_validation": "run the smallest project smoke",
                }
            )
            python_ledger = root / "python-partial.jsonl"
            rust_ledger = root / "rust-partial.jsonl"
            expected_first = RecordedAdapterExecutor(
                partial_results,
                context=fixture.context,
            ).run(fixture.plan, ledger_path=python_ledger)
            actual_first = native(
                partial_results,
                fixture.context,
                ledger=rust_ledger,
            )
            self.assertEqual(actual_first.returncode, 0, actual_first.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual_first.stdout)),
                _normalize_runtime_ledger(expected_first),
            )
            expected_resumed = RecordedAdapterExecutor(
                partial_results,
                context=fixture.context,
            ).run(fixture.plan, ledger_path=python_ledger, resume=True)
            actual_resumed = native(
                partial_results,
                fixture.context,
                ledger=rust_ledger,
                resume=True,
            )
            self.assertEqual(actual_resumed.returncode, 0, actual_resumed.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual_resumed.stdout)),
                _normalize_runtime_ledger(expected_resumed),
            )

            auto_results = complete_results()
            auto_results["apple-3"].update(
                {
                    "status": "partial",
                    "evidence": [],
                    "artifacts": [],
                    "no_test_reason": "fixture has no executable test target",
                    "suggested_validation": "run the smallest approved smoke",
                }
            )
            expected_auto = RecordedAdapterExecutor(
                auto_results,
                context=fixture.context,
            ).run(fixture.plan)
            actual_auto = native(auto_results, fixture.context)
            self.assertEqual(actual_auto.returncode, 0, actual_auto.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual_auto.stdout)),
                _normalize_runtime_ledger(expected_auto),
            )

            python_stale_ledger = root / "python-stale.jsonl"
            rust_stale_ledger = root / "rust-stale.jsonl"
            RecordedAdapterExecutor(
                results,
                context=fixture.context,
            ).run(fixture.plan, ledger_path=python_stale_ledger)
            created = native(
                results,
                fixture.context,
                ledger=rust_stale_ledger,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            changed_context = deepcopy(fixture.context)
            changed_context["target_modules"] = ["DifferentModule"]
            with self.assertRaisesRegex(ContractError, "does not match request"):
                RecordedAdapterExecutor(
                    results,
                    context=changed_context,
                ).run(
                    fixture.plan,
                    ledger_path=python_stale_ledger,
                    resume=True,
                )
            stale = native(
                results,
                changed_context,
                ledger=rust_stale_ledger,
                resume=True,
            )
            self.assertEqual(stale.returncode, 2)
            self.assertEqual(stale.stdout, "")
            self.assertEqual(
                _normalize_runtime_ledger(
                    RunLedger.replay(
                        rust_stale_ledger,
                        fixture.plan["fingerprint"],
                    ).value
                ),
                _normalize_runtime_ledger(
                    RunLedger.replay(
                        python_stale_ledger,
                        fixture.plan["fingerprint"],
                    ).value
                ),
            )

            invalid_results = {
                **fixture._supporting_results(include_downstream=False),
                "apple-1": fixture._result(
                    "apple-1",
                    "delivery",
                    {"changed_files": ["Fixture.swift"]},
                    invocation_id="apple-1-invalid-invocation",
                ),
            }
            invalid_results["apple-1"]["request_id"] = "tampered"
            python_invalid_ledger = root / "python-invalid.jsonl"
            rust_invalid_ledger = root / "rust-invalid.jsonl"
            with self.assertRaises(ContractError):
                RecordedAdapterExecutor(
                    invalid_results,
                    context=fixture.context,
                ).run(fixture.plan, ledger_path=python_invalid_ledger)
            rejected = native(
                invalid_results,
                fixture.context,
                ledger=rust_invalid_ledger,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            python_replayed = RunLedger.replay(
                python_invalid_ledger,
                fixture.plan["fingerprint"],
            ).value
            rust_replayed = RunLedger.replay(
                rust_invalid_ledger,
                fixture.plan["fingerprint"],
            ).value
            self.assertEqual(
                _normalize_runtime_ledger(rust_replayed),
                _normalize_runtime_ledger(python_replayed),
            )
            invalid_attempt = next(
                item
                for item in rust_replayed["node_attempts"]
                if item["node_id"] == "apple-1"
            )
            self.assertFalse(
                any(
                    item["attempt_id"] == invalid_attempt["attempt_id"]
                    for item in rust_replayed["resource_events"]
                )
            )

            corrected_results = complete_results(suffix="corrected")
            expected_recovered = RecordedAdapterExecutor(
                corrected_results,
                context=fixture.context,
            ).run(
                fixture.plan,
                ledger_path=python_invalid_ledger,
                resume=True,
            )
            actual_recovered = native(
                corrected_results,
                fixture.context,
                ledger=rust_invalid_ledger,
                resume=True,
            )
            self.assertEqual(
                actual_recovered.returncode,
                0,
                actual_recovered.stderr,
            )
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(actual_recovered.stdout)),
                _normalize_runtime_ledger(expected_recovered),
            )

    def test_provider_invocation_transport_and_runtime_match_python(self) -> None:
        from tests.test_adapters import AppleProviderAnchorSliceTests

        fixture = AppleProviderAnchorSliceTests()
        fixture.setUp()
        node_id = "apple-2"
        token = "provider-claim-token-compat-0001"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python_root = root / "python"
            rust_root = root / "rust"
            plan_path = root / "plan.json"
            context_path = root / "context.json"
            result_path = root / "result.json"
            selection_path = root / "selection.json"
            token_path = root / "claim-token"
            dump(fixture.plan, plan_path)
            dump(fixture.context, context_path)
            token_path.write_text(token + "\n", encoding="utf-8")
            token_path.chmod(0o600)

            tampered_plan = deepcopy(fixture.plan)
            tampered_node = next(
                node for node in tampered_plan["nodes"] if node["id"] == node_id
            )
            tampered_node["permission_profile"] = "project-write"
            with self.assertRaisesRegex(ContractError, "fingerprint mismatch"):
                validate_provider_invocation_plan(tampered_plan, None)
            tampered_plan_path = root / "tampered-plan.json"
            dump(tampered_plan, tampered_plan_path)
            rejected_tamper = self.run_rust(
                "invocation-prepare",
                str(rust_root),
                str(tampered_plan_path),
                node_id,
                str(context_path),
                "provider-tampered-unlocked-plan",
            )
            self.assertEqual(rejected_tamper.returncode, 2)
            self.assertIn("fingerprint mismatch", rejected_tamper.stderr)

            locked_plan = deepcopy(fixture.plan)
            locked_plan["package_lock_hash"] = "0" * 64
            locked_content = {
                key: value
                for key, value in locked_plan.items()
                if key not in {"fingerprint", "plan_id"}
            }
            locked_plan["fingerprint"] = sha256(locked_content)
            locked_plan["plan_id"] = f"plan-{locked_plan['fingerprint'][:12]}"
            locked_plan_path = root / "locked-plan.json"
            dump(locked_plan, locked_plan_path)
            missing_lock = self.run_rust(
                "invocation-prepare",
                str(rust_root),
                str(locked_plan_path),
                node_id,
                str(context_path),
                "provider-locked-without-lock",
            )
            self.assertEqual(missing_lock.returncode, 2)
            self.assertIn("requires the current package Lockfile", missing_lock.stderr)

            prepared = self.run_rust(
                "invocation-prepare",
                str(rust_root),
                str(plan_path),
                node_id,
                str(context_path),
                "provider-compat-1",
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            actual_prepared = json.loads(prepared.stdout)
            expected_prepared = prepare_provider_invocation(
                python_root,
                fixture.plan,
                node_id,
                context=fixture.context,
                invocation_id="provider-compat-1",
                prepared_at=actual_prepared["prepared_at"],
            )
            self.assertEqual(actual_prepared, expected_prepared)
            request_id = expected_prepared["request"]["request_id"]

            claimed = self.run_rust(
                "invocation-claim",
                str(rust_root),
                request_id,
                "provider-host-1",
                str(token_path),
            )
            self.assertEqual(claimed.returncode, 0, claimed.stderr)
            actual_claimed = json.loads(claimed.stdout)
            expected_claimed = claim_provider_invocation(
                python_root,
                request_id,
                actor_id="provider-host-1",
                claim_token=token,
                claimed_at=actual_claimed["claim"]["claimed_at"],
            )
            self.assertEqual(actual_claimed, expected_claimed)
            self.assertNotIn(token, claimed.stdout)

            result = fixture._result(
                node_id,
                "validation",
                {"level": "affected-tests", "tests": 1},
                invocation_id="provider-compat-1",
            )
            dump(result, result_path)
            submitted = self.run_rust(
                "invocation-submit",
                str(rust_root),
                request_id,
                str(result_path),
                str(token_path),
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            actual_submitted = json.loads(submitted.stdout)
            expected_submitted = submit_provider_invocation(
                python_root,
                request_id,
                result,
                claim_token=token,
                submitted_at=actual_submitted["submitted_at"],
            )
            self.assertEqual(actual_submitted, expected_submitted)
            selection = {
                "schema_version": "1.0",
                "plan_fingerprint": fixture.plan["fingerprint"],
                "requests": {node_id: request_id},
            }
            dump(selection, selection_path)

            expected_inspection = inspect_provider_invocation(
                python_root,
                request_id,
                at=actual_submitted["submitted_at"],
            )
            inspected = self.run_rust(
                "invocation-inspect",
                str(rust_root),
                request_id,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout), expected_inspection)

            results = collect_submitted_results(
                python_root,
                fixture.plan["fingerprint"],
                selection,
                at=actual_submitted["submitted_at"],
            )
            expected_ledger = RecordedAdapterExecutor(
                results,
                context=fixture.context,
            ).run(fixture.plan)
            rejected_runtime_tamper = self.run_rust(
                "runtime-execute-invocations",
                str(tampered_plan_path),
                str(rust_root),
                str(context_path),
                "--selection",
                str(selection_path),
            )
            self.assertEqual(rejected_runtime_tamper.returncode, 2)
            self.assertIn(
                "fingerprint mismatch",
                rejected_runtime_tamper.stderr,
            )
            native = self.run_rust(
                "runtime-execute-invocations",
                str(plan_path),
                str(rust_root),
                str(context_path),
                "--selection",
                str(selection_path),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(native.stdout)),
                _normalize_runtime_ledger(expected_ledger),
            )

            duplicate = self.run_rust(
                "invocation-claim",
                str(rust_root),
                request_id,
                "provider-host-2",
                str(token_path),
            )
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("already submitted", duplicate.stderr)

            concurrent_prepared = self.run_rust(
                "invocation-prepare",
                str(rust_root),
                str(plan_path),
                node_id,
                str(context_path),
                "provider-compat-concurrent",
            )
            self.assertEqual(
                concurrent_prepared.returncode,
                0,
                concurrent_prepared.stderr,
            )
            concurrent_request_id = json.loads(concurrent_prepared.stdout)["request"][
                "request_id"
            ]
            processes = [
                subprocess.Popen(
                    [
                        str(self.rust_cli),
                        "invocation-claim",
                        str(rust_root),
                        concurrent_request_id,
                        f"provider-host-{index}",
                        str(token_path),
                    ],
                    cwd=ROOT,
                    env={**os.environ, "CARGO_TERM_COLOR": "never"},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index in (1, 2)
            ]
            outcomes = [process.communicate(timeout=10) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2])
            self.assertTrue(
                any("cannot be claimed" in stderr for _, stderr in outcomes)
            )

            resource_request_ids = []
            for index in (1, 2):
                prepared_resource = self.run_rust(
                    "invocation-prepare",
                    str(rust_root),
                    str(plan_path),
                    "apple-1",
                    str(context_path),
                    f"provider-resource-{index}",
                )
                self.assertEqual(
                    prepared_resource.returncode,
                    0,
                    prepared_resource.stderr,
                )
                resource_request_ids.append(
                    json.loads(prepared_resource.stdout)["request"]["request_id"]
                )
            claimed_resource = self.run_rust(
                "invocation-claim",
                str(rust_root),
                resource_request_ids[0],
                "provider-resource-host-1",
                str(token_path),
            )
            self.assertEqual(
                claimed_resource.returncode,
                0,
                claimed_resource.stderr,
            )
            rejected_resource = self.run_rust(
                "invocation-claim",
                str(rust_root),
                resource_request_ids[1],
                "provider-resource-host-2",
                str(token_path),
            )
            self.assertEqual(rejected_resource.returncode, 2)
            self.assertIn("resource is already claimed", rejected_resource.stderr)

            approval_plan = deepcopy(fixture.plan)
            approval_node = next(
                node for node in approval_plan["nodes"] if node["id"] == node_id
            )
            approval_node["approval"] = {
                "action": "execute-provider",
                "reason": "requires user approval",
                "scope": {"node_id": node_id},
            }
            approval_content = {
                key: value
                for key, value in approval_plan.items()
                if key not in {"fingerprint", "plan_id"}
            }
            approval_plan["fingerprint"] = sha256(approval_content)
            approval_plan["plan_id"] = f"plan-{approval_plan['fingerprint'][:12]}"
            approval_plan_path = root / "approval-plan.json"
            dump(approval_plan, approval_plan_path)
            rejected_approval = self.run_rust(
                "invocation-prepare",
                str(rust_root),
                str(approval_plan_path),
                node_id,
                str(context_path),
                "provider-approval-bound",
            )
            self.assertEqual(rejected_approval.returncode, 2)
            self.assertIn(
                "runtime-granted attempt proof",
                rejected_approval.stderr,
            )

    @unittest.skipIf(os.name == "nt", "Python worktree registry requires POSIX fcntl")
    def test_session_worktree_create_and_exact_compensation_match_python(
        self,
    ) -> None:
        from tests.test_worktree_sessions import git, make_repo

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = make_repo(parent)
            base = git(root, "rev-parse", "HEAD")
            worktrees = parent / "worktrees"
            (root / "file.txt").write_text("source-only dirty\n", encoding="utf-8")

            rejected = self.run_rust(
                "session-worktree-create",
                str(root),
                "implicit",
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("dirty worktree", rejected.stderr)

            expected_record, expected_notice = create_session_worktree(
                root,
                name="isolated",
                base_ref=base,
                worktree_root=worktrees,
            )
            remove_created_session_worktree(
                expected_record,
                source_repository=root,
            )
            actual = self.run_rust(
                "session-worktree-create",
                str(root),
                "isolated",
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(
                json.loads(actual.stdout),
                {
                    "notice": expected_notice,
                    "repository": expected_record,
                },
            )
            record_path = parent / "created-record.json"
            dump(json.loads(actual.stdout)["repository"], record_path)
            removed = self.run_rust(
                "session-worktree-remove",
                str(root),
                str(record_path),
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(json.loads(removed.stdout), {"removed": True})
            self.assertFalse((worktrees / "isolated").exists())
            self.assertEqual(git(root, "branch", "--list", "agent/isolated"), "")

            created = self.run_rust(
                "session-worktree-create",
                str(root),
                "changed",
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            changed_record = json.loads(created.stdout)["repository"]
            changed_path = Path(changed_record["worktree_path"])
            (changed_path / "file.txt").write_text("changed\n", encoding="utf-8")
            dump(changed_record, record_path)
            guarded = self.run_rust(
                "session-worktree-remove",
                str(root),
                str(record_path),
            )
            self.assertEqual(guarded.returncode, 2)
            self.assertIn(
                "refusing automatic compensation",
                guarded.stderr,
            )
            self.assertTrue(changed_path.exists())
            git(root, "worktree", "remove", "--force", str(changed_path))
            git(root, "branch", "-D", "agent/changed")

            context_input = {
                "capability_closure": {},
                "created_at": "2026-07-18T00:00:00+00:00",
                "dependencies": [],
                "platform_contexts": {},
                "project_id": "project",
                "repositories": [],
                "selected_platforms": [],
                "session_id": "registered",
            }
            context_input_path = parent / "context-input.json"
            dump(context_input, context_input_path)
            registered = self.run_rust(
                "session-create",
                str(root),
                "registered",
                str(context_input_path),
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(registered.returncode, 0, registered.stderr)
            registered_value = json.loads(registered.stdout)
            self.assertEqual(registered_value["operation"], "create")
            self.assertEqual(
                registered_value["session"]["lifecycle"]["state"],
                "active",
            )
            self.assertEqual(
                SessionRegistry(root).load("registered"),
                registered_value["session"],
            )
            registered_record = registered_value["session"]["repositories"][0]
            dump(registered_record, record_path)
            removed = self.run_rust(
                "session-worktree-remove",
                str(root),
                str(record_path),
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)

            invalid_input = deepcopy(context_input)
            invalid_input["project_id"] = ""
            invalid_input["session_id"] = "invalid-context"
            dump(invalid_input, context_input_path)
            invalid = self.run_rust(
                "session-create",
                str(root),
                "invalid-context",
                str(context_input_path),
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertFalse((worktrees / "invalid-context").exists())
            self.assertEqual(
                git(root, "branch", "--list", "agent/invalid-context"),
                "",
            )

            nested_root = root / "must-not-exist" / "worktrees"
            nested = self.run_rust(
                "session-worktree-create",
                str(root),
                "nested",
                "--base-ref",
                base,
                "--worktree-root",
                str(nested_root),
            )
            self.assertEqual(nested.returncode, 2)
            self.assertIn(
                "must not be nested inside the source worktree",
                nested.stderr,
            )
            self.assertFalse((root / "must-not-exist").exists())
            with self.assertRaisesRegex(
                ContractError,
                "must not be nested inside the source worktree",
            ):
                create_session_worktree(
                    root,
                    name="python-nested",
                    base_ref=base,
                    worktree_root=nested_root,
                )
            self.assertFalse((root / "must-not-exist").exists())

            invalid_branch = self.run_rust(
                "session-worktree-create",
                str(root),
                "invalid-branch",
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
                "--branch",
                "HEAD",
            )
            self.assertEqual(invalid_branch.returncode, 2)
            self.assertIn("session branch is invalid", invalid_branch.stderr)
            self.assertEqual(git(root, "branch", "--list", "HEAD"), "")
            self.assertFalse((worktrees / "invalid-branch").exists())

            if os.name != "nt":
                real_worktree_root = parent / "real-worktree-root"
                real_worktree_root.mkdir()
                linked_worktree_root = parent / "linked-worktree-root"
                linked_worktree_root.symlink_to(
                    real_worktree_root,
                    target_is_directory=True,
                )
                linked = self.run_rust(
                    "session-worktree-create",
                    str(root),
                    "linked-root",
                    "--base-ref",
                    base,
                    "--worktree-root",
                    str(linked_worktree_root),
                )
                self.assertEqual(linked.returncode, 2)
                self.assertIn(
                    "session worktree root",
                    linked.stderr,
                )
                with self.assertRaisesRegex(
                    ContractError,
                    "session worktree root",
                ):
                    create_session_worktree(
                        root,
                        name="python-linked-root",
                        base_ref=base,
                        worktree_root=linked_worktree_root,
                    )
                self.assertFalse((real_worktree_root / "linked-root").exists())
                self.assertFalse(
                    (real_worktree_root / "python-linked-root").exists()
                )

            checkout_failure = make_repo(parent, "checkout-failure")
            (checkout_failure / ".gitattributes").write_text(
                "file.txt filter=required-failure\n",
                encoding="utf-8",
            )
            git(checkout_failure, "add", ".gitattributes")
            git(checkout_failure, "commit", "-q", "-m", "require filter")
            failed_base = git(checkout_failure, "rev-parse", "HEAD")
            git(
                checkout_failure,
                "config",
                "filter.required-failure.smudge",
                "false",
            )
            git(
                checkout_failure,
                "config",
                "filter.required-failure.clean",
                "cat",
            )
            git(
                checkout_failure,
                "config",
                "filter.required-failure.required",
                "true",
            )
            failed_worktrees = parent / "failed-worktrees"
            failed = self.run_rust(
                "session-worktree-create",
                str(checkout_failure),
                "broken",
                "--base-ref",
                failed_base,
                "--worktree-root",
                str(failed_worktrees),
            )
            self.assertEqual(failed.returncode, 2)
            self.assertFalse((failed_worktrees / "broken").exists())
            self.assertEqual(
                git(
                    checkout_failure,
                    "branch",
                    "--list",
                    "agent/broken",
                ),
                "",
            )
            with self.assertRaises(ContractError):
                create_session_worktree(
                    checkout_failure,
                    name="python-broken",
                    base_ref=failed_base,
                    worktree_root=failed_worktrees,
                )
            self.assertFalse((failed_worktrees / "python-broken").exists())
            self.assertEqual(
                git(
                    checkout_failure,
                    "branch",
                    "--list",
                    "agent/python-broken",
                ),
                "",
            )

            if os.name != "nt":
                cas_repo = make_repo(parent, "cas-repository")
                cas_base = git(cas_repo, "rev-parse", "HEAD")
                cas_worktrees = parent / "cas-worktrees"
                cas_created = self.run_rust(
                    "session-worktree-create",
                    str(cas_repo),
                    "cas",
                    "--base-ref",
                    cas_base,
                    "--worktree-root",
                    str(cas_worktrees),
                )
                self.assertEqual(cas_created.returncode, 0, cas_created.stderr)
                cas_record = json.loads(cas_created.stdout)["repository"]
                (cas_repo / "file.txt").write_text(
                    "advanced\n",
                    encoding="utf-8",
                )
                git(cas_repo, "add", "file.txt")
                git(cas_repo, "commit", "-q", "-m", "advanced")
                advanced_commit = git(cas_repo, "rev-parse", "HEAD")
                primary_ref = git(cas_repo, "symbolic-ref", "HEAD")
                git(cas_repo, "reset", "--hard", "-q", cas_base)

                real_git = shutil.which("git")
                self.assertIsNotNone(real_git)
                wrapper_directory = parent / "git-wrapper"
                wrapper_directory.mkdir()
                wrapper = wrapper_directory / "git"
                wrapper_log = parent / "git-wrapper.log"
                wrapper.write_text(
                    "#!/bin/sh\n"
                    f"echo \"$*\" >> '{wrapper_log}'\n"
                    'case "$*" in\n'
                    '  *"symbolic-ref -q refs/heads/agent/cas"*)\n'
                    f"    '{real_git}' -C \"$PWD\" symbolic-ref "
                    f"refs/heads/agent/cas {primary_ref}\n"
                    "    ;;\n"
                    '  *"update-ref --no-deref -d refs/heads/agent/python-cas "*)\n'
                    f"    '{real_git}' -C \"$PWD\" update-ref "
                    f"refs/heads/agent/python-cas {advanced_commit}\n"
                    "    ;;\n"
                    "esac\n"
                    f"exec '{real_git}' \"$@\"\n",
                    encoding="utf-8",
                )
                wrapper.chmod(0o755)

                dump(cas_record, record_path)
                with mock.patch.dict(
                    os.environ,
                    {
                        "PATH": (
                            str(wrapper_directory)
                            + os.pathsep
                            + os.environ["PATH"]
                        )
                    },
                ):
                    cas_removed = self.run_rust(
                        "session-worktree-remove",
                        str(cas_repo),
                        str(record_path),
                    )
                self.assertEqual(
                    cas_removed.returncode,
                    2,
                    wrapper_log.read_text(encoding="utf-8"),
                )
                self.assertTrue(Path(cas_record["worktree_path"]).exists())
                self.assertEqual(
                    git(cas_repo, "rev-parse", primary_ref),
                    cas_base,
                )
                self.assertEqual(
                    git(
                        cas_repo,
                        "symbolic-ref",
                        "refs/heads/agent/cas",
                    ),
                    primary_ref,
                )
                git(
                    cas_repo,
                    "worktree",
                    "remove",
                    "--force",
                    cas_record["worktree_path"],
                )
                git(
                    cas_repo,
                    "update-ref",
                    "--no-deref",
                    "-d",
                    "refs/heads/agent/cas",
                )

                python_cas_record, _ = create_session_worktree(
                    cas_repo,
                    name="python-cas",
                    base_ref=cas_base,
                    worktree_root=cas_worktrees,
                )
                with mock.patch.dict(
                    os.environ,
                    {
                        "PATH": (
                            str(wrapper_directory)
                            + os.pathsep
                            + os.environ["PATH"]
                        )
                    },
                ):
                    with self.assertRaisesRegex(
                        ContractError,
                        "refusing automatic compensation",
                    ):
                        remove_created_session_worktree(
                            python_cas_record,
                            source_repository=cas_repo,
                        )
                self.assertFalse(
                    Path(python_cas_record["worktree_path"]).exists()
                )
                self.assertEqual(
                    git(
                        cas_repo,
                        "rev-parse",
                        "refs/heads/agent/python-cas",
                    ),
                    advanced_commit,
                )
                git(cas_repo, "branch", "-D", "agent/python-cas")

    @unittest.skipIf(os.name == "nt", "Python worktree registry requires POSIX fcntl")
    def test_manifest_driven_session_creation_matches_python_closure(
        self,
    ) -> None:
        from agent_workflow.worktree_sessions.cli import (
            _capability_closure,
            _platform_contexts,
            _validate_platform_selection,
        )
        from tests.test_worktree_sessions import git, make_repo

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = make_repo(parent)
            base = git(root, "rev-parse", "HEAD")
            worktrees = parent / "worktrees"
            manifest_root = ROOT / "platforms"
            selected = _validate_platform_selection(["apple"], manifest_root)
            expected_contexts = _platform_contexts(selected, manifest_root)
            expected_closure = _capability_closure(
                expected_contexts,
                manifest_root,
            )

            created = self.run_rust(
                "session-create-manifest",
                str(root),
                "manifest-session",
                "--project-id",
                "project",
                "--created-at",
                "2026-07-18T00:00:00+00:00",
                "--platform",
                "apple",
                "--manifest-root",
                str(manifest_root),
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            value = json.loads(created.stdout)
            session = value["session"]
            self.assertEqual(["apple"], session["selected_platforms"])
            self.assertEqual(expected_contexts, session["platform_contexts"])
            self.assertEqual(expected_closure, session["capability_closure"])
            self.assertEqual("active", session["lifecycle"]["state"])
            self.assertEqual(
                SessionRegistry(root).load("manifest-session"),
                session,
            )
            remove_created_session_worktree(
                session["repositories"][0],
                source_repository=root,
            )

            duplicate = self.run_rust(
                "session-create-manifest",
                str(root),
                "duplicate-platform",
                "--project-id",
                "project",
                "--created-at",
                "2026-07-18T00:00:00+00:00",
                "--platform",
                "apple",
                "--platform",
                "apple",
                "--manifest-root",
                str(manifest_root),
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(2, duplicate.returncode)
            self.assertIn("selected platforms must be unique", duplicate.stderr)
            self.assertFalse((worktrees / "duplicate-platform").exists())
            self.assertEqual(
                "",
                git(root, "branch", "--list", "agent/duplicate-platform"),
            )

            invalid_platform = self.run_rust(
                "session-create-manifest",
                str(root),
                "invalid-platform",
                "--project-id",
                "project",
                "--created-at",
                "2026-07-18T00:00:00+00:00",
                "--platform",
                "../apple",
                "--manifest-root",
                str(manifest_root),
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(2, invalid_platform.returncode)
            self.assertIn("invalid platform id", invalid_platform.stderr)
            self.assertFalse((worktrees / "invalid-platform").exists())
            with self.assertRaisesRegex(ContractError, "invalid platform id"):
                _validate_platform_selection(["../apple"], manifest_root)

            missing_root = self.run_rust(
                "session-create-manifest",
                str(root),
                "missing-root",
                "--project-id",
                "project",
                "--created-at",
                "2026-07-18T00:00:00+00:00",
                "--platform",
                "apple",
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(2, missing_root.returncode)
            self.assertIn("explicit trusted Manifest root", missing_root.stderr)
            self.assertFalse((worktrees / "missing-root").exists())

            bootstrap_only = self.run_rust(
                "session-create-manifest",
                str(root),
                "android-bootstrap",
                "--project-id",
                "project",
                "--created-at",
                "2026-07-18T00:00:00+00:00",
                "--platform",
                "android",
                "--manifest-root",
                str(manifest_root),
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(2, bootstrap_only.returncode)
            self.assertIn(
                "bootstrap_required: platform Provider is not implemented: android",
                bootstrap_only.stderr,
            )
            self.assertFalse((worktrees / "android-bootstrap").exists())

            invalid_context = self.run_rust(
                "session-create-manifest",
                str(root),
                "invalid-manifest-context",
                "--project-id",
                "",
                "--created-at",
                "2026-07-18T00:00:00+00:00",
                "--base-ref",
                base,
                "--worktree-root",
                str(worktrees),
            )
            self.assertEqual(2, invalid_context.returncode)
            self.assertIn(
                "session context input project_id is required",
                invalid_context.stderr,
            )
            self.assertFalse(
                (worktrees / "invalid-manifest-context").exists()
            )
            self.assertEqual(
                "",
                git(
                    root,
                    "branch",
                    "--list",
                    "agent/invalid-manifest-context",
                ),
            )

            if os.name != "nt":
                manifest_link = parent / "manifest-link"
                manifest_link.symlink_to(
                    manifest_root,
                    target_is_directory=True,
                )
                unsafe_root = self.run_rust(
                    "session-create-manifest",
                    str(root),
                    "unsafe-root",
                    "--project-id",
                    "project",
                    "--created-at",
                    "2026-07-18T00:00:00+00:00",
                    "--platform",
                    "apple",
                    "--manifest-root",
                    str(manifest_link),
                    "--base-ref",
                    base,
                    "--worktree-root",
                    str(worktrees),
                )
                self.assertEqual(2, unsafe_root.returncode)
                self.assertIn(
                    "explicit trusted Manifest root",
                    unsafe_root.stderr,
                )
                self.assertFalse((worktrees / "unsafe-root").exists())

    @unittest.skipIf(os.name == "nt", "Python worktree registry requires POSIX fcntl")
    def test_session_registry_checkpoint_matches_python_without_committing(
        self,
    ) -> None:
        from tests.test_worktree_sessions import git, make_repo

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = make_repo(parent)
            base = git(root, "rev-parse", "HEAD")
            context = new_session_context(
                session_id="checkpoint",
                project_id="project",
                repositories=[
                    inspect_repository(
                        root,
                        repository_id="app",
                        base_ref=base,
                    )
                ],
            )
            registry = SessionRegistry(root)
            registry.create(context)
            active = registry.transition("checkpoint", "active")
            expected = deepcopy(active)
            freeze_checkpoint(expected)

            (root / "file.txt").write_text("dirty\n", encoding="utf-8")
            rejected = self.run_rust(
                "session-registry-checkpoint",
                str(root),
                "checkpoint",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("clean worktree", rejected.stderr)
            self.assertEqual(
                registry.load("checkpoint")["lifecycle"]["state"],
                "active",
            )
            git(root, "checkout", "--", "file.txt")

            actual = self.run_rust(
                "session-registry-checkpoint",
                str(root),
                "checkpoint",
            )
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(
                json.loads(actual.stdout),
                {
                    "notice": {
                        "commits_created": False,
                        "staging_changed": False,
                    },
                    "operation": "checkpoint",
                    "schema_version": "1.0",
                    "session": expected,
                },
            )
            self.assertEqual(git(root, "rev-parse", "HEAD"), base)
            self.assertEqual(
                registry.load("checkpoint"),
                expected,
            )

            created_context = new_session_context(
                session_id="created-checkpoint",
                project_id="project",
                repositories=[
                    inspect_repository(
                        root,
                        repository_id="app",
                        base_ref=base,
                    )
                ],
            )
            registry.create(created_context)
            (root / "file.txt").write_text("dirty again\n", encoding="utf-8")
            created_rejected = self.run_rust(
                "session-registry-checkpoint",
                str(root),
                "created-checkpoint",
            )
            self.assertEqual(created_rejected.returncode, 2)
            self.assertEqual(
                registry.load("created-checkpoint")["lifecycle"]["state"],
                "created",
            )

    @unittest.skipIf(os.name == "nt", "Python worktree registry requires POSIX fcntl")
    def test_session_final_gate_and_registry_persistence_match_python(
        self,
    ) -> None:
        from tests.test_worktree_sessions import GateTests

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            fixture = GateTests()
            root, context, pairs, ledger, artifacts = fixture._fixture(parent)
            context_path = parent / "context.json"
            pairs_path = parent / "pairs.json"
            ledger_path = parent / "ledger.json"
            request_path = parent / "request.json"
            result_path = parent / "result.json"

            unattached = deepcopy(context)
            unattached["verification"] = {
                "adapter_result_refs": [],
                "status": "pending",
            }
            unattached["review"] = {
                "adapter_result_refs": [],
                "status": "pending",
            }
            expected_attached = deepcopy(unattached)
            attach_adapter_result(
                expected_attached,
                attempt_id=pairs[0]["attempt_id"],
                request=pairs[0]["request"],
                result=pairs[0]["result"],
            )
            dump(unattached, context_path)
            dump(pairs[0]["request"], request_path)
            dump(pairs[0]["result"], result_path)
            actual_attached = self.run_rust(
                "session-gate-attach",
                str(context_path),
                pairs[0]["attempt_id"],
                str(request_path),
                str(result_path),
            )
            self.assertEqual(
                actual_attached.returncode,
                0,
                actual_attached.stderr,
            )
            self.assertEqual(
                json.loads(actual_attached.stdout),
                expected_attached,
            )

            expected_gate = evaluate_session_gate(
                context,
                adapter_pairs=pairs,
                ledger=ledger,
                artifact_root=artifacts,
            )
            dump(context, context_path)
            dump(pairs, pairs_path)
            dump(ledger, ledger_path)
            actual_gate = self.run_rust(
                "session-gate-evaluate",
                str(context_path),
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(actual_gate.returncode, 0, actual_gate.stderr)
            self.assertEqual(json.loads(actual_gate.stdout), expected_gate)

            malformed_ledger = deepcopy(ledger)
            malformed_ledger["node_attempts"].append(
                deepcopy(malformed_ledger["node_attempts"][0])
            )
            dump(malformed_ledger, ledger_path)
            before_malformed = SessionRegistry(root).load(
                context["session_id"]
            )
            malformed = self.run_rust(
                "session-registry-gate",
                str(root),
                context["session_id"],
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(malformed.returncode, 2)
            self.assertEqual(
                SessionRegistry(root).load(context["session_id"]),
                before_malformed,
            )
            dump(ledger, ledger_path)

            persisted = self.run_rust(
                "session-registry-gate",
                str(root),
                context["session_id"],
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(persisted.returncode, 0, persisted.stderr)
            self.assertEqual(json.loads(persisted.stdout), expected_gate)
            self.assertEqual(
                SessionRegistry(root)
                .load(context["session_id"])["lifecycle"]["state"],
                "gated",
            )

            source_file = root / "file.txt"
            source_contents = source_file.read_bytes()
            source_file.write_text("post-checkpoint mutation\n", encoding="utf-8")
            expected_source_blocked = evaluate_session_gate(
                context,
                adapter_pairs=pairs,
                ledger=ledger,
                artifact_root=artifacts,
            )
            source_blocked = self.run_rust(
                "session-gate-evaluate",
                str(context_path),
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(
                source_blocked.returncode,
                0,
                source_blocked.stderr,
            )
            self.assertEqual(
                json.loads(source_blocked.stdout),
                expected_source_blocked,
            )
            source_file.write_bytes(source_contents)

            (artifacts / "verification.json").write_text(
                "tampered\n",
                encoding="utf-8",
            )
            expected_blocked = evaluate_session_gate(
                context,
                adapter_pairs=pairs,
                ledger=ledger,
                artifact_root=artifacts,
            )
            blocked = self.run_rust(
                "session-gate-evaluate",
                str(context_path),
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertEqual(json.loads(blocked.stdout), expected_blocked)

            verification = artifacts / "verification.json"
            verification.unlink()
            expected_missing = evaluate_session_gate(
                context,
                adapter_pairs=pairs,
                ledger=ledger,
                artifact_root=artifacts,
            )
            missing = self.run_rust(
                "session-gate-evaluate",
                str(context_path),
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(json.loads(missing.stdout), expected_missing)

            preserved = artifacts / "preserved.json"
            verification.write_text(
                '{"passed":true}\n',
                encoding="utf-8",
            )
            preserved.write_bytes(verification.read_bytes())
            if os.name != "nt":
                verification.unlink()
                verification.symlink_to(preserved.name)
                expected_symlink = evaluate_session_gate(
                    context,
                    adapter_pairs=pairs,
                    ledger=ledger,
                    artifact_root=artifacts,
                )
                symlinked = self.run_rust(
                    "session-gate-evaluate",
                    str(context_path),
                    str(pairs_path),
                    str(ledger_path),
                    str(artifacts),
                )
                self.assertEqual(
                    symlinked.returncode,
                    0,
                    symlinked.stderr,
                )
                self.assertEqual(
                    json.loads(symlinked.stdout),
                    expected_symlink,
                )
                self.assertEqual(
                    preserved.read_text(encoding="utf-8"),
                    '{"passed":true}\n',
                )
                verification.unlink()
                verification.write_text(
                    '{"passed":true}\n',
                    encoding="utf-8",
                )

                artifact_alias = parent / "artifact-alias"
                artifact_alias.symlink_to(artifacts, target_is_directory=True)
                expected_root_symlink = evaluate_session_gate(
                    context,
                    adapter_pairs=pairs,
                    ledger=ledger,
                    artifact_root=artifact_alias,
                )
                root_symlink = self.run_rust(
                    "session-gate-evaluate",
                    str(context_path),
                    str(pairs_path),
                    str(ledger_path),
                    str(artifact_alias),
                )
                self.assertEqual(
                    root_symlink.returncode,
                    0,
                    root_symlink.stderr,
                )
                self.assertEqual(
                    json.loads(root_symlink.stdout),
                    expected_root_symlink,
                )
                self.assertEqual(expected_root_symlink["status"], "blocked")

                verification.unlink()
                os.mkfifo(verification)
                expected_fifo = evaluate_session_gate(
                    context,
                    adapter_pairs=pairs,
                    ledger=ledger,
                    artifact_root=artifacts,
                )
                fifo = self.run_rust(
                    "session-gate-evaluate",
                    str(context_path),
                    str(pairs_path),
                    str(ledger_path),
                    str(artifacts),
                )
                self.assertEqual(fifo.returncode, 0, fifo.stderr)
                self.assertEqual(json.loads(fifo.stdout), expected_fifo)
                self.assertEqual(expected_fifo["status"], "blocked")
                verification.unlink()
                verification.write_text(
                    '{"passed":true}\n',
                    encoding="utf-8",
                )

            duplicate_pairs = pairs + [deepcopy(pairs[0])]
            dump(duplicate_pairs, pairs_path)
            duplicate_pair = self.run_rust(
                "session-gate-evaluate",
                str(context_path),
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(duplicate_pair.returncode, 2)
            self.assertIn(
                "worktree session adapter pairs must be unique",
                duplicate_pair.stderr,
            )

            dump(pairs, pairs_path)
            duplicate_ledger = deepcopy(ledger)
            duplicate_ledger["node_attempts"].append(
                deepcopy(duplicate_ledger["node_attempts"][0])
            )
            dump(duplicate_ledger, ledger_path)
            duplicate_attempt = self.run_rust(
                "session-gate-evaluate",
                str(context_path),
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(duplicate_attempt.returncode, 2)
            self.assertIn(
                "attempt ids must be globally unique",
                duplicate_attempt.stderr,
            )

            dump(pairs, pairs_path)
            dump(ledger, ledger_path)
            before_gated_failure = SessionRegistry(root).load(
                context["session_id"]
            )
            gated_failure = self.run_rust(
                "session-registry-gate",
                str(root),
                context["session_id"],
                str(pairs_path),
                str(ledger_path),
                str(artifacts),
            )
            self.assertEqual(gated_failure.returncode, 2)
            self.assertIn(
                "requires a checkpointed worktree session",
                gated_failure.stderr,
            )
            self.assertEqual(
                SessionRegistry(root).load(context["session_id"]),
                before_gated_failure,
            )

    @unittest.skipIf(os.name == "nt", "Python worktree registry requires POSIX fcntl")
    def test_git_workspace_patch_and_source_identity_match_python(self) -> None:
        from tests.test_worktree_sessions import commit_all, git, make_repo

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            app = make_repo(parent, "app")
            dependency = make_repo(parent, "dependency")
            helper = parent / "git-helper.sh"
            marker = parent / "git-helper-marker"
            if os.name != "nt":
                helper.write_text(
                    "#!/bin/sh\n"
                    ': > "$(dirname "$0")/git-helper-marker"\n'
                    'if [ "$#" -gt 0 ] && [ -f "$1" ]; then cat "$1"; fi\n'
                    "exit 0\n",
                    encoding="utf-8",
                )
                helper.chmod(0o755)
                (app / ".gitattributes").write_text(
                    "*.txt diff=evil\n",
                    encoding="utf-8",
                )
                git(app, "add", ".gitattributes")
                git(app, "commit", "-q", "-m", "attributes")
            app_base = git(app, "rev-parse", "HEAD")
            dependency_base = git(dependency, "rev-parse", "HEAD")
            if os.name != "nt":
                git(app, "config", "diff.evil.textconv", str(helper))
                git(app, "config", "core.fsmonitor", str(helper))
                (app / "file.txt").write_text(
                    "helper probe\n",
                    encoding="utf-8",
                )
                expected_probe_status = worktree_status(app)
                self.assertFalse(marker.exists())
                actual_probe_status = self.run_rust(
                    "worktree-status",
                    str(app),
                )
                self.assertEqual(
                    actual_probe_status.returncode,
                    0,
                    actual_probe_status.stderr,
                )
                self.assertEqual(
                    json.loads(actual_probe_status.stdout),
                    expected_probe_status,
                )
                self.assertFalse(marker.exists())
                (app / "file.txt").write_text("base\n", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(dependency / ".git"),
                    "GIT_WORK_TREE": str(dependency),
                },
            ):
                expected_bound = inspect_repository(
                    app,
                    repository_id="app",
                    role="primary",
                    base_ref=app_base,
                )
                actual_bound = self.run_rust(
                    "repository-inspect",
                    str(app),
                    "app",
                    "--base-ref",
                    app_base,
                )
            self.assertEqual(actual_bound.returncode, 0, actual_bound.stderr)
            self.assertEqual(json.loads(actual_bound.stdout), expected_bound)
            self.assertEqual(expected_bound["worktree_path"], str(app.resolve()))
            self.assertFalse(marker.exists())

            expected_status = worktree_status(app)
            actual_status = self.run_rust("worktree-status", str(app))
            self.assertEqual(actual_status.returncode, 0, actual_status.stderr)
            self.assertEqual(json.loads(actual_status.stdout), expected_status)

            (app / "file.txt").write_text("staged\n", encoding="utf-8")
            git(app, "add", "file.txt")
            (app / "file.txt").write_text(
                "unstaged-after-index\n",
                encoding="utf-8",
            )
            (app / "new.bin").write_bytes(b"one\x00two")
            if os.name != "nt":
                (app / "new-link").symlink_to("file.txt")

            expected_status = worktree_status(app)
            actual_status = self.run_rust("worktree-status", str(app))
            self.assertEqual(actual_status.returncode, 0, actual_status.stderr)
            self.assertEqual(json.loads(actual_status.stdout), expected_status)

            expected_patch = repository_patch(
                app,
                repository_id="app",
                base_commit=app_base,
            )
            actual_patch = self.run_rust(
                "repository-patch",
                str(app),
                "app",
                app_base,
            )
            self.assertEqual(actual_patch.returncode, 0, actual_patch.stderr)
            self.assertEqual(json.loads(actual_patch.stdout), expected_patch)

            expected_app = inspect_repository(
                app,
                repository_id="app",
                role="primary",
                base_ref=app_base,
            )
            actual_app = self.run_rust(
                "repository-inspect",
                str(app),
                "app",
                "--base-ref",
                app_base,
            )
            self.assertEqual(actual_app.returncode, 0, actual_app.stderr)
            self.assertEqual(json.loads(actual_app.stdout), expected_app)

            (dependency / "file.txt").write_text(
                "dependency change\n",
                encoding="utf-8",
            )
            expected_dependency = inspect_repository(
                dependency,
                repository_id="dependency",
                role="dependency",
                base_ref=dependency_base,
            )
            actual_dependency = self.run_rust(
                "repository-inspect",
                str(dependency),
                "dependency",
                "--role",
                "dependency",
                "--base-ref",
                dependency_base,
            )
            self.assertEqual(
                actual_dependency.returncode,
                0,
                actual_dependency.stderr,
            )
            self.assertEqual(
                json.loads(actual_dependency.stdout),
                expected_dependency,
            )

            repositories = [expected_dependency, expected_app]
            repositories_path = parent / "repositories.json"
            dump(repositories, repositories_path)
            actual_identity = self.run_rust(
                "session-source-identity",
                str(repositories_path),
            )
            self.assertEqual(
                actual_identity.returncode,
                0,
                actual_identity.stderr,
            )
            self.assertEqual(
                json.loads(actual_identity.stdout),
                session_source_identity(repositories, mode="working"),
            )
            repositories.reverse()
            dump(repositories, repositories_path)
            reversed_identity = self.run_rust(
                "session-source-identity",
                str(repositories_path),
            )
            self.assertEqual(
                reversed_identity.returncode,
                0,
                reversed_identity.stderr,
            )
            self.assertEqual(reversed_identity.stdout, actual_identity.stdout)

            malformed_repositories = deepcopy(repositories)
            malformed_repositories[0]["change_set"]["patch_hash"] = (
                "repository-patch:not-a-digest"
            )
            with self.assertRaises(ContractError):
                session_source_identity(
                    malformed_repositories,
                    mode="working",
                )
            dump(malformed_repositories, repositories_path)
            malformed_identity = self.run_rust(
                "session-source-identity",
                str(repositories_path),
            )
            self.assertEqual(malformed_identity.returncode, 2)
            dump(repositories, repositories_path)

            commit_all(app, "feature")
            expected_committed = inspect_repository(
                app,
                repository_id="app",
                role="primary",
                base_ref=app_base,
                committed=True,
            )
            actual_committed = self.run_rust(
                "repository-inspect",
                str(app),
                "app",
                "--base-ref",
                app_base,
                "--committed",
            )
            self.assertEqual(
                actual_committed.returncode,
                0,
                actual_committed.stderr,
            )
            self.assertEqual(
                json.loads(actual_committed.stdout),
                expected_committed,
            )

            context_input = {
                "capability_closure": {},
                "created_at": "2026-07-18T00:00:00+00:00",
                "dependencies": [],
                "platform_contexts": {},
                "project_id": "project",
                "repositories": [expected_app],
                "selected_platforms": [],
                "session_id": "feature-a",
            }
            expected_context = new_session_context(**context_input)
            context_input_path = parent / "session-input.json"
            context_path = parent / "session-context.json"
            dump(context_input, context_input_path)
            actual_context = self.run_rust(
                "session-context-create",
                str(context_input_path),
            )
            self.assertEqual(
                actual_context.returncode,
                0,
                actual_context.stderr,
            )
            self.assertEqual(
                json.loads(actual_context.stdout),
                expected_context,
            )
            dump(expected_context, context_path)
            validated = self.run_rust(
                "session-context-validate",
                str(context_path),
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(validated.stdout, dumps(expected_context))

            active_context = deepcopy(expected_context)
            active_context["lifecycle"]["state"] = "active"
            validate_worktree_session_context(active_context)
            dump(expected_context, context_path)
            actual_active = self.run_rust(
                "session-context-transition",
                str(context_path),
                "active",
            )
            self.assertEqual(actual_active.returncode, 0, actual_active.stderr)
            self.assertEqual(json.loads(actual_active.stdout), active_context)

            expected_frozen = deepcopy(active_context)
            freeze_checkpoint(expected_frozen)
            dump(active_context, context_path)
            actual_frozen = self.run_rust(
                "session-context-freeze",
                str(context_path),
            )
            self.assertEqual(actual_frozen.returncode, 0, actual_frozen.stderr)
            self.assertEqual(
                json.loads(actual_frozen.stdout),
                expected_frozen,
            )

            expected_reopened = deepcopy(expected_frozen)
            expected_reopened["source_identity"]["mode"] = "working"
            refresh_session_source_identity(expected_reopened)
            expected_reopened["verification"] = {
                "adapter_result_refs": [],
                "status": "pending",
            }
            expected_reopened["review"] = {
                "adapter_result_refs": [],
                "status": "pending",
            }
            expected_reopened["lifecycle"]["state"] = "active"
            validate_worktree_session_context(expected_reopened)
            dump(expected_frozen, context_path)
            actual_reopened = self.run_rust(
                "session-context-transition",
                str(context_path),
                "active",
            )
            self.assertEqual(
                actual_reopened.returncode,
                0,
                actual_reopened.stderr,
            )
            self.assertEqual(
                json.loads(actual_reopened.stdout),
                expected_reopened,
            )

            registry_root = (
                Path(expected_context["repositories"][0]["git_common_dir"])
                / "agent-sessions"
            )

            def reset_registry() -> None:
                if registry_root.exists() or registry_root.is_symlink():
                    if registry_root.is_symlink():
                        registry_root.unlink()
                    else:
                        shutil.rmtree(registry_root)

            empty_list = self.run_rust(
                "session-registry-list",
                str(app),
            )
            self.assertEqual(empty_list.returncode, 0, empty_list.stderr)
            self.assertEqual(json.loads(empty_list.stdout), [])
            self.assertFalse(registry_root.exists())

            python_registry = SessionRegistry(app)
            expected_created = python_registry.create(expected_context)
            reset_registry()
            dump(expected_context, context_path)
            actual_created = self.run_rust(
                "session-registry-create",
                str(app),
                str(context_path),
            )
            self.assertEqual(
                actual_created.returncode,
                0,
                actual_created.stderr,
            )
            self.assertEqual(
                json.loads(actual_created.stdout),
                expected_created,
            )
            actual_loaded = self.run_rust(
                "session-registry-load",
                str(app),
                expected_context["session_id"],
            )
            self.assertEqual(actual_loaded.returncode, 0, actual_loaded.stderr)
            self.assertEqual(json.loads(actual_loaded.stdout), expected_context)
            actual_list = self.run_rust(
                "session-registry-list",
                str(app),
            )
            self.assertEqual(actual_list.returncode, 0, actual_list.stderr)
            self.assertEqual(json.loads(actual_list.stdout), [expected_context])
            duplicate = self.run_rust(
                "session-registry-create",
                str(app),
                str(context_path),
            )
            self.assertEqual(duplicate.returncode, 2)
            self.assertEqual(duplicate.stdout, "")

            reset_registry()
            python_registry = SessionRegistry(app)
            python_registry.create(expected_context)
            expected_registry_active = python_registry.transition(
                expected_context["session_id"],
                "active",
            )
            reset_registry()
            created = self.run_rust(
                "session-registry-create",
                str(app),
                str(context_path),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            actual_registry_active = self.run_rust(
                "session-registry-transition",
                str(app),
                expected_context["session_id"],
                "active",
            )
            self.assertEqual(
                actual_registry_active.returncode,
                0,
                actual_registry_active.stderr,
            )
            self.assertEqual(
                json.loads(actual_registry_active.stdout),
                expected_registry_active,
            )

            reset_registry()
            python_registry = SessionRegistry(app)
            python_registry.create(expected_context)
            python_registry.transition(
                expected_context["session_id"],
                "active",
            )
            expected_registry_frozen = python_registry.write(expected_frozen)
            reset_registry()
            created = self.run_rust(
                "session-registry-create",
                str(app),
                str(context_path),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            transitioned = self.run_rust(
                "session-registry-transition",
                str(app),
                expected_context["session_id"],
                "active",
            )
            self.assertEqual(transitioned.returncode, 0, transitioned.stderr)
            dump(expected_frozen, context_path)
            actual_registry_frozen = self.run_rust(
                "session-registry-write",
                str(app),
                str(context_path),
            )
            self.assertEqual(
                actual_registry_frozen.returncode,
                0,
                actual_registry_frozen.stderr,
            )
            self.assertEqual(
                json.loads(actual_registry_frozen.stdout),
                expected_registry_frozen,
            )
            immutable_drift = deepcopy(expected_registry_frozen)
            immutable_drift["project_id"] = "different-project"
            with self.assertRaisesRegex(
                ContractError,
                "immutable registry identity changed",
            ):
                SessionRegistry(app).write(immutable_drift)
            dump(immutable_drift, context_path)
            rejected_drift = self.run_rust(
                "session-registry-write",
                str(app),
                str(context_path),
            )
            self.assertEqual(rejected_drift.returncode, 2)
            self.assertEqual(rejected_drift.stdout, "")
            self.assertIn(
                "immutable registry identity changed",
                rejected_drift.stderr,
            )

            if os.name != "nt":
                reset_registry()
                registry_root.mkdir()
                lock_target = parent / "foreign-lock"
                lock_target.write_text("foreign", encoding="utf-8")
                (registry_root / ".registry.lock").symlink_to(lock_target)
                with self.assertRaisesRegex(ContractError, "lock is unsafe"):
                    SessionRegistry(app).create(expected_context)
                dump(expected_context, context_path)
                rejected_lock = self.run_rust(
                    "session-registry-create",
                    str(app),
                    str(context_path),
                )
                self.assertEqual(rejected_lock.returncode, 2)
                self.assertEqual(rejected_lock.stdout, "")
                self.assertIn("lock is unsafe", rejected_lock.stderr)
                self.assertEqual(
                    lock_target.read_text(encoding="utf-8"),
                    "foreign",
                )
                reset_registry()

            invalid_context = deepcopy(expected_context)
            invalid_context["unexpected"] = True
            with self.assertRaises(ContractError):
                validate_worktree_session_context(invalid_context)
            dump(invalid_context, context_path)
            rejected_context = self.run_rust(
                "session-context-validate",
                str(context_path),
            )
            self.assertEqual(rejected_context.returncode, 2)
            self.assertEqual(rejected_context.stdout, "")

            git(
                dependency,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{dependency_base},vendor/dependency",
            )
            with self.assertRaisesRegex(
                ContractError,
                "does not support Git submodules",
            ):
                repository_patch(
                    dependency,
                    repository_id="dependency",
                    base_commit=dependency_base,
                )
            rejected = self.run_rust(
                "repository-patch",
                str(dependency),
                "dependency",
                dependency_base,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertIn(
                "does not support Git submodules",
                rejected.stderr,
            )

    def test_fake_runtime_semantics_resume_and_lock_match_python(self) -> None:
        registry = ManifestRegistry.from_directory(ROOT / "platforms")
        profile = DiscoveryEngine(registry).discover(
            ROOT / "tests/fixtures/apple-app"
        )
        policy = PolicyResolver().resolve(profile, "实现 iOS 功能")
        plan = PlanCompiler(registry).compile(profile, policy)
        cases = (
            ({}, {}, plan),
            ({"implementation.apple": "failed"}, {}, plan),
            (
                {
                    "verification.apple.affected-tests": [
                        "failed",
                        "passed",
                    ]
                },
                {},
                plan,
            ),
            (
                {"verification.apple.affected-tests": "timed-out"},
                {},
                plan,
            ),
            ({"implementation.apple": "cancelled"}, {}, plan),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            behaviors_path = root / "behaviors.json"
            approvals_path = root / "approvals.json"
            for index, (behaviors, approvals, candidate) in enumerate(cases):
                with self.subTest(case=index):
                    dump(candidate, plan_path)
                    dump(behaviors, behaviors_path)
                    dump(approvals, approvals_path)
                    expected = FakeAdapterExecutor(
                        behaviors,
                        approval_decisions=approvals,
                    ).run(candidate)
                    result = self.run_rust(
                        "runtime-execute",
                        str(plan_path),
                        "--behaviors",
                        str(behaviors_path),
                        "--approvals",
                        str(approvals_path),
                        "--identity-seed",
                        "0123456789abcdef",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        _normalize_runtime_ledger(json.loads(result.stdout)),
                        _normalize_runtime_ledger(expected),
                    )

            optional = deepcopy(plan)
            verification = next(
                node for node in optional["nodes"] if node["id"] == "apple-2"
            )
            verification["mandatory"] = False
            verification["provider"] = None
            optional["status"] = "degraded"
            optional["missing_capabilities"] = [verification["capability"]]
            blocked = deepcopy(plan)
            blocked["status"] = "blocked"
            blocked["missing_capabilities"] = ["routing.platform-selection"]
            for index, candidate in enumerate((optional, blocked)):
                with self.subTest(plan_status=index):
                    dump(candidate, plan_path)
                    expected = FakeAdapterExecutor().run(candidate)
                    result = self.run_rust(
                        "runtime-execute",
                        str(plan_path),
                        "--identity-seed",
                        "0123456789abcdef",
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        _normalize_runtime_ledger(json.loads(result.stdout)),
                        _normalize_runtime_ledger(expected),
                    )

            approval_plan = deepcopy(plan)
            intent = next(
                node for node in approval_plan["nodes"] if node["id"] == "intent"
            )
            intent["approval"] = {
                "action": "repository-read",
                "reason": "fixture",
                "scope": {"root": "."},
            }
            dump(approval_plan, plan_path)
            python_ledger = root / "python-ledger.jsonl"
            rust_ledger = root / "rust-ledger.jsonl"
            first_python = FakeAdapterExecutor().run(
                approval_plan,
                ledger_path=python_ledger,
            )
            first_rust = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--ledger",
                str(rust_ledger),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(first_rust.returncode, 0, first_rust.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(first_rust.stdout)),
                _normalize_runtime_ledger(first_python),
            )
            dump({"core.intent-lock": "granted"}, approvals_path)
            resumed_python = FakeAdapterExecutor(
                approval_decisions={"core.intent-lock": "granted"}
            ).run(
                approval_plan,
                ledger_path=python_ledger,
                resume=True,
            )
            resumed_rust = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--approvals",
                str(approvals_path),
                "--ledger",
                str(rust_ledger),
                "--resume",
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(resumed_rust.returncode, 0, resumed_rust.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(resumed_rust.stdout)),
                _normalize_runtime_ledger(resumed_python),
            )

            retry_python_ledger = root / "retry-python-ledger.jsonl"
            retry_rust_ledger = root / "retry-rust-ledger.jsonl"
            dump(plan, plan_path)
            dump({"implementation.apple": "failed"}, behaviors_path)
            first_python = FakeAdapterExecutor(
                {"implementation.apple": "failed"}
            ).run(
                plan,
                ledger_path=retry_python_ledger,
            )
            first_rust = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--behaviors",
                str(behaviors_path),
                "--ledger",
                str(retry_rust_ledger),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(first_rust.returncode, 0, first_rust.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(first_rust.stdout)),
                _normalize_runtime_ledger(first_python),
            )
            resumed_python = FakeAdapterExecutor().run(
                plan,
                ledger_path=retry_python_ledger,
                resume=True,
            )
            resumed_rust = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--ledger",
                str(retry_rust_ledger),
                "--resume",
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(resumed_rust.returncode, 0, resumed_rust.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(resumed_rust.stdout)),
                _normalize_runtime_ledger(resumed_python),
            )

            install = build_install_bundle(
                ROOT / "platforms",
                platforms=["apple"],
            )
            locked = PlanCompiler(registry).compile(
                profile,
                policy,
                package_lock=install.package_lock,
            )
            lock_path = root / "agent-skills.lock"
            dump(locked, plan_path)
            dump(install.package_lock, lock_path)
            expected = FakeAdapterExecutor().run(
                locked,
                package_lock=install.package_lock,
            )
            result = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--lock",
                str(lock_path),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                _normalize_runtime_ledger(json.loads(result.stdout)),
                _normalize_runtime_ledger(expected),
            )

            missing_lock = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(missing_lock.returncode, 2)
            self.assertEqual(missing_lock.stdout, "")
            self.assertIn("requires the current package Lockfile", missing_lock.stderr)

            invalid_lock_identity = deepcopy(plan)
            invalid_lock_identity["package_lock_hash"] = 0
            dump(invalid_lock_identity, plan_path)
            invalid_ledger = root / "invalid-lock-ledger.jsonl"
            rejected = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--ledger",
                str(invalid_ledger),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("package_lock_hash is invalid", rejected.stderr)
            self.assertFalse(invalid_ledger.exists())

            unbounded = deepcopy(plan)
            verification = next(
                node for node in unbounded["nodes"] if node["id"] == "apple-2"
            )
            verification["idempotent"] = True
            verification["max_retries"] = 100_000
            dump(unbounded, plan_path)
            with self.assertRaisesRegex(
                ContractError,
                "projected events exceed maximum",
            ):
                FakeAdapterExecutor().run(unbounded)
            rejected = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("projected events exceed maximum", rejected.stderr)

            cyclic = deepcopy(plan)
            cyclic["edges"].append({"from": "report", "to": "intent"})
            dump(cyclic, plan_path)
            with self.assertRaisesRegex(ContractError, "dependency cycle"):
                FakeAdapterExecutor().run(cyclic)
            rejected = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("dependency cycle", rejected.stderr)

            if os.name != "nt":
                unmanaged = root / "unmanaged-ledger"
                unmanaged.write_text("unmanaged\n", encoding="utf-8")
                linked_ledger = root / "linked-ledger"
                linked_ledger.symlink_to(unmanaged)
                dump(plan, plan_path)
                rejected = self.run_rust(
                    "runtime-execute",
                    str(plan_path),
                    "--ledger",
                    str(linked_ledger),
                    "--identity-seed",
                    "0123456789abcdef",
                )
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(rejected.stdout, "")
                self.assertIn("regular file", rejected.stderr)
                self.assertEqual(
                    unmanaged.read_text(encoding="utf-8"),
                    "unmanaged\n",
                )
                linked_parent = root / "linked-parent"
                external_parent = root / "external-parent"
                external_parent.mkdir()
                linked_parent.symlink_to(external_parent, target_is_directory=True)
                dump(plan, plan_path)
                rejected = self.run_rust(
                    "runtime-execute",
                    str(plan_path),
                    "--ledger",
                    str(linked_parent / "ledger.jsonl"),
                    "--identity-seed",
                    "0123456789abcdef",
                )
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(rejected.stdout, "")
                self.assertIn("real directories", rejected.stderr)
                self.assertFalse((external_parent / "ledger.jsonl").exists())

            invalid_artifact_ledger = root / "invalid-artifact-ledger.jsonl"
            shutil.copy2(rust_ledger, invalid_artifact_ledger)
            ledger_run_id = json.loads(
                rust_ledger.read_text(encoding="utf-8").splitlines()[0]
            )["run_id"]
            with invalid_artifact_ledger.open("a", encoding="utf-8") as handle:
                handle.write(
                    dumps(
                        {
                            "event_type": "artifact-hash",
                            "run_id": ledger_run_id,
                            "value": {},
                        }
                    )
                )
            dump(approval_plan, plan_path)
            rejected = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--ledger",
                str(invalid_artifact_ledger),
                "--resume",
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("artifact-hash fields are invalid", rejected.stderr)

            blank_line_ledger = root / "blank-line-ledger.jsonl"
            valid_lines = rust_ledger.read_text(encoding="utf-8").splitlines()
            blank_line_ledger.write_text(
                valid_lines[0] + "\n\n" + "\n".join(valid_lines[1:]) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(json.JSONDecodeError):
                RunLedger.replay(
                    blank_line_ledger,
                    approval_plan["fingerprint"],
                )
            rejected = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--ledger",
                str(blank_line_ledger),
                "--resume",
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertTrue(rejected.stderr)

            no_start_ledger = root / "no-start-ledger.jsonl"
            source_events = [
                json.loads(line)
                for line in rust_ledger.read_text(encoding="utf-8").splitlines()
            ]
            node_event = next(
                event
                for event in source_events
                if event["event_type"] == "node-attempt"
            )
            no_start_ledger.write_text(dumps(node_event), encoding="utf-8")
            rejected = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--ledger",
                str(no_start_ledger),
                "--resume",
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("exactly one run-started", rejected.stderr)

            with rust_ledger.open("a", encoding="utf-8") as handle:
                handle.write(
                    dumps(
                        {
                            "event_type": "invented-event",
                            "run_id": ledger_run_id,
                            "value": {},
                        }
                    )
                )
            dump(approval_plan, plan_path)
            rejected = self.run_rust(
                "runtime-execute",
                str(plan_path),
                "--ledger",
                str(rust_ledger),
                "--resume",
                "--identity-seed",
                "0123456789abcdef",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("unknown ledger event type", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
