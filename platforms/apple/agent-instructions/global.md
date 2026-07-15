## Apple 平台规则（仅在选择 Apple 时生效）

<!-- rule:apple.environment-constraints effect=allow -->
- 将 OS、SDK、Xcode、Swift 语言模式以及真机或模拟器视为一等约束；结论依赖这些条件时必须显式说明。
<!-- rule:apple.official-api-and-concurrency effect=allow -->
- Apple API、availability 与 WWDC 行为优先核对官方文档；新实现默认优先 Swift 与结构化并发，UI 更新保持主线程或 `@MainActor` 隔离。
<!-- rule:apple.local-pod-source effect=deny -->
- CocoaPods 或私有组件联调先核对 `Podfile`、`Podfile.lock` 与 `Pods/Manifest.lock`；本地 `:path` Pod 修改真实组件仓，不改 `Pods/` 快照，未经授权不切回线上依赖。
<!-- rule:apple.xcode-validation-route effect=deny -->
- 日常 Xcode 验证优先使用官方 Xcode MCP 的最窄测试或构建；需要项目环境证据时使用项目根 `codex_verify.sh` 或 `~/.codex/bin/codex_verify`，不得直接调用验证型 `xcodebuild`。
<!-- rule:apple.independent-review-gate effect=deny -->
- Apple 实现任务以定向验证或明确的 `no_test_reason` 收口，并由未参与实现的独立 reviewer 执行 `code-review`；高风险时才升级完整 build、Archive、真机或 FULL 验证。
