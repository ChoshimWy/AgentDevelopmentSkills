# AgentDevelopmentSkills

面向 Codex 与其他开发 Agent 的跨平台工作流 Core。项目通过只读仓库发现、策略解析、Capability 合同和确定性 DAG，自动判断目标模块所需的研发流程，并以可解释、可恢复、fail-closed 的 Runtime 执行计划。

> 当前版本：`0.1.0`，Phase 1 Core Foundation 已完成。真实平台 Skill、iOSAgentSkills 接入和安装器属于后续阶段。

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

平台差异由 Manifest 和外部能力包声明，Core 只依赖 Capability ID，不硬编码具体 Skill 名称。

## Phase 1 能力

- **只读项目发现**：识别 Apple、Android、Web、Backend、Desktop、Monorepo、共享协议和 unknown 项目。
- **安全路由**：用户显式目标、任务语义、目标文件、cwd 与项目证据按优先级解析。
- **可解释决策**：输出 reason code、置信度、来源、覆盖候选和 Policy fingerprint。
- **Capability 合同**：统一输入输出 Schema、权限、副作用、幂等性、资源键和失败码。
- **确定性 DAG**：检测缺失能力、Provider 冲突和依赖循环；相同输入生成稳定计划。
- **Runtime 状态机**：覆盖 retry、timeout、cancel、stale、approval 和 resume。
- **资源调度**：确定性锁顺序，记录 requested、acquired、released、timed-out 与 cancelled 事件。
- **可恢复 Ledger**：append-only JSONL、Plan fingerprint 校验和中断恢复。
- **离线合同校验**：13 个版本化 JSON Schema、Manifest 校验和非法 golden 样例。
- **跨版本 Conformance**：GitHub Actions 配置 Python 3.11–3.14 matrix。

## 项目结构

```text
AgentDevelopmentSkills/
├── AGENTS.md                 # 仓库级执行合同
├── platforms/                # 内置平台 Manifest
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

### 3. 生成确定性执行计划

```bash
agent-workflow plan /path/to/repository \
  --task "修复设备离线状态" \
  --dry-run > workflow-plan.json
```

### 4. 校验机器产物

```bash
agent-workflow validate workflow-plan workflow-plan.json
```

### 5. 验证 Runtime 合同

```bash
agent-workflow run workflow-plan.json \
  --ledger run-ledger.jsonl \
  --fake-adapters

agent-workflow resume workflow-plan.json \
  --ledger run-ledger.jsonl \
  --fake-adapters
```

`--fake-adapters` 只用于 Phase 1 Runtime / Conformance 验证，不代表真实平台实现、构建或测试已经执行。

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

运行完整 Phase 1 Conformance：

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
```

也可以分别执行：

```bash
PYTHONPATH=src python3 scripts/validate_schemas.py
PYTHONPATH=src python3 scripts/validate_manifests.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src scripts tests
```

当前基线：

- 59 个 unittest
- 13 个 JSON Schema
- 6 个非法 contract golden
- 6 个内置 Manifest
- 独立 reviewer：阻塞问题无

## 架构与实施文档

- [跨平台研发工作流架构](docs/cross-platform-agent-workflow-architecture.html)
- [Phase 1 Core Foundation 实施清单](docs/implementation/phase-1-core-foundation.html)
- [Phase 2 iOSAgentSkills Compatibility Integration 实施清单](docs/implementation/phase-2-ios-agent-skills-integration.html)
- [Phase 3 Design Provider & Canonical UI IR 实施清单](docs/implementation/phase-3-design-provider-and-canonical-ir.html)
- [Phase 4 QA Core & Desktop Minimum Package 实施清单](docs/implementation/phase-4-qa-core-and-desktop-minimum.html)
- [Phase 5 Android, Web, Backend & UI Binding Expansion 实施清单](docs/implementation/phase-5-platform-expansion.html)
- [Phase 6 Distribution, Lockfile & Governance 实施清单](docs/implementation/phase-6-distribution-and-governance.html)
- [仓库执行合同](AGENTS.md)

## 路线图

| 阶段 | 状态 | 范围 |
|---|---|---|
| Phase 0 | 已完成 | Architecture Baseline v1.0 |
| Phase 1 | 已完成 | Core、Schema、Manifest、CLI、Runtime、fixtures 与 Conformance |
| Phase 2 | 待启动 | iOSAgentSkills Manifest 与兼容接入 |
| Phase 3 | 待启动 | Product Design Provider、Figma / Sketch 与 Canonical Design IR |
| Phase 4 | 待启动 | QA Core 与 Desktop 最小包 |
| Phase 5 | 待启动 | Android、Web、Backend 与更多平台/UI Binding |
| Phase 6 | 待启动 | Lockfile、权限、安装升级与发布治理 |

## 当前限制

- 内置平台 Manifest 目前只提供检测信号和 Capability 合同，尚未连接真实平台 adapter。
- `run` / `resume` 目前要求显式使用 `--fake-adapters`。
- 本地只验证了 Python 3.14.3；Python 3.11–3.14 由 CI matrix 覆盖。
- wheel / sdist 安装态 smoke 尚未纳入 Phase 1 完成门禁。

## 设计原则

- 本地仓库事实优先，不执行 Discovery 期间发现的项目脚本。
- 默认最小权限、无网络、无凭据读取。
- canonical JSON 使用 UTF-8、键排序、紧凑格式、禁止 NaN，并保留末尾换行。
- 必需能力缺失、未知版本、非法状态、权限扩大和依赖循环必须 fail-closed。
- 平台专属实现保留在独立能力包，Core 不演化成跨平台巨型 Skill。
