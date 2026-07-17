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

from agent_workflow.canonical_json import dumps, sha256
from agent_workflow.canonical_json import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
)


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

        for arguments in ((), ("unknown",), ("canonicalize",)):
            with self.subTest(arguments=arguments):
                result = self.run_rust(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
