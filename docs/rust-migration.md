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

Python remains the compatibility implementation for every route that has not
passed its Rust differential and release gates. Eligible hosted fresh installs
have now passed that boundary and default to Rust.

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

Phases 1 through 5 are complete. Phase 6 is in controlled rollout: eligible
hosted fresh installs and dry-runs now select Rust, while upgrade, legacy
adoption, the remaining bootstrap compatibility layer, and other routes remain
pending.
The repository contains:

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
  managed-root layout checks, Install/Persistent Lock anchoring, Core runtime
  identity comparison, runtime Schema inventory comparison, and managed
  Activation file verification. It also verifies installed package trees,
  package and Provider Manifest identities, ordered package closure, and
  installed package identity, dependency, and side-effect semantics against
  both Lockfiles. It now also verifies installed Skill identities and trees,
  global `AGENTS.md` content, canonical fragment order and rule trace,
  Capability Binding digests and Provider closure, and permission profiles and
  per-Capability grants against rebuilt installed Manifest semantics.
  Persistent rollback points are validated as complete read-only snapshots:
  their own Lock pair, packages, Skills, AGENTS composition, external state,
  optional Activation ownership, semantic closure, and full tree digest must
  agree. It can now wrap the projection in a validated Doctor Report v1. Since
  v1 records its Python host, `doctor-report` requires an explicit
  `--python-version` host attestation and never discovers or executes an
  interpreter. The crate also exposes the first mutating-lifecycle prerequisite:
  an identity-bound RAII directory lock with atomic exclusion, capability-safe
  missing-target creation, visible crash residue, and identity-checked cleanup.
  A `LifecycleWorkspace` now adds unique POSIX mode-`0700` stage/backup
  directories under the held lock, capability-bound access, recursive
  no-follow cleanup, crash visibility, and explicit incomplete-recovery backup
  preservation. The workspace retains its tree-local copy API and now also
  accepts a `ValidatedInstallPlan` that validates the complete Install Plan and
  persistent package Lockfile, normalizes the installed projection, and binds
  both identity anchors. Plan-bound staging assembles canonical `AGENTS.md`,
  Install Lock, persistent Lockfile, package trees, and Skill trees. Its
  managed pre-swap gate then rejects missing or extra roots, noncanonical
  Lockfile bytes, unselected records, tree or Manifest drift, rebuilt semantic
  drift, Binding/permission drift, and plan/Lock identity drift. External
  staging now freezes and copies `skills/.system` without following symlinks,
  preserves exact validated Activation Lock bytes, and revalidates target and
  staged snapshots around the complete gate. Windows `.system` symlink entries
  fail closed until stable capability APIs can distinguish file and directory
  links without following them. For an intact current installation, the
  workspace can now assemble a persistent rollback point inside the new stage.
  It freezes and validates the current Lock pair, packages, Skills, AGENTS,
  optional Activation ownership, package-owned external files, absent-file
  records, and parent-directory state. Source and staged identities are
  revalidated around the complete gate; external paths must be sorted, unique,
  relative, and disjoint from managed roots. The workspace now also publishes
  all three managed roots through atomic no-replace renames: supported Unix
  targets use `renameat2`/`renamex_np`, while Windows uses `MoveFileExW`
  without replace flags. Every source and destination object identity is
  rechecked, an existing install requires a verified rollback point, and a
  `PublishedInstall` RAII guard holds the lock and old roots until explicit
  commit or rollback. The complete backup is revalidated against the frozen
  source semantics before publication and before restoration. The fully
  restored old installation is then revalidated before cleanup; recovery-time
  drift after a complete publication reinstates the new roots and preserves the
  backup. Partial failures reverse completed moves without overwriting unknown
  targets, and dropping an uncommitted guard attempts the same recovery. The
  guard now records when transaction-bound external mutation starts. It
  preflights the published rollback snapshot and backup, finishes managed-root
  recovery first, then revalidates the frozen rollback point from the private
  stage before touching external paths. Existing external entries are moved
  into a private quarantine with atomic no-replace renames, and snapshot files
  are published with the same rule. Hard-link aliases, replaced parent
  symlinks, and drift preserve both stage and backup. Windows derives rename
  paths from held directory handles so junction-ancestor replacement cannot
  redirect nested operations. The lifecycle lock coordinates lifecycle
  commands only; the approved external scope must remain quiescent with no
  concurrently writable handles. The first trusted handler now implements
  source deactivation inside the same guard. It derives the exact owned-file
  plus `config.toml` scope from the validated Activation Lock, requires exact
  equality with the frozen rollback scope, validates all preimages, performs a
  byte-preserving TOML 1.0 removal of only the managed root assignment, removes
  the Activation Lock last, and supports both guarded commit and full external
  rollback. Replacement transactions with an exact rollback point can now run
  source activation through the same guard. Fresh-install activation freezes
  the same exact rollback scope before publication by reading verified assets
  from the stage and unmanaged destination, profile, and config preimages from
  the target. Both paths reject unmanaged conflicts, preserve existing
  profiles, publish through private no-replace quarantine, render Codex config
  natively, and write the Activation Lock last. Fresh rollback first removes
  the new managed roots and then restores the frozen external preimages. The
  session launcher is still explicit caller input until release packaging
  binds a verified native executable. Full managed uninstall now runs behind a
  `PublishedUninstall` RAII guard: it freezes a complete managed/external
  rollback point, moves all managed roots into a private backup, validates the
  supported Activation ownership set, preserves profiles, Codex config
  semantics, and `skills/.system`, and can commit or restore every preimage.
  The non-default `lifecycle-uninstall` compatibility command now drives this
  guard without creating a missing target; its success report and resulting
  filesystem state match the Python source uninstaller on the supported POSIX
  source-installer path. Windows keeps native unit coverage for target
  spelling, ownership, and transaction recovery while the Python source
  installer remains POSIX-mode only. The command now also provides a read-only
  dry-run, Python-compatible human output, and canonical blocked JSON.
  `uninstall.sh` still uses the Python compatibility path.
  The crate now also resolves the source package catalog used before native
  installation: explicit platform, discipline, and runtime-config selection;
  required and optional package dependencies; numeric version constraints;
  deterministic provider-before-consumer order; and selection reasons. The
  `install-selection` compatibility command is byte-level differential-tested
  against the Python planner. The follow-on `install-source-snapshot`
  compatibility command now freezes declared package assets, optional
  migration metadata, Package/Provider Manifests, instruction Fragments, and
  installable Skill trees through bounded no-follow traversal. It normalizes
  executable modes, excludes source caches, applies aggregate 100,000-entry and
  64 MiB retained-content limits before collection, re-reads each package to
  detect mutation, and is byte-level differential-tested against Python
  `_load_package`. The non-default `install-bundle` command now consumes this
  frozen boundary and independently rebuilds Manifest Registry validation,
  dependency capability checks, instruction/rule composition, Skill and asset
  identities, bindings, permissions, side effects, Install Plan v2, and the
  persistent package Lockfile. Core-only, Apple, QA, Codex runtime-config, and
  previous-Lock lineage projections are byte-level differential-tested against
  Python `build_install_bundle`. The `lifecycle-install` compatibility command
  now adds a read-only dry-run and a fresh-only transaction that reopens every
  source through directory capabilities, stages Package/Skill trees, preserves
  external state, verifies complete semantics, publishes all managed roots
  atomically, verifies again, and rolls back on failure. Core-only and Apple
  result/filesystem projections are differential-tested against Python
  `install_bundle`. The production `install` command reuses that transaction
  and, for Apple, freezes the verified native executable as the session
  launcher and completes source activation before commit. When invoked through
  the installed `agent-session` path, the same binary preserves the public
  create/list/inspect/fingerprint/checkpoint/gate interface. Replacement,
  upgrade, and legacy adoption remain separate approval-bound gates.
  Portable name-based release assumes a trusted target parent, and callers must
  expand `~` before acquisition. The Doctor path holds directory capabilities
  and opens contract files without following symlinks; unlike the explicit
  lock API, it never repairs or writes the inspected installation. Activation
  and installed-package tree mode parity are
  POSIX-only for now; Windows-native Doctor verifies the Lock contract,
  no-follow paths, and content hashes without treating POSIX mode bits as an
  ACL guarantee;
- schema-aligned capability-contract type validation shared by the Python
  baseline and native normalization path;
- Python-to-Rust byte-level differential tests covering malicious provider
  roles, optional Manifest fields, symlinks, normalization mutations,
  unbounded-size numeric SemVer components, recipe closures, discovery
  fixtures and edge cases, policy corpora, compiled workflow plans, package
  Lockfile sources/lineage/tamper cases, and failure limits;
- formatting, unit-test, Clippy, Python 3.11–3.14, Linux, macOS, and Windows
  compatibility gates in CI;
- Rust workspace sources in source releases and Python sdists/wheels, plus a
  six-target native release matrix for macOS, Linux, and Windows on `aarch64`
  and `x86_64`. The native index binds source revision, Cargo Lock, Rust 1.97.1,
  target binary headers, smoke output, sizes, and hashes. Qualification,
  provenance, exact release allowlisting, external review, and the final Gate
  cover the merged binaries.

Release Manifest v2 freezes the complete native index and defaults eligible
hosted fresh Apple/Desktop install and dry-run requests to the matching verified
Rust executable.
For macOS and supported glibc 2.39+ Linux hosts, the gated release now renders
the exact immutable asset base, source archive identity, and six-target native
matrix into the POSIX bootstrap. Musl and older glibc hosts are deliberately
ineligible and remain on the Python compatibility route. An eligible explicit
fresh Apple/Desktop request downloads both bounded assets with HTTPS-only
redirects, verifies their exact size and SHA-256, extracts the frozen source
archive, and invokes the verified native installer without requiring Python.
The Final Gate independently recomputes the rendered bootstrap from source
SBOM materials and the verified native index. Source-checkout, interactive,
compatibility-only, existing-install, upgrade, legacy-adoption, and PowerShell
requests still use the Python compatibility path. Operators may explicitly
select that path with `AGENT_SKILLS_INSTALL_ENGINE=python`; forced Rust fails
closed when the request is ineligible, and a selected native acquisition or
execution failure never silently downgrades. The parallel CLI currently covers canonical JSON,
hashing, the shared schema-version boundary, registry snapshots, targeted
binding resolution, source package-selection, package-snapshot, and complete
Install Bundle/Plan/Lock compatibility, fresh-only guarded source install, an
internal recipe-closure compatibility probe, repository discovery, policy
resolution, and plan compilation. Package-lock resolution is also available
through the parallel CLI, including local-registry,
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
persistence. The parallel CLI also exposes guarded full uninstall through
the public `uninstall` command (`lifecycle-uninstall` remains a compatibility
alias). Eligible Apple installs publish the frozen executable as both
`bin/agent-session` and `bin/agent-skills`, so installed users can preview and
execute the native full-uninstall transaction without Python. Host-specific
live Provider execution and complete production CLI parity remain later phase
gates. Although the release matrix includes Windows binaries, Windows
production source install remains blocked until its full filesystem contract
is enabled.

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
for the eligible fresh-install route is complete; full command-surface parity
and host-specific live Provider execution remain later phase gates.

The native lifecycle lane is also deliberately incomplete. Its current
`doctor-baseline` command emits a non-public compatibility projection of
existing Doctor checks so differential tests can freeze read-only semantics;
it writes canonical JSON to stdout and exits with status 2 whenever any
projected check fails, matching the production Doctor shell contract.
Skill/AGENTS/Binding/permission integrity is now included in that native
projection and covered by healthy-install, cross-Lock, semantic-forgery, and
content-tamper differential cases. Rollback-point validation is also projected
with healthy internal/external snapshots and contract, content, symlink, and
snapshot-digest tamper cases. The companion `doctor-report` command now
assembles the complete v1 artifact, recomputes summary/status/fingerprint, and
validates its cross-field invariants. Its required `--python-version` is
supplied by the compatibility host rather than inferred or executed by Rust,
so this closes report-emission parity without claiming the production CLI is
Python-free. Doctor, upgrade, rollback, uninstall, and source activation now
have native compatibility commands with differential, tamper, concurrency,
rollback, and independent-review evidence. Their hosted public CLI cutover
remains separate from the now Python-free eligible fresh-install route.

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
