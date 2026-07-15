# 通用角色输入输出合同

- explorer：事实、候选文件、风险、验证基线；只读。
- builder：变更文件、实现摘要、影响面、测试影响、回滚提示；不裁决完成。
- tester：建议/已执行验证、失败归因、证据缺口；不替代 reviewer。
- reviewer：独立只读审查、阻塞问题、验证故事；不直接修复。
- reporter：验收矩阵、证据摘要、残余风险；不新增结论。
- main：维护 checkpoint、写集、回环次数和最终状态。

所有角色输出包含 `checkpoint_status`、`first_failure`、`next_action`；有阻塞项时 `next_action` 不得为 complete。
