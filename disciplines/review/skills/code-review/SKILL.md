---
name: code-review
description: 跨平台静态代码审查与独立 reviewer 门禁。用于审查任务全量 diff、直接影响面、正确性、安全性、并发、数据与权限不变量、验证故事及回归风险；平台语言/API/工具链细节由对应平台 review extension 补充。本 Skill 只读，不直接修复或执行构建测试。
---

# 独立代码审查

## Purpose

Perform evidence-based, read-only review across platforms while preserving reviewer independence, explicit scope, severity, and verification-story judgement.

## When to Use

- 审查工作区、提交、PR、公共合同或测试充分性。
- 实现链路进入最终独立 reviewer 门禁。
- 规则、Schema、安装或权限变更需要静态风险评估。

## When Not to Use

- 直接修改实现或测试。
- 运行时 crash、profiling、build/test 执行或发布操作。
- 用实现 Agent 的自审替代独立 reviewer。

## Agent Rules

- 实现链路必须由未参与实现的独立 reviewer subAgent 执行；无法确认独立性时报告 blocked。
- 默认覆盖 staged、unstaged、untracked 与任务基线后的相关提交；无法确定基线时说明 fallback。
- 先从调用方、状态写入点、持久化/缓存/输出路径提取业务不变量，再判断正确性。
- 同时审查正向触发与负向保护，不能只检查 happy path。
- 发现必须绑定文件和尽可能窄的行号；不得把推断写成已复现事实。
- 平台专属 API、并发模型、文件头、依赖与构建规则交给已解析的 `review.<platform>.*` extension。
- 本 Skill 不修改文件，不运行构建测试，不伪造运行时证据。

## Review Priority

```text
正确性 → 安全性 → 数据/权限不变量 → 并发 → 性能 → 可维护性 → 一致性
```

## Inputs

```json
{
  "review_base_ref": "optional",
  "changed_files": [],
  "diff_scope": "working-tree",
  "validation_result": {},
  "platform_extensions": [],
  "reviewer_independence": "independent-subagent | pure-review | unavailable"
}
```

## Outputs

可见回复默认使用：`阻塞问题`、`非阻塞建议`、`审查范围`、`影响面`、`未审查变更`、`首个失败`、`验证故事`、`审查独立性`、`风险等级`、`下一步`。无阻塞项必须明确写 `阻塞问题：无`。

## Exit Conditions

- 审查独立性成立。
- 审查基线、范围、直接影响面和未覆盖部分明确。
- 验证故事已裁决。
- 无阻塞项时才允许 `下一步：complete`。
