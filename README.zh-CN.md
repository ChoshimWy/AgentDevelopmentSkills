# AgentDevelopmentSkills（中文）

AgentDevelopmentSkills 是一个面向编码 Agent 的离线优先工作流核心。它负责发现仓库能力、解析平台与能力合同、生成确定性的执行计划，并保存可审计的执行证据。

## 项目特点

- **保守路由**：只依据仓库事实和显式能力合同进行平台与任务路由。
- **确定性产物**：计划、锁文件、Manifest、迁移报告和发布物可重复生成。
- **事务化生命周期**：支持安装、升级、回滚、Doctor 诊断和卸载，并在冲突或篡改时安全停止。
- **跨平台支持**：Apple 与 Desktop 已实现；Android、Web、Backend 当前仅提供 bootstrap-only 合同。
- **可复现打包**：支持 Python 3.11–3.14 的确定性 wheel 与 sdist。
- **可审计发布**：提供 SBOM、provenance、外部 review 签名和 fail-closed 发布门禁。
- **隐私优先**：默认不启用 telemetry，不收集凭据，不执行隐式远程操作。

## 当前状态

仓内实现和验证套件已经完成。仓库已包含 MIT License、NOTICE 以及经过 hash 校验的迁移审计记录。

正式发布仍需要：

- 外部 release signer 签字；
- GitHub `release` / `github-pages` Environment 审批；
- GitHub Pages 的首次线上部署与 smoke 验证。

在这些条件完成前，不应把 Pages 安装入口视为已上线的生产入口。

## 环境要求

- Python 3.11 或更高版本；
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

正式发布后，Pages 控制面将提供薄 bootstrap，具体版本资产仍由不可变 GitHub Release 承载：

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh | bash
```

Windows PowerShell：

```powershell
iwr -useb https://choshimwy.github.io/AgentDevelopmentSkills/install.ps1 | iex
```

Pages 尚未上线时，请使用源码安装，不要执行上述远程命令。

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

## 发布治理

`Publish verified release` workflow 只接受受保护 `main` 分支当前 revision 产生的成功 qualification run。它会重新执行最终 Gate，拒绝已存在的 tag 或 Release，以原子方式创建 tag，并校验 Pages 与 Release 资产的 hash。所有第三方 GitHub Actions 均固定到完整 commit SHA，并使用 job 级最小权限。

首次公开发布前，管理员还需要配置 branch protection、`release` / `github-pages` Environment、required reviewers 和外部 review trust store。

## 公开文档

- [英文 README](README.md)
- [架构概览](docs/architecture.md)
- [多会话 Worktree 架构](docs/multi-session-worktree.md)
- [Skill 命名约定](docs/skill-naming.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)

## License

本项目使用 MIT License。第三方组件和迁移来源仍须保留其原有版权及许可证声明；相关归属记录见 `NOTICE` 和 `migration/ios-agent-skills-map-v2.json`。
