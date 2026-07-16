---
name: design-source-gateway
description: 跨平台 Design Source Gateway。把 Figma、Sketch、manual evidence 与 screenshot fallback 的最小 source slice 归一化为 Design Evidence v1，并执行 read/export/write 权限隔离与精确 scope approval；不直接持有凭据或调用特定平台实现。
---

# Design Source Gateway

## Purpose

Normalize a minimal source slice into traceable Design Evidence without granting connector access implicitly.

## When to Use

- 需要统一 Figma、Sketch、人工合同或截图降级证据。
- 需要冻结 document/page/node scope、parser version、content hash 与 permission mode。
- 写回前需要校验单 attempt approval。

## When Not to Use

- 设计探索或产品审计；使用对应 Product Design capability。
- Canonical IR、Registry 或 Packet 编译；使用 `design-ir-compiler`。
- 平台组件 binding 或产品代码实现。

## Agent Rules

- 默认 `read`；只读取明确 document/page/node 的最小 slice，不抓取整份文件。
- `export` 与 `write` 分离；export approval 不得升级为 document/node write。
- `write` 必须匹配 approval 的 attempt、document、page 与 node allowlist，任一不匹配即 blocked。
- 不把 token、credential、cookie、原始完整文档或未受控路径写入 Evidence、Ledger 或 Packet；credential key aliases 必须归一化后拒绝。
- screenshot 必须标记 inference 和低置信度，隐藏状态/交互缺口进入 blocking unknowns；单截图不得生成实施 Packet。
- Connector 缺失时允许 manual/screenshot degraded；必需 structured fact 缺失时 blocked。
- Gateway 不创建 source/credential cache；Ledger projection 仅保存 Evidence identity、source/scope/approval hash、受控 `artifact://` URI、`retention=task` 与 `cleanup=not-required`。

## Token Budget

- 只传目标 screen/region/component 的最小 source slice，默认不超过 128 个 node。
- 完整 Evidence 写 artifact；回复只返回 identity、diagnostics、unknowns 与 next action。

## Inputs

```json
{"mode":"normalize","source_kind":"figma | sketch | manual | screenshot","document_id":"...","document_version":"...","scope":{"page_id":null,"node_ids":["..."]},"source_slice":[],"permission":{"mode":"read | export | write","approval_id":null},"attempt_id":"..."}
```

## Outputs

```json
{"status":"complete | partial | blocked","design_evidence":"path-or-null","diagnostics":[],"next_action":"compile-ir | collect-evidence | request-approval | blocked"}
```

## Exit Conditions

- Evidence 符合 `disciplines/design/contracts/design-evidence-v1.schema.json`。
- source、scope、hash、parser 与 provenance 完整。
- 未扩大权限，未泄露凭据或无关设计数据。
- Ledger projection 不包含 source slice 或原始 approval，且 cleanup 结果明确。

## Escalation Rules

- Connector 缺失但允许降级时使用 manual/screenshot，并显式保留 unknown；必需 structured fact 缺失时返回 `blocked`。
- write 未获精确 approval 时返回 `request-approval`，不得尝试扩大 scope。
- provenance 冲突或 source slice 无法归一化时交给 `design-ir-compiler(validate)`，不得猜测修复。

## Relationship to Other Skills

- Product Design Provider 提供 context/research/ideation/audit/prototype/share，不拥有 structured source read。
- `design-ir-compiler` 消费本 Skill 的 Evidence。
- Apple `apple-design-source` 只补 Apple evidence extension，不替代本权限边界。
