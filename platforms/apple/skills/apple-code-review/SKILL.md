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

## Inputs

共享 review packet，加上 Apple SDK/Xcode/Swift 版本、目标平台及验证基线。

## Outputs

只向共享 reviewer 返回 Apple-specific findings、影响范围与验证缺口；最终 `阻塞问题` 由独立 reviewer 聚合。

## Exit Conditions

Apple 专属影响面已检查；未伪造运行时证据；所有 finding 可追溯到 diff 或已知合同。
