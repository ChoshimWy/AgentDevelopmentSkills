---
name: apple-design-source
description: Apple 设计源 evidence extension。读取明确的 Sketch/Figma/蓝湖标注或导出资产，生成可追溯 Design Evidence/source packet，补充 Apple HIG、Dynamic Type、VoiceOver 与 iOS/iPadOS/macOS 约束；不生成 Canonical IR、平台 binding 或产品代码。
---

# Apple Design Source Extension

## Purpose

Extract traceable Apple-targeted design facts, pass them through the shared read-first Design Source Gateway, and hand the normalized Evidence to the shared IR compiler.

## When to Use

- Apple 产品的 Sketch/Figma/蓝湖/PNG 真源需要读取和归档 evidence。
- 需要记录 artboard/node、assets、tokens、states、responsive 和 accessibility facts。

## When Not to Use

- 开放式设计探索；使用共享 `ui-ux-design-system`。
- Canonical UI IR；使用共享 `design-ir-compiler`。
- UIKit/SwiftUI binding；使用 `apple-design-context-compiler`。

## Agent Rules

- 按 `references/sketch-to-code-spec.md` 获取最小真源切片。
- 真源读取默认只读；export 与 write 分离，任何 write 必须由共享 `design-source-gateway` 校验单 attempt 的精确 document/page/node approval。
- 记录 document/node/version/hash 与 unknowns，不把截图猜测冒充事实。
- 不在 source packet、Ledger 或 Packet 中保存 token、cookie、完整设计文档或无关页面。
- Apple 约束只作为 evidence/acceptance extension，不污染共享 IR base。
- 输出 source slice 后先交给共享 `design-source-gateway` 生成 Design Evidence v1，再交给 `design-ir-compiler` 与 Apple binding extension。

## Token Budget

- 只读取目标 Apple screen/region/component 与必需 assets、states、accessibility facts。
- 完整 source slice 写受控 artifact；回复仅返回 identity、unknowns 和 next action。

## Inputs

Design source identity、target frame/node、Apple target、appearance、locale 与 accessibility constraints。

## Outputs

返回 `status`、Design Evidence/source packet、assets、unknowns、Apple acceptance extension 与 `next_action`。

## Exit Conditions

真源可追溯，unknown 明确，read/export/write 权限未扩大，且已形成共享 Gateway 可消费的最小 source slice。

## Escalation Rules

- Apple 真源无法读取时交给 `design-source-gateway` 使用 manual/screenshot 降级；实现必需事实缺失时返回 `blocked`。
- write approval 缺失、过期或越界时停止，不调用 Connector。
- Apple HIG、Dynamic Type 或 VoiceOver 决策冲突时交给 `ui-ux-design-system` 或人工设计 owner。

## Relationship to Other Skills

共享 `ui-ux-design-system` 决策设计方向；`design-source-gateway` 归一化 Evidence；`design-ir-compiler` 生成通用 IR/Registry/Packet；`apple-design-context-compiler` 只负责 Apple binding 与 Apple Agent Packet。
