---
name: desktop-orchestration
description: Desktop 平台只读发现、框架路由、环境画像与最小实施工作流。用于 Electron、Tauri、.NET、Qt、macOS/Windows/Linux native 桌面仓库的分析或代码实施；不用于 Web-only、Backend-only、Apple 移动端、无明确 Desktop 强证据的仓库，也不替代 QA Core 或独立 review。
---

# Desktop Workflow

## Purpose

把 Desktop 平台限定为可替换 Provider：先用只读证据确定 framework 与 module root，再选择显式 Adapter；环境、权限、资源锁和不支持能力必须结构化披露。

## Agent Rules

- 先运行 `scripts/desktop_discovery.py inspect --repository <root>`；单个 weak signal、多个 strong framework 或多个 module root 均不得直接定案。
- 环境画像使用 `scripts/desktop_discovery.py environment`；未知 DPI、display、input 或 permission 保持 `unknown`，不得从宿主机猜测目标环境。
- `framework_hints-v1.json` 是框架信号与 Adapter extension 真源；新增框架只扩展该配置和平台 Adapter，不修改 QA Core Schema。
- `implement` mode 只修改已选择 module root 内的用户授权范围；framework 为 ambiguous/unsupported 时返回 `blocked`。
- build、affected-tests、UI smoke 与 interaction 必须经 `scripts/desktop_adapter.py` 的对应 mode；不得自行拼接未冻结命令。
- window、keyboard、mouse、tray、sleep/wake、install/update 的支持情况来自 framework hints；`unsupported` 或 `manual` 必须进入 QA coverage gap。
- filesystem、network、notification、automation、installer-elevation 等权限必须在环境画像中列出；扩大权限时先请求显式审批。
- build 与 tests 使用 `build-queue:{target-root}`；UI/interaction 使用 `desktop-session:{target-root}`。取消后必须释放已获取资源并写入 cleanup。
- Verification 与 QA 独立：Adapter 通过不等于产品质量 passed，仍由 `qa-workflow` 聚合覆盖与发布建议。
- `status` 只从结构化 Provider/Adapter evidence 派生；阻塞时返回明确 `next_action`，不得把未执行标记为 completed。
- Token Budget 优先用于 framework ambiguity、权限、兼容矩阵和失败证据；低风险路径保持短输出但不省略 fingerprint 与 cleanup。

## Workflow

1. 只读扫描 framework markers，输出 evidence level、candidate framework、module root 与 ambiguity。
2. 生成 OS、arch、DPI/display、input、permission 环境画像及 fingerprint。
3. 对照 framework hints 检查所需 build/test/interaction capability 是否为 `supported | manual | unsupported`。
4. 实施改动后，通过绑定 Adapter 运行最窄 build / affected-tests / UI-smoke。
5. 将 structured evidence、resource cleanup 和 coverage gap 交回 Ledger 与 QA Report。

## Inputs

- `analysis.desktop`：repository root、target files 与只读扫描边界。
- `analysis.desktop.environment`：可选冻结 facts 文件；未提供时只采集非敏感宿主机事实。
- `implementation.desktop`：已冻结 project profile、module root、scope 与 acceptance criteria。
- build / verification / automation：Adapter Request v1，必须包含 framework、module root、environment fingerprint、resource keys 与 checkpoints。

## Escalation Rules

- framework 或 module root 不唯一、命令不存在、permission 未授权、资源锁冲突、取消清理失败时 fail-closed。
- 不读取凭据，不联网，不执行目标仓库脚本进行 discovery。
- 未声明的 framework command 不得回退到 shell 猜测；返回 `blocked` 并给出 adapter extension action。

## Relationship to Other Skills

- `qa-workflow`：负责 PRD/Bug/Release artifacts、coverage、defect 与 release recommendation。
- `workflow-orchestration`：负责 DAG、Ledger、loop limit、resource event 与 cleanup。
- Design 与 documentation disciplines：按显式选择提供平台无关输入，不随 Desktop 隐式安装。
- `code-review`：保留独立 reviewer gate。

## Outputs

- discovery：`desktop-project-profile-v1`。
- environment：`desktop-environment-profile-v1`。
- implementation：scoped change-set 与 `no_test_reason` 或 Adapter evidence。
- execution：Adapter Result v1；unsupported/manual 能力返回 `partial | blocked`，不得伪装 passed。

## Exit Conditions

- framework、module root、环境 fingerprint 与 capability support 可追溯。
- 所需权限和 resource keys 未扩大，取消 cleanup 可审计。
- 最窄验证已执行或有明确 `no_test_reason`，并通过独立 reviewer gate。
