---
name: apple-design-source
description: Apple 设计源 evidence extension。读取明确的 Sketch/Figma/蓝湖标注或导出资产，生成可追溯 Design Evidence/source packet，补充 Apple HIG、Dynamic Type、VoiceOver 与 iOS/iPadOS/macOS 约束；不生成 Canonical IR、平台 binding 或产品代码。
---

# Apple Design Source Extension

## Purpose

Extract traceable Apple-targeted design evidence and hand it to the shared design system and IR compiler.

## When to Use

- Apple 产品的 Sketch/Figma/蓝湖/PNG 真源需要读取和归档 evidence。
- 需要记录 artboard/node、assets、tokens、states、responsive 和 accessibility facts。

## When Not to Use

- 开放式设计探索；使用共享 `ui-ux-design-system`。
- Canonical UI IR；使用共享 `design-ir-compiler`。
- UIKit/SwiftUI binding；使用 `design-context-compiler`。

## Agent Rules

- 按 `references/sketch-to-code-spec.md` 获取最小真源切片。
- 记录 document/node/version/hash 与 unknowns，不把截图猜测冒充事实。
- Apple 约束只作为 evidence/acceptance extension，不污染共享 IR base。
- 输出 source packet 后交给共享 `design-ir-compiler`，再交给 Apple binding extension。

## Inputs

Design source identity、target frame/node、Apple target、appearance、locale 与 accessibility constraints。

## Outputs

返回 `status`、Design Evidence/source packet、assets、unknowns、Apple acceptance extension 与 `next_action`。

## Exit Conditions

真源可追溯，unknown 明确，且已形成共享 IR compiler 可消费的输入。

## Relationship to Other Skills

共享 `ui-ux-design-system` 决策设计方向；`design-ir-compiler` 生成通用 IR；`design-context-compiler` 只负责 Apple binding 与 Agent Packet。
