#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # Python 3.10 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        try:
            import pip._vendor.tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise SystemExit(
                "lint_workflow_contract_policy.py requires Python 3.11+, `tomli`, or pip's vendored tomli"
            ) from exc


ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parents[1]
SKILL_ROOT = ROOT / "skills" / "codex-subagent-orchestration"
CODEX_TEMPLATE_AGENTS = ROOT / "config" / "codex" / "templates" / "agents"
WORKFLOW_ROOT = REPOSITORY_ROOT / "disciplines" / "workflow"
WORKFLOW_SKILL_ROOT = WORKFLOW_ROOT / "skills" / "workflow-orchestration"
WORKFLOW_TEMPLATE_AGENTS = WORKFLOW_ROOT / "assets" / "codex" / "agents"
REVIEW_ROOT = REPOSITORY_ROOT / "disciplines" / "review"
REVIEW_TEMPLATE_AGENTS = REVIEW_ROOT / "assets" / "codex" / "agents"
CODEX_RUNTIME_ROOT = REPOSITORY_ROOT / "runtime-configs" / "codex" / "assets"
CODEX_AGENT_VALIDATE_SCRIPT = ROOT / "scripts" / "validate_codex_agent_templates.py"
EXPECTED_DISABLED_PLUGINS = {
    "build-ios-apps@openai-curated",
    "build-macos-apps@openai-curated",
    "figma@openai-curated",
    "github@openai-curated",
    "figma@openai-curated-remote",
    "product-design@openai-curated-remote",
    "superpowers@openai-curated-remote",
    "documents@openai-primary-runtime",
    "spreadsheets@openai-primary-runtime",
    "presentations@openai-primary-runtime",
    "pdf@openai-primary-runtime",
    "browser@openai-bundled",
    "chrome@openai-bundled",
    "computer-use@openai-bundled",
}

FORBIDDEN_SUBAGENT_RESTRICTION_PHRASES = [
    "默认进入编排入口不等于必须 spawn",
    "默认进入 `codex-subagent-orchestration` 不等于必须 spawn",
    "full multi-agent execution",
    "write ownership",
    "write set is safe",
    "throughput benefit",
    "运行时工具可用",
    "写集安全",
    "拆分有质量/效率收益",
    "收益明确",
    "最少必要",
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


def require_exists(path: Path, failures: list[str]) -> None:
    if not path.exists():
        failures.append(f"{path.relative_to(ROOT)} missing")


def require_codex_plugins_disabled(path: Path, failures: list[str]) -> None:
    if not path.exists():
        failures.append(f"{path.relative_to(ROOT)} missing")
        return

    data = tomllib.loads(path.read_text())
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        failures.append(f"{path.relative_to(ROOT)} missing plugins table")
        return

    missing = sorted(EXPECTED_DISABLED_PLUGINS - set(plugins))
    if missing:
        failures.append(f"{path.relative_to(ROOT)} missing disabled plugin entries: {', '.join(missing)}")

    enabled_or_invalid = sorted(
        plugin_id
        for plugin_id, config in plugins.items()
        if not isinstance(config, dict) or config.get("enabled") is not False
    )
    if enabled_or_invalid:
        failures.append(
            f"{path.relative_to(ROOT)} plugins must all set enabled = false: {', '.join(enabled_or_invalid)}"
        )


def validate_codex_sync_behavior(failures: list[str]) -> None:
    sync_script = CODEX_RUNTIME_ROOT / "scripts" / "sync_codex_shared_config.py"
    shared_config = CODEX_RUNTIME_ROOT / "codex" / "codex.shared.toml"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        existing = temp / "existing.toml"
        existing.write_text(
            '''model = "gpt-5.6-sol"\n'''
            '''model_reasoning_effort = "high"\n'''
            '''service_tier = "fast"\n'''
            '''[mcp_servers.codegraph]\ncommand = "codegraph"\n'''
            '''args = ["serve", "--mcp"]\n'''
            '''[mcp_servers.openaiDeveloperDocs]\nurl = "https://developers.openai.com/mcp"\n'''
            '''[mcp_servers.openaiDeveloperDocs.tools.search_openai_docs]\napproval_mode = "approve"\n'''
            '''[mcp_servers.appleDeveloperDocs]\ncommand = "npx"\n'''
            '''args = ["-y", "@kimsungwhee/apple-docs-mcp@latest"]\n'''
            '''[mcp_servers.local_only]\ncommand = "local-tool"\n'''
        )
        result = subprocess.run(
            [
                sys.executable,
                str(sync_script),
                "--shared-config",
                str(shared_config),
                "--existing-config",
                str(existing),
                "--agents-path",
                "/tmp/AGENTS.md",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append("sync_codex_shared_config.py migration smoke failed")
            return
        merged = tomllib.loads(result.stdout)
        if merged.get("model") != "gpt-5.6-sol" or merged.get("model_reasoning_effort") != "high":
            failures.append("Codex sync must preserve explicit local model/reasoning")
        if "service_tier" in merged:
            failures.append("Codex sync must retire legacy unpaired global Fast mode")
        servers = merged.get("mcp_servers", {})
        if ({"codegraph", "openaiDeveloperDocs", "appleDeveloperDocs"} & set(servers)) or "local_only" not in servers:
            failures.append("Codex sync must retire legacy global MCPs and preserve unrelated local MCPs")

        custom_mcp = temp / "custom-mcp.toml"
        custom_mcp.write_text(
            '''[mcp_servers.codegraph]\ncommand = "my-codegraph"\nargs = ["custom"]\n'''
            '''[mcp_servers.openaiDeveloperDocs]\nurl = "https://example.invalid/custom-mcp"\n'''
        )
        result = subprocess.run(
            [
                sys.executable,
                str(sync_script),
                "--shared-config",
                str(shared_config),
                "--existing-config",
                str(custom_mcp),
                "--agents-path",
                "/tmp/AGENTS.md",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append("sync_codex_shared_config.py custom MCP preservation smoke failed")
            return
        merged = tomllib.loads(result.stdout)
        servers = merged.get("mcp_servers", {})
        if servers.get("codegraph") != {"command": "my-codegraph", "args": ["custom"]}:
            failures.append("Codex sync must preserve customized same-name codegraph MCP")
        if servers.get("openaiDeveloperDocs") != {"url": "https://example.invalid/custom-mcp"}:
            failures.append("Codex sync must preserve customized same-name docs MCP")

        explicit_fast = temp / "explicit-fast.toml"
        explicit_fast.write_text(
            '''model = "gpt-5.5"\nservice_tier = "fast"\n'''
            '''[features]\nfast_mode = true\n'''
        )
        result = subprocess.run(
            [
                sys.executable,
                str(sync_script),
                "--shared-config",
                str(shared_config),
                "--existing-config",
                str(explicit_fast),
                "--agents-path",
                "/tmp/AGENTS.md",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append("sync_codex_shared_config.py explicit Fast smoke failed")
            return
        merged = tomllib.loads(result.stdout)
        if merged.get("service_tier") != "fast" or merged.get("features", {}).get("fast_mode") is not True:
            failures.append("Codex sync must preserve an explicitly paired local Fast mode")


def main() -> int:
    failures: list[str] = []
    shared_config = CODEX_RUNTIME_ROOT / "codex" / "codex.shared.toml"
    require_contains(
        shared_config,
        ["image_model = \"gpt-image-2\"", "multi_agent = true", "[agents]", "max_threads = 6"],
        failures,
    )
    require_not_contains(
        shared_config,
        ["model_reasoning_effort =", "plan_mode_reasoning_effort =", "service_tier =", "[mcp_servers."],
        failures,
    )
    require_codex_plugins_disabled(shared_config, failures)
    validate_codex_sync_behavior(failures)

    require_contains(
        WORKFLOW_SKILL_ROOT / "SKILL.md",
        [
            "## Task Classification",
            "## Checkpoints",
            "`CP0 Intent Lock`",
            "`CP1 Anchor Slice`",
            "`CP2 Validation Baseline Freeze`",
            "`CP3 Final Gate`",
            "fail-fix-report",
            "acceptance_matrix",
        ],
        failures,
    )
    require_contains(
        WORKFLOW_SKILL_ROOT / "references" / "role-contracts.md",
        ["checkpoint_status", "first_failure", "next_action", "reviewer"],
        failures,
    )
    require_contains(
        WORKFLOW_SKILL_ROOT / "references" / "prompt-templates.md",
        ["## builder", "## reviewer", "## tester", "changed_files", "no_test_reason"],
        failures,
    )
    require_contains(
        WORKFLOW_SKILL_ROOT / "references" / "handoff-loop.md",
        ["explorer", "builder", "CP0", "CP2", "独立 reviewer", "最多两轮"],
        failures,
    )
    require_contains(
        SKILL_ROOT / "SKILL.md",
        ["Apple 工作流 Overlay", "workflow-orchestration", "ios-verification", "apple-code-review"],
        failures,
    )
    require_contains(
        REVIEW_ROOT / "skills" / "code-review" / "SKILL.md",
        ["跨平台", "只读", "独立 reviewer subAgent", "阻塞问题：无"],
        failures,
    )

    for path, snippets in (
        (WORKFLOW_TEMPLATE_AGENTS / "pm.toml", ['name = "pm"', '"checkpoint_status"']),
        (WORKFLOW_TEMPLATE_AGENTS / "explorer.toml", ['name = "explorer"', '"validation_baseline"']),
        (WORKFLOW_TEMPLATE_AGENTS / "reporter.toml", ['name = "reporter"', '"acceptance_matrix"']),
        (REVIEW_TEMPLATE_AGENTS / "reviewer.toml", ['name = "reviewer"', 'sandbox_mode = "read-only"']),
        (CODEX_TEMPLATE_AGENTS / "builder.toml", ['name = "builder"', '"changed_files"']),
        (CODEX_TEMPLATE_AGENTS / "tester.toml", ['name = "tester"', '"failure_attribution"']),
    ):
        require_contains(path, snippets, failures)

    model_policy = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_codex_model_policy.py"), "--offline"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if model_policy.returncode:
        failures.append(model_policy.stderr.strip() or model_policy.stdout.strip())
    if (REPOSITORY_ROOT / ".codex").exists():
        failures.append("repository root .codex must not be distributed")
    if failures:
        print("workflow contract policy lint failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("workflow contract policy lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
