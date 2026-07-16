#!/usr/bin/env python3
"""Run the deterministic Core and Provider conformance gates."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    commands = [
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
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    print("PASS Phase 4 Core/Provider/Design/QA/Desktop conformance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
