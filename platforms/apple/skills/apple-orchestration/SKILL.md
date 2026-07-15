---
name: apple-orchestration
description: Apple 平台工作流 overlay 与规范入口。用于 iOS、macOS、watchOS、tvOS、visionOS 或 Xcode/CocoaPods 任务；组合 workflow-orchestration，并补充 Apple Skill 路由、验证、依赖与 review extension。
---

# Apple 工作流 Overlay

## Purpose

Compose the shared `workflow-orchestration` contract with Apple-specific implementation, verification, review, debugging, performance, build, automation, and documentation capabilities.

## When to Use

- iOS、macOS、watchOS、tvOS 或 visionOS 开发任务。
- 用户要求 Apple 平台的默认编排入口。
- 需要基于 Apple/Xcode/CocoaPods 事实选择平台专项 Skill。

## When Not to Use

- 未选择 Apple 的任务。
- 只需要平台无关 checkpoint、角色或报告合同；直接使用 `workflow-orchestration`。

## Agent Rules

- 先应用共享 `workflow-orchestration` 的 `analyze` / `orchestrate` / `report` 模式与 CP0–CP3。
- `analysis-only` 模式只读，不启动实现、build/test、设备或配置写入。
- 生产与测试代码统一路由 `ios-feature-implementation`；验证路由 `apple-verification`。
- 静态审查先用共享 `code-review`，再组合 `apple-code-review`，且必须由独立 reviewer subAgent 执行。
- 构建配置用 `xcode-build`，运行时症状用 `apple-debugging`，性能证据用 `ios-performance`，设备自动化用 `ios-automation`。
- Apple API/availability/WWDC 使用 `apple-docs`；正式 HTML 使用共享 `html-docs`。
- 所有验证统一进入 `apple-verification` 的 `codex_verify` + shared build-queue 路径；日常使用 `quick-verify` 复用 Verification Session/证据 fingerprint，不使用 Xcode MCP，也不得直接调用验证型 `xcodebuild`。
- 私有 Pod 联调保持主项目本地 `:path`，修改真实组件仓，不改 `Pods/` 快照。
- 具体 Apple 门禁见 `references/apple-gate-rules.md`、`references/coding-standards.md` 和 `references/tool-routing.md`。
- 不把 Codex、Claude Code 等运行时名称写入新的平台 Skill ID；平台 Skill 命名遵守仓库 `skill-naming-policy.json` 与 `docs/skill-naming-convention.html`。

## Inputs

共享 workflow packet，加上 OS/SDK/Xcode/Swift、workspace/scheme/destination、依赖与用户验证约束。

## Outputs

沿用共享 workflow 输出，并补充 Apple route、verification baseline、local dependency state 与 Apple review extension 证据。

## Exit Conditions

共享工作流门禁通过，Apple 定向验证或 `no_test_reason` 有效，独立 reviewer 已组合 Apple extension 且 `阻塞问题：无`。

## Escalation Rules

- 只需平台无关 checkpoint、角色或报告时转交 `workflow-orchestration`。
- Apple Provider、Binding 或必需 Skill 缺失时返回 blocked，不得回退到历史入口或复制合同。

## Relationship to Other Skills

- `workflow-orchestration` 拥有通用工作流合同，本 Skill 只拥有 Apple overlay。
- 实施、验证和审查分别转交 `ios-feature-implementation`、`apple-verification`、`code-review` + `apple-code-review`。

## Reporting Format

输出 `status`、Apple route、validation evidence、review result 与 `next_action`。

## Token Budget

只加载当前任务涉及的 Apple references，不重复展开共享工作流合同。
