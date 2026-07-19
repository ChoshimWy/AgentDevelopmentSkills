# AgentDevelopmentSkills

AgentDevelopmentSkills is an offline-first, fail-closed workflow core for coding agents. It discovers repository capabilities, resolves platform and discipline contracts, builds deterministic execution plans, and records auditable evidence.

## Highlights

- Conservative repository discovery and explicit capability routing
- Deterministic plans, locks, manifests, migrations, and release artifacts
- Transactional install, upgrade, rollback, doctor, and uninstall workflows
- Cross-platform packages for Apple and Desktop; Android, Web, and Backend remain explicit bootstrap-only targets
- A qualified six-target Rust binary matrix plus reproducible Python compatibility artifacts
- A compatibility-gated, incremental migration whose first fresh-install route now defaults to Rust
- Signed release review, provenance, SBOM, and fail-closed release gates
- GitHub Pages control plane with immutable GitHub Release assets
- No telemetry, credential collection, or implicit remote execution

## Status

The incremental Rust migration has reached the controlled bootstrap phase.
Release-manifest v2 binds a complete macOS, Linux, and Windows native binary
matrix. A hosted, explicit, fresh Apple or Desktop install or dry-run selects
the verified Rust lifecycle transaction by default. Source-checkout installs,
interactive or compatibility-only requests, existing installations, upgrades,
and legacy adoption still use the documented Python compatibility path. The
repository carries an MIT `LICENSE`, a `NOTICE`, and verified
migration-audit hashes. The GitHub Pages control plane is deployed; public
release assets and remote installation remain gated on an external release
signature and GitHub environment approval.

## Requirements

- Python 3.11 or newer for the current thin bootstrap, source-checkout install,
  and compatibility fallback
- Rust 1.97.1 for native development; hosted v2 releases download a qualified
  target binary
- macOS, Linux, or WSL2 for the production bootstrap path
- Windows bootstrap is validated in CI but is not yet a production install target

## Install from a checkout

```bash
./install.sh
```

For a dry run or an explicit platform selection:

```bash
./install.sh --dry-run
./install.sh --platform apple
./install.sh --platform desktop
```

## Remote release installation

The public bootstrap entry point is intentionally kept separate from immutable versioned assets:

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh | bash
```

Windows PowerShell:

```powershell
iwr -useb https://choshimwy.github.io/AgentDevelopmentSkills/install.ps1 | iex
```

The Pages control plane is online, but the remote installer remains unavailable until a signed release has been published. Use a source checkout before that release gate is satisfied.

After a v2 release is published, an explicit fresh `--platform apple` or
`--platform desktop` install or dry-run on macOS or a supported glibc 2.39+
Linux host defaults to the verified Rust binary. Musl and older glibc hosts
remain on the Python compatibility route. The gated release renders exact
source and host-binary sizes and SHA-256 identities into the POSIX bootstrap,
so this route needs `curl`, `unzip`, and a system SHA-256 command but does not
require Python. Set
`AGENT_SKILLS_INSTALL_ENGINE=python` to request the transitional compatibility
path. `AGENT_SKILLS_INSTALL_ENGINE=rust` fails closed if the request is not
eligible; once Rust has been selected, a native failure never silently
downgrades to Python. Source-checkout, existing-install, upgrade, and other
compatibility-only requests still require Python 3.11+. The PowerShell
bootstrap also remains on that compatibility path because Windows is blocked
as a production source-install target until its complete install contract is
enabled.

An Apple native install publishes the verified executable as both
`~/.codex/bin/agent-session` and `~/.codex/bin/agent-skills`. The latter exposes
the guarded Rust lifecycle CLI. Preview before removing the exact managed
installation:

```bash
~/.codex/bin/agent-skills uninstall ~/.codex --platform all --dry-run
~/.codex/bin/agent-skills uninstall ~/.codex --platform all
```

The gated hosted uninstaller authenticates that installed executable against
its embedded release matrix before selecting Rust:

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/uninstall.sh \
  | bash -s -- --dry-run
```

Set `AGENT_SKILLS_UNINSTALL_ENGINE=python` for the compatibility route or
`AGENT_SKILLS_UNINSTALL_ENGINE=rust` to fail closed unless the installed binary
matches the hosted release. A source-checkout `uninstall.sh` intentionally
remains on Python 3.11+; a selected native uninstall never falls back after
execution begins.

## Development

- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

Run the complete conformance suite:

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
```

Run focused tests:

```bash
PYTHONPATH=src python3 -m unittest tests.test_pages_distribution tests.test_github_publication
```

Validate the current Rust compatibility layer:

```bash
cargo fmt --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
AGENT_SKILLS_RUST_COMPATIBILITY=1 \
  PYTHONPATH=src python3 -m unittest tests.test_rust_compatibility -v
```

Inspect the current registry through the non-default native CLI:

```bash
cargo run --locked -p agent-skills-rs -- registry-snapshot platforms
```

The same compatibility lane can resolve policies, discover repository evidence,
compile deterministic plans, resolve or inspect persistent package Lockfiles,
and simulate workflow runtime contracts without invoking external providers:

```bash
cargo run --locked -p agent-skills-rs -- \
  repository-discover tests/fixtures/apple-app
cargo run --locked -p agent-skills-rs -- \
  policy-resolve /path/to/profile.json "implement the requested feature"
cargo run --locked -p agent-skills-rs -- \
  plan-compile /path/to/profile.json /path/to/policy.json \
  --manifests platforms
cargo run --locked -p agent-skills-rs -- \
  lock-resolve /path/to/install-plan.json --schemas schemas \
  --output /path/to/agent-skills.lock
cargo run --locked -p agent-skills-rs -- \
  lock-validate /path/to/agent-skills.lock
cargo run --locked -p agent-skills-rs -- \
  lifecycle-install platforms /path/to/fresh-target \
  --platform apple --schemas schemas --dry-run
cargo run --locked -p agent-skills-rs -- \
  doctor-baseline /path/to/installed-root --schemas schemas
cargo run --locked -p agent-skills-rs -- \
  doctor-report /path/to/installed-root --schemas schemas \
  --python-version 3.11.0
cargo run --locked -p agent-skills-rs -- \
  doctor --target-root /path/to/installed-root
cargo run --locked -p agent-skills-rs -- \
  rollback /path/to/installed-root \
  --approve-current-lock <sha256> --approve-rollback-point <sha256>
cargo run --locked -p agent-skills-rs -- \
  upgrade /path/to/source/platforms /path/to/installed-root \
  /path/to/upgrade-conformance-evidence.json --dry-run \
  --output /path/to/upgrade-plan.json
cargo run --locked -p agent-skills-rs -- \
  upgrade-source-qualification-validate \
  /path/to/upgrade-source-qualification.json
cargo run --locked -p agent-skills-rs -- \
  lifecycle-uninstall /path/to/installed-root --platform all
cargo run --locked -p agent-skills-rs -- \
  runtime-execute /path/to/workflow-plan.json \
  --behaviors /path/to/fake-behaviors.json
cargo run --locked -p agent-skills-rs -- \
  adapter-request-build /path/to/workflow-plan.json node-id \
  /path/to/task-context.json invocation-id
cargo run --locked -p agent-skills-rs -- \
  adapter-result-validate /path/to/adapter-request.json \
  /path/to/adapter-result.json
cargo run --locked -p agent-skills-rs -- \
  runtime-execute-recorded /path/to/workflow-plan.json \
  /path/to/adapter-results.json /path/to/task-context.json
cargo run --locked -p agent-skills-rs -- \
  invocation-prepare /path/to/handoff /path/to/workflow-plan.json node-id \
  /path/to/task-context.json invocation-id
cargo run --locked -p agent-skills-rs -- \
  invocation-claim /path/to/handoff adapter-request-id host-actor \
  /path/to/private-claim-token
cargo run --locked -p agent-skills-rs -- \
  invocation-submit /path/to/handoff adapter-request-id \
  /path/to/adapter-result.json /path/to/private-claim-token
cargo run --locked -p agent-skills-rs -- \
  runtime-execute-invocations /path/to/workflow-plan.json \
  /path/to/handoff /path/to/task-context.json \
  --selection /path/to/provider-invocation-selection.json
cargo run --locked -p agent-skills-rs -- \
  repository-inspect /path/to/repository app --base-ref HEAD
cargo run --locked -p agent-skills-rs -- \
  session-context-create /path/to/session-context-input.json
cargo run --locked -p agent-skills-rs -- \
  session-registry-list /path/to/repository
cargo run --locked -p agent-skills-rs -- \
  session-create /path/to/repository feature \
  /path/to/session-context-input.json --base-ref HEAD
cargo run --locked -p agent-skills-rs -- \
  session-create-manifest /path/to/repository feature \
  --project-id project --created-at 2026-07-18T00:00:00+00:00 \
  --platform apple --manifest-root /path/to/platforms --base-ref HEAD
cargo run --locked -p agent-skills-rs -- \
  session-registry-checkpoint /path/to/repository session-id
cargo run --locked -p agent-skills-rs -- \
  session-registry-gate /path/to/repository session-id \
  /path/to/adapter-pairs.json /path/to/run-ledger.json /path/to/artifacts
```

For a plan containing `package_lock_hash`, append
`--lock /path/to/agent-skills.lock` to `invocation-prepare` and supply the same
validated Lockfile when consuming it.

The migration sequence and cutover gates are documented in
[`docs/rust-migration.md`](docs/rust-migration.md). The Python CLI remains the
production entry point until every relevant differential test and release gate
passes. The current native lane includes canonical contracts and a read-only
manifest registry, repository discovery, policy resolution, and plan
compilation, plus package Lockfile resolution, validation, diff, explanation,
and locked-plan binding checks. Phase 4 has started with a native deterministic
fake-adapter runtime covering node state transitions, retries, approvals,
resource scheduling, append-only ledger replay, and locked-plan execution. It
also freezes and validates Adapter Request/Result v1 contracts and consumes
those recorded results through the same ledger, resource, resume, and
final-status contracts. The next native increment now covers bounded Git
Worktree inspection, `repository-patch-v1`, `session-source-v1`, Session
Context validation, exact Worktree creation/compensation, checkpoint
transitions, the locked persistent Session Registry, trusted Manifest-driven
platform/Provider closure compilation and Session creation, and Final Gate
evidence revalidation/persistence. A filesystem-backed Provider Invocation v1
handoff now freezes permissions, side effects, resources, provenance, and a
hard timeout; it supports one hashed-token claim and accepts only an
identity-matched Adapter Result. Runtime consumption requires an explicit
Provider Invocation Selection v1 mapping from each node to its submitted
request ID; retry results are never selected by timestamp. The external host
still owns actual Provider execution: Core does not discover or read Provider
credentials, execute a binding or package code, or make network calls. It
reads only a caller-supplied, owner-private, high-entropy transport claim
token. After a failure around result publication, inspect the request before
retrying claim or submit. The first native lifecycle slice now provides a
read-only Doctor compatibility projection for the safe target, recovery
residue, managed layout, Install/Persistent Lock anchors, Core runtime
identity, runtime Schema inventory, and managed Activation file integrity. It
also verifies installed package trees, package and Provider Manifests, the
ordered package closure, and installed package identity, dependency, and
side-effect semantics against both Lockfiles. The projection now also verifies
installed Skill identities and trees, the unique global `AGENTS.md` content,
fragment order and rule trace, frozen Capability Bindings and Provider
closure, and permission profiles and per-Capability grants against rebuilt
installed Manifest semantics. Persistent rollback points are now checked
read-only as complete snapshots, including their own Lock pair, package,
Skill, AGENTS, external-state, Activation, semantic, and snapshot-digest
anchors. The compatibility-only `doctor-report` command still emits Doctor
Report v1 with an explicit `--python-version` host attestation. The public
`doctor` command now emits runtime-neutral Doctor Report v2 instead: the Rust
binary embeds its build-time Schema inventory and requires neither Python, a
source checkout, a network connection, nor a caller-supplied Schema path.
Fresh install, uninstall, and rollback already use guarded native
transactions. The public native `upgrade` command requires explicit verified
source and Conformance evidence; hosted automatic acquisition remains behind
its separate release gate. `agent-lifecycle` uses an identity-bound RAII
directory lock with atomic exclusion, safe missing-target creation,
crash-residue visibility, and identity-checked cleanup.
The companion `LifecycleWorkspace` now creates a unique POSIX mode-`0700`
stage/backup pair under that lock, holds both directory capabilities, exposes
them for later native staging, removes symlink-safe temporary trees, and can
preserve an incomplete-recovery backup. It can also copy and revalidate
tree-local, Install-Plan-shaped package and Skill records with canonical POSIX
modes, bounded paths, atomic destinations, and no-follow source traversal.
`ValidatedInstallPlan` now binds a complete Install Plan to its persistent
package Lockfile in both directions. Plan-bound workspace methods assemble
canonical `AGENTS.md`, Install Lock, persistent Lockfile, package trees, and
Skill trees, then run a complete managed pre-swap gate over exact topology,
canonical bytes, Manifest-derived semantics, Bindings, permissions, and both
identity anchors. The workspace now also freezes and copies external
`skills/.system` trees without following symlinks, preserves the exact validated
Activation Lock, and revalidates both the target and staged external state
around the complete pre-swap gate. On Windows, `.system` symlink entries
currently fail closed because stable `cap-std` cannot recover the file-vs-dir
link kind without following the link. For an intact current installation, the
workspace can now assemble and validate a persistent rollback point inside the
new stage. It freezes the current Lock pair, packages, Skills, `AGENTS.md`,
optional Activation Lock, package-owned external files, absent-file records,
and parent-directory state, then revalidates both source and staged identities
around the complete gate. External paths must be sorted, unique, relative, and
disjoint from managed roots. `publish_staged_install` now moves the three
managed roots with atomic no-replace renames (`renameat2`/`renamex_np` on
supported Unix targets and `MoveFileExW` without replace flags on Windows),
verifies every source and destination object identity, and returns a
`PublishedInstall` guard that keeps the lifecycle lock and old roots until
explicit commit or rollback.
Existing installs require a verified staged rollback point. The recovery
backup is revalidated against the frozen source semantics before publication
and again before restoration. A restored installation is fully revalidated
before cleanup; if recovery-time content drift is detected after a complete
publication, the new roots are reinstated and the backup is preserved for
diagnosis. Partial failures reverse completed moves, while identity or content
drift never overwrites an unknown target. Dropping an uncommitted guard
attempts the same safe rollback.
The guard now also tracks the start of a transaction-bound external mutation.
It first validates the published rollback snapshot and recovery backup, then
restores or safely reinstates the managed roots. Only after the old roots are
complete does it revalidate the frozen rollback point from the private stage
and restore external file, absent-file, mode, and ancestor-directory
preimages. Existing external entries move through atomic no-replace renames
into a private quarantine before snapshot files are published with the same
no-replace rule. Aliased destinations, replaced parent symlinks, or drift
preserve both stage and backup. On Windows, rename paths are resolved from the
held directory handles, so replaced junction ancestors cannot redirect nested
external operations. The lifecycle lock coordinates lifecycle commands, not
arbitrary same-user file handles: callers must keep the approved external
scope quiescent and must not retain writable handles to those entries during a
transaction. The first trusted handler, source deactivation, now derives its
exact scope from the validated Activation Lock, requires an exact frozen
rollback scope, validates every owned preimage, removes only owned files and
the managed root-level `model_instructions_file`, and permits commit only after
the Activation Lock is absent and the remaining installation is revalidated.
Its config rewrite uses TOML 1.0 parsing while preserving every unrelated byte
and the original POSIX mode. The source-activation prerequisite that overlays
the Codex shared config is also available as a native, non-executing TOML
renderer with differential parity against the installed source script. The
same guard can now run source activation for replacement and fresh-install
transactions backed by an exact rollback point. Replacement activation freezes
assets from the newly published package snapshot. Fresh activation derives the
same scope before publication by reading package assets from the managed stage
and unmanaged config, profile, and destination preimages from the target. Both
paths refuse unmanaged conflicts, create only missing profiles, use private
no-replace publication, and write the Activation Lock last. A fresh failure
removes the new managed roots before restoring every frozen external preimage.
Compatibility commands still accept an explicit session launcher; the v2
bootstrap freezes the same verified native executable and activates it inside
the eligible fresh-install transaction. A separate
`PublishedUninstall` guard now freezes a complete
managed and external rollback point, moves all managed roots into a private
backup, removes only Activation-owned files, preserves local profiles,
`config.toml` semantics, and `skills/.system`, and supports explicit commit,
rollback, and drop-time recovery. Remaining production command routing is a
later lifecycle slice. The non-default
`lifecycle-uninstall` compatibility command now drives this guard, rejects a
missing target without creating it, and matches the successful Python JSON
report and resulting filesystem state on the supported POSIX source-installer
path. Windows keeps native unit coverage for target spelling, ownership, and
transaction recovery while the Python source installer remains POSIX-mode
only. The native command now also matches the source CLI's read-only dry-run,
human-readable success output, and canonical blocked JSON surface. The gated
hosted `uninstall.sh` authenticates the installed executable against its
embedded host record and defaults eligible requests to this Rust guard without
Python. Source-checkout, release-mismatch, unsupported-host, and
compatibility-only requests retain the verified Python route.

The parallel `install-selection` compatibility command now resolves the
installable source package catalog, explicit platform/discipline/runtime
selection, required and optional dependency closure, version constraints,
deterministic topological order, and selection reasons with Python
differential parity. The follow-on `install-source-snapshot` command freezes
declared package assets, Package/Provider Manifests, instruction Fragments,
and installable Skill trees through bounded no-follow traversal and source
mutation revalidation. Both are differential-tested against Python. This lane
now feeds the non-default `install-bundle` command, which independently
rebuilds Manifest Registry, dependency capability, instruction/rule, Skill,
asset, binding, permission, side-effect, Install Plan v2, and persistent
package Lockfile identities. Core-only, Apple, QA, Codex runtime-config, and
previous-Lock lineage outputs are byte-for-byte differential-tested against
Python. The native lifecycle lane now provides a read-only
`lifecycle-install` compatibility command plus a production `install` command
for eligible fresh installs. It performs staging, semantic verification,
atomic publication, post-publication verification, rollback-on-failure, and
cleanup. Apple installation also freezes the exact launcher bytes and
completes source activation in the same guarded transaction. Core-only and
Apple projections remain differential-tested against Python. Replacement
installs, upgrades, legacy adoption, and compatibility-only requests remain on
separately gated paths. As the first upgrade cutover gate, Rust now strictly
validates Upgrade Conformance Evidence v1 and Upgrade Plan v1, including
stable attestation semantics, exact selections/removals, permission approvals,
external-handler identity, migration ordering, rollback identity, and
self-consistent tampering. The non-default `upgrade-plan-build` command now
accepts only the candidate Plan/Lock, Conformance evidence, target, removal
request, and—when source Activation is retained—the frozen native launcher.
The lifecycle crate opens the installed target through directory capabilities,
loads and rechecks the current locks itself, derives Activation ownership,
selects the only permitted activation/deactivation/preserve handler, freezes
the exact external paths and rollback state, and issues a receipt bound to the
locked local Rust source/dependency, target, and toolchain build identity for
in-process compiler consumption. Raw current locks, rollback
points, migrations, handlers, and external paths are no longer CLI inputs.
Legacy Activation Lock v1 migration is planned from that receipt; changed
Apple, preserve, and deactivation planning now fail closed on ownership or
scope drift instead of being categorically rejected. External-free results
remain byte-for-byte differential-tested against Python, while native
Activation results are semantic-differential-tested because the Rust route
also owns `bin/agent-skills` and has its own handler implementation hash.
`upgrade-evidence-validate` and `upgrade-plan-validate` retain negative
contract coverage. `agent-lifecycle` now also contains the first native
mutating executor: it rebuilds the exact approved Plan under the same held
target lock, compares the complete Plan and permission approvals, stages the
candidate and persistent rollback point, publishes through `PublishedInstall`,
and dispatches only the receipt-bound activate/deactivate/preserve handler.
Before an Apple activation can write external state, the newly published
installation must pass a native installed-registry smoke covering discovery,
policy, a package-Lock-bound ready Plan, Skill bindings, Recorded Adapter
execution, independent review, and delivery reporting. Any smoke or handler
failure remains inside the managed/external rollback window. The public
`upgrade` command now compiles a source candidate against an
initially locked snapshot of the installed lineage, emits or saves the exact
Plan in `--dry-run` mode, and requires both that saved Plan and its explicit
`--approve-plan` fingerprint before execution. Apply reacquires the target
lock, regenerates and compares the complete Plan, then delegates to the guarded
executor; a concurrent target change therefore fails closed instead of
silently rebasing. The public `rollback` command now requires
the exact current Lock and persistent rollback-point fingerprints before it
creates a workspace. It stages the validated prior projection, preserves the
current `.system` tree, restores the frozen external preimages inside the same
`PublishedInstall` recovery window, and persists the displaced current state
as the next rollback point. `lifecycle-rollback` remains a visible
compatibility alias. `lifecycle-upgrade` likewise remains a visible alias;
hosted automatic upgrade acquisition remains behind its separate release gate.
Upgrade Source Qualification v1 now provides the next release boundary: it
binds the completed repository Conformance suite to one immutable source
archive, source revision, complete SBOM material identity, Schema inventory,
and stable command set without falsely binding that release-time evidence to a
future installation-specific Lock lineage. The contract is validated by both
Python and Rust, but it does not authorize acquisition or apply by itself.
The installed native
`agent-session` dispatch preserves
the public `create`, `list`, `inspect`, `fingerprint`, `checkpoint`, and `gate`
surface.

The `agent-release` crate now freezes a six-target native matrix for macOS,
Linux, and Windows on `aarch64` and `x86_64`. Every record binds the exact
source revision, Cargo lock hash, Rust 1.97.1 toolchain, target-specific
executable header, smoke result, size, and SHA-256. CI builds and executes each
binary on a matching GitHub-hosted architecture, then merges only the complete
sorted matrix into `native-artifacts.json`. Qualification copies those exact
binaries into the release candidate; provenance, the exact release allowlist,
the external review signature, and the final Release Gate all cover them.
Release Manifest v2 now binds that exact matrix and makes the Rust executable
the default engine for eligible hosted fresh installs. The thin `install.sh`
and `install.ps1` acquisition layer and all ineligible requests still use the
explicit Python compatibility path during this controlled phase.

The target parent namespace must remain trusted while portable name-based
release runs. Callers must expand `~` before using these APIs. The Doctor path
holds directory capabilities and opens contract files without following
symlinks; unlike the explicit lock API, it does not repair, install, upgrade,
roll back, uninstall, or otherwise write the target. Failed projected checks
keep canonical JSON on
stdout and return exit status 2. Doctor does not create commits, change
staging, or make installation changes.
Activation and installed-package tree mode parity are currently POSIX
contracts; Windows-native Doctor still verifies the Lock shape, paths,
no-follow traversal, and content hashes but does not interpret POSIX mode
bits as a Windows ACL guarantee.

## Release governance

The `Publish verified release` workflow only accepts a successful qualification run from the protected `main` branch at the current workflow revision. It re-runs the final gate, rejects existing tags and releases, creates tags atomically, verifies Pages and Release assets by hash, and uses pinned GitHub Actions with job-level least privilege.

Before the first public release, repository administrators must configure branch protection, the `release` and `github-pages` environments, required reviewers, and the external review trust store. License/NOTICE evidence is now present and verified. See [`README.zh-CN.md`](README.zh-CN.md) for the Chinese guide, [`docs/architecture.md`](docs/architecture.md) for the public architecture overview, and [`docs/rust-migration.md`](docs/rust-migration.md) for the Rust migration plan.

## License

The repository-level License/NOTICE decision is recorded as MIT in the migration audit and the exact `NOTICE` hash is verified during the release gate. Any future change to licensing or attribution must update both files and regenerate the audit before release.
