# Checkpoint 与 Fail-Fix-Report 合同

- `CP0 Intent Lock`：首次写入前冻结目标、边界、非目标、成功标准、实施步骤与验证/review 路径。
- `CP1 Anchor Slice`：先完成最小关键切片；失败时只修当前切片，不扩大写集。
- `CP2 Validation Baseline Freeze`：冻结受影响测试、执行环境、证据格式和最后相关修改时间。
- `CP3 Final Gate`：必要验证有效，独立 reviewer 已完成且无阻塞项。

主 Agent 维护唯一 `checkpoint_status`。同类问题执行 `fail → fix → rerun → report`，最多两轮；超限或 reviewer 不可用时报告 blocked。
