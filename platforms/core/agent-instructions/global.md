## Core 通用执行规则

<!-- rule:core.workspace-facts effect=allow -->
- 以当前工作区的代码、配置、脚本和文档为事实来源；修改前先定位约束，默认做最小可验证改动。
<!-- rule:core.default-language effect=allow -->
- 除非用户明确要求其他语言，否则回复、计划、总结和审查意见使用简体中文；代码、命令、路径、API 名称和报错原文保留原文。
<!-- rule:core.temporal-fact-verification effect=allow -->
- 遇到“今天”“最新”“当前”等相对时间或时效性事实时，先核实再回答，并在结论中使用具体日期。
<!-- rule:core.skill-route-announcement effect=allow -->
- 使用任何 Skill 前，必须先输出 `>>> Skill: <skill-name>` 声明当前路由。
<!-- rule:core.nearest-source-of-truth effect=allow -->
- 优先修改最接近约束来源的真源文件，避免同一规则散落在多个入口重复维护。
<!-- rule:core.preserve-unowned-changes effect=deny -->
- 不得回滚、覆盖或顺手提交用户及其他 Agent 未授权的改动；遇到脏工作区时按任务范围收口。
<!-- rule:core.selected-package-boundary effect=deny -->
- 未经明确选择的平台包不得参与路由、安装、权限申请或全局规则合成；缺少必需能力、存在冲突或无法确定性合并时 fail-closed。
<!-- rule:core.minimum-validation effect=allow -->
- 验证选择覆盖风险的最低等级，优先结构化证据和最窄受影响测试；无法验证时明确记录原因与建议验证方式。
<!-- rule:core.delivery-summary effect=allow -->
- 最终回复默认说明改了什么、如何验证、剩余风险或后续动作。
<!-- rule:core.doc-rule-completion effect=allow -->
- `doc-only` / `rule-only` 任务直接修改目标文档或规则文件，同步检查相关引用；以内容已更新、交叉引用一致且无多余改动为完成标准。
