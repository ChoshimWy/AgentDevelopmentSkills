# AgentDevelopmentSkills

面向 Codex 与其他开发 Agent 的跨平台工作流 Core。项目通过只读仓库发现、策略解析、Capability 合同和确定性 DAG，自动判断目标模块所需的研发流程，并以可解释、可恢复、fail-closed 的 Runtime 执行计划。

> 当前版本：`0.2.0`。Phase 1、Phase 2A–2C 与 Phase 3 已完成且不回滚：共享 Discipline、Product Design/Figma 显式 Provider、Design Source Gateway、Canonical UI IR/Registry/Packet 与 Apple Packet v2 已落地；全量 Conformance 与独立最终审查均已通过。真实本机旧安装切换、live Connector/设备采集与发布级生命周期仍需显式执行或归 Phase 6。

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
- **离线合同校验**：23 个版本化 JSON Schema、Manifest/Provider/Install Plan/Migration Audit 校验和非法 golden 样例。
- **仓内 Apple Provider**：`platforms/apple/provider/manifest.json` 默认参与源码态和安装态解析；P2A 外部 Provider 路径仍保留为兼容测试，重复 Provider 不静默覆盖。
- **选择安装**：`agent-skills install` 支持 `--core-only`、单/多平台、`all` 与显式 `--discipline`；版本化 `package_requires` 自动求必需依赖闭包，并记录选择原因和解析边。
- **单一全局 AGENTS**：Core、共享 Discipline 与已选平台只贡献带 scope 的 Fragment，按依赖拓扑稳定合成一个受管 `AGENTS.md`；Fragment/Skill 冲突及未受管目标均 fail-closed。
- **共享 Discipline**：`documentation`、`git`、`workflow`、`review`、`design` 各自拥有独立 Manifest、版本、权限与安装边界；Apple 通过 `package_requires` 获得闭包，不保留重复可安装副本。
- **平台真值**：Apple 与 Desktop 为 `implemented`；Android、Web、Backend 为 `bootstrap-only`，只能输出 `bootstrap_required`，不会产生 phantom Binding 或 ready Plan。
- **迁移审计 v2**：不可变 iOSAgentSkills 来源清单通过 relocation/transformation map 映射到当前包清单；117 项 retained、113 项 relocated、57 项 transformed、1 项 removed，并记录 35 个仓内 addition；License provenance 明确标为 pending。
- **安装完整性**：Install Plan/Lock v2 冻结 package source hash、Capability Provider、flattened asset allowlist、rule trace 及完整 path/hash/canonical mode；篡改、额外文件、symlink、Binding 越界、Provider 权限扩大、兼容越界及 staged TOCTOU 均在 swap 前 fail-closed。
- **显式 Runtime Config**：Codex profiles/shared config 已迁入 `runtime-configs/codex`；只有显式 `--runtime-config codex` 才会进入安装闭包，选择 Apple 不会隐式改写全局工具行为。
- **结构化 Adapter**：冻结 Provider binding/hash 与每次外部调用的 `invocation_id`，校验 request/result identity、验证缺口、artifact hash 与独立 reviewer actor。
- **iOS 自动验证门禁**：Apple code DAG 固定为 `implementation → affected-tests → verification.apple.auto → review → report`；`auto` 必须给出实际执行或已接受证据，否则只能显式返回 `no_test_reason`，测试选择本身不能宣称验证完成。Apple 验证已移除 Xcode MCP 快车道，统一为 `quick-verify` / checkpoint / final lane，经 `codex_verify` + shared build-queue 执行。当前 wrapper 已落地 exact-request fingerprint、in-flight attach、成功缓存、原子入队与结构化 artifact 校验；Verification Session、三层 fingerprint、same-or-stronger 跨请求复用、失败缓存、优先级与 `.xctestrun` 自动复用目前是可执行 scaffold / 后续集成合同，不得当作已执行 daemon 证据。
- **双路径基线**：`doc-only / code-small / code-medium / code-risky` 四类 legacy/Core route comparison 使用 canonical baseline hash。
- **跨版本 Conformance**：GitHub Actions 配置 Python 3.11–3.14 matrix。
- **Skill 命名门禁**：`skill-naming-policy.json` 规定共享、平台、目标系统、工具链与显式例外生命周期的稳定命名；`scripts/validate_skill_naming.py` 校验扁平命名空间、目录/frontmatter 一致性、平台前缀、canonical orchestration、deprecated binding 与 bootstrap-only phantom Skill。人类可读规范见 [`docs/skill-naming-convention.html`](docs/skill-naming-convention.html)。

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
├── skill-naming-policy.json # Skill 命名机器真源
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

### 推荐：源码仓一键安装 Apple 工作流

```bash
# 只预览，不修改 ~/.codex
./install.sh --dry-run

# 安装 Apple、共享 Discipline 与 Codex Runtime Config
./install.sh

# 自动化场景输出 canonical JSON
./install.sh --platform apple --dry-run --json

# 预览卸载，不修改目标目录
./uninstall.sh --platform apple --dry-run

# 卸载当前 source installer 管理的全部平台与激活资产
./uninstall.sh --platform all
```

### 远程一键安装发布合同（Bootstrap Anchor）

发布后面向用户的入口采用两个薄 bootstrap；当前尚未发布公开 distribution host，因此以下 URL 仅表示冻结后的交付形态，不是已可用地址：

```bash
# macOS / Linux / WSL2
curl -fsSL --proto '=https' --tlsv1.2 https://<distribution-host>/install.sh | bash
```

```powershell
# Windows PowerShell；当前 release manifest 尚未声明 windows artifact，会 fail-closed
iwr -useb https://<distribution-host>/install.ps1 | iex
```

`install.sh` / `install.ps1` 先下载 canonical `release-manifest.json`，读取版本固定的 `asset_base_url`，再按 manifest 声明的 size + SHA-256 校验共享 `bootstrap_install.py` 后执行；共享 Core 按 host 选择唯一 artifact，验证 size + SHA-256，并拒绝非 HTTPS/降级重定向、ZIP 规范化别名、大小写或 Unicode normalization 冲突、symlink、path traversal 和解压上限越界。默认 production artifact 当前仅声明 `darwin` / `linux`，WSL2 复用 Linux；Windows bootstrap 已进入 CI，但在 Windows 权限、路径与事务 Conformance 完成前不得把 `windows` 写入 production manifest。

开发态可生成确定性 ZIP、bootstrap assets 与 manifest：

```bash
# 正式 stable/beta 构建拒绝 dirty source
python3 scripts/build_release_bundle.py --output dist/release

# 仅供未提交工作区验证
python3 scripts/build_release_bundle.py \
  --allow-dirty --channel development --output /tmp/agent-skills-release
```

`install.sh` 在 TTY 中默认显示 Manifest 驱动的平台复选菜单：Apple/iOS 与 Desktop 均具备 `implemented` Manifest 和 source smoke handler；Apple 默认显示为 `[x]`，Desktop 可显式选择且不激活 Apple/Codex 专属文件；Android、Web、Backend 以 `[ ]` 和 `bootstrap-only` 状态列出但暂不可选。使用 `↑` / `↓` 移动光标、`Space` 选择或取消、`Enter` 确认；确认后清除整个平台菜单，未来多个平台就绪后可组合选择。默认人类可读结果直接从所选平台的预览/完成状态开始，不重复显示产品标题与分隔线，并保留变更摘要与安装态验证；目标目录、规划平台、Runtime Config、安装包、Skill 数量及旧软链迁移状态等完整元信息仅由 `--json` 输出。非交互或自动化场景使用可重复的 `--platform <id>`（或 `--platform all`），传 `--json` 时必须显式选择平台并输出 canonical JSON；dry-run 与真实安装共用 managed-root preflight，避免预览通过但执行失败。默认目标为 `${CODEX_HOME:-$HOME/.codex}`，也支持 `--target-root <path>`。它只直接移除可精确识别的旧 iOSAgentSkills `AGENTS.md` / `skills` 软链，不创建旧配置持久备份；除普通文件形式的 macOS Finder 元数据 `.DS_Store` 外，未知文件、目录或软链继续 fail-closed。迁移时保留 Codex 管理的 `skills/.system`，合并而非清空本机 `config.toml`，并激活 8 个 custom agents、6 个缺省 profile、`codex_verify`、构建日志摘要器与 UI smoke 模板。安装后直接针对目标目录执行 Apple route/plan/review/report smoke；后置 smoke/activation 与受管根共享单进程临时回滚窗口，临时数据完成后删除。

`uninstall.sh` 只接受 Lock、AGENTS、Skills、package snapshot 与 activation file 均未被修改的受管安装；dry-run 与真实卸载执行相同 preflight。卸载使用单进程临时回滚窗口，移除受管根与 activation lock 记录的 custom agents/bin/templates，保留 Codex 自身的 `skills/.system`、现有 profiles、未归属本工具的目录内容，以及 ownership 未记录的 activation 父目录；`config.toml` 只定向移除仍指向受管 `AGENTS.md` 的根级 `model_instructions_file` assignment，保留其余原始 bytes、注释、排版和文件 mode。旧 iOSAgentSkills 软链因安装时未创建持久备份而不会自动恢复。当前仅支持一次卸载当前安装中全部已选平台；未来多平台的部分卸载与剩余规则重组仍属于 Phase 6 后续范围。

仓库内执行 `./install.sh` 时继续走 source-checkout fast path，不联网且不默认执行 `pip`；只有托管/管道执行且找不到同目录 `scripts/install_local.py` 时才进入远程 bootstrap。确定性 ZIP/manifest 已具备，但公开 distribution host、stable release、wheel/sdist、doctor、upgrade、多平台部分卸载与跨进程恢复仍未完成。

### 底层安装 CLI

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

`--discipline <id>` 支持显式选择共享包；选择 Apple 时会通过版本化 `package_requires` 自动闭包 `documentation`、`git`、`workflow`、`review` 与 `design`。`--runtime-config <id>` 只接受显式选择。`install` 默认执行写入；预览必须显式传 `--dry-run`。未传 `--target-root` 时目标为 `~/.codex`。底层安装器只管理 `AGENTS.md`、`skills/` 与 `.agent-skills/`；旧软链迁移与 Codex 全局资产激活统一由根目录 `install.sh` 处理。

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
python3 scripts/validate_skill_naming.py
PYTHONPATH=src python3 scripts/validate_apple_package.py
PYTHONPATH=src:. python3 scripts/build_phase4_qa_goldens.py --check
PYTHONPATH=src:. python3 scripts/build_phase4_desktop_goldens.py --check
PYTHONPATH=src python3 scripts/run_ios_installed_workflow_smoke.py
PYTHONPATH=src:. python3 scripts/run_desktop_installed_workflow_smoke.py
python3 platforms/apple/scripts/lint_skill_schema.py --skills-dir platforms/apple/skills
python3 platforms/apple/scripts/lint_skill_schema.py --skills-dir disciplines/qa/skills --strict
python3 platforms/apple/scripts/lint_skill_schema.py --skills-dir platforms/desktop/skills --strict
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src scripts tests
```

当前基线：

- 325 个 P1–P4 scoped unittest
- 43 个 Core / Shared Discipline JSON Schema
- 11 个非法 contract golden
- 17 个仓内/运行时 Manifest + 2 个显式外部 Provider Manifest：Core、Apple/Desktop package 与 provider、3 个 bootstrap-only 平台、6 个共享 Discipline、2 个 Design Provider bootstrap 与 1 个显式 Codex Runtime Config；外部 Provider 默认不启用
- 288 个 iOSAgentSkills 来源受控文件：117 retained、113 relocated、57 transformed、1 removed，另有 35 additions；当前为 13 个 Apple Skills + 10 个共享 Skills，历史名称不保留兼容副本
- 4 个 Apple legacy/Core route comparison cases

## 架构与实施文档

- [跨平台研发工作流架构](docs/cross-platform-agent-workflow-architecture.html)
- [多 Session Worktree 跨平台架构与 iOS 首期方案](docs/multi-session-worktree-architecture.html)
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
| Phase 3 | 已完成；Conformance 与独立 reviewer 均通过 | Product Design / Figma 显式 Provider、只读 Gateway、共享 Evidence/IR/Registry/Packet、Apple Packet v2 与 UI report 已贯通；live Connector 仍需显式授权 |
| Phase 4 | 已完成；CP0–CP3、Conformance 与独立 reviewer 均通过 | QA Core、三类 QA workflow、Desktop 最小 Provider/Adapter、环境画像、真实 Plan/RunLedger 聚合与 goldens 已落地 |
| Phase 5 | 暂缓（先完成 iOS host readiness） | Android、Web、Backend 继续保持 bootstrap-only；Desktop 后续仅扩展更多 framework Adapter/UI Binding |
| Phase 6 | 进行中（源码安装/全量卸载已落地） | 根目录 `install.sh` 已贯通平台选择、资产激活与安装态 smoke；`uninstall.sh` 已提供 dry-run、完整性拒绝、事务回滚与全量卸载。doctor、upgrade、多平台部分卸载、跨进程恢复、打包与发布治理仍待实施 |

## 当前限制

- Apple 源码态与隔离安装态已不依赖 sibling；sibling 只保留为 P2A 冻结对照，不得继续作为新修改真源。
- 根目录 `install.sh` 可识别并直接移除旧 iOSAgentSkills `~/.codex/AGENTS.md` / `skills` 软链；按用户约束不生成旧配置持久备份，但未知本地内容仍拒绝覆盖。2026-07-15 已完成真实 `~/.codex` 切换，随后 dry-run 显示 config、12 个受管文件与 6 个 profiles 均已一致。
- `install.sh` 显式选择 Codex Runtime Config，激活 config/profile/agents/bin/templates；受管激活文件记录在 `.agent-skills/activation-lock.json`，本机 profile 与 config 中的 runtime 偏好保留。`uninstall.sh` 可安全移除受管安装和激活文件，但不会推断删除既有 profile/shared config，也不会恢复未备份的旧软链。
- Phase 4 已完成 QA discipline、10 个 QA Schema、PRD/Bug/Release workflow、risk-based coverage、Delivery Report 双轴摘要、Desktop Provider/Adapter、15 份 QA golden 与 6 份 Desktop/CP1 golden；公开 workflow compiler 只规划、不合成 outcome/evidence，聚合器绑定真实 Workflow Plan/RunLedger、环境指纹、缺陷与回归 ownership，完整 Conformance 与独立 reviewer gate 已通过。
- Core 已支持 recorded structured Adapter evidence，但不会自行调用 Skill、Verification Coordinator 或 wrapper；真实执行仍由 Agent/iOSAgentSkills 负责。`scripts/run_ios_installed_workflow_smoke.py` 证明隔离安装态的 discovery/plan/structured evidence/review/report 合同闭环，不冒充真实业务工程 Xcode build/test。
- Phase 2A、Phase 2B 与 Phase 2C 的历史独立 reviewer 均已通过；本轮 iOS readiness、source install anchor 与验证闭环优化的 183 个 unittest、完整 Conformance 已通过，安装脚本独立复审结论为“阻塞问题：无”。这些证据不代表真实业务工程 Xcode build/test 已完成。
- iOSAgentSkills 来源 commit/hash 与每个文件去向可审计，但 License/NOTICE provenance 当前仍为 `pending`；解决前不得把仓库标记为发布就绪。
- 本地只验证了 Python 3.14.3；Python 3.11–3.14 由 CI matrix 覆盖。
- wheel / sdist、doctor、upgrade、多平台部分卸载与跨进程恢复仍未完成；当前源码仓 install → dry-run uninstall → transactional uninstall 已在临时目标验证，尚未对真实 `~/.codex` 执行卸载。

## 设计原则

- 本地仓库事实优先，不执行 Discovery 期间发现的项目脚本。
- 默认最小权限、无网络、无凭据读取。
- canonical JSON 使用 UTF-8、键排序、紧凑格式、禁止 NaN，并保留末尾换行。
- 必需能力缺失、未知版本、非法状态、权限扩大和依赖循环必须 fail-closed。
- 平台专属实现保留在同仓独立平台包，Core 不演化成跨平台巨型 Skill；物理同仓不等于把平台细节并入 Core。
