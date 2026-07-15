---
name: ui-ux-design-system
description: 跨平台 UI/UX 设计系统与开放式设计探索 Skill。用于视觉方向、token、组件、交互、无障碍、设计评审和原型规划；只输出平台无关设计合同，不读取平台专属设计源、不编译平台 binding、不直接实现产品代码。
---

# 跨平台 UI/UX Design System

## Purpose

Create a platform-neutral design direction and design-system contract that can be consumed by a design IR compiler and a selected platform extension.

## When to Use

- 视觉方向、设计系统、色板、字体、间距、组件状态或交互规则。
- 跨平台设计评审、无障碍检查和原型范围规划。
- 将已提供的设计事实整理为 tokens、component semantics 与 acceptance criteria。

## When Not to Use

- 读取 Sketch/Figma 等平台工具真源；交给已选 design-source extension。
- 生成 Canonical UI IR；使用 `design-ir-compiler`。
- SwiftUI/UIKit/Web 等平台代码实现。
- 正式 HTML 文档；使用 `html-docs`。

## Agent Rules

- 模式只允许 `design-exploration`、`design-system`、`design-review`、`prototype-planning`。
- 先记录 product、users、platform set、brand/accessibility constraints 和 unknowns。
- 从 tokens 到组件与状态逐层收敛，不把平台 API 或组件 symbol 写进通用合同。
- 不把截图推断冒充设计真源；不确定项进入 `unknowns`。
- 需要设计源或平台 binding 时只输出 handoff，不在本 Skill 越权执行。

## Token Budget

- 只保留目标 flow 与组件所需 token/状态；大清单写入 artifact。
- 优先表格与稳定 ID，不输出泛化设计理论。

## Inputs

```json
{"goal":"design direction or system","platforms":[],"design_facts":[],"constraints":[],"unknowns":[]}
```

## Outputs

```json
{"status":"completed | partial | blocked","mode":"design-exploration | design-system | design-review | prototype-planning","tokens":{},"components":[],"states":[],"accessibility":[],"acceptance_criteria":[],"unknowns":[],"next_action":"compile-ir | collect-source | platform-binding | document | blocked"}
```

## Exit Conditions

- 设计方向、tokens、状态、无障碍和 unknowns 可追溯。
- 未混入平台实现细节。
- `status` 与 `next_action` 明确。

## Relationship to Other Skills

- `design-ir-compiler` 把稳定设计事实归一化为 Canonical UI IR。
- 平台 design-source/binding extension 负责工具真源与代码组件绑定。
- `html-docs` 负责正式文档，平台 implementation Skill 负责代码。
