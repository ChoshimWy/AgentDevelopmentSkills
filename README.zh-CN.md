# AgentDevelopmentSkills（中文说明）

AgentDevelopmentSkills 是面向编码 Agent 的离线优先、fail-closed 工作流 Core：发现仓库能力，解析平台与纪律合同，生成确定性执行计划，并记录可审计证据。

## 项目特性

- 保守的仓库发现与显式能力路由
- 确定性的计划、Lock、Manifest、迁移和发布物
- 安装、升级、回滚、Doctor、卸载事务
- Apple 与 Desktop 发布包；Android、Web、Backend 明确保持 bootstrap-only
- Python 3.11–3.14 可复现 wheel/sdist
- 外部签名 review、provenance、SBOM 与 fail-closed 发布门禁
- GitHub Pages 控制面与不可变 GitHub Release 资产
- 默认不启用 telemetry，不收集凭据，不隐式远程执行

## 当前状态

仓内实现与验证套件已完成。仓库现已加入 MIT `LICENSE`、`NOTICE`，并在 migration audit 中完成 hash 验证。正式公开发布仍需外部 release signer 签字和 GitHub Environment 审批；GitHub Pages 尚未实际部署。

## 环境要求

- Python 3.11 或更高版本
- production bootstrap 支持 macOS、Linux、WSL2
- Windows bootstrap 已进入 CI，但尚未作为 production install target

## 从源码安装

```bash
./install.sh
./install.sh --dry-run
./install.sh --platform apple
./install.sh --platform desktop
```

## 远程安装入口

Pages 控制面与不可变版本资产分离。Pages 尚未部署前，以下命令不可视为可用的正式入口：

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh | bash
```

PowerShell：

```powershell
iwr -useb https://choshimwy.github.io/AgentDevelopmentSkills/install.ps1 | iex
```

## 开发验证

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
PYTHONPATH=src python3 -m unittest tests.test_pages_distribution tests.test_github_publication
```

## 首次公开发布前置条件

仓库管理员需要完成：

1. 为 `main` 配置 branch protection 和 required checks。
2. 配置 `release`、`github-pages` Environment 与 required reviewers。
3. 写入 `RELEASE_REVIEW_TRUST_STORE_BASE64`，并由外部 signer 签署冻结 payload。
4. License/NOTICE 证据已提交并验证；若内容变更，必须重新生成 migration audit。
5. 首次发布后保存 Release、Pages manifest 和线上 smoke 输出。

详见英文主 README 与 [`docs/architecture.md`](docs/architecture.md) 架构文档。

## License

仓库级 License/NOTICE 当前记录为 MIT，`NOTICE` 的 exact SHA-256 已由 release gate 校验。任何许可证或归属变更都必须同步更新审计文件。
