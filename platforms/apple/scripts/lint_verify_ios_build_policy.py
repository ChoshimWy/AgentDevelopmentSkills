#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parents[1]
WORKFLOW_AGENT_DIR = REPOSITORY_ROOT / "disciplines" / "workflow" / "assets" / "codex" / "agents"
POLICY_SKILLS = [
    "ios-feature-implementation",
    "debugging",
    "ios-performance",
    "xcode-build",
    "ios-automation",
]


def require_contains(path: Path, snippets: list[str], failures: list[str]) -> None:
    if not path.exists():
        failures.append(f"{path.relative_to(ROOT)} missing")
        return
    text = path.read_text()
    missing = [snippet for snippet in snippets if snippet not in text]
    if missing:
        failures.append(f"{path.relative_to(ROOT)} missing: {', '.join(missing)}")


def require_not_contains(path: Path, snippets: list[str], failures: list[str]) -> None:
    if not path.exists():
        failures.append(f"{path.relative_to(ROOT)} missing")
        return
    text = path.read_text()
    present = [snippet for snippet in snippets if snippet in text]
    if present:
        failures.append(f"{path.relative_to(ROOT)} should not contain: {', '.join(present)}")


def main() -> int:
    failures: list[str] = []
    policy_paths = [
        ROOT / "skills" / "TAXONOMY.md",
        ROOT / "skills" / "codex-subagent-orchestration" / "SKILL.md",
        ROOT / "config" / "claude-code" / "agents" / "orchestration.md",
        ROOT / "config" / "claude-code" / "memory-seed.md",
        ROOT / "config" / "codex" / "templates" / "agents" / "README.md",
        WORKFLOW_AGENT_DIR / "pm.toml",
        ROOT / "config" / "codex" / "templates" / "agents" / "tester.toml",
    ]
    for path in policy_paths:
        require_not_contains(
            path,
            [
                "最终都必须进入 `ios-verification`",
                "任务都不算完成",
                "四步收口",
                "固定四步",
                "未通过 CP3 不得宣告完成",
                "Apple 相关改动必须进入 ios-verification",
                "ios-verification / ios-verification",
                "testing/定向验证",
            ],
            failures,
        )

    require_contains(
        ROOT / "agent-instructions" / "global.md",
        ["Apple 平台规则", "独立 reviewer", "Xcode MCP", "codex_verify", "本地 `:path` Pod"],
        failures,
    )
    require_contains(
        ROOT / "skills" / "TAXONOMY.md",
        [
            "默认完成标准：定向测试或必要验证通过",
            "最窄定向单测",
            "sandbox 结果",
            "sandbox_permissions=\"require_escalated\"",
            "`.xcworkspace` 优先",
            "shared build-queue daemon",
            "--queue-status",
            "`实现 skill -> 定向验证 / no_test_reason -> reviewer subAgent(code-review)`",
            "`ios-verification` 统一负责验证前路由",
        ],
        failures,
    )

    for skill in POLICY_SKILLS:
        skill_md = ROOT / "skills" / skill / "SKILL.md"
        require_contains(skill_md, ["ios-verification"], failures)

        openai_yaml = ROOT / "skills" / skill / "agents" / "openai.yaml"
        if openai_yaml.exists():
            require_contains(openai_yaml, ["$ios-verification", "按需"], failures)

    require_contains(
        ROOT / "skills" / "ios-feature-implementation" / "SKILL.md",
        ["test-implementation", "ios-verification", "code-review", "no_test_reason"],
        failures,
    )
    require_contains(
        ROOT / "skills" / "ios-feature-implementation" / "agents" / "openai.yaml",
        ["test-implementation", "$code-review", "$ios-verification", "no_test_reason"],
        failures,
    )
    require_contains(ROOT / "skills" / "ios-automation" / "SKILL.md", ["Simulator", "真机", "ios-verification"], failures)
    require_contains(ROOT / "skills" / "xcode-build" / "SKILL.md", ["ios-verification", "Build Settings"], failures)

    ios_verification = ROOT / "skills" / "ios-verification" / "SKILL.md"
    require_contains(
        ios_verification,
        [
            "统一验证入口",
            "verification_mode",
            "route",
            "affected-tests",
            "execute",
            "digest",
            "final-gate",
            "项目环境",
            "codex_verify.sh",
            "~/.codex/bin/codex_verify",
            "shared build-queue daemon",
            "Xcode 系统 DerivedData",
            "Xcode MCP Fast Lane",
            "RunSomeTests",
            "XcodeWrite",
        ],
        failures,
    )
    require_contains(
        ROOT / "skills" / "ios-verification" / "references" / "override-config.md",
        [
            "项目环境",
            "shared build-queue daemon",
            "--queue-status",
            "公开配置已移除",
            "项目环境执行",
            "`.xcworkspace` 优先于 `.xcodeproj`",
            "不得直接调用 `xcodebuild` 二进制",
            "不要切到单独 `-derivedDataPath` 跑同一组验证来绕过锁",
        ],
        failures,
    )
    require_contains(
        ROOT / "config" / "claude-code" / "memory-seed.md",
        ["GetTestList", "RunSomeTests", "GetBuildLog", "同一 fingerprint 不重复 wrapper"],
        failures,
    )
    require_contains(
        ROOT / "config" / "claude-code" / "agents" / "orchestration.md",
        ["GetTestList", "RunSomeTests", "不得调用 MCP 写工具", "才升级 `ios-verification` 的 wrapper 路径"],
        failures,
    )
    require_contains(
        ROOT / "config" / "codex" / "templates" / "agents" / "tester.toml",
        ["GetTestList", "RunSomeTests", "XcodeWrite", "同一 fingerprint 不重复 wrapper", "升级 wrapper"],
        failures,
    )
    require_contains(
        ROOT / "skills" / "ios-verification" / "agents" / "openai.yaml",
        ["GetTestList", "RunSomeTests", "XcodeWrite", "不要为同一 fingerprint 重复 wrapper", "才通过目标项目 ./codex_verify.sh"],
        failures,
    )
    for settings_path in sorted((ROOT / "config" / "claude-code").glob("settings*.json")):
        require_not_contains(settings_path, ["Bash(xcodebuild:*)"], failures)
    require_contains(
        ROOT / "skills" / "ios-verification" / "scripts" / "build-check.sh",
        [
            "CODEX_VERIFY_BYPASS_WRAPPER",
            "TARGET_VERIFY_SCRIPT",
            "GLOBAL_VERIFY_SCRIPT",
            "--build-check",
            "fallback_to_available_simulator_or_macos",
            "xcrun devicectl list devices failed",
            "platform=iOS Simulator,id=",
        ],
        failures,
    )
    verify_template = ROOT / "config" / "codex" / "templates" / "codex_verify.example.sh"
    require_contains(
        verify_template,
        [
            "--repo-root",
            "--build-check",
            "--queue-status",
            "xcodebuild",
            "shared build-queue daemon",
            "CODEX_VERIFY_BYPASS_WRAPPER",
            "CODEX_VERIFY_EXIT_CODE",
            "write_xcode_entry_sanity",
            "normalize_xcodebuild_entry_args",
            "environment_sanity",
        ],
        failures,
    )
    require_contains(
        ROOT / "tools" / "digest-xcodebuild-log.sh",
        [
            "CODEX_VERIFY_EXIT_CODE",
            "non_blocking_noise_patterns",
            "Profile is missing the required UUID property",
            "Command CodeSign failed",
            "test_runner_restart",
            "deleting .+\\.framework/_CodeSignature",
            "Failing tests?:\\s*(?P<tail>.*)",
            "xcodebuild_exit_code == 0",
            "workspace_path_error",
            "simulator_service_unavailable",
            "env_issue",
            "Inspect the local Xcode workspace/destination/Simulator environment",
            '"status": status',
        ],
        failures,
    )
    require_contains(
        ROOT / "manifest.json",
        ["instruction_fragments", "provider_manifest", "skill_roots"],
        failures,
    )
    require_contains(
        ROOT / "config" / "codex" / "templates" / "agents" / "README.md",
        ["codex_verify.example.sh", "codex_verify.sh", "~/.codex/bin/codex_verify", "~/.codex/templates/codex_verify.example.sh", "shared build-queue daemon", "--queue-status"],
        failures,
    )
    require_contains(
        ROOT / "skills" / "ios-verification" / "scripts" / "build_check.py",
        [
            "is_unit_test_preferred_scheme",
            "scheme_has_unit_test_binding",
            "BuildableName",
            "Library/Developer/Xcode/DerivedData",
            "WORKSPACE_PATH_ERROR_PATTERN",
            "SIMULATOR_SERVICE_ERROR_PATTERN",
            "environment_sanity_payload",
            '"env_issue"',
        ],
        failures,
    )
    require_contains(
        ROOT / "skills" / "ios-automation" / "scripts" / "device" / "device_helpers.sh",
        [
            "BuildableName",
            "TestableReference",
            "select_connected_xcode_destination",
            "select_xcode_simulator_destination",
            "cd \"$root\"",
        ],
        failures,
    )
    require_contains(
        ROOT / "skills" / "ios-automation" / "scripts" / "simulator" / "xcode" / "builder.py",
        ["is_unit_test_preferred_scheme", "scheme_has_unit_test_binding", "BuildableName", "TestableReference"],
        failures,
    )

    if failures:
        print("ios-verification policy lint failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("ios-verification policy lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
