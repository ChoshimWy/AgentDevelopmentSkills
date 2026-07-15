# 通用 Handoff 与失败回环

1. explorer 返回事实、候选文件与风险，不修改文件。
2. main 完成 CP0，明确 ownership 与禁止范围。
3. builder 完成 CP1 切片并返回 changed files、影响面与测试影响。
4. tester 或 main 冻结 CP2，只执行覆盖风险的最低验证。
5. 独立 reviewer 审查累计差异和验证故事。
6. 阻塞项按首个真实失败回写 builder；同类最多两轮。
7. reporter 只汇总已有证据，不能补造结论。

任何平台命令、权限和环境选择均由平台 Capability 决定。
