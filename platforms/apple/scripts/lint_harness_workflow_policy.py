#!/usr/bin/env python3
"""Validate the shared workflow/review foundation and the thin Apple overlays."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


APPLE_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = APPLE_ROOT.parents[1]
WORKFLOW_SKILL = REPOSITORY_ROOT / "disciplines" / "workflow" / "skills" / "workflow-orchestration"
REVIEW_SKILL = REPOSITORY_ROOT / "disciplines" / "review" / "skills" / "code-review"
APPLE_WORKFLOW_SKILL = APPLE_ROOT / "skills" / "apple-orchestration"
APPLE_REVIEW_SKILL = APPLE_ROOT / "skills" / "apple-code-review"
SHARED_AGENT_DIRS = [
    REPOSITORY_ROOT / "disciplines" / "workflow" / "assets" / "codex" / "agents",
    REPOSITORY_ROOT / "disciplines" / "review" / "assets" / "codex" / "agents",
]
APPLE_AGENT_DIR = APPLE_ROOT / "config" / "codex" / "templates" / "agents"
VALIDATOR = APPLE_ROOT / "scripts" / "validate_codex_agent_templates.py"


def display(path: Path) -> str:
    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return str(path)


def require_contains(path: Path, snippets: list[str], failures: list[str]) -> None:
    if not path.is_file():
        failures.append(f"{display(path)} missing")
        return
    text = path.read_text(encoding="utf-8")
    missing = [snippet for snippet in snippets if snippet not in text]
    if missing:
        failures.append(f"{display(path)} missing: {', '.join(missing)}")


def require_not_exists(path: Path, failures: list[str]) -> None:
    if path.exists():
        failures.append(f"{display(path)} must not exist")


def validate_agent_templates(failures: list[str]) -> None:
    paths = [*SHARED_AGENT_DIRS, APPLE_AGENT_DIR]
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), *(str(path) for path in paths)],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        failures.append(result.stderr.strip() or result.stdout.strip())


def main() -> int:
    failures: list[str] = []
    require_contains(
        WORKFLOW_SKILL / "SKILL.md",
        [
            "platform-neutral",
            "## Task Classification",
            "## Checkpoints",
            "`CP0 Intent Lock`",
            "`CP1 Anchor Slice`",
            "`CP2 Validation Baseline Freeze`",
            "`CP3 Final Gate`",
            "fail-fix-report",
            "独立 reviewer subAgent",
            "不在本 Skill 中硬编码平台 Skill 名",
        ],
        failures,
    )
    for reference in (
        "checkpoint-contract.md",
        "handoff-loop.md",
        "model-selection.md",
        "prompt-templates.md",
        "role-contracts.md",
    ):
        require_contains(WORKFLOW_SKILL / "references" / reference, ["#"], failures)
    require_contains(
        REVIEW_SKILL / "SKILL.md",
        ["跨平台", "独立 reviewer subAgent", "只读", "阻塞问题：无", "review.<platform>.*"],
        failures,
    )
    require_contains(
        APPLE_WORKFLOW_SKILL / "SKILL.md",
        [
            "Apple 工作流 Overlay",
            "workflow-orchestration",
            "ios-feature-implementation",
            "apple-verification",
            "code-review",
            "apple-code-review",
            "Verification Session",
            "codex_verify",
            "本地 `:path`",
        ],
        failures,
    )
    require_contains(
        APPLE_REVIEW_SKILL / "SKILL.md",
        ["Apple", "code-review", "Swift", "MainActor"],
        failures,
    )
    require_contains(
        APPLE_ROOT / "skills" / "TAXONOMY.md",
        [
            "共享 Discipline",
            "workflow-orchestration",
            "code-review",
            "apple-code-review",
            "apple-orchestration",
        ],
        failures,
    )
    require_not_exists(APPLE_ROOT / "skills" / "code-review", failures)
    for name in ("explorer.toml", "pm.toml", "reporter.toml"):
        require_contains(SHARED_AGENT_DIRS[0] / name, [f'name = "{name[:-5]}"'], failures)
        require_not_exists(APPLE_AGENT_DIR / name, failures)
    require_contains(SHARED_AGENT_DIRS[1] / "reviewer.toml", ['name = "reviewer"'], failures)
    require_not_exists(APPLE_AGENT_DIR / "reviewer.toml", failures)
    for name in ("builder.toml", "tester.toml"):
        require_contains(APPLE_AGENT_DIR / name, [f'name = "{name[:-5]}"'], failures)
    validate_agent_templates(failures)
    if failures:
        print("harness workflow policy lint failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("harness workflow policy lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
