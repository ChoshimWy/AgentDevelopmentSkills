#!/usr/bin/env python3
"""Run the deterministic Core and Provider conformance gates."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_workflow.canonical_json import dumps, load, sha256  # noqa: E402
from agent_workflow.package_lock import schema_inventory, validate_package_lock  # noqa: E402
from agent_workflow.upgrade import make_upgrade_conformance_evidence  # noqa: E402


def commands() -> list[list[str]]:
    return [
        [sys.executable, "scripts/validate_schemas.py"],
        [sys.executable, "scripts/validate_manifests.py"],
        [sys.executable, "scripts/build_design_phase3_fixtures.py", "--check"],
        [sys.executable, "scripts/build_phase4_qa_goldens.py", "--check"],
        [sys.executable, "scripts/build_phase4_desktop_goldens.py", "--check"],
        [sys.executable, "scripts/validate_skill_naming.py"],
        [sys.executable, "scripts/build_migration_audit.py", "--check"],
        [sys.executable, "scripts/validate_apple_package.py"],
        [sys.executable, "scripts/compare_apple_routes.py"],
        [sys.executable, "scripts/run_apple_dual_route_smoke.py"],
        [sys.executable, "scripts/run_ios_installed_workflow_smoke.py"],
        [sys.executable, "scripts/run_desktop_installed_workflow_smoke.py"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "platforms/apple/skills"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "disciplines/documentation/skills"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "disciplines/design/skills"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "disciplines/git/skills"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "disciplines/review/skills"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "disciplines/workflow/skills"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "disciplines/qa/skills", "--strict"],
        [sys.executable, "platforms/apple/scripts/lint_skill_schema.py", "--skills-dir", "platforms/desktop/skills", "--strict"],
        [sys.executable, "platforms/apple/scripts/lint_harness_workflow_policy.py"],
        [sys.executable, "platforms/apple/scripts/lint_subagent_orchestration_policy.py"],
        [sys.executable, "platforms/apple/scripts/lint_verify_ios_build_policy.py"],
        [sys.executable, "platforms/apple/scripts/lint_workflow_contract_policy.py"],
        [sys.executable, "platforms/apple/scripts/check_codex_model_policy.py", "--offline"],
        [
            sys.executable,
            "platforms/apple/scripts/validate_codex_agent_templates.py",
            "platforms/apple/config/codex/templates/agents",
            "disciplines/workflow/assets/codex/agents",
            "disciplines/review/assets/codex/agents",
            "disciplines/design/assets/codex/agents",
        ],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests"],
    ]


def _command_identity(command: list[str]) -> str:
    return " ".join(command[1:])


def _run_with_evidence(candidate_lock_path: Path) -> int:
    package_lock = load(candidate_lock_path)
    validate_package_lock(package_lock)
    actual_schema_inventory = schema_inventory(ROOT / "schemas")
    if package_lock["schema_inventory"] != actual_schema_inventory:
        print("candidate Lock schema inventory differs from the Conformance source root", file=sys.stderr)
        return 2
    results: list[dict[str, object]] = []
    test_count: int | None = None
    manifest_count: int | None = None
    for command in commands():
        completed = subprocess.run(command, cwd=ROOT, check=False, capture_output=True)
        stdout = completed.stdout
        stderr = completed.stderr
        if completed.returncode:
            sys.stderr.buffer.write(stdout)
            sys.stderr.buffer.write(stderr)
            return completed.returncode
        identity = _command_identity(command)
        results.append(
            {
                "command": identity,
                "exit_code": 0,
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            }
        )
        if command[1:4] == ["-m", "unittest", "discover"]:
            match = re.search(rb"Ran (\d+) tests?", stdout + stderr)
            if match is not None:
                test_count = int(match.group(1))
        if command[1:] == ["scripts/validate_manifests.py"]:
            match = re.search(rb"PASS (\d+) manifest", stdout + stderr)
            if match is not None:
                manifest_count = int(match.group(1))
    if test_count is None or test_count < 1 or manifest_count is None or manifest_count < 1:
        print("Conformance manifest or unittest count was not observed", file=sys.stderr)
        return 2
    definitions = sorted(_command_identity(command) for command in commands())
    evidence = make_upgrade_conformance_evidence(
        package_lock,
        manifest_count=manifest_count,
        negative_contract_count=len(list((ROOT / "tests/golden/invalid").glob("*.json"))),
        test_count=test_count,
        suite_definition_hash=sha256(definitions),
        runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        environment={
            "platform": sys.platform,
            "python": ".".join(str(item) for item in sys.version_info[:3]),
        },
        command_results=sorted(results, key=lambda item: str(item["command"])),
    )
    print(dumps(evidence), end="")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upgrade-lock",
        type=Path,
        help="run all gates and emit a candidate-bound Upgrade Conformance evidence artifact",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.upgrade_lock is not None:
        return _run_with_evidence(args.upgrade_lock)
    for command in commands():
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    print("PASS Core/Provider/Design/QA/Desktop/Phase 6 Lockfile, Doctor and lifecycle conformance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
