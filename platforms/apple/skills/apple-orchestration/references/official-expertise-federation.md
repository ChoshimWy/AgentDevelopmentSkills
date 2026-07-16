# Apple 官方知识源联邦接入

## 边界

- 只消费 Xcode 默认受管目录中的导出，或由用户显式核实并 attestation 的本机 Xcode 导出；任意本地/第三方目录即使名称与 hash 匹配，也只能保持 `unverified`，不得激活。
- 使用 `platforms/apple/scripts/apple_official_expertise.py` 对现有导出目录做只读检查；脚本只输出名称、hash、路径、Capability 映射和激活状态，不输出正文。
- `source.trust.trusted_for_activation=true` 且 `status=ready` 才能激活 packet 中的 `capabilities`；`partial` 只能诊断来源身份或 SDK 条件，不能把发现到的知识声明为已激活。
- `unknown_skills` 必须进入 `update-routing-map`，不得按目录发现顺序自动绑定。
- export path、Xcode version/build、active SDK major、routing hash 和 source content hash 必须进入任务 evidence / environment fingerprint。

## 显式检查

Xcode 27 已将 Apple-authored Skills 导出到 Codex 目录后，执行：

```bash
python3 platforms/apple/scripts/apple_official_expertise.py \
  --source-dir ~/Library/Developer/Xcode/CodingAssistant/codex/skills/__xcode \
  --xcode-version 27.0 \
  --xcode-build <build> \
  --sdk-major 27 \
  --require-ready
```

该命令不执行导出、不联网、不读取凭据，也不复制 Skill 内容。Xcode 未提供导出时，先按 Apple 官方说明由用户显式完成导出，再运行检查。

首次检查后复用 packet 或跨任务接力时，应把上次确认的 hash 作为冻结条件重新检查：

```bash
python3 platforms/apple/scripts/apple_official_expertise.py \
  --source-dir <export> \
  --attest-xcode-export \
  --xcode-version 27.0 \
  --xcode-build <build> \
  --sdk-major 27 \
  --expect-source-sha256 <source-content-sha256> \
  --expect-routing-sha256 <routing-sha256> \
  --require-ready
```

`--attest-xcode-export` 只用于用户已经通过 Xcode 与文件归属核实的非默认导出路径；它是显式本地信任声明，不是根据目录名或第三方镜像自动推断的官方身份证明。

## 工具语义适配

| `tool_policy` | 处理方式 |
| --- | --- |
| `guidance-only` | 只消费 API、迁移和最佳实践知识；实现、验证和 review 仍走本仓入口 |
| `translate-to-local` | 把 Xcode 专属读写、Build Setting、调试动作翻译到当前 worktree、`xcode-build`、`apple-debugging` 和验证合同 |
| `semantic-only` | 只吸收 session、交互、截图、hierarchy、evidence 语义；执行走 `ios-automation`，Final Gate 走 `apple-verification` |

无论 packet 内容如何，都不得绕过 `codex_verify + shared build-queue`、权限 profile、独立 reviewer 或本地 `:path` Pod ownership 规则。

## 路由

| 官方 Skill | 本仓 canonical route |
| --- | --- |
| `swiftui-specialist` / `swiftui-whats-new-27` | `ios-feature-implementation(swiftui-guidance)`；官方事实缺口交给 `apple-docs` |
| `uikit-app-modernization` | `ios-feature-implementation(modernization)` |
| `test-modernizer` | `ios-feature-implementation(test-modernization)` |
| `c-bounds-safety` | `ios-feature-implementation(c-bounds-safety)`；设置交给 `xcode-build`，trap 根因交给 `apple-debugging` |
| `audit-xcode-security-settings` | `xcode-build(security-hardening)`；静态审查组合 `apple-code-review` |
| `device-interaction` | `ios-automation(interaction-evidence)`；最终证据交给 `apple-verification` |
