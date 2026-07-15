---
name: apple-verification
description: Apple / Xcode 项目统一验证 Skill。用于按当前 Diff 生成 evidence requirements、选择受影响测试、通过 codex_verify + shared build-queue 执行 quick/checkpoint/final 验证、复用 Verification Session 与相同或更强证据、读取结构化 artifact 收敛首个阻塞错误，并在独立 code-review 后执行 Final Evidence Gate；不要用它编写生产/测试代码、修改构建配置或直接运行裸 xcodebuild。
---

# Apple Verification（统一验证入口）

## Purpose

只补齐当前 Diff 缺失的验证证据。所有验证型 `xcodebuild`、项目发现和 destination 选择统一进入 `codex_verify` / shared build-queue；不依赖已打开的 Xcode 窗口或 Xcode MCP。

## Modes

| Mode | Responsibility |
| --- | --- |
| `route` | 分类 Diff，输出最低有效等级与 `required_evidence` |
| `affected-tests` | 选择最窄 XCTest；无入口时输出 `no_test_reason` |
| `auto` | 自动选择 dev/checkpoint/final lane，并只补缺失证据 |
| `quick-verify` | 开发循环：缓存优先、受影响测试优先、紧凑结果 |
| `execute` | checkpoint/final 或高风险项目环境验证，生成可归档证据 |
| `digest` | 从结构化 artifact 提取第一个 blocking failure |
| `final-gate` | 校验证据新鲜度、覆盖面、Review 与残余风险 |

不负责：生产/测试代码实现、Mock/fixture/Page Object 编写、Build Settings、签名、Archive/Export、CI/CD、crash 调试、性能 profiling 或普通设备导航。

## Agent Rules

### Current Implementation Boundary

- The current wrapper implements exact request-fingerprint in-flight attachment, successful-result reuse, schema/generation-bound immutable job publication, token/heartbeat-bound daemon ownership, offline startup reconciliation, live-safe read-only doctor, offline-only repair and structured-artifact validation. Legacy terminal history is diagnostic-only unless it satisfies the current immutable publication contract.
- A committed Apple Worktree Session request is an implemented wrapper/daemon input: pass it with `--worktree-session-request`; submission and daemon pre/post execution validation, frozen destination/test plan export, Slot lease and structured Session summary are enforced. Read `references/worktree-session.md` before using it.
- Worktree Session source identity is daemon-recomputed, but request `environment_fingerprint` and `target_fingerprints` remain caller-frozen assertions until an upstream coordinator independently derives them; never report those labels alone as verified evidence.
- `verification_coordinator.py`, `session_store.py`, `fingerprint.py`, `evidence_cache.py` and `affected_tests.py` are executable contract/scaffold tools, but the wrapper/daemon does not yet invoke them as one end-to-end coordinator.
- Lane-aware priority scheduling, persisted Session mutation, same-or-stronger cross-request evidence reuse, deterministic failure caching, request coalescing and automatic `.xctestrun` registration remain follow-up implementation work. Until then, do not report those behaviors as executed daemon evidence.
- `build-for-testing` remains supported. General non-Worktree `test-without-building` remains an action, but Worktree Session requests fail closed until the daemon consumes a validated immutable build artifact identity; compatibility must never be inferred from a Shared DerivedData path.

### Core Rules

### One Entry Point

- Never run validation-type `xcodebuild` directly, including `-list`, `-showdestinations`, build, test, `build-for-testing`, or `test-without-building`.
- Prefer target project `./codex_verify.sh`; fall back to `~/.codex/bin/codex_verify`.
- `codex_verify` owns workspace/project, scheme, test plan, configuration, destination, formatter, DerivedData, queueing, parsing and artifact generation.
- The wrapper must submit work to the shared build-queue daemon and use Xcode system DerivedData. Do not create per-Agent DerivedData to bypass `build.db` locks.
- Xcode MCP、AppleScript、Accessibility 点击 Xcode GUI，以及第三方 Xcode GUI 控制 MCP 不属于验证回退路径。

### Evidence First

- `route` must output evidence requirements, not only a linear level.
- Evidence kinds are independent: `compile:<target>`, `test:<selector>`, `ui:<scenario>:<screen>`, `review:current-diff`, and release-specific evidence.
- Reuse only passed same-or-stronger evidence whose environment/source/evidence fingerprints still match the latest tracked and untracked changes.
- A cached result is evidence only when the producer reports `cached=true`, the source job/artifact hashes remain available, and invalidation rules pass.
- Never claim cache reuse when fingerprinting or the evidence index is unavailable.

### Verification Session

- Persist one task session below `.codex/verification/sessions/<session-id>/`.
- Cache project metadata, test manifest, target/source inputs, evidence index, in-flight requests and failure classification.
- Re-discover workspace, scheme, destination or test plan only when relevant project configuration changes.
- Use the helper scripts in `scripts/` for deterministic session/fingerprint/evidence operations; do not reimplement these decisions ad hoc.
- Read `references/verification-session-schema.md` and `references/fingerprint-rules.md` before changing session or cache semantics.

### Lanes

- **Dev / quick-verify:** narrowest evidence, cache allowed, compact artifacts, no complete `.xcresult` by default.
- **Checkpoint / execute:** affected target build + affected tests + required UI smoke.
- **Final / execute:** final Diff, consumer integration, affected tests, required UI/runtime evidence, structured `.xcresult`, independent Review, Final Gate.
- Do not upgrade directly to full. Escalate only when required evidence cannot be satisfied by a narrower lane.

## Diff Routing

| Diff Type | Examples | Default evidence |
| --- | --- | --- |
| `doc-only` | Markdown, comments | none / lint |
| `rule-only` | `AGENTS.md`, `SKILL.md` | policy lint |
| `asset-only` | image/color/static JSON | resource check; UI only if runtime presentation changes |
| `test-only` | XCTest/XCUITest | targeted test |
| `swift-small` | narrow Swift/ObjC logic | affected tests or target compile |
| `swift-risky` | DB, BLE, concurrency, payment, network | affected tests + app/consumer compile |
| `ui-only` | SwiftUI/UIKit/layout/localization/accessibility | compile + targeted UI evidence |
| `project-config` | pbxproj, scheme, xctestplan, xcconfig | checkpoint/final project evidence |
| `dependency` | Podfile/lockfiles/Package.resolved | resolve + consumer integration evidence |
| `release` | signing, entitlement, Archive | route `xcode-build` + final release evidence |

## Affected Tests

- Prefer exact method -> class -> file -> bundle.
- Prefer basename and feature-folder matches: `*ViewModel` -> `*ViewModelTests`; Service/Repository/UseCase/Manager follow the same rule.
- StoreKit/subscription changes require purchase/receipt/entitlement candidates; persistence requires DB tests; BLE/mesh requires parser/state-machine tests.
- View/layout-only changes normally require compile + UI evidence rather than unrelated unit suites.
- Use `scripts/affected_tests.py` and project impact maps when available.
- If there is no deterministic low-cost test, return non-empty `no_test_reason` and `suggested_validation`; selection-only output is never executed evidence.

## Quick Verify, In-flight Dedupe and Build/Test Reuse

- Invoke `quick-verify` through the wrapper/daemon; never through Xcode MCP.
- For a reusable compatible test build, prefer `test-without-building` with the cached `.xctestrun`.
- Otherwise create one `build-for-testing` artifact and register its environment, target source, test bundle, test plan and destination fingerprints before reuse.
- Attach duplicate in-flight requests to the existing queue job. Do not enqueue the same fingerprint twice.
- Coalesce compatible build + test requests when the coordinator can prove the environment/source fingerprints match.
- Cache deterministic compile/link/test failures for the same fingerprint; retry only after source/config/destination changes, an environment/flaky classification, or explicit `--force`.
- Read `references/daemon-protocol.md` before modifying queue or reuse behavior.

## Script-Owned Decisions

The wrapper/coordinator owns:

- `.xcworkspace` vs `.xcodeproj` discovery and shared scheme selection.
- Test plan and XCTest manifest discovery.
- `.codex/xcodebuild.env` loading and session metadata refresh.
- Connected device discovery and simulator fallback.
- `TARGETED_DEVICE_FAMILY` preference, explicit destination overrides and destination locks.
- Environment/target/evidence fingerprints, in-flight dedupe and evidence reuse.
- `build-for-testing` / `.xctestrun` compatibility checks.
- Formatter bootstrap, redaction, `.xcresult` digest and first-error classification.

Agents may provide changed files, selectors, scenario/screen and requested lane. Agents must not hand-compose workspace, scheme, configuration, destination or DerivedData parameters.

## UI and Runtime Evidence

- Route device lifecycle, launch, semantic snapshot, accessibility tree, scenario injection and screenshots to `ios-automation`.
- UI-sensitive changes require the smallest sufficient combination of structure, behavior and visual-region evidence.
- Prefer deterministic Debug/Test-only scenarios and fixtures over manual login, BLE, network, pairing or navigation setup.
- A screenshot alone is insufficient when semantic/accessibility state is available.

## Digest

Read only as needed, in this order:

1. `agent-summary.json`
2. `verification-report.json`
3. `diagnostics.json`
4. `test-summary.json`
5. `ui-summary.json`
6. `xcresult-summary.json`
7. `build-summary.txt`
8. a small source/log excerpt explicitly requested by the summary

- Do not read full raw logs or recursive `.xcresult` JSON by default.
- Report and fix only the first real blocking error before resuming the original evidence request.
- Classify failure as `current_change`, `pre_existing`, `environment`, `flaky`, or `unknown`; never guess `pre_existing` without evidence.
- Use Recovery Validation: validate the narrow repaired target first, then resume the original failed evidence request.

## Final Evidence Gate

Accept only when all apply:

1. Evidence happened after the latest code/config/resource/dependency/fixture change.
2. Every `required_evidence` item has matching or stronger accepted evidence.
3. Environment and destination match the delivery baseline, or the evidence is explicitly stronger.
4. No stale test build, snapshot baseline, fixture, scenario or test-plan fingerprint is reused.
5. There is no unexplained `no_test_reason`.
6. An independent reviewer subAgent ran `code-review`, reviewed the verification story and reported no blocking issues.
7. Project/dependency/signing/resource/release risks received the stronger lane they require.
8. UI changes have structure, behavior or visual evidence appropriate to the risk.
9. Residual risks are explicit.

See `references/evidence-model.md` for dominance and gate rules. `final-gate` must not execute missing evidence itself; it reports the missing set and next action.

## Inputs

```json
{
  "verification_mode": "route | affected-tests | auto | quick-verify | execute | digest | final-gate",
  "lane": "dev | checkpoint | final | auto",
  "session_id": "optional",
  "changed_files": [],
  "target_project_root": ".",
  "requested_level": "none | lint | typecheck | unit | build | ui | full | auto",
  "only_testing": [],
  "scenario": null,
  "screen": null,
  "force": false,
  "no_cache": false,
  "constraints": ["narrowest sufficient evidence", "no full log"]
}
```

## Outputs

```json
{
  "status": "passed | failed | skipped | blocked | accepted | proposed",
  "verification_mode": "route | affected-tests | auto | quick-verify | execute | digest | final-gate",
  "lane": "dev | checkpoint | final",
  "verification_level": "none | lint | typecheck | unit | build | ui | full",
  "session_id": null,
  "environment_fingerprint": null,
  "target_fingerprints": {},
  "required_evidence": [],
  "accepted_evidence": [],
  "missing_evidence": [],
  "in_flight_request": null,
  "cached": false,
  "artifact_paths": {},
  "first_blocking_error": null,
  "failure_attribution": "none | current_change | pre_existing | environment | flaky | unknown",
  "no_test_reason": null,
  "suggested_validation": [],
  "verification_story": "accepted | incomplete | blocked",
  "residual_risk": [],
  "next_action": "none | run-missing-evidence | fix-first-error | recovery-validation | ios-automation | code-review | xcode-build | blocked"
}
```

## Exit Conditions

- `passed`: the requested evidence ran or valid same-or-stronger evidence was reused.
- `accepted`: Final Gate accepted all evidence and independent Review.
- `skipped`: no Apple build/test evidence is required; explain why.
- `blocked`: the required environment, target, tool, scenario or review is unavailable.
- `failed`: execution produced a classified first blocking failure.
- `proposed`: route/affected-tests returned a plan only; never present it as executed validation.

## Escalation Rules

- Route Scenario/Fixture/UI capture to `ios-automation`.
- Route Build Settings, signing, Archive/Export and CI/CD to `xcode-build`.
- Route production/test implementation to `ios-feature-implementation`.
- Route runtime crashes/hangs/leaks to `apple-debugging` and performance evidence to `ios-performance`.
- Escalate dev -> checkpoint -> final only when missing evidence or risk requires it.

## Relationship to Other Skills

`workflow-orchestration` owns platform-neutral checkpoints; this Skill owns Apple verification evidence. `ios-automation` supplies UI/runtime artifacts, independent `code-review` supplies review evidence, and `apple-verification(final-gate)` only judges the combined story.

多 Worktree Session 必须先由共享 Git/Workflow 能力冻结 committed source identity，再按 `references/worktree-session.md` 生成 Apple daemon 请求和 Shared DerivedData 产物身份；本 Skill 不复制通用 Git Patch/Registry 实现。

## Token Budget

Return baseline, fingerprint/cache decision, required/accepted/missing evidence, first blocker, artifact paths, residual risk and next action. Do not paste large logs, diffs or `.xcresult` dumps.
