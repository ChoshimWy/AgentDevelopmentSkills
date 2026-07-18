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
- bounded native registry discovery (128 directory levels, 100,000 entries,
  4,096 manifests) and bounded capability graphs (16,384 nodes, 65,536 edges);
- bounded on-disk contract inputs (64 MiB), repository discovery (100,000
  entries/files, 10,000,000 match work units, 100,000 evidence entries), policy
  merges (1,024 layers and 16,384 fields/items), and workflow plans (16,384
  nodes and 65,536 edges);
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

Phases 1 through 3 are complete and Phase 4 is in progress. The repository
contains:

- a Rust workspace pinned to Rust 1.97.1;
- `agent-contracts` canonical JSON, SHA-256, and schema-version primitives;
- a parallel `agent-skills-rs` diagnostic CLI;
- an `agent-registry` crate for read-only manifest discovery, manifest shape
  validation, version ranges, graph conflicts and cycles, provider/bootstrap
  compatibility, permission and side-effect ceilings, binding normalization,
  external provider roots, disabled providers, and deterministic registry
  snapshots;
- an `agent-engine` crate for bounded, read-only repository discovery,
  deterministic task classification and policy resolution, workflow-plan
  compilation over the native registry, persistent package Lockfile
  resolution/validation/diff/explanation, and locked-plan binding checks;
- an `agent-runtime` crate for deterministic fake-adapter execution, node
  lifecycle transitions, idempotent retry limits, attempt-scoped approvals,
  resource scheduling, append-only JSONL ledger replay, and package-Lock-bound
  workflow execution, plus Adapter Request/Result v1 identity freezing and
  structured evidence validation and recorded-result consumption, bounded Git
  Worktree inspection, repository/session source identities, Session Context
  validation, exact Worktree creation/compensation, checkpoint transitions, a
  locked persistent Session Registry, Manifest-driven platform/Provider
  closure compilation and Session creation, and Final Gate evidence
  revalidation and passed-state persistence, plus a filesystem-backed Provider
  Invocation v1 handoff with frozen execution permissions, single
  hashed-token claims, hard deadlines, atomic result publication, and
  explicit request-ID selection for Recorded Runtime consumption;
- an `agent-lifecycle` crate with the first read-only Doctor compatibility
  slice: safe target acquisition, interrupted-transaction residue discovery,
  managed-root layout checks, Install/Persistent Lock anchoring, and runtime
  Schema inventory comparison. It holds directory capabilities and opens
  contract files without following symlinks, and it never repairs or writes
  the inspected installation;
- schema-aligned capability-contract type validation shared by the Python
  baseline and native normalization path;
- Python-to-Rust byte-level differential tests covering malicious provider
  roles, optional Manifest fields, symlinks, normalization mutations,
  unbounded-size numeric SemVer components, recipe closures, discovery
  fixtures and edge cases, policy corpora, compiled workflow plans, package
  Lockfile sources/lineage/tamper cases, and failure limits;
- formatting, unit-test, Clippy, Python 3.11–3.14, Linux, and macOS
  compatibility gates in CI;
- Rust workspace sources in source releases, Python sdists/wheels, SBOM, and
  provenance inputs, without shipping or activating a Rust executable.

The Rust binary is not yet installed by the production bootstrap and is not a
binary release artifact. The parallel CLI currently covers canonical JSON,
hashing, the shared schema-version boundary, registry snapshots, targeted
binding resolution, an internal recipe-closure compatibility probe, repository
discovery, policy resolution, and plan compilation. Package-lock resolution is
also available through the parallel CLI, including local-registry,
relative-path, and pinned HTTPS sources, deterministic lineage, validation,
diff, explanation, and plan freezing. Phase 4 now also exposes a deterministic
fake-adapter runtime for semantic differential testing; it never invokes an
external Provider or package code. Adapter Request/Result v1 contracts are now
available through the parallel CLI, and validated Recorded Results can be
consumed with resume, stale-context, no-retry, structured-evidence, and partial
status semantics matching the Python baseline. Native Worktree/Session support
now covers staged/unstaged/untracked patch identity, Gitlink rejection,
working/committed source identity, exact Worktree creation/compensation,
context refresh/checkpoint semantics, locked Registry lifecycle operations,
Manifest-driven native Session creation with bootstrap-only and trusted-root
gates, and Final Gate Adapter/Ledger/artifact revalidation with passed-state
persistence. Host-specific live Provider execution and production CLI parity
remain later phase gates.

The native and Python lanes now also expose the same Provider Invocation v1
transport. `prepare` freezes the Adapter Request together with the node's
permission profile, side effects, resource keys, approval, idempotency/retry
metadata, Provider Manifest digest, and timeout. `claim` is single-use and
stores only the SHA-256 identity of a bearer token loaded from a no-follow
private file; active claims with overlapping resource keys are mutually
exclusive within one handoff root. `submit` fails at the exact deadline and
validates the full Adapter Result identity and evidence contract before an
atomic terminal write. Approval-bound nodes remain fail-closed until a
runtime-granted attempt proof can be frozen, rather than letting the handoff
CLI bypass Approval Gate. The Recorded Runtime consumes only request IDs
explicitly authorized by a Provider Invocation Selection v1 artifact. This
keeps repeated submissions and concurrent retries deterministic without
silently selecting the latest result.

This transport is deliberately not a subprocess runner. A trusted external
orchestrator remains responsible for invoking a logical `skill`, `agent`,
`script`, or `tool` binding. Core neither treats Manifest names as commands nor
discovers or reads Provider credentials, accesses the network, or executes
package code. It reads only a caller-supplied, owner-private, high-entropy
transport claim token. After a crash around atomic publication, the host must
inspect the request before retrying claim or submit. Production CLI cutover
and host-specific live Provider execution remain later phase gates.

The native lifecycle lane is also deliberately incomplete. Its current
`doctor-baseline` command emits a non-public compatibility projection of
existing Doctor checks so differential tests can freeze read-only semantics;
it writes canonical JSON to stdout and exits with status 2 whenever any
projected check fails, matching the production Doctor shell contract.
Package/Skill/AGENTS/Binding/permission/activation integrity, rollback-point
validation, full Doctor Report v1 emission, and every mutating lifecycle
transaction remain on the Python production path until their own differential,
tamper, concurrency, rollback, and independent-review gates pass.

For the native compatibility command, a supplied `--ledger` parent directory
must already exist and contain only real directories. The runtime opens the
ledger relative to a held parent-directory capability and keeps one exclusively
locked file handle for replay and append operations.

## Cutover policy

A component may become default only after:

1. its contract corpus passes against both implementations;
2. negative, malformed, tamper, and concurrency cases are covered;
3. release and rollback paths are verified;
4. an independent review finds no unresolved correctness or security issue;
5. the previous implementation remains available for a documented rollback
   window.

Removing Python is the last phase, not the first milestone.
