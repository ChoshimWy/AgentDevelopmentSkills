# 角色运行时选择

角色模型、推理强度、sandbox 与专属工具由运行时配置决定，不写入全局 AGENTS，也不向 subAgent 调用注入未公开字段。

- builder：实现质量与工具调用能力优先。
- reviewer：高可靠、只读且必须独立。
- explorer/pm/tester：平衡速度与证据质量。
- reporter：低成本汇总已有证据。
- researcher：只加载任务所需的官方资料或设计工具。

目标角色不可用时继承运行时默认能力并显式报告 fallback；独立 reviewer 不得降级为实现者自审。
