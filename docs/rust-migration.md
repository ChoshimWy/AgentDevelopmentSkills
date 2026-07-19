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
hosted fresh installs and dry-runs now select Rust, explicit-source native
upgrade is public, and release qualification emits a Manifest-v3-bound
immutable Upgrade Source Qualification. An operator-invoked hosted upgrade
route now performs authenticated acquisition, approval-envelope generation,
and guarded native apply. The signed POSIX release bootstrap now routes an
explicit `--upgrade` request through the release-matched Rust executable with
no Python fallback. An explicit fresh Apple/Desktop source-checkout request now
builds the pinned Rust CLI offline in a private target directory and executes
that exact binary. PowerShell and other compatibility bootstrap surfaces,
interactive selection, legacy adoption, and other routes remain pending.
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
  agree. The compatibility-only `doctor-report` command still emits validated
  Doctor Report v1 with an explicit `--python-version` host attestation. The
  public `doctor` command emits runtime-neutral Doctor Report v2 from the same
  native checks and an exact build-time embedded Schema inventory, so it does
  not require Python, a source checkout, network access, or an external Schema
  directory. The crate also exposes the first mutating-lifecycle prerequisite:
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
  The gated hosted `uninstall.sh` verifies the installed executable against its
  embedded host artifact size and SHA-256 before defaulting to the native
  guard. Source-checkout uninstall now defaults compatible target/platform
  requests to a locked, offline Cargo build in a private temporary target.
  Release mismatch, unsupported hosts, compatibility-only requests, and an
  explicitly selected Python engine retain the verified Python path; a
  selected native build or execution never silently falls back.
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

Release Manifest v3 freezes the complete native index and the qualified
immutable upgrade source, and defaults eligible hosted fresh Apple/Desktop
install and dry-run requests to the matching verified Rust executable. The
release builder still emits v2 before Conformance; qualification
deterministically adds the reviewed source qualification and promotes that
frozen candidate to v3. The same transition promotes release provenance to v2
and emits a canonical Release Qualification Handoff v2, while the validator
keeps archived v1 handoffs readable.
The native release crate now provides the bounded HTTPS acquisition and
fail-closed ZIP extraction substrate for that v3 source. It infers the exact
installed selection from the validated Lock pair, compiles the candidate twice
from independent extractions, and privately binds the qualified Conformance
result to the exact Package Lock before issuing an Upgrade Plan. This is not
an unattended updater: the public `hosted-upgrade` command now additionally
downloads and authenticates the exact current-host executable, binds Manifest,
source revision, Source Qualification, candidate Lock, and lifecycle Plan into
one canonical approval envelope, and applies only that exact envelope through
the guarded executor. Source Activation treats a changed native launcher as a
planned state migration even when package semantics are unchanged, so both
installed command names are replaced transactionally.
For macOS and supported glibc 2.39+ Linux hosts, the gated release now renders
the exact immutable asset base, source archive identity, and six-target native
matrix into the POSIX bootstrap. Musl and older glibc hosts are deliberately
ineligible and remain on the Python compatibility route. An eligible explicit
fresh Apple/Desktop request downloads both bounded assets with HTTPS-only
redirects, verifies their exact size and SHA-256, extracts the frozen source
archive, and invokes the verified native installer without requiring Python.
The Final Gate independently recomputes the rendered bootstrap from source
SBOM materials and the verified native index. The same signed POSIX bootstrap
routes an explicit existing-install `--upgrade` request to the exact
release-matched host executable after checking its embedded size and SHA-256;
it does not download the source archive, trust the installed launcher, or
permit Python fallback. An explicit fresh Apple/Desktop source-checkout request
now selects Rust when `cargo` is available, performs a locked offline build
from the pinned checkout into a private temporary target, and runs that exact
binary. It accepts explicit discipline and runtime-config selection; the same
selection is preserved by the shared verified bootstrap compatibility core.
Interactive, existing-install, legacy-adoption, and other
compatibility-only source requests remain on Python. Operators may explicitly
select that path with `AGENT_SKILLS_INSTALL_ENGINE=python`; forced Rust fails
closed when a fresh request is ineligible or Cargo is unavailable, while an
explicit hosted upgrade rejects the Python engine. A selected native build,
acquisition, or execution failure never silently downgrades. PowerShell is not
promoted solely because the native matrix contains Windows executables: the
release source artifact still excludes Windows as a production install host,
so its bootstrap remains on the compatibility path until that complete
filesystem contract and its Conformance gate are enabled. The parallel CLI
currently covers canonical JSON,
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
gates. The POSIX source-checkout uninstaller now builds that transaction with
locked offline Cargo in an isolated target by default, while preserving an
explicit Python compatibility engine. Although the release matrix includes
Windows binaries, Windows production source install remains blocked until its
full filesystem contract is enabled.

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
snapshot-digest tamper cases. The companion compatibility command
`doctor-report` assembles the complete v1 artifact and retains exact Python
differential coverage. The public `doctor` route now assembles and validates
Doctor Report v2, removes the Python-host field, identifies its generic
implementation, and compares the installed Lock against the Schema inventory
embedded in the release binary. The next upgrade slice now validates Upgrade Conformance Evidence
v1 and Upgrade Plan v1 natively. It rejects unknown fields, unstable command,
selection, migration, or step ordering, stale attestations, malformed
permission approvals, invalid external-handler/rollback identities, and
self-consistent semantic tampering. The non-default `upgrade-plan-build`
command now takes only candidate artifacts, evidence, target and removal
intent, plus the frozen launcher when retained source Activation requires it.
`agent-lifecycle` reads and rechecks current state through held directory
capabilities while holding the target transaction lock, derives Activation
ownership, selects the trusted activation/deactivation/preserve handler,
previews and rechecks the exact rollback scope, and emits an opaque in-process
receipt. That receipt binds target, action, current/candidate identities,
removals, exact paths, external-state hash, rollback point and planned
migrations. Its handler identity hashes the locked local Rust source closure,
workspace Cargo lock, target build context and pinned toolchain declaration.
The lifecycle compiler rejects unknown fields, altered receipts,
ownership-policy drift, path/hash
drift and unbound migrations. CLI callers cannot supply raw current locks,
rollback points, migrations, handlers or external paths. Legacy Activation
Lock v1 planning and changed Apple activation/deactivation are now covered;
external-free results remain byte-differential-tested against Python, while
native Activation uses semantic differential checks because Rust additionally
owns `bin/agent-skills` and uses a native handler implementation hash.
`upgrade-evidence-validate` and `upgrade-plan-validate` retain negative
contract coverage. The first mutating Rust executor now binds those validated
artifacts to the lifecycle transaction: it holds the planning lock through
staging and publication, requires the complete approved Plan and exact
permission approvals, persists the verified rollback point, and dispatches
only the receipt-bound source handler. Apple activation additionally runs a
native installed-registry smoke before any external write. That smoke covers
discovery, policy, package-Lock-bound planning, Skill resolution, Recorded
Adapter execution, independent review, and delivery reporting while
`PublishedInstall` can still restore both managed and external preimages.
No-change, partial removal, legacy Activation migration, approval rejection,
activate/deactivate/preserve dispatch, smoke-failure compensation, and
post-write handler compensation have native transaction tests. The public
native `upgrade` CLI now connects explicit verified source selection and
Conformance evidence to that executor; `lifecycle-upgrade` remains a visible
compatibility alias. It
compiles a no-lineage candidate, loads and cross-validates the installed Lock
pair under an initial target lock, and recompiles changed candidates with the
exact current Lock as lineage. Dry-run emits or saves a canonical Plan; apply
requires the saved Plan, its exact explicit fingerprint, and the complete
permission approval set. The executor then reacquires the target lock,
regenerates the complete Plan, and rejects any candidate or target drift before
staging. The public native `rollback` command now consumes the
persistent point without accepting caller-supplied paths or content: exact
current-Lock and rollback-point fingerprints are checked before workspace
creation, the complete prior managed projection is rebuilt from the validated
snapshot, the current `.system` tree is preserved, and frozen external
preimages are restored while `PublishedInstall` can still recover both sides.
The displaced current state becomes the next validated rollback point, making
the transaction reversible again. `lifecycle-rollback` remains a visible
compatibility alias. The operator-invoked `hosted-upgrade` command now fixes
the repository Pages URL in the binary, bounds HTTPS redirects and payloads,
authenticates the canonical Manifest v3, Source Qualification, source archive,
and matching host executable, compiles the candidate twice, and issues a
release-provenance-bound approval envelope. Apply reacquires the same materials
and rejects any source, candidate, target, Plan, or approval drift.
Doctor, uninstall, rollback, and source activation now have native public or
transaction routes with differential, tamper, concurrency, recovery, and
independent-review evidence. The signed POSIX release bootstrap now exposes
the explicit guarded `--upgrade` route without Python, and POSIX source
checkout install/uninstall default eligible non-interactive requests to
isolated locked offline Rust builds. The remaining hosted lifecycle cutover is
PowerShell and the other compatibility bootstrap surfaces, separate from both
the operator-invoked native routes and the Python-free eligible source routes.
Upgrade Source Qualification v1 establishes the release-side input for that
cutover. It binds the completed repository Conformance suite to the immutable
source archive hash and size, source revision, complete SBOM material identity,
Schema inventory, runner, and stable command set. It deliberately omits a
candidate Package Lock hash because upgrade lineage is installation-specific
and cannot be truthfully precomputed at release time. Both Python and Rust
validate the new artifact; acquisition must later authenticate the exact
archive and rebind the resulting candidate to the current installed Lock
before Plan approval.

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
