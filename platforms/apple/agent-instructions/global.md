## Apple 平台规则（仅在选择 Apple 时生效）

<!-- rule:apple.environment-constraints effect=allow -->
- 将 OS、SDK、Xcode、Swift 语言模式以及真机或模拟器视为一等约束；结论依赖这些条件时必须显式说明。
<!-- rule:apple.official-api-and-concurrency effect=allow -->
- Apple API、availability 与 WWDC 行为优先核对官方文档；新实现默认优先 Swift 与结构化并发，UI 更新保持主线程或 `@MainActor` 隔离。
<!-- rule:apple.local-pod-source effect=deny -->
- CocoaPods 或私有组件联调先核对 `Podfile`、`Podfile.lock` 与 `Pods/Manifest.lock`；本地 `:path` Pod 修改真实组件仓，不改 `Pods/` 快照，未经授权不切回线上依赖。
<!-- rule:apple.xcode-validation-route effect=deny -->
- 所有 Xcode 验证统一使用项目根 `codex_verify.sh` 或 `~/.codex/bin/codex_verify`，经 shared build-queue 执行最窄证据；当前 wrapper 提供 exact-request fingerprint 去重，Verification Session 与 same-or-stronger 跨请求复用仍以独立 scaffold 工具提供，不得伪报为 daemon 已执行能力。不得使用 Xcode MCP 或直接调用验证型 `xcodebuild`。
<!-- rule:apple.independent-review-gate effect=deny -->
- Apple 实现任务以定向验证或明确的 `no_test_reason` 收口，并由未参与实现的独立 reviewer 执行 `code-review`；高风险时才升级完整 build、Archive、真机或 FULL 验证。
