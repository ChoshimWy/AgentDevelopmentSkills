# AgentDevelopmentSkills（中文）

AgentDevelopmentSkills 是一个离线优先、fail-closed 的编码 Agent 工作流核心。
它负责发现仓库能力、解析显式的平台与职责合同、生成确定性执行计划，并记录可审计证据。

## 项目能力

- 保守的仓库发现与基于 Capability 的路由
- 可确定性生成的计划、Manifest、Lockfile、迁移报告和发布产物
- 受保护的安装、升级、回滚、Doctor 诊断和卸载事务
- Apple 与 Desktop 平台包；Android、Web、Backend 当前为 `bootstrap-only`
- Python 兼容实现与逐步扩展的 Rust 原生运行时
- 可复现发布物、provenance、SBOM 和 fail-closed 发布门禁
- 不启用 telemetry、不收集凭据、不执行隐式远程操作

## 当前状态

项目正在进行受控 Rust 迁移。源码安装器对符合条件的全新 Apple/Desktop
安装可以使用固定 Rust 工具链离线构建；兼容性路径仍使用 Python。Release
Manifest v3 和六目标原生二进制矩阵已经实现，但在外部签名发布和所需 GitHub
环境审批完成前，托管安装仍不是生产入口。

具体功能矩阵和切换门禁见 [Rust 迁移计划](docs/rust-migration.md)。

## 环境要求

- Python 3.11+：兼容实现需要
- Rust 1.97.1：原生开发和源码离线构建需要
- macOS、Linux 或 WSL2：支持的 POSIX bootstrap 环境
- Windows bootstrap 已通过 CI 验证；托管 Windows 安装仍受门禁限制

## 从源码安装

在签名 Release 发布前，推荐从本地 checkout 安装：

```bash
./install.sh
```

示例：

```bash
./install.sh --platform apple
./install.sh --platform desktop
./install.sh --platform all
./install.sh --platform apple --discipline qa --runtime-config codex --dry-run
```

全新安装在 `cargo`（或已激活的 `rustup` toolchain）和所需离线依赖可用时默认使用 Rust。也可以显式选择实现：

```bash
AGENT_SKILLS_INSTALL_ENGINE=rust ./install.sh --platform apple
AGENT_SKILLS_INSTALL_ENGINE=python ./install.sh --platform apple
```

Rust 执行开始后失败不会静默回退到 Python。POSIX 环境下，安装器也能识别
`iOSAgentSkills` 的精确旧版软链布局，并在受保护的迁移事务中处理它。

## 远程安装

Pages bootstrap 与不可变 GitHub Release 资产分离。在签名 Release 发布前，
不要将其作为生产安装入口：

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh \
  | bash -s -- --platform apple
```

Windows PowerShell：

```powershell
iwr -useb https://choshimwy.github.io/AgentDevelopmentSkills/install.ps1 | iex
```

托管升级和卸载同样要求发布来源校验与审批门禁；它们由操作者显式调用，
不会在后台自动更新。

## 开发与验证

运行完整 Conformance：

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
```

运行定向 Python 测试：

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_pages_distribution \
  tests.test_github_publication
```

验证 Rust workspace：

```bash
cargo fmt --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
```

原生 CLI 仍属于非默认迁移面。命令示例和兼容性边界见
[docs/rust-migration.md](docs/rust-migration.md)。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `src/` | Python 兼容实现 |
| `crates/` | Rust contracts、engine、lifecycle、runtime、registry 和 release 包 |
| `platforms/` | 平台包与 Manifest |
| `disciplines/` | 跨平台工作流职责包 |
| `runtime-configs/` | 显式运行时配置包 |
| `schemas/` | 版本化机器可读合同 |
| `scripts/` | 安装、Conformance、发布和验证工具 |
| `docs/` | 架构、迁移、实施阶段和运维文档 |

## 设计与安全原则

1. **显式选择**：平台包和运行时配置由调用方选择，不会隐式激活。
2. **确定性输出**：机器输出使用稳定排序、禁止 NaN 的 canonical UTF-8 JSON。
3. **Fail-closed**：能力缺失、依赖循环、Schema 不匹配、权限扩大、篡改和不安全布局都会终止事务。
4. **边界化信任**：Discovery 只读；Core 不读取 Provider 凭据、不执行 Provider 代码、不发起隐式网络请求。
5. **事务化生命周期**：安装变更先暂存并验证，在支持的平台上原子发布，失败时可恢复。

## 文档

- [架构概览](docs/architecture.md)
- [Rust 迁移与切换策略](docs/rust-migration.md)
- [跨平台 Agent 工作流架构](docs/cross-platform-agent-workflow-architecture.html)
- [Phase 4 QA Core 与 Desktop 状态](docs/implementation/phase-4-qa-core-and-desktop-minimum.html)
- [多 Session Worktree 架构](docs/multi-session-worktree.md)
- [Skill 命名规范](docs/skill-naming.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [English README](README.md)

## License

MIT。详见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
