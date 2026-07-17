# Multi-session Worktree Architecture

The worktree session layer provides deterministic, auditable coordination for concurrent coding-agent sessions.

## Invariants

- Every session has an explicit repository identity, source revision, scope, and lifecycle state.
- Immutable build/test identities bind evidence to the exact source and request that produced it.
- Queue publication is atomic; stale, forged, or cross-session records fail closed.
- Verification evidence is reusable only when its frozen identity is equal or stronger and all referenced artifacts remain unchanged.

## Operational boundary

The session layer coordinates requests and evidence; it does not grant broad repository or device permissions. Platform-specific execution remains behind the corresponding provider and approval contracts.

See the schemas and tests for the normative machine contracts.
