# AgentDevelopmentSkills

## Scope

- 本仓库统一维护跨平台 Agent 工作流 Core 与平台专属 AgentSkills；平台实现放在仓内独立平台包，保持独立 Manifest、版本、权限与 Conformance 边界。
- 跨平台通用 workflow、review、documentation、git 与 design 基础能力放在 `disciplines/`，平台包只保留平台 Binding/扩展；运行时全局配置放在 `runtime-configs/`，必须显式选择，不得随平台隐式激活。
- 安装时按用户选择的平台集合部署，不默认安装全部平台包；Core 不吸收 Xcode、Gradle、浏览器或后端工具链细节。
- 本机全局指令只允许一个受管入口；平台包不得各自覆盖 `~/.codex/AGENTS.md`。平台细节优先放在 Skill/Manifest，必须常驻的规则以显式 scope 分片合成，冲突 fail-closed。
- Android、Web、Backend、Desktop 当前是 `bootstrap-only`：允许发现但不得产生 phantom Binding 或 ready Plan；只有安装真实 Provider 后才可解锁。
- 架构真源：`docs/cross-platform-agent-workflow-architecture.html`。
- 当前实施进度真源：`docs/implementation/phase-3-design-provider-and-canonical-ir.html`。

## Phase 1–3 Rules

- Python 3.11+，标准库优先；Manifest 使用 JSON，不引入 YAML 解析依赖。
- Discovery 只读，不执行目标仓库脚本、不联网、不读取凭据。
- 所有机器输出使用 UTF-8 canonical JSON：键排序、紧凑分隔符、禁止 NaN、末尾换行。
- Core 依赖 Capability ID，不依赖平台 Skill 名称；Binding 由 Manifest 提供。
- Install Plan v2 必须冻结包来源 hash、Capability Provider、flattened asset allowlist 与 AGENTS rule trace；Binding 目标必须存在于所选依赖闭包且权限 profile 完全兼容。
- 必需能力缺失、依赖循环、非法状态转换、权限扩大和未知 Schema 版本必须 fail-closed。
- iOSAgentSkills Migration Audit v2 必须保持 288 个来源项唯一可追踪；License/NOTICE provenance 为 `pending` 时不得宣称发布就绪。
- 新增或修改任何 package 的 `skills/*/SKILL.md` 时，保持 Skill Schema v1 frontmatter 与必需章节；影响职责边界、路由或 Capability Binding 时，同步更新对应 Manifest、Provider 或 `platforms/apple/skills/TAXONOMY.md`。
- Skill 改动后对受影响目录运行 `python3 platforms/apple/scripts/lint_skill_schema.py --skills-dir <package>/skills`；需将警告作为失败时追加 `--strict`。
- 实现改动执行最窄单元测试和 Conformance；最终由独立 reviewer subAgent 执行 `code-review`。
