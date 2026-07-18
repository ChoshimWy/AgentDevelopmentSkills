# AgentDevelopmentSkills（中文）

AgentDevelopmentSkills 是一个面向编码 Agent 的离线优先工作流核心。它负责发现仓库能力、解析平台与能力合同、生成确定性的执行计划，并保存可审计的执行证据。

## 项目特点

- **保守路由**：只依据仓库事实和显式能力合同进行平台与任务路由。
- **确定性产物**：计划、锁文件、Manifest、迁移报告和发布物可重复生成。
- **事务化生命周期**：支持安装、升级、回滚、Doctor 诊断和卸载，并在冲突或篡改时安全停止。
- **跨平台支持**：Apple 与 Desktop 已实现；Android、Web、Backend 当前仅提供 bootstrap-only 合同。
- **可复现打包**：提供六目标 Rust 原生矩阵及 Python 3.11–3.14 兼容产物。
- **渐进式 Rust 迁移**：首个首次安装入口已默认选择 Rust，其他路径继续按门禁迁移。
- **可审计发布**：提供 SBOM、provenance、外部 review 签名和 fail-closed 发布门禁。
- **隐私优先**：默认不启用 telemetry，不收集凭据，不执行隐式远程操作。

## 当前状态

渐进式 Rust 重构已经进入受控 bootstrap 阶段。Release Manifest v2
绑定 macOS、Linux、Windows 的完整原生二进制矩阵；托管发布中的显式
Apple/Desktop 首次安装默认选择经过校验的 Rust 生命周期事务。源码检出安装、
dry-run、交互/兼容参数、既有安装、升级和旧版本接管仍走有文档说明的 Python
兼容路径。仓库已包含 MIT License、NOTICE 以及经过 hash 校验的迁移审计记录。
GitHub Pages 控制面已经部署，但公开 Release 资产与远程安装仍受发布门禁保护。

正式发布仍需要：

- 外部 release signer 签字；
- GitHub `release` / `github-pages` Environment 审批；
- 签名 Release 发布及远程安装 smoke 验证。

在这些条件完成前，Pages 仅是已上线的控制面，不应把远程安装入口视为可用的生产入口。

## 环境要求

- Python 3.11 或更高版本（当前薄 bootstrap、源码安装和兼容回退仍需要）；
- Rust 1.97.1（原生开发需要；托管 v2 发布会下载经过校验的目标二进制）；
- production bootstrap 支持 macOS、Linux 和 WSL2；
- Windows bootstrap 已在 CI 中验证，但尚未作为 production install target。

## 从源码安装

```bash
./install.sh
```

预览安装计划或选择平台：

```bash
./install.sh --dry-run
./install.sh --platform apple
./install.sh --platform desktop
```

## 远程安装入口

Pages 控制面已经上线；正式发布后，它将提供薄 bootstrap，具体版本资产仍由不可变 GitHub Release 承载：

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh | bash
```

Windows PowerShell：

```powershell
iwr -useb https://choshimwy.github.io/AgentDevelopmentSkills/install.ps1 | iex
```

签名 Release 尚未发布时，请使用源码安装，不要执行上述远程命令。

v2 Release 发布后，显式的首次 `--platform apple` 或
`--platform desktop` 请求默认选择 Rust。可设置
`AGENT_SKILLS_INSTALL_ENGINE=python` 强制使用过渡期兼容路径；
`AGENT_SKILLS_INSTALL_ENGINE=rust` 在条件不满足时会 fail-closed。Rust
一旦被选中，执行失败不会静默降级到 Python。本阶段薄 shell/PowerShell
bootstrap 仍要求兼容的 Python 解释器。Windows 原生二进制已经进入发布矩阵，
但完整 Windows 安装合同启用前，仍不是 production source-install target。

## 开发与验证

完整 Conformance：

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
```

定向测试：

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_pages_distribution \
  tests.test_github_publication
```

验证当前 Rust 兼容层：

```bash
cargo fmt --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
AGENT_SKILLS_RUST_COMPATIBILITY=1 \
  PYTHONPATH=src python3 -m unittest tests.test_rust_compatibility -v
```

通过非默认原生 CLI 检查当前 Registry：

```bash
cargo run --locked -p agent-skills-rs -- registry-snapshot platforms
```

同一兼容路径还可以解析策略、只读发现仓库证据、编译确定性计划、解析或检查持久化 Package Lock，并在不调用外部 Provider 的前提下模拟工作流 Runtime 合同：

```bash
cargo run --locked -p agent-skills-rs -- \
  repository-discover tests/fixtures/apple-app
cargo run --locked -p agent-skills-rs -- \
  policy-resolve /path/to/profile.json "implement the requested feature"
cargo run --locked -p agent-skills-rs -- \
  plan-compile /path/to/profile.json /path/to/policy.json \
  --manifests platforms
cargo run --locked -p agent-skills-rs -- \
  lock-resolve /path/to/install-plan.json --schemas schemas \
  --output /path/to/agent-skills.lock
cargo run --locked -p agent-skills-rs -- \
  lock-validate /path/to/agent-skills.lock
cargo run --locked -p agent-skills-rs -- \
  lifecycle-install platforms /path/to/fresh-target \
  --platform apple --schemas schemas --dry-run
cargo run --locked -p agent-skills-rs -- \
  doctor-baseline /path/to/installed-root --schemas schemas
cargo run --locked -p agent-skills-rs -- \
  doctor-report /path/to/installed-root --schemas schemas \
  --python-version 3.11.0
cargo run --locked -p agent-skills-rs -- \
  runtime-execute /path/to/workflow-plan.json \
  --behaviors /path/to/fake-behaviors.json
cargo run --locked -p agent-skills-rs -- \
  adapter-request-build /path/to/workflow-plan.json node-id \
  /path/to/task-context.json invocation-id
cargo run --locked -p agent-skills-rs -- \
  adapter-result-validate /path/to/adapter-request.json \
  /path/to/adapter-result.json
cargo run --locked -p agent-skills-rs -- \
  runtime-execute-recorded /path/to/workflow-plan.json \
  /path/to/adapter-results.json /path/to/task-context.json
cargo run --locked -p agent-skills-rs -- \
  invocation-prepare /path/to/handoff /path/to/workflow-plan.json node-id \
  /path/to/task-context.json invocation-id
cargo run --locked -p agent-skills-rs -- \
  invocation-claim /path/to/handoff adapter-request-id host-actor \
  /path/to/private-claim-token
cargo run --locked -p agent-skills-rs -- \
  invocation-submit /path/to/handoff adapter-request-id \
  /path/to/adapter-result.json /path/to/private-claim-token
cargo run --locked -p agent-skills-rs -- \
  runtime-execute-invocations /path/to/workflow-plan.json \
  /path/to/handoff /path/to/task-context.json \
  --selection /path/to/provider-invocation-selection.json
cargo run --locked -p agent-skills-rs -- \
  repository-inspect /path/to/repository app --base-ref HEAD
cargo run --locked -p agent-skills-rs -- \
  session-context-create /path/to/session-context-input.json
cargo run --locked -p agent-skills-rs -- \
  session-registry-list /path/to/repository
cargo run --locked -p agent-skills-rs -- \
  session-create /path/to/repository feature \
  /path/to/session-context-input.json --base-ref HEAD
cargo run --locked -p agent-skills-rs -- \
  session-create-manifest /path/to/repository feature \
  --project-id project --created-at 2026-07-18T00:00:00+00:00 \
  --platform apple --manifest-root /path/to/platforms --base-ref HEAD
cargo run --locked -p agent-skills-rs -- \
  session-registry-checkpoint /path/to/repository session-id
cargo run --locked -p agent-skills-rs -- \
  session-registry-gate /path/to/repository session-id \
  /path/to/adapter-pairs.json /path/to/run-ledger.json /path/to/artifacts
```

若计划包含 `package_lock_hash`，`invocation-prepare` 必须追加
`--lock /path/to/agent-skills.lock`，消费时也必须提供同一份已验证 Lockfile。

迁移顺序和切换门禁见 [Rust 迁移计划](docs/rust-migration.md)。当前原生路径已覆盖 canonical contracts、只读 Manifest Registry、仓库发现、策略解析、计划编译，以及 Package Lock 的解析、验证、差异、解释与锁定计划绑定检查。Phase 4 已迁移确定性 fake-adapter Runtime、Adapter Request/Result v1 冻结与验证合同、Recorded Result 消费，以及带输出上限的 Git Worktree 检查、`repository-patch-v1`、`session-source-v1`、Session Context、精确 Worktree 创建/失败补偿、checkpoint、带文件锁的持久化 Session Registry、基于受信 Manifest 的平台/Provider 能力闭包编译与 Session 创建，以及 Final Gate 证据复验/持久化。新增的 Provider Invocation v1 文件交接会冻结权限、副作用、资源、Provider provenance 与 hard timeout，以单次 claim token 的 hash 保护认领，并且只接受与请求 identity 完全一致的 Adapter Result；Runtime 消费必须提供 Provider Invocation Selection v1，显式把每个节点绑定到准确的 submitted request ID，不会按时间静默选择重试结果。真正的 Provider 调用仍由外部宿主负责，Core 不发现或读取 Provider 凭据、不执行 binding/package code 也不联网，只读取调用方显式提供、仅 owner 可读且应来自高熵随机源的 transport claim token；若进程在发布结果附近异常退出，应先 inspect 再重试 claim/submit。首个原生 Lifecycle 切片现提供只读 Doctor 兼容投影，覆盖安全目标、恢复残留、受管布局、Install/Persistent Lock 双锚、Core 运行时 identity、运行时 Schema inventory、受管 Activation 文件完整性，以及安装包树、Package/Provider Manifest、顺序闭包，并对照双 Lock 验证 package identity、dependency 与 side-effect semantics。该投影现也会对照重建的已安装 Manifest 语义，验证 Skill identity 与文件树、唯一全局 `AGENTS.md` 内容、Fragment 顺序与 rule trace、冻结的 Capability Binding 与 Provider closure，以及权限 Profile 与逐 Capability 授权。持久化 rollback point 也已纳入原生只读验证，会检查其独立双 Lock、Package、Skill、AGENTS、external-state、Activation、语义闭包与完整快照摘要。原生 `doctor-report` 现在可以组装并验证完整 Doctor Report v1；由于 v1 冻结的是 Python 宿主版本，该兼容命令要求宿主显式证明 `--python-version`，不会自行发现或执行解释器，也不代表已经完成无 Python 的生产切换。可变生命周期事务仍保留在 Python 路径；作为首个原生前置能力，`agent-lifecycle` 已提供 identity-bound RAII 目录锁，覆盖原子互斥、安全创建缺失目标、crash residue 可见性和 identity-checked 清理，但尚未接入生产 install/upgrade 命令。跨平台按名称释放要求目标父目录在释放期间保持受信，调用方也必须预先展开 `~`。只读 Doctor 实现会持有目录 capability，并以 no-follow 方式打开合同文件；与显式锁 API 不同，它不会修复、安装、升级、回滚、卸载或写入目标。任一投影检查失败时仍在 stdout 输出 canonical JSON，并以退出码 2 返回。Activation 与安装包树的 mode 差分目前都属于 POSIX 合同；Windows 原生 Doctor 仍验证 Lock 结构、路径、no-follow 遍历与内容 hash，但不会把 POSIX mode 位解释为 Windows ACL 保证。它也不会创建 Commit、改变 staging、切换生产 CLI。在所有相关差分测试和发布门禁通过前，Python CLI 仍是生产入口。

原生 `LifecycleWorkspace` 会在同一生命周期锁下创建私有 stage/backup，持有目录 capability，并以 no-follow、identity-bound 和 canonical-contract 门禁组装与验证 `AGENTS.md`、双 Lock、Package、Skill、`skills/.system` 及 Activation 状态。持久化 rollback point 会冻结受管树、外部文件、缺失状态、mode 与父目录预像；原子 no-replace rename 在 Unix 使用 `renameat2`/`renamex_np`，在 Windows 使用不带 replace flag 的 `MoveFileExW`。任何 identity、内容、alias 或父目录 symlink 漂移都会 fail-closed，并在必要时保留 stage/backup 作为恢复证据。

`PublishedInstall` 现覆盖替换和首次安装两类 source activation。替换路径从已发布 Package snapshot 冻结资产；首次安装路径在发布前从 stage 读取已验证资产，并从目标读取 unmanaged destination、profile 与 `config.toml` 预像，再把精确 scope 写入 rollback point。两条路径都会拒绝冲突、只创建缺失 profile、通过私有 quarantine 发布，并最后写入 Activation Lock。首次安装失败时先撤回全部新 managed roots，再恢复每个外部预像。source deactivation 与 `PublishedUninstall` 同样受 rollback scope 约束，只移除 Activation-owned 内容，同时保留本机 profile、`config.toml` 语义和 `skills/.system`。

兼容命令仍要求调用方显式提供 `agent-session` launcher；合格的托管首次安装则由 v2 bootstrap 把同一份经过校验的原生可执行文件冻结为 launcher，并在事务内完成激活。现有 `uninstall.sh` 尚未切换到原生 guard。生命周期锁只协调 lifecycle 命令；事务期间调用方仍须保持获批 external scope 静止，并关闭相关可写 handle。

非默认的 `lifecycle-uninstall` 兼容命令现已接入原生卸载 guard：缺失目标不会被创建，执行与只读 dry-run 的 JSON、默认人类可读输出、canonical blocked report 及最终文件系统状态均已对照 Python 路径验证；这仍不代表 `uninstall.sh` 已完成生产切换。

并行的 `install-selection` 兼容命令现已覆盖可安装源包目录、显式平台/Discipline/Runtime Config 选择、必需与可选依赖闭包、版本约束、确定性拓扑顺序和选择原因。后续的 `install-source-snapshot` 命令会通过有界、no-follow 遍历冻结声明的 Package 资产、Package/Provider Manifest、Instruction Fragment 与可安装 Skill 树，并复读源包检查并发变化；两者均已与 Python 完成差分验证。新增的 `install-bundle` 命令会在冻结快照上独立重建 Manifest Registry、依赖能力、Instruction/rule、Skill、资产、Binding、权限、副作用、Install Plan v2 与持久化 Package Lockfile identity；core-only、Apple、QA、Codex Runtime Config 及 previous-Lock lineage 输出已与 Python 做逐字节差分。原生生命周期现同时提供只读 `lifecycle-install` 兼容命令，以及面向合格首次安装的 production `install` 命令；它会执行 staging、语义复验、原子发布、发布后复验、失败回滚与清理。Apple 安装还会冻结同一份 launcher 字节，并在同一受保护事务中完成 source activation。安装后的原生 `agent-session` 保留公开的 `create`、`list`、`inspect`、`fingerprint`、`checkpoint` 与 `gate` 命令面。core-only 和 Apple 投影继续与 Python 做差分。替换安装、升级、旧版本接管及兼容参数仍属于独立门禁路径。

新增的 `agent-release` crate 会冻结 macOS、Linux、Windows 在
`aarch64` 与 `x86_64` 上的六目标原生发布矩阵。每个记录都绑定准确的
source revision、Cargo Lock hash、Rust 1.97.1 toolchain、目标格式与架构
header、smoke 结果、文件大小和 SHA-256。CI 会在匹配架构的 GitHub-hosted
runner 上构建并执行各目标二进制，只有完整且排序固定的六目标集合才能合并为
`native-artifacts.json`。Qualification 会把这些原始二进制纳入同一候选版本，
并由 provenance、精确 Release allowlist、外部 review 签名和最终 Release Gate
共同约束。Release Manifest v2 已把该矩阵绑定为合格托管首次安装的默认 Rust
引擎；薄 `install.sh` / `install.ps1` 获取层及所有不合格请求在本阶段仍走显式
Python 兼容路径。

## 发布治理

`Publish verified release` workflow 只接受受保护 `main` 分支当前 revision 产生的成功 qualification run。它会重新执行最终 Gate，拒绝已存在的 tag 或 Release，以原子方式创建 tag，并校验 Pages 与 Release 资产的 hash。所有第三方 GitHub Actions 均固定到完整 commit SHA，并使用 job 级最小权限。

首次公开发布前，管理员还需要配置 branch protection、`release` / `github-pages` Environment、required reviewers 和外部 review trust store。

## 公开文档

- [英文 README](README.md)
- [架构概览](docs/architecture.md)
- [Rust 迁移计划](docs/rust-migration.md)
- [多会话 Worktree 架构](docs/multi-session-worktree.md)
- [Skill 命名约定](docs/skill-naming.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)

## License

本项目使用 MIT License。第三方组件和迁移来源仍须保留其原有版权及许可证声明；相关归属记录见 `NOTICE` 和 `migration/ios-agent-skills-map-v2.json`。
