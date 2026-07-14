# AgentDevelopmentSkills

## Scope

- 本仓库实现跨平台 Agent 工作流 Core；平台专属实现保留在独立平台包。
- 架构真源：`docs/cross-platform-agent-workflow-architecture.html`。
- 当前实施进度真源：`docs/implementation/phase-1-core-foundation.html`。

## Phase 1 Rules

- Python 3.11+，标准库优先；Manifest 使用 JSON，不引入 YAML 解析依赖。
- Discovery 只读，不执行目标仓库脚本、不联网、不读取凭据。
- 所有机器输出使用 UTF-8 canonical JSON：键排序、紧凑分隔符、禁止 NaN、末尾换行。
- Core 依赖 Capability ID，不依赖平台 Skill 名称；Binding 由 Manifest 提供。
- 必需能力缺失、依赖循环、非法状态转换、权限扩大和未知 Schema 版本必须 fail-closed。
- 实现改动执行最窄单元测试和 Conformance；最终由独立 reviewer subAgent 执行 `code-review`。
