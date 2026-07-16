---
name: design-ir-compiler
description: 跨平台 Canonical UI IR、Registry 与 task-scoped Agent Packet 基础编译 Skill。把可追溯 Design Evidence 归一化、解析共享设计系统引用并裁剪稳定输入；不解析 SwiftUI/UIKit、Android、Web 或其它平台 binding。
---

# Canonical Design IR Compiler

## Purpose

Normalize traceable design evidence into a deterministic, platform-neutral Canonical UI IR before any platform binding or implementation.

## When to Use

- 设计事实需要稳定 Schema、provenance、unknown 和引用闭包。
- 多个平台需要消费同一 design IR base。
- 平台 binding extension 需要明确输入边界。

## When Not to Use

- 视觉方向探索；使用 `ui-ux-design-system`。
- 平台组件 binding、代码 symbol、截图执行或产品代码实现。

## Agent Rules

- `normalize` 只接受可追溯 evidence；原始事实、人工合同和推断必须区分 provenance。
- `registry` 只生成平台中立 token/component/variant/slot/state/motion 合同；平台 symbol 仅能作为外部 binding reference。
- `packet` 只裁剪当前 screen/region/component 及其 token/component 依赖闭包；不得携带完整设计文档、凭据或无关页面。
- IR 只包含 platform-neutral geometry intent、tokens、semantics、states、interactions、responsive、accessibility 和 unknowns。
- blocking unknown 必须令 `status=blocked`；不得猜测填充。
- source/parser/registry fingerprint 不一致时必须产生 `stale_inputs`，旧 Packet 不得复用。
- 不输出 UIKit/SwiftUI/Compose/DOM symbol，也不调用平台构建或设备工具。

## Token Budget

- 按 screen/region/component 裁剪；保留祖先约束、token 引用和状态闭包。
- 完整 IR 写 artifact，回复仅返回诊断与 artifact identity。

## Inputs

```json
{"mode":"normalize | validate | registry | packet","design_evidence":{},"canonical_ui_ir":{},"design_system_registry":{},"target":{"kind":"screen | region | component","id":"..."},"context_budget":null}
```

## Outputs

```json
{"status":"completed | partial | blocked","artifact":"path-or-null","artifact_kind":"canonical-ui-ir | design-system-registry | design-agent-packet","fingerprint":"design-v1:...","diagnostics":[],"blocking_unknowns":[],"stale_inputs":[],"next_action":"platform-binding | collect-evidence | fix-contract | blocked"}
```

## Exit Conditions

- Design Evidence 与 IR provenance 可追溯。
- 引用、状态与 unknown 合同有效。
- IR、Registry 与 Packet 使用 `disciplines/design/contracts/` 中唯一机器真源并可确定性复现。
- 未包含平台 binding。

## Escalation Rules

- source scope、provenance 或 blocking unknown 不完整时返回 `blocked` 并交给 `design-source-gateway` 补证据。
- Registry 语义冲突交给 `ui-ux-design-system` 决策，不在编译器中静默覆盖。
- 平台 symbol、availability 与实现约束交给对应平台 binding extension。

## Relationship to Other Skills

- `ui-ux-design-system` 提供设计决策。
- `design-source-gateway` 归一化最小 source slice；平台 design-source extension 补充平台约束。
- 平台 design-binding extension 消费本 IR 并生成平台 Agent Packet。
