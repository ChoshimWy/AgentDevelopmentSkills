---
name: qa-workflow
description: 跨平台 QA 策略、用例、缺陷、回归与质量报告工作流。用于从 PRD、Bug 或 Release 输入生成 risk-based QA Plan，设计 Test Case、记录 Test Result / Defect Report、维护 Regression Set，或形成 go / conditional-go / no-go 建议；不替代平台验证 Skill、具体测试工具或独立 code-review。
---

# QA Workflow

## Purpose

以平台中立合同编排 PRD、Bug 与 Release 三类 QA 工作流，独立表达产品质量覆盖、证据缺口、残余风险和发布建议。平台包只提供环境画像与执行 Adapter；Verification Level 与 QA Coverage Level 始终分开。

## Agent Rules

- 使用 `validate` mode 时，只校验 `qa-plan-v1`、`test-case-v1`、`test-result-v1`、`defect-report-v1`、`regression-set-v1` 或 `qa-report-v1`，不得补造缺失证据。
- 使用 `coverage` mode 时，按风险 likelihood × impact、风险 category 与 workflow floor 编译 `smoke → targeted → regression → compatibility → end-to-end → release-candidate`；不得据此选择或宣称 Verification 已通过。
- 使用 `plan` mode 时，冻结目标、included/excluded scope、风险、环境 fingerprint、入口/退出条件、coverage 与独立 verification 字段。
- 使用 `report` mode 时，只聚合已存在的结果、缺陷、waiver 和 evidence；未执行、blocked、cancelled 与 stale 不得伪装为 passed。
- PRD 流程保持 requirement-to-case trace；覆盖边界、异常、权限、离线、兼容与可访问性。
- Bug 流程冻结复现环境与最小证据；`not-reproduced` 必须记录 blocker 和下一证据动作；关闭缺陷必须拥有 fix verification 与 regression case。
- Release 流程只有在 QA 与当前 Ledger Verification 均 passed、Delivery completed、无 waiver 且无 residual risk 时输出 `go`；有明确 owner/expiry 的 waiver 才能输出 `conditional-go`；失败或阻塞输出 `no-go`。
- 环境、测试数据或上游 source fingerprint 变化后，将旧 regression evidence 标为 `stale`。
- 小任务可由同一 Agent 顺序承担 planner、case-designer、executor、triage、regression-owner 与 reporter；独立 reviewer 必须由未参与实现的 Agent 承担，QA 不得替代 code-review。
- 具体 build、test、UI automation、device、browser、installer 或平台权限操作必须交给已选择的平台 Capability；缺少 Provider 时返回 `blocked`。
- 同类 fail-fix-report 最多回环两次；超限后输出 `blocked` 和 `next_action`。
- Token Budget 优先投入风险、边界、失败与残余风险；低风险小任务缩减角色切换和解释，不得删减证据字段。

## Workflow

1. 识别 `prd | bug | release`，收集 scope、requirement、risk 与环境事实。
2. 生成或校验 QA Plan；将 coverage 与 verification 作为两个字段记录。
3. 设计 traceable Test Cases，并由平台 Adapter 执行。
4. 把结果记录为 Test Result；失败时创建 Defect Report，修复后更新 Regression Set。
5. 聚合 QA Report；Release 工作流额外给出发布建议。
6. 把 QA summary 写入 Delivery Report 的 `quality` 字段，保留 `validation` 字段原值。

仓内参考编译器位于 `src/agent_workflow/qa/workflows.py`。冻结工作流样本时运行 `PYTHONPATH=src python3 scripts/build_phase4_qa_goldens.py`，并校验 `tests/golden/phase4-qa/golden-index.json`；不得手工改写 fingerprint 或 golden hash。

合同真源位于同一 package 的 `contracts/`。字段或枚举不确定时读取对应 Schema，不在 Skill 内复制第二套合同。

## Inputs

- `validate`：使用 `generic-request-v1` 包装 artifact kind 与内容。
- `coverage`：严格使用 `qa-coverage-request-v1`。
- `plan`：严格使用 `qa-plan-request-v1`。
- `report`：严格使用 `qa-report-request-v1`，同时提供 QA Plan、Test Results、Defect Reports、Regression Set、waivers、evidence、评估日期与当前 workflow/run identity。

## Escalation Rules

- 未知 Schema version、缺失环境 fingerprint、证据 URI 不受控或 QA/Verification 状态被混用时 fail-closed。
- 缺少 Desktop 或其他平台 Provider 时保留 QA Plan，但执行节点与发布建议必须 `blocked`。
- 需要扩大文件、设备、网络或凭据权限时，交回平台 Capability 和显式审批，不在 QA Core 中升级权限。

## Relationship to Other Skills

- `workflow-orchestration`：负责任务 DAG、checkpoint、fail-fix-report 上限和 Delivery Report 总编排。
- 平台 verification / automation Skill：负责真实环境与执行证据。
- `code-review`：负责独立实现审查；不得被 QA 角色合并。
- Design discipline：可选提供 UI Validation Report；QA 合同不得依赖特定 Design Provider。

## Outputs

- `validate`：`generic-result-v1`，status 只使用 `completed | partial | blocked | failed`。
- `coverage`：`qa-coverage-result-v1`，仅返回 canonical coverage 子合同，不伪装为完整 QA Plan。
- `plan`：`qa-plan-v1`。
- `report`：`qa-report-v1`；`status`、`verification.status`、release recommendation 与 residual risk 按 Schema 分字段表达。
- 所有 mode 额外给出 `next_action: none | collect_evidence | run_platform_adapter | fix | blocked` 时，必须放在外层 orchestration result，不得写入上述封闭 artifact Schema。

## Exit Conditions

- 所有 artifacts 通过对应 Schema 与运行时不变量校验。
- requirement、risk、case、result、defect 与 regression ownership 可追溯。
- QA status 与 Verification status 分开报告，未执行证据未被伪造。
- Release 建议包含 blocker、waiver owner/expiry 与 residual risk。
- 实现改动已有定向验证或明确 `no_test_reason`，并通过独立 reviewer gate。
