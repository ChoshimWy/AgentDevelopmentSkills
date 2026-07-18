# AgentDevelopmentSkills（中文）

AgentDevelopmentSkills 是一个面向编码 Agent 的离线优先工作流核心。它负责发现仓库能力、解析平台与能力合同、生成确定性的执行计划，并保存可审计的执行证据。

## 项目特点

- **保守路由**：只依据仓库事实和显式能力合同进行平台与任务路由。
- **确定性产物**：计划、锁文件、Manifest、迁移报告和发布物可重复生成。
- **事务化生命周期**：支持安装、升级、回滚、Doctor 诊断和卸载，并在冲突或篡改时安全停止。
- **跨平台支持**：Apple 与 Desktop 已实现；Android、Web、Backend 当前仅提供 bootstrap-only 合同。
- **可复现打包**：支持 Python 3.11–3.14 的确定性 wheel 与 sdist。
- **渐进式 Rust 迁移**：通过差分兼容测试逐步替换 Python 实现，不做一次性切换。
- **可审计发布**：提供 SBOM、provenance、外部 review 签名和 fail-closed 发布门禁。
- **隐私优先**：默认不启用 telemetry，不收集凭据，不执行隐式远程操作。

## 当前状态

当前 Python 实现和验证套件已经完成。仓库正在进行渐进式 Rust 重构：Rust 组件在合同、输出和失败语义通过差分验证前，只作为并行实现，不会替换默认 Python 入口。仓库已包含 MIT License、NOTICE 以及经过 hash 校验的迁移审计记录。GitHub Pages 控制面已经部署，但公开 Release 资产与远程安装仍受发布门禁保护。

正式发布仍需要：

- 外部 release signer 签字；
- GitHub `release` / `github-pages` Environment 审批；
- 签名 Release 发布及远程安装 smoke 验证。

在这些条件完成前，Pages 仅是已上线的控制面，不应把远程安装入口视为可用的生产入口。

## 环境要求

- Python 3.11 或更高版本；
- Rust 1.97.1（仅 Rust 迁移开发需要）；
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
  doctor-baseline /path/to/installed-root --schemas schemas
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

迁移顺序和切换门禁见 [Rust 迁移计划](docs/rust-migration.md)。当前原生路径已覆盖 canonical contracts、只读 Manifest Registry、仓库发现、策略解析、计划编译，以及 Package Lock 的解析、验证、差异、解释与锁定计划绑定检查。Phase 4 已迁移确定性 fake-adapter Runtime、Adapter Request/Result v1 冻结与验证合同、Recorded Result 消费，以及带输出上限的 Git Worktree 检查、`repository-patch-v1`、`session-source-v1`、Session Context、精确 Worktree 创建/失败补偿、checkpoint、带文件锁的持久化 Session Registry、基于受信 Manifest 的平台/Provider 能力闭包编译与 Session 创建，以及 Final Gate 证据复验/持久化。新增的 Provider Invocation v1 文件交接会冻结权限、副作用、资源、Provider provenance 与 hard timeout，以单次 claim token 的 hash 保护认领，并且只接受与请求 identity 完全一致的 Adapter Result；Runtime 消费必须提供 Provider Invocation Selection v1，显式把每个节点绑定到准确的 submitted request ID，不会按时间静默选择重试结果。真正的 Provider 调用仍由外部宿主负责，Core 不发现或读取 Provider 凭据、不执行 binding/package code 也不联网，只读取调用方显式提供、仅 owner 可读且应来自高熵随机源的 transport claim token；若进程在发布结果附近异常退出，应先 inspect 再重试 claim/submit。首个原生 Lifecycle 切片现提供只读 Doctor 兼容投影，覆盖安全目标、恢复残留、受管布局、Install/Persistent Lock 双锚与运行时 Schema inventory；实现会持有目录 capability，并以 no-follow 方式打开合同文件，不会修复、安装、升级、回滚、卸载或写入目标；任一投影检查失败时仍在 stdout 输出 canonical JSON，并以退出码 2 返回。它也不会创建 Commit、改变 staging、切换生产 CLI。在所有相关差分测试和发布门禁通过前，Python CLI 仍是生产入口。

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
