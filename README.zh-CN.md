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

渐进式 Rust 重构已经进入受控 bootstrap 阶段。Release Manifest v3
绑定 macOS、Linux、Windows 的完整原生二进制矩阵，以及发布资格阶段生成的
不可变 Upgrade Source Qualification；托管发布中的显式 Apple/Desktop 首次
安装或 dry-run 默认选择经过校验的 Rust 生命周期事务。已安装的原生 CLI 也
支持显式源码升级、回滚、Doctor 与卸载事务，并新增由操作者显式调用的
`hosted-upgrade`：它只信任固定 Pages 控制面，校验合格源码归档与当前主机
可执行文件，再生成绑定发布来源的审批 envelope。签名 POSIX Release
bootstrap 现已把显式 `--upgrade` 请求路由到与 Release 匹配的 Rust
可执行文件，并且不存在 Python 回退。源码 checkout 在显式选择全新
Apple/Desktop 安装时也会离线构建并执行固定版本的 Rust installer；交互式或
兼容参数、PowerShell bootstrap 升级和旧版本接管仍走独立门禁或 Python 兼容路径。
仓库已包含 MIT License、NOTICE 以及经过 hash 校验的迁移审计记录。
GitHub Pages 控制面已经部署，但公开 Release 资产与远程安装仍受发布门禁保护。

正式发布仍需要：

- 外部 release signer 签字；
- GitHub `release` / `github-pages` Environment 审批；
- 签名 Release 发布及远程安装 smoke 验证。

在这些条件完成前，Pages 仅是已上线的控制面，不应把远程安装入口视为可用的生产入口。

## 环境要求

- Python 3.11 或更高版本（交互式源码选择和兼容回退仍需要）；
- Rust 1.97.1（原生开发需要；托管 v2 发布会下载经过校验的目标二进制）；
- production bootstrap 支持 macOS、Linux 和 WSL2；
- Windows bootstrap 已在 CI 中验证，但尚未作为 production install target。

## 从源码安装

```bash
./install.sh
```

不带参数的 `./install.sh` 仍走 Python 交互式兼容路径。显式选择全新的
Apple/Desktop 安装时，只要 `cargo` 可用就默认使用 Rust：

```bash
./install.sh --platform apple
./install.sh --platform desktop
./install.sh --platform apple --discipline qa --runtime-config codex --dry-run
```

源码 bootstrap 会在私有临时 target 中执行
`cargo build --locked --offline`，随后运行该精确二进制。依赖必须已存在于
本机 Cargo cache；原生构建一旦开始，失败不会回退到 Python。可设置
`AGENT_SKILLS_INSTALL_ENGINE=python` 强制兼容实现，或设置为 `rust`
要求必须进入原生路径。

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

v3 Release 发布后，显式的首次 `--platform apple` 或
`--platform desktop` 安装或 dry-run 在 macOS 或受支持的 glibc 2.39+ Linux
主机上默认选择 Rust；musl 与旧版 glibc 主机继续走 Python 兼容路径。发布门禁
会把源包与对应主机二进制的精确大小、SHA-256 身份确定性写入 POSIX bootstrap，
因此该首次安装路径只需要 `curl`、`unzip` 和系统 SHA-256 命令，不再要求
Python。可设置
`AGENT_SKILLS_INSTALL_ENGINE=python` 强制使用过渡期兼容路径；
`AGENT_SKILLS_INSTALL_ENGINE=rust` 在条件不满足时会 fail-closed。Rust
一旦被选中，执行失败不会静默降级到 Python。签名 POSIX Release bootstrap
也会把显式 `--upgrade` 请求直接路由到与 Release 匹配的 Rust 可执行文件，
该路径不存在 Python 回退。源码 checkout 中不属于显式全新 Apple/Desktop
选择的请求与其他兼容参数仍要求 Python 3.11+；PowerShell 也暂时保留该兼容
路径。Windows 原生二进制已经进入发布矩阵，但完整 Windows 安装合同启用前，
仍不是 production source-install target。

Apple 原生安装会把已验证的同一可执行文件发布为
`~/.codex/bin/agent-session` 与 `~/.codex/bin/agent-skills`；后者提供受保护的
Rust 生命周期 CLI。移除精确受管安装前可先预览：

```bash
~/.codex/bin/agent-skills uninstall ~/.codex --platform all --dry-run
~/.codex/bin/agent-skills uninstall ~/.codex --platform all
```

签名 Manifest v3 Release 可用后，现有原生安装可显式预览并批准托管升级。
Manifest URL 和资产来源固定在二进制中，调用方不能覆盖：

```bash
~/.codex/bin/agent-skills hosted-upgrade \
  --target-root ~/.codex --dry-run \
  --output /path/to/hosted-upgrade-plan.json

~/.codex/bin/agent-skills hosted-upgrade \
  --target-root ~/.codex \
  --plan /path/to/hosted-upgrade-plan.json \
  --approve-plan <envelope-fingerprint> \
  --approve <逐项提供计划要求的权限批准>
```

Apply 会重新获取并认证 Release、独立编译候选两次、逐值比较完整审批
envelope，并在受保护生命周期事务中用当前主机的已验证可执行文件同时替换
两个 launcher 名称。该命令不是无人值守的后台自动更新。

签名 POSIX Release bootstrap 也提供同一套显式两阶段流程，并且不会信任
当前已经安装的 launcher：

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh \
  | bash -s -- --upgrade --target-root ~/.codex --dry-run \
      --output /path/to/hosted-upgrade-plan.json

curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh \
  | bash -s -- --upgrade --target-root ~/.codex \
      --plan /path/to/hosted-upgrade-plan.json \
      --approve-plan <envelope-fingerprint> \
      --approve <逐项提供计划要求的权限批准>
```

bootstrap 只下载当前 Release 对应主机的精确可执行文件，校验内嵌大小和
SHA-256 身份，并且只转发受保护升级参数。主机不受支持、审批模式错误、从
源码 checkout 调用、显式选择 Python engine 或资产被篡改时，都会在执行
原生程序前 fail-closed。

发布门禁生成的托管卸载入口会先用内嵌 Release Matrix 校验已安装可执行文件，
再选择 Rust：

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/uninstall.sh \
  | bash -s -- --dry-run
```

`AGENT_SKILLS_UNINSTALL_ENGINE=python` 可强制兼容路径；
`AGENT_SKILLS_UNINSTALL_ENGINE=rust` 会在已安装二进制与当前托管 Release
不一致时 fail-closed。源码 checkout 中的 `uninstall.sh` 仍明确保留 Python
3.11+ 路径；一旦原生卸载已被选择，执行失败不会再降级。

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
  doctor --target-root /path/to/installed-root
cargo run --locked -p agent-skills-rs -- \
  rollback /path/to/installed-root \
  --approve-current-lock <sha256> --approve-rollback-point <sha256>
cargo run --locked -p agent-skills-rs -- \
  upgrade /path/to/source/platforms /path/to/installed-root \
  /path/to/upgrade-conformance-evidence.json --dry-run \
  --output /path/to/upgrade-plan.json
cargo run --locked -p agent-skills-rs -- \
  upgrade-source-qualification-validate \
  /path/to/upgrade-source-qualification.json
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

迁移顺序与门禁见 [Rust 迁移计划](docs/rust-migration.md)。原生路径已覆盖 canonical contracts、Manifest Registry、发现与策略、计划和 Package Lock、受控 Runtime、Worktree Session、Provider Invocation，以及事务化 Lifecycle。

公开 `doctor` 生成运行时中立的 Doctor Report v2，并使用构建时嵌入二进制的精确 Schema inventory，不依赖 Python、源码 checkout、网络或外部 Schema 路径。兼容命令 `doctor-report` 继续保留 Doctor Report v1 与显式 `--python-version` 差分门禁。Doctor 全程只读；失败检查仍输出 canonical JSON 并返回退出码 2。

首次安装、卸载与回滚已使用公开原生事务。公开原生 `upgrade` 要求显式提供已验证源码、Conformance evidence、保存的 Plan、精确 Plan fingerprint 与完整权限批准；`lifecycle-upgrade` 保留为可见别名。操作者显式调用的 `hosted-upgrade` 只从固定仓库控制面和 Release 资产获取候选，并把发布来源、当前安装 lineage 与候选 Lock 绑定到独立审批 envelope；签名 POSIX Release bootstrap 已接通显式受保护的 `--upgrade` 路由，PowerShell 与其余兼容 bootstrap 入口仍受独立门禁控制。

原生 `LifecycleWorkspace` 会在同一生命周期锁下创建私有 stage/backup，持有目录 capability，并以 no-follow、identity-bound 和 canonical-contract 门禁组装与验证 `AGENTS.md`、双 Lock、Package、Skill、`skills/.system` 及 Activation 状态。持久化 rollback point 会冻结受管树、外部文件、缺失状态、mode 与父目录预像；原子 no-replace rename 在 Unix 使用 `renameat2`/`renamex_np`，在 Windows 使用不带 replace flag 的 `MoveFileExW`。任何 identity、内容、alias 或父目录 symlink 漂移都会 fail-closed，并在必要时保留 stage/backup 作为恢复证据。

`PublishedInstall` 现覆盖替换和首次安装两类 source activation。替换路径从已发布 Package snapshot 冻结资产；首次安装路径在发布前从 stage 读取已验证资产，并从目标读取 unmanaged destination、profile 与 `config.toml` 预像，再把精确 scope 写入 rollback point。两条路径都会拒绝冲突、只创建缺失 profile、通过私有 quarantine 发布，并最后写入 Activation Lock。首次安装失败时先撤回全部新 managed roots，再恢复每个外部预像。source deactivation 与 `PublishedUninstall` 同样受 rollback scope 约束，只移除 Activation-owned 内容，同时保留本机 profile、`config.toml` 语义和 `skills/.system`。

兼容命令仍要求调用方显式提供 `agent-session` launcher；合格的托管首次安装则由 v2 bootstrap 把同一份经过校验的原生可执行文件冻结为 launcher，并在事务内完成激活。发布门禁生成的托管 `uninstall.sh` 已切换到 release-matched 原生 guard；源码 checkout 卸载、Release 不匹配、host 不支持或兼容参数仍走经过校验的 Python 路径。生命周期锁只协调 lifecycle 命令；事务期间调用方仍须保持获批 external scope 静止，并关闭相关可写 handle。

非默认的 `lifecycle-uninstall` 兼容别名与公开 `uninstall` 命令均已接入原生卸载 guard：缺失目标不会被创建，执行与只读 dry-run 的 JSON、默认人类可读输出、canonical blocked report 及最终文件系统状态均已对照 Python 路径验证。托管 `uninstall.sh` 只在已安装二进制与内嵌 host artifact 的大小和 SHA-256 完全一致时默认进入该路径，原生一旦选中不静默降级。

原生安装流水线由 `install-selection`、`install-source-snapshot` 与 `install-bundle` 分层完成：先解析显式平台、Discipline、Runtime Config 和依赖闭包，再通过有界 no-follow 遍历冻结声明资产，最后重建 Manifest Registry、能力、规则、Skill、Binding、权限、Install Plan v2 与 Package Lockfile。core-only、Apple、QA、Codex Runtime Config 和 previous-Lock lineage 均保留 Python 差分证据。

公开 `install` 会执行 staging、语义复验、原子发布、发布后复验、失败回滚与清理；`lifecycle-install` 作为只读兼容命令保留。Apple 安装还会冻结同一份原生 launcher，并在受保护事务中完成 source activation。安装后的 `agent-session` 继续提供 `create`、`list`、`inspect`、`fingerprint`、`checkpoint` 与 `gate`。

升级门禁严格验证 Upgrade Conformance Evidence v1 与 Upgrade Plan v1，覆盖 attestation、精确选择和移除、权限审批、external handler identity、迁移顺序、rollback identity 与自洽篡改。Lifecycle 在目标事务锁下复验双 Lock 和 Activation ownership，冻结外部作用域与 rollback state，并签发绑定 Rust 源码闭包、Cargo Lock、目标和固定 toolchain build identity 的 opaque 收据；调用方不能伪造当前 Lock、rollback point、迁移、handler 或外部路径。

公开 `upgrade` 会根据首次持锁读取的安装 lineage 编译候选。`--dry-run` 输出或保存精确 Plan；执行时必须提供该 Plan、对应的 `--approve-plan` fingerprint 和完整权限批准。Apply 会重新取得目标锁、重建并逐值比较 Plan，再交给受保护 executor；候选或目标发生并发漂移时 fail-closed。Apple activation 在任何外部写入前还必须通过原生 installed-registry smoke，覆盖 discovery、policy、Package-Lock-bound ready Plan、Skill Binding、Recorded Adapter Runtime、独立 review 与 delivery report。`lifecycle-upgrade` 仅作为可见兼容别名；`hosted-upgrade` 在此基础上绑定固定托管来源、Source Qualification、Manifest、当前主机二进制及 envelope fingerprint。签名 POSIX Release bootstrap 已接通显式 `--upgrade` 调用；PowerShell 与其他兼容入口仍受独立门禁控制。

Upgrade Source Qualification v1 是下一层发布边界：它把已完成的仓库 Conformance 套件绑定到不可变源码归档、source revision、完整 SBOM material identity、Schema inventory 和稳定命令集合，同时避免把发布期证据错误绑定到未来某个安装实例的 Lock lineage。Python 与 Rust 已共同验证该合同，但它本身不授权下载或执行升级。

公开 `rollback` 会在创建 workspace 前校验精确的当前 Lock 与持久 rollback-point fingerprint；随后从已验证快照重建旧 managed projection、保留当前 `.system`、在同一 `PublishedInstall` 恢复窗口内还原冻结的外部预像，并把被替换的当前状态持久化为下一 rollback point。`lifecycle-rollback` 作为可见兼容别名继续保留。

新增的 `agent-release` crate 会冻结 macOS、Linux、Windows 在
`aarch64` 与 `x86_64` 上的六目标原生发布矩阵。每个记录都绑定准确的
source revision、Cargo Lock hash、Rust 1.97.1 toolchain、目标格式与架构
header、smoke 结果、文件大小和 SHA-256。CI 会在匹配架构的 GitHub-hosted
runner 上构建并执行各目标二进制，只有完整且排序固定的六目标集合才能合并为
`native-artifacts.json`。Qualification 会把这些原始二进制纳入同一候选版本，
并由 provenance、精确 Release allowlist、外部 review 签名和最终 Release Gate
共同约束。Release Manifest v3 已把该矩阵和发布资格阶段生成的不可变升级
源码资格一起纳入合同，并继续把合格托管首次安装的默认引擎设为 Rust；薄
`install.sh` / `install.ps1` 获取层及所有不合格请求在本阶段仍走显式 Python
兼容路径。

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
