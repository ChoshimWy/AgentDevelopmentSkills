# Worktree Session Registry and Gate

## Ownership

- Git Discipline: Worktree, Branch, Base Commit, repository inspection, Repository Patch.
- Workflow Discipline: Session Registry, lifecycle transitions, Stacked dependencies, Session/Integration/Delivery Gate.
- Review Discipline: review evidence, finding ownership, reviewer independence.
- Platform Provider: environment/cache identity and verification execution.

## Lifecycle

```text
created -> active -> checkpointed -> gated -> integrated -> closed
                \-> blocked -> active | closed
checkpointed -> active | blocked
```

`checkpointed` requires every repository Worktree to be clean and each HEAD/tree to be frozen. Final validation and review run after this point and bind the committed multi-repository `source_identity`. Gate success never creates or amends a Commit; integration accepts only the exact frozen Commit SHA set.

## Evidence

Worktree Session Context only indexes evidence. `adapter-request-v1`, `adapter-result-v1`, `node-attempt-v1`, and `run-ledger-v1` remain the evidence truth sources. Gate must verify:

- request/session/source identity equality;
- latest attempt and Adapter outcome linkage;
- Provider, Binding, plan, node, invocation and actor identity;
- artifact path, SHA-256 and Ledger linkage;
- exact normalized evidence semantics, including `summary` and structured `data` (not only kind/status/artifact ids);
- successful structured validation and independent review with no blocking issues;
- current clean Worktrees still equal the frozen checkpoint commit/tree set.

Pure Git Sessions use `verification.git.repository`. Once one or more platforms are selected, generic Git verification cannot replace them: every selected platform must contribute passed `verification.<platform>.*` evidence.

Any missing or stale identity is blocking.
