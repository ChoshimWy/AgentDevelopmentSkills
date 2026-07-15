---
name: session-worktree
description: 跨平台多 Session Git Worktree 隔离技能。用于从稳定 Commit 创建独立 Worktree、检查 Dirty Gate、计算多仓 Repository Patch/Session Source Identity，并为 Workflow Session Registry 提供 Git 身份；不负责平台构建缓存、测试命令、集成 Gate 或自动提交。
---

# Session Worktree

## Purpose

Provide platform-neutral Git workspace isolation and deterministic source identities for concurrent Agent Sessions.

## When to Use

- 多个 Agent Session 并行修改同一个 Git 项目。
- 需要从稳定 Commit 创建隔离 Worktree/Branch。
- 需要把 tracked、staged、unstaged 与 untracked 内容纳入同一 Repository Patch identity。
- Workflow Gate 需要刷新或冻结多仓 Session source identity。

## When Not to Use

- 不用它选择 Xcode、Gradle、npm、设备或测试命令。
- 不用它自动暂存、提交、合并、rebase 或删除用户修改。
- 不把未提交的 Index、Stash 或目录复制结果当作 Session Base。

## Agent Rules

- 新 Worktree 必须从解析为完整 SHA 的 Commit 创建。
- 未显式指定 Base 时，调用 Worktree 必须 clean；显式 Base 可以从 dirty 调用目录创建，但其修改绝不继承。
- 默认命令为 `agent-session`。源码仓可使用：

```bash
PYTHONPATH=src python3 -m agent_workflow.worktree_sessions.cli --help
```

- `checkpoint` 只冻结已经存在的 clean HEAD Commit，不执行 `git add` 或 `git commit`。
- 本地 Pod、Package、Framework 等可写仓库必须使用 dependency Worktree，并作为独立 repository identity 加入同一 Session。
- v1 遇到 Git submodule/gitlink 直接 fail-closed；当前不能通过仅登记 dependency identity 绕过，需等待后续 Patch 算法显式覆盖 gitlink identity。
- Patch 算法必须包含 binary/full-index tracked diff 及 untracked 路径、类型、mode、content hash；检测到计算期间变化即拒绝。
- `verification.git.repository` 只验证纯 Git Session 的 clean committed source identity；一旦选择平台，就不能用它替代对应 `verification.<platform>.*` 证据。
- Git owner 只负责 Worktree、Base、Git identity 与 Patch；Registry 状态机、Stacked dependency 和 Gate 交给 `workflow-orchestration`。

## Commands

```bash
agent-session create feature-a --repository /path/to/repo --project-id project --base agent/integration
agent-session list --repository /path/to/repo
agent-session inspect feature-a --repository /path/to/repo --refresh
agent-session fingerprint feature-a --repository /path/to/repo
agent-session checkpoint feature-a --repository /path/to/repo
```

选择平台时必须提供可验证的 Manifest root；任何 `bootstrap-only` 平台返回 `bootstrap_required`，不会生成平台 context 或 evidence。
`inspect --refresh` 与 `fingerprint` 只返回重新计算的 Context，不写 Registry；只有 manage/checkpoint 命令可以持久化。

## Inputs

```json
{"base_ref":"optional stable Commit/Ref","project_id":"project","repository":"/absolute/repo","selected_platforms":[],"session_id":"feature-a"}
```

## Outputs

Machine output uses canonical UTF-8 JSON with a trailing newline. Inspect/fingerprint conform to `worktree-session-context-v1`; list conforms to `worktree-session-list-v1`; create/checkpoint conform to `worktree-session-operation-result-v1`.

```json
{"next_action":"inspect | checkpoint | workflow-gate | resolve-blocker","status":"completed | blocked"}
```

## Exit Conditions

- `completed`: Worktree/identity operation completed and its registry context validates.
- `blocked`: Base、path、Git identity、dirty state、submodule 或 platform readiness 无法安全证明。

## Escalation Rules

- 把 Registry transition、Stacked dependency 与 Final Gate 转交 `workflow-orchestration`。
- 把平台 environment/cache/verification 转交已选择且 implemented 的平台 Provider；Provider 缺失时保持 `bootstrap_required`。
- 不为绕过 dirty、未知依赖、symlink 或身份漂移申请扩大权限。

## Token Budget

- 优先输出 canonical Session Context 与首个阻塞原因，不粘贴完整 binary diff、artifact 或 Git 历史。

## Relationship to Other Skills

- `workflow-orchestration` owns the Session Registry lifecycle and Final Gate.
- Pure Git Final Gate uses `verification.git.repository`; selected-platform Sessions require each selected platform's verification capability instead.
- `code-review` owns Session-scoped findings and reviewer independence.
- Platform verification skills consume the opaque committed `source_identity`; they do not reimplement Git hashing.
