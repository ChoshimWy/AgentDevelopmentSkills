#!/usr/bin/env python3
"""Validate platform-neutral orchestration and Apple routing boundaries."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys


APPLE_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = APPLE_ROOT.parents[1]
WORKFLOW_ROOT = REPOSITORY_ROOT / "disciplines" / "workflow"
REVIEW_ROOT = REPOSITORY_ROOT / "disciplines" / "review"
APPLE_WORKFLOW = APPLE_ROOT / "skills" / "apple-orchestration"


def display(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def require_contains(path: Path, snippets: list[str], failures: list[str]) -> None:
    if not path.is_file():
        failures.append(f"{display(path)} missing")
        return
    text = path.read_text(encoding="utf-8")
    missing = [snippet for snippet in snippets if snippet not in text]
    if missing:
        failures.append(f"{display(path)} missing: {', '.join(missing)}")


def require_not_contains(path: Path, snippets: list[str], failures: list[str]) -> None:
    if not path.is_file():
        failures.append(f"{display(path)} missing")
        return
    text = path.read_text(encoding="utf-8")
    present = [snippet for snippet in snippets if snippet in text]
    if present:
        failures.append(f"{display(path)} must not contain: {', '.join(present)}")


def require_binding(
    manifest: dict[str, object], capability: str, skill: str, mode: str | None, failures: list[str]
) -> None:
    bindings = manifest.get("bindings")
    binding = bindings.get(capability) if isinstance(bindings, dict) else None
    if (
        not isinstance(binding, dict)
        or binding.get("name") != skill
        or (mode is not None and binding.get("mode") != mode)
    ):
        failures.append(f"{manifest.get('id')} binding {capability} must target {skill}:{mode}")


def main() -> int:
    failures: list[str] = []
    workflow_manifest = json.loads((WORKFLOW_ROOT / "manifest.json").read_text(encoding="utf-8"))
    review_manifest = json.loads((REVIEW_ROOT / "manifest.json").read_text(encoding="utf-8"))
    apple_manifest = json.loads((APPLE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    provider_manifest = json.loads((APPLE_ROOT / "provider" / "manifest.json").read_text(encoding="utf-8"))

    require_binding(workflow_manifest, "workflow.analysis", "workflow-orchestration", "analyze", failures)
    require_binding(workflow_manifest, "workflow.orchestration", "workflow-orchestration", "orchestrate", failures)
    require_binding(workflow_manifest, "reporting.delivery", "workflow-orchestration", "report", failures)
    require_binding(review_manifest, "review.independent", "code-review", None, failures)
    require_binding(provider_manifest, "analysis.apple", "apple-orchestration", "analysis-only", failures)
    require_binding(provider_manifest, "review.apple.static", "apple-code-review", None, failures)

    requires = {item["id"] for item in apple_manifest.get("package_requires", [])}
    for package in ("workflow", "review"):
        if package not in requires:
            failures.append(f"apple package must depend on {package}")

    shared_skill = WORKFLOW_ROOT / "skills" / "workflow-orchestration" / "SKILL.md"
    require_contains(
        shared_skill,
        [
            "platform-neutral",
            "`doc-only` / `rule-only`",
            "`code-small` / `code-medium`",
            "`code-risky`",
            "独立 reviewer subAgent",
            "同类失败",
            "不在本 Skill 中硬编码平台 Skill 名",
        ],
        failures,
    )
    require_not_contains(
        shared_skill,
        ["xcodebuild", "codex_verify", "apple-verification", "ios-feature-implementation"],
        failures,
    )
    require_contains(
        APPLE_WORKFLOW / "SKILL.md",
        [
            "Apple 工作流 Overlay",
            "workflow-orchestration",
            "ios-feature-implementation",
            "apple-verification",
            "apple-code-review",
            "Verification Session",
            "CocoaPods",
        ],
        failures,
    )
    require_contains(
        APPLE_WORKFLOW / "agents" / "openai.yaml",
        ["$workflow-orchestration", "$apple-orchestration", "$ios-feature-implementation", "$apple-verification", "$code-review"],
        failures,
    )
    require_contains(
        REVIEW_ROOT / "skills" / "code-review" / "SKILL.md",
        ["独立 reviewer subAgent", "review.<platform>.*", "阻塞问题：无"],
        failures,
    )
    model_policy = subprocess.run(
        [sys.executable, str(APPLE_ROOT / "scripts" / "check_codex_model_policy.py"), "--offline"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if model_policy.returncode:
        failures.append(model_policy.stderr.strip() or model_policy.stdout.strip())

    if failures:
        print("subagent orchestration policy lint failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("subagent orchestration policy lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
