## Core 通用执行规则

<!-- rule:core.workspace-facts effect=allow -->
- 以当前工作区的代码、配置、脚本和文档为事实来源；修改前先定位约束，默认做最小可验证改动。
<!-- rule:core.preserve-unowned-changes effect=deny -->
- 不得回滚、覆盖或顺手提交用户及其他 Agent 未授权的改动；遇到脏工作区时按任务范围收口。
<!-- rule:core.selected-package-boundary effect=deny -->
- 未经明确选择的平台包不得参与路由、安装、权限申请或全局规则合成；缺少必需能力、存在冲突或无法确定性合并时 fail-closed。
<!-- rule:core.minimum-validation effect=allow -->
- 验证选择覆盖风险的最低等级，优先结构化证据和最窄受影响测试；无法验证时明确记录原因与建议验证方式。
<!-- rule:core.delivery-summary effect=allow -->
- 最终回复默认说明改了什么、如何验证、剩余风险或后续动作。
