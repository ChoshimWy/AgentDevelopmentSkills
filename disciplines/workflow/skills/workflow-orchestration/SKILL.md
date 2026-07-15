---
name: workflow-orchestration
description: 跨平台开发任务的通用编排与报告合同。用于在不依赖任何平台 Skill 名称的前提下执行 CP0–CP3、lite/standard/full 分型、角色协作、fail-fix-report、独立 reviewer 门禁与交付汇总；平台 API、工具链、构建与验证细节必须交给已选平台 overlay。
---

# 跨平台工作流编排

## Purpose

Provide platform-neutral task classification, checkpoints, role boundaries, review independence, failure loops, and delivery reporting. This Skill never selects Xcode, Gradle, browser, backend, or device commands.

## When to Use

- 任务需要跨文件实施、验证、审查或交付汇总。
- 平台包需要复用统一 checkpoint、角色或失败回环合同。
- 用户明确要求 subAgent、多 Agent、并行协作或全局任务推进。

## When Not to Use

- 平台 API、构建、设备、签名或运行时诊断的具体执行。
- 单一专项 Skill 已能完整处理的纯查询。
- 用通用编排替代平台实现、验证或权限审批。

## Agent Rules

- `analyze` 模式只读，输出目标、范围、风险和候选验证，不得写文件或触发执行型工具。
- `orchestrate` 模式可以协调实施，但实际副作用由已解析平台/discipline Capability 承担。
- `report` 模式只汇总已有证据，不新增实现、验证或审查结论。
- 修复/实现任务首次写入前必须完成 `CP0 Intent Lock` 简短计划。
- 实现链路必须是：实施 → 定向验证或 `no_test_reason` → 独立 reviewer subAgent。
- reviewer 不可用或存在阻塞问题时不得宣告完成。
- 同类失败按 `fail-fix-report` 最多回环两次；超限后报告 blocked。
- 不在本 Skill 中硬编码平台 Skill 名、workspace、scheme、device、Gradle task、浏览器或后端部署命令。

## Task Classification

| 类型 | 默认档位 |
| --- | --- |
| `doc-only` / `rule-only` | `lite` |
| `code-small` / `code-medium` | `standard` |
| `code-risky` | `full` |

最低逻辑角色为 `explorer + builder + reporter`。实现链路额外要求独立 `reviewer`；`pm`、`tester` 和资料研究角色按任务证据启用。

## Checkpoints

- `CP0 Intent Lock`：目标、边界、非目标、成功标准、计划与验证路径冻结。
- `CP1 Anchor Slice`：先完成最小关键切片，再扩大实施面。
- `CP2 Validation Baseline Freeze`：冻结受影响测试、环境与证据口径。
- `CP3 Final Gate`：验证有效且独立 review 无阻塞。

详见 `references/checkpoint-contract.md` 与 `references/handoff-loop.md`。

## Inputs

```json
{
  "goal": "task objective",
  "context": [],
  "constraints": [],
  "success_criteria": [],
  "selected_platforms": [],
  "mode": "analyze | orchestrate | report"
}
```

## Outputs

```json
{
  "status": "completed | partial | blocked",
  "task_type": "doc-only | rule-only | code-small | code-medium | code-risky",
  "orchestration_level": "lite | standard | full",
  "checkpoint_status": {},
  "roles_used": [],
  "validation": {},
  "review": {},
  "acceptance_matrix": [],
  "known_risks": [],
  "next_action": "none | blocked | needs_user_input | needs_verification"
}
```

## Exit Conditions

- 需求与改动范围可追溯。
- 必要验证发生在最后一次相关修改之后，或明确记录 `no_test_reason`。
- 实现任务已由未参与实现的 reviewer 审查，且 `阻塞问题：无`。
- 平台专属执行证据由对应平台 Capability 提供，而非本 Skill 伪造。
