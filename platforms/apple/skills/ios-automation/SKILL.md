---
name: ios-automation
description: iOS 设备自动化 Skill，覆盖 Simulator 与真机，用于设备发现、生命周期、Scenario/Fixture、语义 snapshot、accessibility tree、交互、UI smoke、截图、UI Summary、replay 与诊断；`interaction-evidence` 可吸收 ready Xcode 官方 device-interaction 的 session/evidence 语义，但执行仍走本仓工具和验证合同；构建配置、业务实现、测试编写和一次性验收不属于本 Skill。
---

# iOS 设备自动化

## Purpose

Automate iOS Simulator and physical-device workflows for install, launch, navigation, accessibility checks, UI smoke, screenshots, device diagnostics, and lifecycle management without replacing build configuration, test implementation, or final verification Skills.

## 中文说明

该 Skill 是 iOS 设备自动化统一入口，覆盖两类模式：

- Simulator 模式：模拟器生命周期、安装启动、语义导航、accessibility tree、UI smoke、截图和视觉取证。
- 真机模式：设备发现、build/test 设备选择、安装启动、进程查询和常见真机诊断。

该 Skill 不负责 Build Settings、签名策略、Archive/Export、普通业务实现、测试代码编写或一次性完整构建验收。

## When to Use

Use this Skill when the task needs:

- Simulator boot / shutdown / create / erase / delete。
- App install / launch / terminate on Simulator or device。
- UI navigation by text / accessibility。
- Semantic UI snapshot with snapshot-local element refs such as `@e1`。
- Accessibility tree inspection。
- UI smoke execution。
- Debug/Test-only Scenario、Fixture 注入与直接页面路由。
- 固定设备、语言、主题、Dynamic Type 的可重复 UI 取证。
- 结构/行为断言、区域截图与 `ui-summary.json`。
- Replayable exploratory UI flow capture。
- Screenshot or visual evidence capture。
- Device discovery and connected-device diagnosis。
- Simulator status bar, clipboard, privacy permission, push notification setup。
- Real-device install / launch / diagnose workflow。
- Ready official-expertise packet routed `device-interaction` semantics into `interaction-evidence` mode.

## When Not to Use

Do not use this Skill when:

- The task is Build Settings, signing, certificates, Archive, Export, or CI/CD; use `xcode-build`.
- The task is one-off project-environment build verification; use `apple-verification`.
- The task is test writing; use `ios-feature-implementation(test-implementation)`. For affected unit test selection, use `apple-verification`.
- The task is normal feature implementation; use implementation Skills.
- The task is crash or runtime root-cause analysis; use `apple-debugging`.
- The task is performance profiling, benchmark, `xctrace`, or Instruments; use `ios-performance`.

## Agent Rules

### Device Mode Rules

- First classify target mode: `simulator` or `device`.
- Once a mode is selected, keep the chain in that mode unless the user asks to switch or the current mode is blocked.
- Do not mix Simulator UDID, `xcodebuild` destination id, and `devicectl` device identifier.
- If the problem narrows to signing/certificates/Archive/CI, route to `xcode-build`.
- If the problem narrows to build verification, route to `apple-verification`.

### Simulator Rules

- Prefer structured UI data over pixels: `screen_mapper.py` + `navigator.py`.
- Start UI navigation from semantic snapshots: run `screen_mapper.py --refs` before tapping by ref.
- Treat `@e1` / `@e2` refs as snapshot-local only; refresh refs after navigation, scrolling, alerts, keyboard changes, or any state-changing action.
- Use refs for same-screen actions (`navigator.py --ref @e1 --tap`) and text / accessibility identifiers for durable UI smoke specs.
- Use text-before-pixels.
- Use screenshots as evidence, not as the only state assertion.
- Prefer accessibility tree and text assertions for UI smoke.
- Convert useful exploratory flows into replayable UI smoke specs or recorder artifacts when the flow may need rerun / CI handoff.
- Explicit `--udid` should override auto-selection.
- Without `--udid`, prefer an already booted Simulator when appropriate.

### Scenario Rules

- 复杂页面优先使用 Debug/Test-only 启动参数构造状态：`-AgentScenario`、`-AgentScreen`、`-AgentFixture`；不要要求 Agent 手工完成登录、配网、BLE 连接或深层导航。
- Scenario 必须数据确定、无真实网络/BLE 依赖、可重复启动、可清理，并允许固定 locale、appearance、Dynamic Type 与设备。
- Scenario/Fixture 是项目显式提供的测试合同；本 Skill 不得在生产路径注入 fixture，也不得猜测不存在的页面路由。
- UI 验证按结构、行为、视觉区域分层。先检查 accessibility identifier/文案/状态/可操作性，再检查交互，最后只比较关键区域。
- Scenario、Fixture、Snapshot baseline、locale、appearance 或 Dynamic Type 改变时，相关 UI evidence fingerprint 必须失效。

### Physical Device Rules

- For build/test destination selection, prefer `xcodebuild -showdestinations` real iOS destinations.
- For install/launch/diagnose, prefer `xcrun devicectl list devices` connected devices.
- `connected` devices are preferred over `available (paired)`.
- `unavailable` devices are diagnostic targets only, not default run targets.
- Do not treat paired but disconnected devices as connected devices.
- `xcodebuild` destination id and `devicectl` device identifier are different and must not be mixed.

### Build/Test Rules

- This Skill may call scripts that trigger build/test only as part of device automation.
- Validation-type `xcodebuild` invoked by automation scripts should use the project wrapper / shared build-queue daemon.
- If no scheme is explicit, prefer schemes bound to unit test targets / bundles such as `*Tests`.
- Do not use this Skill as the default final validation step for all code changes.
- If final evidence is required, route through `apple-verification`.

### Evidence Rules

- Capture structured state first: semantic refs, accessibility summary, app state, then screenshot / logs only when needed.
- For successful same-screen navigation, prefer concise refs / assertions over screenshots.
- On failure, capture the minimum useful bundle: first failing step, current semantic snapshot, accessibility excerpt, screenshot path, app state path, and relevant logs.
- Store replay artifacts when the task is exploratory but likely reusable; do not paste full replay scripts if a path is enough.
- Report device/simulator identifier, OS/runtime, bundle id, and command path.
- Record evidence path for semantic snapshot, replay script, screenshots, accessibility dumps, app state, or logs.
- If the user asks for a formal HTML UI smoke report, visual evidence report, or handoff document, route the collected evidence bundle to `html-docs` for final document generation.
- Do not paste huge logs.
- Do not claim UI state from screenshots alone if accessibility/text state contradicts it.

### Interaction Evidence Federation Rules

- Consume only a ready packet entry for `device-interaction` with `tool_policy=semantic-only`.
- Reuse its session lifecycle and evidence intent, but do not call `DeviceInteraction*`, `DeviceEventSynthesize` or Xcode MCP tools from this workflow.
- Translate install/run/gesture/hierarchy/screenshot operations to the selected Simulator or physical-device scripts and keep identifiers type-safe.
- Start sessions only when device evidence is explicitly required and close resource-heavy sessions promptly.
- Store source/routing hash with the UI evidence fingerprint; any source, app, scenario, locale, appearance, Dynamic Type or destination change invalidates reuse.
- Device interaction evidence supports behavior claims but does not replace `apple-verification` Final Evidence Gate.

### Token Budget

- Prefer structured summaries from scripts.
- Do not paste full simulator logs.
- Do not paste full device logs.
- Do not dump full accessibility tree unless requested.
- Include only relevant nodes or failure excerpts.
- For build/test failure logs, use `apple-verification`.

## Device Selection Strategy

### Simulator

1. Use explicit `--udid` when provided.
2. Otherwise resolve current booted Simulator.
3. If none is booted and a device name is provided, boot that device.
4. If no device is specified, choose the project/default simulator policy.

### Physical Device

1. For build/test: prefer first real iOS destination from `xcodebuild -showdestinations` that is usable.
2. For install/launch/diagnose: prefer `connected` device from `xcrun devicectl list devices`.
3. Then match user-provided device name or identifier.
4. Use `available (paired)` only when explicitly acceptable.
5. Use `unavailable` only for diagnosis.

## Core Workflow

1. Classify target mode: simulator or physical device.
2. Resolve target identifier and verify availability.
3. Identify app bundle id, app path, workspace/scheme if needed.
4. Resolve explicit Scenario/Screen/Fixture and deterministic locale/appearance/Dynamic Type inputs when UI state construction is needed.
5. Capture semantic snapshot before UI actions when navigation is needed.
6. Run the narrowest automation task: install, launch, navigate, inspect, screenshot, diagnose, or UI smoke.
7. Capture structure/behavior/visual-region results into `ui-summary.json`; escalate to full screenshots/logs only when useful.
8. Persist replay / UI smoke artifacts if the flow should be rerunnable.
9. Report result and next action.
10. If the issue is build/signing/configuration, route to the correct Skill.

## Simulator Workflow

1. Health check: `bash scripts/simulator/sim_health_check.sh`.
2. Boot / shutdown / lifecycle:
   - `python3 scripts/simulator/simctl_boot.py --name "iPhone 16 Pro"`
   - `python3 scripts/simulator/simctl_shutdown.py --all`
3. Launch and state:
   - `python3 scripts/simulator/app_launcher.py --launch <bundle_id>`
   - `python3 scripts/simulator/screen_mapper.py --refs`
4. Semantic interaction:
   - `python3 scripts/simulator/navigator.py --ref @e1 --tap`
   - `python3 scripts/simulator/navigator.py --find-text "Login" --tap`
5. Validation and diagnostics:
   - `python3 scripts/simulator/accessibility_audit.py`
   - `python3 scripts/simulator/app_state_capture.py --app-bundle-id <bundle_id>`
   - `python3 scripts/simulator/ui_smoke_runner.py --spec .codex/ui-smoke.yml`

## Physical Device Workflow

1. List devices: `xcrun devicectl list devices`.
2. Build/test if needed: `bash scripts/device/device_build_and_test.sh <repo-root>`.
3. Install/launch:
   - `bash scripts/device/device_install_and_launch.sh --app <path> --bundle-id <bundle_id>`
4. Diagnose:
   - `bash scripts/device/device_diagnose.sh --device <devicectl-device-id>`

## Script Groups

### Simulator

- Build & Logs: `scripts/simulator/build_and_test.py`, `scripts/simulator/log_monitor.py`
- Navigation & Interaction: `scripts/simulator/screen_mapper.py`, `scripts/simulator/navigator.py`, `scripts/simulator/gesture.py`, `scripts/simulator/keyboard.py`, `scripts/simulator/app_launcher.py`
- Testing & Analysis: `scripts/simulator/accessibility_audit.py`, `scripts/simulator/visual_diff.py`, `scripts/simulator/test_recorder.py`, `scripts/simulator/app_state_capture.py`, `scripts/simulator/ui_smoke_runner.py`, `scripts/simulator/sim_health_check.sh`
- Advanced & Permissions: `scripts/simulator/clipboard.py`, `scripts/simulator/status_bar.py`, `scripts/simulator/push_notification.py`, `scripts/simulator/privacy_manager.py`, `scripts/simulator/sim_list.py`, `scripts/simulator/simulator_selector.py`
- Simulator Lifecycle: `scripts/simulator/simctl_boot.py`, `scripts/simulator/simctl_shutdown.py`, `scripts/simulator/simctl_create.py`, `scripts/simulator/simctl_delete.py`, `scripts/simulator/simctl_erase.py`

### Physical Device

- Build & Test: `scripts/device/device_build_and_test.sh`
- Install & Launch: `scripts/device/device_install_and_launch.sh`
- Diagnose: `scripts/device/device_diagnose.sh`

## Inputs

Expected input contract:

```json
{
  "mode": "simulator | device | auto",
  "task": "boot | install | launch | navigate | inspect | screenshot | ui-smoke | diagnose | shutdown",
  "bundle_id": "com.example.app",
  "app_path": "optional",
  "udid": "optional-simulator-udid",
  "device_identifier": "optional-devicectl-id",
  "xcode_destination_id": "optional-xcodebuild-destination-id",
  "workspace": "optional",
  "scheme": "optional",
  "ui_smoke_spec": ".codex/ui-smoke.yml",
  "scenario": "device-connected",
  "screen": "device-control",
  "fixture_path": "Fixtures/device-connected.json",
  "locale": "zh-Hans",
  "appearance": "dark",
  "dynamic_type": "normal",
  "semantic_ref": "@e1",
  "replay_output": ".codex/ui-smoke-artifacts/",
  "constraints": [],
  "official_expertise": {
    "status": "ready | partial | blocked | absent",
    "selected_skill": "device-interaction | null",
    "selected_skill_sha256": "optional"
  }
}
```

## Outputs

Return compact structured output:

```json
{
  "status": "passed | failed | skipped | blocked",
  "mode": "simulator | device",
  "task": "launch | navigate | ui-smoke | diagnose",
  "target": {
    "name": "iPhone 16 Pro",
    "udid": "...",
    "device_identifier": "...",
    "xcode_destination_id": "...",
    "runtime": "iOS 18.x"
  },
  "bundle_id": "com.example.app",
  "executed_commands": [],
  "evidence": {
    "semantic_snapshot": "path-or-summary",
    "replay": "path-or-none",
    "accessibility_tree": "path-or-summary",
    "ui_summary": "path-to-ui-summary.json",
    "screenshot": "path",
    "app_state": "path-or-summary",
    "logs": "path-or-summary"
  },
  "first_failure": null,
  "official_expertise_used": [],
  "next_action": "none | retry | route-xcode-build | route-apple-verification | route-apple-debugging | blocked"
}
```

## Exit Conditions

Return `passed` when:

- The requested automation task completed.
- Target device/simulator identity is recorded.
- Evidence or structured state is captured when relevant.

Return `failed` when:

- The task executed but app launch, navigation, UI smoke, install, or diagnosis failed.
- First failure is captured with enough context.

Return `blocked` when:

- Required device/simulator is unavailable.
- App path or bundle id is missing.
- Signing/install permission prevents progress.
- Required simulator runtime is missing.
- Tooling such as `simctl` or `devicectl` is unavailable.

Return `skipped` when:

- Automation is not needed for the current risk level.
- A higher-level Skill decides targeted tests + review are sufficient.

## Escalation Rules

Escalate to `xcode-build` when:

- Signing, certificates, profiles, Build Settings, Archive, Export, or CI configuration blocks automation.

Escalate to `apple-verification` when:

- The user asks for final build verification.
- The issue is project-environment build evidence, not device automation.

Escalate to `ios-feature-implementation(test-implementation)` when test code is needed; escalate to `apple-verification` when targeted validation is needed:

- The task is writing XCTest/XCUITest code or selecting affected tests.

Escalate to `apple-debugging` when:

- The app launches but crashes, hangs, leaks, or shows runtime symptoms.

Escalate to `ios-performance` when:

- The task becomes startup performance, frame rate, CPU/memory/energy profiling, `xctrace`, or Instruments.

Escalate to `apple-verification` when:

- Automation-triggered build/test logs need compact failure attribution.

Escalate to `html-docs` when:

- Automation evidence, screenshots, replay artifacts, or UI smoke results must be packaged as a formal HTML report or handoff document.

## Reporting Format

```text
Automation status: passed | failed | skipped | blocked
Mode: simulator | device
Task: launch | navigate | inspect | ui-smoke | diagnose
Target: <name / udid / device id>
Bundle ID: <bundle id>
Evidence:
- semantic_snapshot: <path or summary>
- replay: <path or none>
- accessibility: <path or summary>
- screenshot: <path or none>
- app_state: <path or summary>
First failure: none | ...
Next action: none | route-xcode-build | route-apple-debugging | blocked
```

## Optional Evidence Verification

- `ios-automation` is not the default final validation step for all code changes.
- Default closure remains targeted validation / necessary validation plus independent reviewer subAgent `code-review`.
- Use automation only when user asks, UI/device evidence is needed, or the main Agent decides device-level evidence is required.
- If full project-environment build evidence is needed, use `apple-verification` / `apple-verification`.
- Any optional full verification evidence must come from target project root, not sandbox-only results.

## Reference Resources

- `references/semantic-snapshot-and-replay.md`
- `references/accessibility_checklist.md`
- `references/test_patterns.md`
- `references/simctl_quick.md`
- `references/idb_quick.md`
- `references/devicectl-quick.md`
- `references/device-troubleshooting.md`

## Relationship to Other Skills

- Business, SwiftUI, UIKit, mixed UI, advanced Swift, and refactor implementation: `ios-feature-implementation` with the matching internal mode.
- Build Settings, signing, Archive/Export, CI/CD: `xcode-build`.
- Final build verification: `apple-verification`.
- Test writing: `ios-feature-implementation(test-implementation)`; affected tests and validation: `apple-verification`.
- Runtime root-cause analysis: `apple-debugging`.
- Performance profiling: `ios-performance`.
- Build/test log attribution: `apple-verification`.
