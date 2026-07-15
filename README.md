# AgentDevelopmentSkills

面向 Codex 与其他开发 Agent 的跨平台工作流 Core。项目通过只读仓库发现、策略解析、Capability 合同和确定性 DAG，自动判断目标模块所需的研发流程，并以可解释、可恢复、fail-closed 的 Runtime 执行计划。

> 当前版本：`0.2.0`。Phase 1、Phase 2A 与 Phase 2B 的源码态/隔离安装基线已完成且不回滚。Phase 2C 的 A–G 实现与 CP3 已完成：通用 workflow/review/documentation/git/design 基础能力已唯一迁入共享 Discipline，Apple 只保留平台扩展；全量 Conformance 与独立最终审查均已通过。真实本机旧安装切换与发布级生命周期仍归 Phase 6。

## 为什么需要它

当同一工作环境同时包含 iOS、Android、Web、Backend、Desktop、Design 与 QA 时，仅依赖文件扩展名或提示词猜测容易误路由。本项目将判断过程拆成稳定合同：

```text
Repository / Task / Target Files
              ↓
      Discovery Engine
              ↓
       Project Profile
              ↓
       Policy Resolver
              ↓
      Capability DAG Plan
              ↓
 Runtime + Scheduler + Ledger
              ↓
      Delivery Report
```

平台差异由仓库内独立平台包及其 Manifest 声明，Core 只依赖 Capability ID，不硬编码具体 Skill 名称。平台包与 Core 同仓维护，但保持独立目录、版本、权限和 Conformance 边界。

## 当前能力

- **只读项目发现**：识别 Apple、Android、Web、Backend、Desktop、Monorepo、共享协议和 unknown 项目。
- **安全路由**：用户显式目标、任务语义、目标文件、cwd 与项目证据按优先级解析。
- **可解释决策**：输出 reason code、置信度、来源、覆盖候选和 Policy fingerprint。
- **Capability 合同**：统一输入输出 Schema、权限、副作用、幂等性、资源键和失败码。
- **确定性 DAG**：检测缺失能力、Provider 冲突和依赖循环；相同输入生成稳定计划。
- **Runtime 状态机**：覆盖 retry、timeout、cancel、stale、approval 和 resume。
- **资源调度**：确定性锁顺序，记录 requested、acquired、released、timed-out 与 cancelled 事件。
- **可恢复 Ledger**：append-only JSONL、Plan fingerprint 校验和中断恢复。
- **离线合同校验**：18 个版本化 JSON Schema、Manifest/Provider/Install Plan/Migration Audit 校验和非法 golden 样例。
- **仓内 Apple Provider**：`platforms/apple/provider/manifest.json` 默认参与源码态和安装态解析；P2A 外部 Provider 路径仍保留为兼容测试，重复 Provider 不静默覆盖。
- **选择安装**：`agent-skills install` 支持 `--core-only`、单/多平台、`all` 与显式 `--discipline`；版本化 `package_requires` 自动求必需依赖闭包，并记录选择原因和解析边。
- **单一全局 AGENTS**：Core、共享 Discipline 与已选平台只贡献带 scope 的 Fragment，按依赖拓扑稳定合成一个受管 `AGENTS.md`；Fragment/Skill 冲突及未受管目标均 fail-closed。
- **共享 Discipline**：`documentation`、`git`、`workflow`、`review`、`design` 各自拥有独立 Manifest、版本、权限与安装边界；Apple 通过 `package_requires` 获得闭包，不保留重复可安装副本。
- **平台真值**：Apple 为 `implemented`；Android、Web、Backend、Desktop 为 `bootstrap-only`，只能输出 `bootstrap_required`，不会产生 phantom Binding 或 ready Plan。
- **迁移审计 v2**：不可变 iOSAgentSkills 来源清单通过 relocation/transformation map 映射到当前包清单；206 项 retained、59 项 relocated、22 项 transformed、1 项 removed，并记录 5 个仓内 addition；License provenance 明确标为 pending。
- **安装完整性**：Install Plan/Lock v2 冻结 package source hash、Capability Provider、flattened asset allowlist、rule trace 及完整 path/hash/canonical mode；篡改、额外文件、symlink、Binding 越界、Provider 权限扩大、兼容越界及 staged TOCTOU 均在 swap 前 fail-closed。
- **显式 Runtime Config**：Codex profiles/shared config 已迁入 `runtime-configs/codex`；只有显式 `--runtime-config codex` 才会进入安装闭包，选择 Apple 不会隐式改写全局工具行为。
- **结构化 Adapter**：冻结 Provider binding/hash 与每次外部调用的 `invocation_id`，校验 request/result identity、验证缺口、artifact hash 与独立 reviewer actor。
- **双路径基线**：`doc-only / code-small / code-medium / code-risky` 四类 legacy/Core route comparison 使用 canonical baseline hash。
- **跨版本 Conformance**：GitHub Actions 配置 Python 3.11–3.14 matrix。

## 项目结构

```text
AgentDevelopmentSkills/
├── AGENTS.md                 # 仓库级执行合同
├── platforms/                # 同仓平台包：Manifest、Skills、脚本与平台规则
│   ├── core/                 # Core Fragment
│   ├── apple/                # Apple Provider、Skills、配置、脚本与 Fragment
│   ├── android/
│   ├── web/
│   ├── backend/
│   └── desktop/
├── disciplines/              # 跨平台共享能力包；独立 Manifest、权限与安装边界
│   ├── documentation/        # html-docs
│   ├── git/                  # git-workflow、gh-pr-flow
│   ├── workflow/             # 通用编排、角色模板与报告合同
│   ├── review/               # 独立 code-review 合同与 reviewer 模板
│   └── design/               # 设计系统与 Canonical UI IR 基础
├── runtime-configs/
│   └── codex/                # 显式 opt-in 的 Codex profiles/shared config
├── migration/                # Migration Audit v2 map 与当前包 inventory
├── schemas/                  # 版本化 JSON Schema
├── src/agent_workflow/
│   ├── discovery/            # 只读项目画像
│   ├── policy/               # 策略解析与 Decision Trace
│   ├── planning/             # Capability DAG 编译
│   ├── registry/             # Manifest 与 Provider 注册
│   ├── runtime/              # 状态机、Scheduler、Approval、Ledger
│   └── reporting/            # Delivery Report
├── tests/                    # 单元测试、fixtures、非法 golden
├── scripts/                  # Schema、Manifest、Conformance 校验
└── docs/                     # 架构与实施文档
```

`platforms/apple/migration-source.json` 冻结 iOSAgentSkills 来源 commit、288 个受控文件及内容 hash；迁入后以本仓副本为修改真源，不进行长期双写。

多平台安装只生成一个受管 `~/.codex/AGENTS.md`：Core 提供通用规则，已选平台只贡献带 scope 的最小分片，详细规则按需保留在 Skill/Manifest。平台包不得分别软链或复制自己的根 `AGENTS.md` 到同一全局路径；无法确定性合并时必须 fail-closed。

## 环境要求

- Python 3.11+
- Runtime 无第三方依赖
- 构建后端：`setuptools>=68`

## 快速开始

### 从源码直接运行

```bash
git clone git@github.com:ChoshimWy/AgentDevelopmentSkills.git
cd AgentDevelopmentSkills

PYTHONPATH=src python3 -m agent_workflow.cli detect /path/to/repository
```

### 安装 CLI

```bash
python3 -m pip install -e .
agent-workflow --help
agent-skills --help
```

## CLI 示例

### 1. 生成项目画像

```bash
agent-workflow detect /path/to/repository \
  --target-file apps/ios/Sources/Feature.swift
```

### 2. 查看路由解释

```bash
agent-workflow route /path/to/repository \
  --task "实现 iOS 页面并补充测试" \
  --explain
```

### 3. 选择安装平台包

```bash
# 先在隔离目录预览，不写入当前本机配置
agent-skills install --platform apple \
  --target-root /tmp/agent-skills-codex \
  --dry-run

# 仅安装 Core；不会安装 Apple Skills、binding、权限或规则
agent-skills install --core-only \
  --target-root /tmp/agent-skills-core

# 多个平台通过重复参数选择；all 只选择当前可安装的平台包
agent-skills install --platform apple --platform web
agent-skills install --platform all

# Runtime Config 必须显式选择，不随 Apple 隐式安装
agent-skills install --platform apple --runtime-config codex
```

`--discipline <id>` 支持显式选择共享包；选择 Apple 时会通过版本化 `package_requires` 自动闭包 `documentation`、`git`、`workflow`、`review` 与 `design`。`--runtime-config <id>` 只接受显式选择。`install` 默认执行写入；预览必须显式传 `--dry-run`。未传 `--target-root` 时目标为 `~/.codex`。安装器只管理 `AGENTS.md`、`skills/` 与 `.agent-skills/`；若这些路径不是本安装器创建的（包括当前 iOSAgentSkills 软链），会拒绝覆盖。实际替换本机旧流程与发布级生命周期仍必须在 Phase 6 完成。

### 4. 生成确定性执行计划（仓内 Apple Provider）

```bash
agent-workflow plan /path/to/repository \
  --task "修复设备离线状态" \
  --dry-run > workflow-plan.json
```

默认注册仓内 Apple Provider，不再要求 sibling 路径；`--provider-manifests` 仅保留给 ID 不冲突的第三方扩展。

### 5. 校验机器产物

```bash
agent-workflow validate workflow-plan workflow-plan.json
```

### 6. 验证 Runtime 合同

```bash
agent-workflow run workflow-plan.json \
  --ledger run-ledger.jsonl \
  --fake-adapters

agent-workflow resume workflow-plan.json \
  --ledger run-ledger.jsonl \
  --fake-adapters
```

`--fake-adapters` 只用于 Runtime / Conformance 验证，不代表真实平台实现、构建或测试已经执行。Phase 2 还可用 `prepare-adapter --invocation-id <id>`、`validate-adapter-result` 与 `--adapter-results/--adapter-context` 接收外部 Provider 的结构化证据；每次真实外部调用必须使用新的 `invocation_id`，Core 自身不执行 Xcode 命令。

## 路由安全语义

| 场景 | 结果 |
|---|---|
| 必需 Capability 缺失 | `blocked` |
| unknown code / QA / investigation 项目 | `blocked` |
| 同路径存在未解除的平台歧义 | `blocked` |
| 可选 Capability 缺失 | `degraded`，Runtime 最终为 `partial` |
| Provider 冲突或依赖循环 | fail-closed |
| 非幂等节点失败 | 不自动重试 |
| Plan fingerprint 变化 | 拒绝恢复旧 Ledger |

## 验证

运行完整 Phase 2 Core/Provider Conformance：

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
```

也可以分别执行：

```bash
PYTHONPATH=src python3 scripts/validate_schemas.py
PYTHONPATH=src python3 scripts/validate_manifests.py
PYTHONPATH=src python3 scripts/validate_apple_package.py
python3 platforms/apple/scripts/lint_skill_schema.py --skills-dir platforms/apple/skills
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src scripts tests
```

当前基线：

- 154 个 unittest
- 18 个 JSON Schema
- 6 个非法 contract golden
- 13 个 package Manifest：Core、Apple、4 个 bootstrap-only 平台、5 个共享 Discipline 与 1 个显式 Codex Runtime Config；外部 Provider fixture 仅作兼容回归
- 288 个 iOSAgentSkills 来源受控文件：206 retained、59 relocated、22 transformed、1 removed，另有 5 additions；当前为 13 个 Apple Skills + 7 个共享 Skills
- 4 个 Apple legacy/Core route comparison cases

## 架构与实施文档

- [跨平台研发工作流架构](docs/cross-platform-agent-workflow-architecture.html)
- [Phase 1 Core Foundation 实施清单](docs/implementation/phase-1-core-foundation.html)
- [Phase 2 iOSAgentSkills Compatibility & Monorepo Integration 实施清单](docs/implementation/phase-2-ios-agent-skills-integration.html)
- [Phase 3 Design Provider & Canonical UI IR 实施清单](docs/implementation/phase-3-design-provider-and-canonical-ir.html)
- [Phase 4 QA Core & Desktop Minimum Package 实施清单](docs/implementation/phase-4-qa-core-and-desktop-minimum.html)
- [Phase 5 Android, Web, Backend & UI Binding Expansion 实施清单](docs/implementation/phase-5-platform-expansion.html)
- [Phase 6 Distribution, Lockfile & Governance 实施清单](docs/implementation/phase-6-distribution-and-governance.html)
- [仓库执行合同](AGENTS.md)

## 路线图

| 阶段 | 状态 | 范围 |
|---|---|---|
| Phase 0 | 已完成 | Architecture Baseline v1.2（单仓平台包与选择安装决策已回写） |
| Phase 1 | 已完成 | Core、Schema、Manifest、CLI、Runtime、fixtures 与 Conformance |
| Phase 2A | 已完成 | iOSAgentSkills 外部 Manifest、Provider Adapter、双路径 baseline/smoke、回退与真实仓 dry-run |
| Phase 2B | 已完成（源码/隔离安装范围） | Apple 包已迁入，选择安装、单一 AGENTS、完整性/回滚门禁与独立复审通过；真实本机迁移归 Phase 6 |
| Phase 2C | 已完成 | A–G 已落地：共享 Discipline、Design Split、Apple Normalize、bootstrap-only 平台、Lock v2、rule trace 与显式 Runtime Config；全量 Conformance 与独立最终审查通过 |
| Phase 3 | 待启动（基础边界已预抽取） | P2C 已提供 design system / Canonical UI IR base 与 Apple extension 边界；Product Design Provider、来源 Gateway、完整 Schema/权限/验收仍待实施 |
| Phase 4 | 待启动 | QA Core 与 Desktop 最小包 |
| Phase 5 | 待启动（平台占位已真值化） | Android、Web、Backend、Desktop 当前均为 bootstrap-only；真实 Provider、Skills 与 Conformance 仍待实施 |
| Phase 6 | 待启动（Lock v2 基础已前置） | P2C 已提供安装 Lock v2 与合成 trace；doctor、upgrade/uninstall、跨进程恢复、打包与发布治理仍待实施 |

## 当前限制

- Apple 源码态与隔离安装态已不依赖 sibling；sibling 只保留为 P2A 冻结对照，不得继续作为新修改真源。
- 当前本机若仍由旧 iOSAgentSkills 安装器维护 `~/.codex/AGENTS.md` / `skills` 软链，新安装器会 fail-closed，不会自动覆盖；本次未改写真实 `~/.codex`。
- Apple 平台资产与共享 Discipline 会进入各自受管包；Codex profiles/shared config 只有显式选择 `runtime-configs/codex` 才进入安装计划。全局 config/profile/bin 的激活、备份、升级与卸载仍由 Phase 6 治理，不能把隔离安装 smoke 等同于完整本机替换。
- Core 已支持 recorded structured Adapter evidence，但不会自行调用 Skill、Xcode MCP 或 wrapper；真实执行仍由 Agent/iOSAgentSkills 负责。
- Phase 2A、Phase 2B 与 Phase 2C 均已通过独立 reviewer；P2C A–G 的 154 个 unittest、完整 Conformance 与最终负向复审均已通过，阻塞问题：无。这些证据不代表真实业务工程 build/test 或真实 `~/.codex` 切换已完成。
- iOSAgentSkills 来源 commit/hash 与每个文件去向可审计，但 License/NOTICE provenance 当前仍为 `pending`；解决前不得把仓库标记为发布就绪。
- 本地只验证了 Python 3.14.3；Python 3.11–3.14 由 CI matrix 覆盖。
- wheel / sdist、全局 config/profile/bin 激活与真实本机旧流程切换尚未完成，归入 Phase 6；当前 `pip install -e .` 与源码隔离安装路径已验证。

## 设计原则

- 本地仓库事实优先，不执行 Discovery 期间发现的项目脚本。
- 默认最小权限、无网络、无凭据读取。
- canonical JSON 使用 UTF-8、键排序、紧凑格式、禁止 NaN，并保留末尾换行。
- 必需能力缺失、未知版本、非法状态、权限扩大和依赖循环必须 fail-closed。
- 平台专属实现保留在同仓独立平台包，Core 不演化成跨平台巨型 Skill；物理同仓不等于把平台细节并入 Core。
