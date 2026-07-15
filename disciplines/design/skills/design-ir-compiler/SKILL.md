---
name: design-ir-compiler
description: 跨平台 Canonical UI IR 基础编译 Skill。把可追溯 Design Evidence 归一化为平台中立的 screen/region/component/token/state/interaction/accessibility/unknown 合同；不解析 SwiftUI/UIKit、Android、Web 或其它平台 binding。
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
- 平台组件 registry、代码 symbol、截图执行或产品代码实现。

## Agent Rules

- `normalize` 只接受可追溯 evidence；原始事实、人工合同和推断必须区分 provenance。
- IR 只包含 platform-neutral geometry intent、tokens、semantics、states、interactions、responsive、accessibility 和 unknowns。
- blocking unknown 必须令 `status=blocked`；不得猜测填充。
- 不输出 UIKit/SwiftUI/Compose/DOM symbol，也不调用平台构建或设备工具。

## Token Budget

- 按 screen/region/component 裁剪；保留祖先约束、token 引用和状态闭包。
- 完整 IR 写 artifact，回复仅返回诊断与 artifact identity。

## Inputs

```json
{"mode":"normalize | validate","design_evidence":{},"target":{},"context_budget":null}
```

## Outputs

```json
{"status":"completed | partial | blocked","canonical_ui_ir":"path-or-null","diagnostics":[],"blocking_unknowns":[],"next_action":"platform-binding | collect-evidence | fix-contract | blocked"}
```

## Exit Conditions

- Design Evidence 与 IR provenance 可追溯。
- 引用、状态与 unknown 合同有效。
- 未包含平台 binding。

## Relationship to Other Skills

- `ui-ux-design-system` 提供设计决策。
- 平台 design-source extension 提供真源 evidence。
- 平台 design-binding extension 消费本 IR 并生成平台 Agent Packet。
