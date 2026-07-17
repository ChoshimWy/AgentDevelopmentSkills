# Rust Migration Plan

AgentDevelopmentSkills is migrating from Python to Rust through a strangler
architecture. The migration preserves the current contracts, security
boundaries, deterministic artifacts, exit codes, and release governance. It is
not a big-bang rewrite.

## Non-negotiable compatibility

Every migrated component must preserve:

- UTF-8 canonical JSON, sorted keys, compact separators, no `NaN`, and one
  trailing newline;
- a shared fail-closed limit of 4,300 decimal integer digits and 512 nested
  arrays/objects;
- schema-version rejection and all other fail-closed behavior;
- CLI stdout, stderr, exit-code, and filesystem side-effect contracts;
- manifest capability, permission, dependency, and provenance checks;
- transactional lifecycle safety, including symlink, inode, mode, rollback,
  and concurrent-update behavior;
- deterministic release artifacts, SBOM, provenance, and qualification gates.

Python remains the production implementation until the corresponding Rust path
passes differential tests against it.

## Workspace boundaries

The target workspace is split by contract rather than by the historical Python
file layout:

1. `agent-contracts` — canonical JSON, hashes, schema versions, shared model
   validation;
2. `agent-registry` — manifests, capabilities, providers, dependencies, and
   permissions;
3. `agent-engine` — discovery, routing, policy, planning, and lock resolution;
4. `agent-runtime` — workflow execution and evidence recording;
5. `agent-lifecycle` — install, upgrade, rollback, doctor, and uninstall;
6. `agent-session` — session and worktree orchestration;
7. `agent-release` — deterministic packaging, SBOM, provenance, and release
   qualification;
8. `agent-platforms` — isolated platform bindings and platform-specific tools;
9. `agent-skills` — the native CLI assembled from the crates above.

## Migration phases

| Phase | Scope | Cutover gate |
| --- | --- | --- |
| 0 | Inventory and compatibility matrix | Python behavior and risk boundaries frozen |
| 1 | Rust workspace, contracts, hidden parallel CLI | Byte-for-byte canonical JSON and hash parity |
| 2 | Registry, manifests, permissions, providers | Differential validation across all package manifests |
| 3 | Discovery, policy, planning, and lock files | Plan and lock artifacts match for the fixture corpus |
| 4 | Runtime, sessions, lifecycle operations | Transaction, recovery, concurrency, and tamper tests pass |
| 5 | Release, packaging, provenance, platform tools | Reproducibility and release qualification remain fail-closed |
| 6 | Bootstrap and default CLI cutover | Cross-platform CI, compatibility window, and rollback plan pass |

## Current state

Phase 1 is active. The repository contains:

- a Rust workspace pinned to Rust 1.97.1;
- `agent-contracts` canonical JSON, SHA-256, and schema-version primitives;
- a parallel `agent-skills-rs` diagnostic CLI;
- Python-to-Rust differential tests;
- formatting, unit-test, Clippy, and Python 3.11–3.14 compatibility gates in
  CI;
- Rust workspace sources in source releases, Python sdists/wheels, SBOM, and
  provenance inputs, without shipping or activating a Rust executable.

The Rust binary is not yet installed by the production bootstrap and is not a
binary release artifact. The parallel CLI currently covers only canonical JSON,
hashing, and the shared schema-version boundary; production CLI parity is a
later phase gate.

## Cutover policy

A component may become default only after:

1. its contract corpus passes against both implementations;
2. negative, malformed, tamper, and concurrency cases are covered;
3. release and rollback paths are verified;
4. an independent review finds no unresolved correctness or security issue;
5. the previous implementation remains available for a documented rollback
   window.

Removing Python is the last phase, not the first milestone.
