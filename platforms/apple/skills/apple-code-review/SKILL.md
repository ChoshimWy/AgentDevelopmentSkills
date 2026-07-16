---
name: apple-code-review
description: Apple 平台静态审查 extension。配合共享 code-review 使用，补充 Swift/Objective-C API、并发隔离、availability、文件头、中文文档注释、CocoaPods 本地依赖与 Xcode 验证故事检查；只读，不替代独立 reviewer 门禁或直接修复。
---

# Apple 代码审查扩展

## Purpose

Extend the shared independent review contract with Apple/Swift-specific correctness and verification rules.

## When to Use

- 已选择 Apple 平台且任务涉及 Swift、Objective-C、Xcode 工程或 Apple SDK。
- 共享 `code-review` 需要补充 Apple 专属检查。

## When Not to Use

- 非 Apple 项目。
- 直接实现、构建、测试、运行时调试或性能取证。

## Agent Rules

- 必须与共享 `code-review` 的独立性、scope、severity 和 evidence 合同组合使用。
- 检查 Swift concurrency、actor/`@MainActor`、Sendable、availability 与 fallback。
- 新增 `.swift/.h/.m/.mm` 时按同目录规范检查真实用户名和 `YYYY/M/D` 文件头。
- `public/open` 与跨模块 API 必须有中文 `///`，说明并发、副作用与失败语义。
- CocoaPods 私有组件必须修改真实源码并保持主项目本地 `:path` 验证基线；不得审查 `Pods/` 快照代替源码。
- 最窄定向验证足以覆盖低风险改动时，不得仅因缺少真机/模拟器就判定证据不足。
- 高风险工程、签名、资源、设备或依赖变更才建议升级 `apple-verification`。
- API 细节参考 `references/api-design.md`。
- 若 diff 声称使用 Xcode 官方知识源，检查 packet 为 `ready`、selected Skill eligible、source/routing/skill hash 可追溯，并确认没有把 Apple 原文提交进仓库。
- `swiftui-guidance` 检查 deployment target、active SDK、identity/data flow/soft deprecation；`modernization` 检查 scene/window ownership；`test-modernization` 检查语义等价与 XCUI 排除；`c-bounds-safety` 检查 ABI/count/lifetime；`security-hardening` 检查逐 target 决策与显式批准。
- Xcode-exported tool names 不得绕过当前 worktree、`codex_verify + shared build-queue`、权限或独立 reviewer 合同。

## Inputs

共享 review packet，加上 Apple SDK/Xcode/Swift 版本、目标平台、验证基线及可选 official-expertise source packet/hash。

## Outputs

只向共享 reviewer 返回 `status`、Apple-specific findings、影响范围、验证/知识源缺口与 `next_action`；最终 `阻塞问题` 由独立 reviewer 聚合。

## Exit Conditions

Apple 专属影响面已检查；未伪造运行时证据；所有 finding 可追溯到 diff 或已知合同。

## Escalation Rules

- API/availability 事实不确定时交给 `apple-docs`。
- 需要修改代码或 Build Settings 时退回 `ios-feature-implementation` / `xcode-build`，reviewer 不直接修复。
- 缺少 ready expertise packet 时报告证据缺口，不从第三方镜像补齐。

## Token Budget

- 不粘贴完整导出 Skill、构建日志或全量工程设置。
- 只返回与 diff 直接相关的 finding、source/hash 证据和最小验证缺口。

## Relationship to Other Skills

- 与共享 `code-review` 组合使用，不替代独立 reviewer。
- 官方知识源由 `apple-orchestration` 检查并路由；实现、配置、自动化和验证分别由既有 canonical Skill 承担。
