# AgentDevelopmentSkills

面向 Codex 与其他开发 Agent 的跨平台工作流 Core。项目通过只读仓库发现、策略解析、Capability 合同和确定性 DAG，自动判断目标模块所需的研发流程，并以可解释、可恢复、fail-closed 的 Runtime 执行计划。

> 当前版本：`0.2.0`。Phase 1、Phase 2A–2C、Phase 3 与 Phase 4 已完成且不回滚；Phase 6 仓内实现已完成 Persistent Lock、只读 Doctor、managed-root/source 可逆 Upgrade/Rollback、多平台部分卸载、真实 Schema migration、deterministic Packaging、qualification handoff、GitHub Pages 控制面与 GitHub Releases 不可变资产发布链路，并通过供应链/RC 门禁。Python 3.11–3.14 clean CI aggregate 已在 commit `44c52d2` 与 run `29550350238` 通过；发布资格仍由仓库级 License/NOTICE owner 决策及可信 signer 签字 fail-closed，Pages 当前未部署，真实本机旧安装切换、live Connector/设备采集与发布动作必须显式执行。

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
- **离线合同校验**：61 个版本化 JSON Schema、Manifest/Provider/Install Plan/Persistent Lock/Doctor/Upgrade/Rollback/Migration/Packaging/Signed Release Review/Python Compatibility Evidence/Release Gate/Release Qualification Handoff 校验和非法 golden 样例。
- **仓内 Apple Provider**：`platforms/apple/provider/manifest.json` 默认参与源码态和安装态解析；P2A 外部 Provider 路径仍保留为兼容测试，重复 Provider 不静默覆盖。
- **选择安装**：`agent-skills install` 支持 `--core-only`、单/多平台、`all` 与显式 `--discipline`；版本化 `package_requires` 自动求必需依赖闭包，并记录选择原因和解析边。
- **单一全局 AGENTS**：Core、共享 Discipline 与已选平台只贡献带 scope 的 Fragment，按依赖拓扑稳定合成一个受管 `AGENTS.md`；Fragment/Skill 冲突及未受管目标均 fail-closed。
- **共享 Discipline**：`documentation`、`git`、`workflow`、`review`、`design` 各自拥有独立 Manifest、版本、权限与安装边界；Apple 通过 `package_requires` 获得闭包，不保留重复可安装副本。
- **平台真值**：Apple 与 Desktop 为 `implemented`；Android、Web、Backend 为 `bootstrap-only`，只能输出 `bootstrap_required`，不会产生 phantom Binding 或 ready Plan。
- **迁移审计 v2**：不可变 iOSAgentSkills 来源清单通过 relocation/transformation map 映射到当前包清单；117 项 retained、113 项 relocated、57 项 transformed、1 项 removed，并记录 35 个仓内 addition；License provenance 明确标为 pending。
- **安装完整性**：Install Plan/Lock v2 冻结安装事务输入；独立 `agent-skills.lock` v1 持久化 Core/Schema/package/source/version/Manifest/Binding/permission 身份并进入 Workflow Plan 与 RunLedger 指纹。篡改、额外文件、symlink、Binding 越界、Provider 权限扩大、兼容越界及 staged TOCTOU 均 fail-closed。
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

- Python 3.11+；`install.sh` 会自动跳过系统旧版 `python3`，并探测 PATH 中任意兼容的 `python3.x`、Homebrew、`~/.local/bin` 与 pyenv；也可用 `AGENT_SKILLS_PYTHON` 显式指定并提前校验
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

发布控制面采用 GitHub Pages，版本化不可变资产采用 GitHub Releases。当前 Pages 尚未通过最终 Release Gate 部署，因此以下 URL 是已冻结但尚不可用的正式入口：

```bash
# macOS / Linux / WSL2
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh | bash
```

```powershell
# Windows PowerShell；当前 release manifest 尚未声明 windows artifact，会 fail-closed
iwr -useb https://choshimwy.github.io/AgentDevelopmentSkills/install.ps1 | iex
```

Pages 只发布 `index.html`、两个薄 bootstrap、canonical `release-manifest.json`、Gate/版本摘要和 `.nojekyll`，不承载 ZIP、wheel 或 sdist。Manifest 的 `asset_base_url` 固定指向 `https://github.com/ChoshimWy/AgentDevelopmentSkills/releases/download/v<version>/`。`install.sh` / `install.ps1` 先从 Pages 下载 manifest，再按 manifest 声明的 size + SHA-256 验证 Releases 中的共享 `bootstrap_install.py`；共享 Core 按 host 选择唯一 artifact，验证 size + SHA-256，拒绝非 HTTPS/降级重定向、ZIP 规范化别名、大小写或 Unicode normalization 冲突、symlink、path traversal 和解压上限越界，再从临时目录调用同一 `scripts/install_local.py`。默认发布 artifact 当前仅声明 `darwin` / `linux`，WSL2 复用 Linux；Windows bootstrap 已进入源码与 CI 的 source-checkout 真实执行门禁，但在 Windows 权限/路径/事务 Conformance 完成前不得把 `windows` 写入 production manifest。

开发态可生成确定性 ZIP、bootstrap assets 与 manifest：

```bash
# 正式 stable/beta 构建拒绝 dirty source
python3 scripts/build_release_bundle.py --output dist/release

# 仅供未提交工作区验证
python3 scripts/build_release_bundle.py \
  --allow-dirty --channel development --output /tmp/agent-skills-release
```

每个 release directory 同时包含 source ZIP、byte-stable wheel/sdist、`python-artifacts.json`、逐文件 `sbom.json` 与 builder/source/artifact 绑定的 `provenance.json`。source ZIP、wheel data 与 sdist 都包含 `.github/workflows/conformance.yml`，确保发布物内完整 Conformance 不依赖 checkout 外的 governance 文件。官方 Python 产物不依赖联网下载 build backend；仓库根 `agent_build_backend.py` 实现 dependency-free PEP 517，以下两条路径生成同一 wheel：

```bash
python3 scripts/build_python_artifacts.py --output /tmp/python-artifacts
python3 -m pip wheel --no-index --no-deps . --wheel-dir /tmp/pep517-wheel
```

Python 3.11–3.14 兼容性不能仅依赖 CI job 名称。每个干净环境运行同一 evidence runner，冻结 source revision、完整 Python patch version、平台/架构、wheel/sdist hash、PEP 517 byte identity 与 packaging smoke；smoke 在 wheel-only venv 中覆盖 core-only、Apple、Desktop、Apple+Desktop 与 `all` 的安装/Doctor/全量卸载，并对 Apple/desktop 单平台及 multi/all 的选定路由执行 installed-wheel route，聚合器只接受四个版本齐全且 artifact bytes 跨版本一致的 matrix：

```bash
python3 scripts/python_compatibility_evidence.py run \
  --source-revision "$GITHUB_SHA" \
  --output "/tmp/python-${PYTHON_MINOR}.json"

python3 scripts/python_compatibility_evidence.py merge \
  /tmp/python-3.11.json /tmp/python-3.12.json \
  /tmp/python-3.13.json /tmp/python-3.14.json \
  --output /tmp/python-compatibility-v1.json
```

GitHub Actions 的 `python-distribution` matrix 上传单版本 evidence，`python-compatibility-matrix` 负责下载、exact merge 并保存 30 天 aggregate artifact。Release review 签名必须绑定 aggregate fingerprint；缺版本、source revision 不同、PEP 517 不一致或跨版本 artifact 漂移都会在执行 Candidate 前阻断。

在 clean commit 上可手动触发 `Cross-platform Conformance` 的 `workflow_dispatch`，显式选择 `beta` / `stable` 并输入外部 reviewer 公钥的 64 位 `key_id`。`release-qualification-handoff` job 只在四版本 aggregate 成功后运行：重新解析 Apple+Desktop candidate Lock、执行 candidate-bound 全量 Conformance、构建 clean release，再输出单一 `release-qualification-<commit>` artifact。Handoff 使用 `release-qualification-handoff-v1`，冻结 candidate Lock、release directory、Conformance evidence、Python matrix、未签名 review draft/payload 与无 candidate execution 的 Gate preflight；validate 会从 source ZIP 重建 exact Lock、复核 schema inventory/runner/Python artifacts，并重新执行无签名 Gate static preflight 后要求报告完全一致，任何 symlink、dirty/development candidate、unexpected blocker、文件或 cross-binding 漂移都会 fail-closed。输出父目录必须由当前 CI job 独占且不存在同用户并发 writer；完成内容通过单次目录 rename 原子发布。它不包含私钥或签名，也不等同于批准：

```bash
python3 scripts/prepare_release_qualification.py validate \
  /path/to/release-qualification-handoff

# reviewer 在隔离环境审查 handoff/release 后，只对 frozen payload 签名
openssl dgst -sha256 -sign /secure/reviewer-private.pem \
  -out /tmp/release-review.sig \
  /path/to/release-qualification-handoff/release-review-payload.json

python3 scripts/prepare_release_review.py finalize \
  --draft /path/to/release-qualification-handoff/release-review-draft.json \
  --signature /tmp/release-review.sig \
  --review-trust-store /secure/release-review-trust-store.json \
  --output /tmp/release-review.json

python3 scripts/run_release_gate.py \
  --release-dir /path/to/release-qualification-handoff/release \
  --conformance-evidence /path/to/release-qualification-handoff/conformance-evidence.json \
  --python-compatibility-evidence /path/to/release-qualification-handoff/python-compatibility-evidence.json \
  --review-evidence /tmp/release-review.json \
  --review-trust-store /secure/release-review-trust-store.json \
  --output /tmp/release-gate.json
```

最终发布由受保护的 `Publish verified release` workflow 完成。仓库 `main` 必须启用 branch protection；Environment `release` 必须限制为 `main`、配置 required reviewers，并保存 `RELEASE_REVIEW_TRUST_STORE_BASE64` secret（候选外部 trust store 的标准 Base64）。`workflow_dispatch` 只接受 qualification run ID、冻结的 40 位 source revision 和公开 review signature 的 Base64。Workflow 通过 GitHub API 要求 qualification run 来自本仓 `.github/workflows/conformance.yml` 的 successful `workflow_dispatch`、其 `head_branch/head_sha` 为 `main` 与当前 workflow revision，且该 revision 仍是受保护 `main` 的当前 commit；随后从指定 run 下载 exact handoff，验证外部签名，并在 GitHub-hosted disposable runner 重新执行完整 Gate。只有 Gate 为 `passed` 才创建 `v<version>` GitHub Release、上传候选及公开 evidence，并通过 `build_pages_site.py` 从 bounded snapshot 生成小型 Pages control plane（单文件 2 MiB、总计 8 MiB；构建期间输入变化即阻塞）。所有第三方 Actions 固定到完整 commit SHA。Pages Source 必须在仓库 Settings 中选择 **GitHub Actions**；任意同名 lightweight/annotated tag 或 Release 已存在时 workflow fail-closed，不覆盖、移动或重签已有资产。

### 首次真实发布前的外部收口清单

以下项目不能由仓内代码伪造，必须由仓库管理员和外部 signer 在首次发布前完成并留存证据：

1. 在 Settings → Branches 为 `main` 启用 required status checks、禁止 force-push，并确认 API 返回 `protected: true`。
2. 在 Settings → Environments 创建 `release` 与 `github-pages`；`release` 限制部署分支为 `main`、启用 required reviewers，写入 `RELEASE_REVIEW_TRUST_STORE_BASE64`；Pages Source 选择 **GitHub Actions**。
3. License/NOTICE owner 提供仓库级授权决策及 exact NOTICE 文件；重新生成 qualification handoff，使 Migration Audit 为 `verified`。
4. 外部 signer 审阅冻结 payload，使用 trust store 中 `trusted`、`phase-6-release` scope 的密钥签发 review signature；不得把私钥或 signer 工作目录上传到仓库/候选。
5. 仅使用当前受保护 `main` 的 qualification run、source revision 与 review signature dispatch 发布；workflow 完成后保存 Release、Pages `release.json`、manifest 与部署 smoke 输出，作为首次线上证据。

Handoff preflight 有意在签名前阻断 `release.independent-review`、candidate Conformance 与 wheel/sdist execution；只有 supply-chain、source policy、完整 Python matrix 等静态前置检查必须通过。最终 Gate 仍须在可销毁隔离 worker 中执行。

Release Candidate 不能只凭“构建成功”或自哈希 receipt 放行。`run_release_gate.py` 先把完整候选、evidence、signed review 与候选外部的 trust store 复制到有大小上限的稳定 snapshot，exact 绑定 source ZIP、sdist、wheel、standalone bootstrap、Manifest、Python index、SBOM 与 provenance；随后从 sdist 重建并逐字节比较 wheel/sdist，在干净 venv 离线安装 wheel，执行 Apple+Desktop install→Doctor，再用实际 Package Lock 在已验证 source ZIP 内真实运行完整 Conformance，并要求外部 receipt 的 suite/count/command identity 与真实结果一致。独立 review v3 必须绑定完整 release directory identity、source revision 与完整 Python compatibility matrix fingerprint，并由 trust store 中状态为 `trusted`、scope 包含 `phase-6-release` 的 RSA-2048+ key 使用 `rsa-pkcs1v15-sha256` 签发。私钥不得进入候选、仓库或 trust store；可先生成 canonical payload，交给外部签名设备，再组装并本地验签：

```bash
python3 scripts/prepare_release_review.py prepare \
  --release-dir /path/to/release \
  --python-compatibility-evidence /path/to/python-compatibility-v1.json \
  --key-id <trusted-key-id> \
  --draft-output /tmp/release-review-draft.json \
  --payload-output /tmp/release-review-payload.json

openssl dgst -sha256 -sign /secure/reviewer-private.pem \
  -out /tmp/release-review.sig /tmp/release-review-payload.json

python3 scripts/prepare_release_review.py finalize \
  --draft /tmp/release-review-draft.json \
  --signature /tmp/release-review.sig \
  --review-trust-store /secure/release-review-trust-store.json \
  --output /tmp/release-review.json
```

Trust store 使用 `release-review-trust-store-v1` 合同；`key_id` 是 `{algorithm, exponent, modulus_hex}` 的 canonical SHA-256 identity。撤销 key、未知 key、签名或 review 内容篡改、候选内自带 trust store、以及 Gate 执行期间 evidence/trust store 漂移均 fail-closed。

`run_release_gate.py` **不是恶意代码沙盒**：候选 wheel/sdist/source 中的代码只有在 exact release identity 已获得外部可信签名后才允许执行，签名 review 是执行授权边界。正式 Gate 必须运行在完成后销毁的隔离 CI worker/VM/container 中；reviewer 必须确认候选不创建 daemon、新 session 或其它脱离生命周期的后台进程，残余执行隔离风险由 Release Engineering owner。Gate 仍提供 defense-in-depth：冻结全部 artifact bytes、先安装/smoke frozen wheel 再执行 sdist builder、对 candidate command 设置 120–1800 秒 timeout、每个输出 stream 8 MiB 硬限制、POSIX `RLIMIT_FSIZE`/`RLIMIT_CPU`/禁用 core dump、独立 process group 与无条件 terminate/reap。上述边界不能替代 disposable runner，也不得用于执行尚未签字的 hostile candidate。

```bash
python3 scripts/run_release_gate.py \
  --release-dir /path/to/release \
  --conformance-evidence /path/to/upgrade-evidence.json \
  --python-compatibility-evidence /path/to/python-compatibility-v1.json \
  --review-evidence /path/to/release-review.json \
  --review-trust-store /secure/release-review-trust-store.json \
  --output /path/to/release-gate.json
```

Gate 只接受 clean `beta` / `stable` source；development/dirty build、缺 evidence、artifact/hash 漂移、无外部可信签名的独立 review 或 License/NOTICE provenance 未 `verified` 均返回 canonical `blocked` 和退出码 `2`。固定来源 commit `7e15f01fef050a8444f845893a74f8ab8ff4dcab` 没有仓库级 License/NOTICE，只有迁移到 `git` discipline 的 `gh-pr-flow/LICENSE.txt`；因此不能由构建器自行推断整体许可证。发布 owner 必须先提供仓库级授权决策与对应 NOTICE bytes，再将 SPDX identity、notice 相对路径和 SHA-256 写入 Migration Audit v2；Gate 会从 sdist 复核 exact bytes，独立 reviewer 仍需在 v3 签字中批准同一 release identity。在此之前 provenance 必须保持 `pending`。当前也尚无真实 release signer 签字，所以仓库已具备 RC gate，但**仍不得发布或宣称 release-ready**。

`install.sh` 在 TTY 中默认显示 Manifest 驱动的平台复选菜单：Apple/iOS 与 Desktop 均具备 `implemented` Manifest 和 source smoke handler；Apple 默认显示为 `[x]`，Desktop 可显式选择且不激活 Apple/Codex 专属文件；Android、Web、Backend 以 `[ ]` 和 `bootstrap-only` 状态列出但暂不可选。使用 `↑` / `↓` 移动光标、`Space` 选择或取消、`Enter` 确认；确认后清除整个平台菜单，未来多个平台就绪后可组合选择。默认人类可读结果直接从所选平台的预览/完成状态开始，不重复显示产品标题与分隔线，并保留变更摘要与安装态验证；目标目录、规划平台、Runtime Config、安装包、Skill 数量及旧软链迁移状态等完整元信息仅由 `--json` 输出。非交互或自动化场景使用可重复的 `--platform <id>`（或 `--platform all`），传 `--json` 时必须显式选择平台并输出 canonical JSON；dry-run 与真实安装共用 managed-root preflight，避免预览通过但执行失败。默认目标为 `${CODEX_HOME:-$HOME/.codex}`，也支持 `--target-root <path>`。它只直接移除可精确识别的旧 iOSAgentSkills `AGENTS.md` / `skills` 软链，不创建旧配置持久备份；除普通文件形式的 macOS Finder 元数据 `.DS_Store` 外，未知文件、目录或软链继续 fail-closed。迁移时保留 Codex 管理的 `skills/.system`，合并而非清空本机 `config.toml`，并激活 8 个 custom agents、6 个缺省 profile、`codex_verify`、构建日志摘要器与 UI smoke 模板。安装后直接针对目标目录执行 Apple route/plan/review/report smoke；后置 smoke/activation 与受管根共享单进程临时回滚窗口，临时数据完成后删除。

`uninstall.sh` 只接受 Lock、AGENTS、Skills、package snapshot 与（存在时）activation file 均未被修改的受管安装；dry-run 与真实卸载执行相同 preflight。它是 source install 以及 wheel CLI package-only install 的全量卸载入口：选择了 Codex Runtime Config/Package 的安装必须具备 activation lock，缺失时 fail-closed；core-only、Desktop 或未激活的 CLI 安装允许无 activation lock，并只移除三个受管根。事务使用单进程临时回滚窗口，移除受管根与 activation lock 记录的 custom agents/bin/templates，保留 Codex 自身的 `skills/.system`、现有 profiles、未归属本工具的目录内容，以及 ownership 未记录的 activation 父目录；`config.toml` 只定向移除仍指向受管 `AGENTS.md` 的根级 `model_instructions_file` assignment，保留其余原始 bytes、注释、排版和文件 mode。旧 iOSAgentSkills 软链因安装时未创建持久备份而不会自动恢复。多平台安装的定向移除改用底层 `agent-skills uninstall`，它从剩余 selection 重新解析闭包和唯一全局 AGENTS，保留显式 Discipline/Runtime Config；只有移除已激活 Apple 时才移除 activation-owned Codex Runtime Config，并以 exact rollback point 支持逆转。

仓库内执行 `./install.sh` 时继续走 source-checkout fast path，不联网且不默认执行 `pip`；只有托管/管道执行且找不到同目录 `scripts/install_local.py` 时才进入 Pages → Releases 远程 bootstrap。确定性 ZIP/manifest/wheel/sdist、底层只读 Doctor、managed-root/source 可逆 Upgrade/Rollback、多平台部分卸载与 activation-lock v1→v2 migration 已具备；Pages workflow 已落地但不会绕过最终 RC gate，公开入口与 stable release 仍受 License/NOTICE provenance 和真实 signer 阻塞。

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

### 4. 冻结与检查持久化 Lockfile

```bash
agent-skills lock resolve install-plan.json \
  --output agent-skills.lock

agent-skills lock validate agent-skills.lock
agent-skills lock explain agent-skills.lock
agent-skills lock diff previous.lock agent-skills.lock

agent-workflow plan /path/to/repository \
  --task "修复设备离线状态" \
  --lock agent-skills.lock \
  --dry-run > workflow-plan.json
```

`lock resolve` 默认使用本地 `registry://<package-id>` 身份，也接受显式 `--source package=./relative/path`；相对源会逐文件复核 Install Plan snapshot。受控 HTTPS URI 不得包含凭据、query 或 fragment，并必须同时提供 `--source-sha256 package=<artifact-sha256>`，下载执行仍由 bootstrap 的 artifact gate 负责。安装时 Lockfile 写入 `.agent-skills/agent-skills.lock`，其 fingerprint 同时锚入 `install-lock.json`；已存在但尚无持久化 Lockfile 的完整旧受管安装可安全升级，Lockfile 一旦存在则任何内容或 mode 漂移（包括自洽重算 fingerprint）都会阻塞替换。`--previous` 冻结前序 Lock hash；受控 Upgrade 会保存完整 managed-root rollback point。

### 5. 只读诊断安装态

```bash
agent-skills doctor --target-root /tmp/agent-skills-codex
```

Doctor 不修改目标目录，输出 canonical `doctor-report` v1，并检查 Python/Core、运行时 Schema inventory、Install Lock、Persistent Lock anchor、package/Skill/Manifest hash、Capability Binding、permission、唯一全局 `AGENTS.md` 的来源/分片顺序/rule trace/最终 hash、activation file 及安装/卸载恢复残留。全部通过时退出码为 `0`；任何篡改、symlink、未知路径、Schema 漂移、旧安装缺少 Persistent Lock 或恢复残留都会输出 `status=blocked` 并返回 `2`。源码树之外的打包环境可用 `--schemas <path>` 指向随发布物安装的 Schema 根。

### 6. 受控 managed-root Upgrade 与 Rollback

```bash
# 每次 preview/apply 都真实执行仓库 owned Conformance；输出完整审计 evidence
agent-skills upgrade --target-root /tmp/agent-skills-codex \
  --dry-run --output upgrade-plan.json --evidence-output upgrade-evidence.json

agent-skills upgrade --target-root /tmp/agent-skills-codex \
  --plan upgrade-plan.json --approve-plan <plan-fingerprint> \
  --evidence-output apply-evidence.json

agent-skills rollback --target-root /tmp/agent-skills-codex \
  --approve-current-lock <current-lock-hash> \
  --approve-rollback-point <rollback-point-fingerprint>

# 从 Apple + Desktop 安装中仅移除 Desktop；apply 必须复用 exact Plan
agent-skills uninstall --target-root /tmp/agent-skills-codex \
  --platform desktop --dry-run --output uninstall-plan.json
agent-skills uninstall --target-root /tmp/agent-skills-codex \
  --platform desktop --plan uninstall-plan.json \
  --approve-plan <plan-fingerprint>
```

Upgrade/Partial-Uninstall Plan 冻结 current/candidate selection、明确 removal request、稳定 Conformance attestation、权限差异、identity-only Schema compatibility、ordered steps、受信 Core activation/deactivation/preserve handler 与 exact rollback point。Install/Upgrade/Rollback/Source Install/Uninstall 共享目标级 lifecycle lock；中断后的 lock/stage/backup residue 由 Doctor 阻塞并报告。Source upgrade 先执行 installed workflow smoke，再原子 reconcile agents/bin/templates/config/profile；partial uninstall 对 remaining package/Core/Schema 强制 identity，只在移除已激活 Apple 时定向 deactivation，保留 Apple 时 snapshot 后 no-op。rollback point 同时冻结文件与 ancestor directory 的存在、bytes 和 mode，retired asset 与 0600 config 均可逆。当前只允许 Schema identity compatibility。

### 7. 生成确定性执行计划（仓内 Apple Provider）

```bash
agent-workflow plan /path/to/repository \
  --task "修复设备离线状态" \
  --lock agent-skills.lock \
  --dry-run > workflow-plan.json
```

默认注册仓内 Apple Provider，不再要求 sibling 路径；`--provider-manifests` 仅保留给 ID 不冲突的第三方扩展。

### 8. 校验机器产物

```bash
agent-workflow validate workflow-plan workflow-plan.json
```

### 9. 验证 Runtime 合同

```bash
agent-workflow run workflow-plan.json \
  --ledger run-ledger.jsonl \
  --lock agent-skills.lock \
  --fake-adapters

agent-workflow resume workflow-plan.json \
  --ledger run-ledger.jsonl \
  --lock agent-skills.lock \
  --fake-adapters
```

带 `package_lock_hash` 的 Plan 在每次 `run` / `resume` 时都必须通过 `--lock` 提供当前有效 Lockfile；只提供旧 Plan/旧 Ledger 而不重验当前 Lock 会 fail-closed。未冻结 Lock 的历史 Plan 可继续按原合同运行。`--fake-adapters` 只用于 Runtime / Conformance 验证，不代表真实平台实现、构建或测试已经执行。Phase 2 还可用 `prepare-adapter --invocation-id <id>`、`validate-adapter-result` 与 `--adapter-results/--adapter-context` 接收外部 Provider 的结构化证据；每次真实外部调用必须使用新的 `invocation_id`，Core 自身不执行 Xcode 命令。

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

- 436 个 P1–P6 scoped unittest
- 61 个 Core / Shared Discipline / Phase 6 JSON Schema
- 18 个非法 contract golden
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
| Phase 6 | 技术实现与 clean CI aggregate 已收口；发布资格 blocked | Lock/Doctor/Upgrade/Rollback/全量与部分卸载、activation-lock v1→v2 migration、deterministic wheel/sdist、SBOM/provenance、signed review、Python 3.11–3.14 compatibility aggregate 与 fail-closed RC gate 已落地并完成独立复审；仍需仓库级 License/NOTICE owner 决策及真实 release signer 签字 |

## 当前限制

- Apple 源码态与隔离安装态已不依赖 sibling；sibling 只保留为 P2A 冻结对照，不得继续作为新修改真源。
- 根目录 `install.sh` 可识别并直接移除旧 iOSAgentSkills `~/.codex/AGENTS.md` / `skills` 软链；按用户约束不生成旧配置持久备份，但未知本地内容仍拒绝覆盖。2026-07-15 已完成真实 `~/.codex` 切换，随后 dry-run 显示 config、12 个受管文件与 6 个 profiles 均已一致。
- `install.sh` 显式选择 Codex Runtime Config，激活 config/profile/agents/bin/templates；受管激活文件记录在 `.agent-skills/activation-lock.json`，本机 profile 与 config 中的 runtime 偏好保留。`uninstall.sh` 可安全移除受管安装和激活文件，但不会推断删除既有 profile/shared config，也不会恢复未备份的旧软链。
- Phase 4 已完成 QA discipline、10 个 QA Schema、PRD/Bug/Release workflow、risk-based coverage、Delivery Report 双轴摘要、Desktop Provider/Adapter、15 份 QA golden 与 6 份 Desktop/CP1 golden；公开 workflow compiler 只规划、不合成 outcome/evidence，聚合器绑定真实 Workflow Plan/RunLedger、环境指纹、缺陷与回归 ownership，完整 Conformance 与独立 reviewer gate 已通过。
- Core 已支持 recorded structured Adapter evidence，但不会自行调用 Skill、Verification Coordinator 或 wrapper；真实执行仍由 Agent/iOSAgentSkills 负责。`scripts/run_ios_installed_workflow_smoke.py` 证明隔离安装态的 discovery/plan/structured evidence/review/report 合同闭环，不冒充真实业务工程 Xcode build/test。
- Phase 2A、Phase 2B 与 Phase 2C 的历史独立 reviewer 均已通过；本轮 iOS readiness、source install anchor 与验证闭环优化的 183 个 unittest、完整 Conformance 已通过，安装脚本独立复审结论为“阻塞问题：无”。这些证据不代表真实业务工程 Xcode build/test 已完成。
- iOSAgentSkills 来源 commit/hash 与每个文件去向可审计，但 License/NOTICE provenance 当前仍为 `pending`；解决前不得把仓库标记为发布就绪。
- 支持矩阵：Core 与 deterministic wheel/sdist 目标 Python 3.11–3.14；production bootstrap 当前仅 macOS、Linux 与 WSL2。Windows bootstrap/语法/源码 dry-run 进入 CI，但 Windows transaction 未 ready，production manifest fail-closed；Xcode 27 官方知识源 host smoke、真实设备/Remote/Design Connector 均不随发布物伪造。
- Telemetry 默认且始终关闭：Core、installer、Doctor、migration、packaging 与 RC gate 不上传源码、凭据、设计数据、设备数据或运行日志；诊断只写用户显式指定的本地 canonical artifacts。删除 release/rollback/evidence 由操作者按本地 retention policy 显式执行，不提供隐式远端收集或自动清理用户文件。
- 版本与弃用：Core/package 使用 SemVer compatibility range，Schema 使用显式 `schema_version` 与 migration graph；deprecated artifact 先保持 readable + Doctor warning，再 blocked-new-use，只有 Lock/rollback 不再引用且迁移窗口关闭后才能移除。activation-lock v1 当前为 readable/blocked-new-use，所有新 writer 只生成 v2。
- 本地 Python 3.14.3 完整 Conformance 已通过；2026-07-17 的 clean CI run `29550350238` 已通过 macOS、Linux、Windows bootstrap，并在 CPython 3.11.15、3.12.13、3.13.14、3.14.6 完成 byte-identical wheel/sdist aggregate，fingerprint 为 `6b2893c5a938`，四版本完整 Conformance 均通过。
- 远程 bootstrap 当前具备离线 fixture、deterministic development bundle、wheel/sdist clean-venv smoke、GitHub Pages 控制面 builder、受 Gate 约束的 GitHub Release/Pages workflow 与 supply-chain metadata 证据；默认 production manifest 仅允许 macOS/Linux/WSL2。Pages 尚未实际部署，Windows install transaction、crash-residue 自动恢复、License/NOTICE 决策与真实 signer 仍未完成；RC gate 对这些边界保持 fail-closed。

## 设计原则

- 本地仓库事实优先，不执行 Discovery 期间发现的项目脚本。
- 默认最小权限、无网络、无凭据读取。
- canonical JSON 使用 UTF-8、键排序、紧凑格式、禁止 NaN，并保留末尾换行。
- 必需能力缺失、未知版本、非法状态、权限扩大和依赖循环必须 fail-closed。
- 平台专属实现保留在同仓独立平台包，Core 不演化成跨平台巨型 Skill；物理同仓不等于把平台细节并入 Core。
