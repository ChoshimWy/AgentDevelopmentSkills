# AgentDevelopmentSkills

AgentDevelopmentSkills is an offline-first, fail-closed workflow core for coding agents. It discovers repository capabilities, resolves platform and discipline contracts, builds deterministic execution plans, and records auditable evidence.

## Highlights

- Conservative repository discovery and explicit capability routing
- Deterministic plans, locks, manifests, migrations, and release artifacts
- Transactional install, upgrade, rollback, doctor, and uninstall workflows
- Cross-platform packages for Apple and Desktop; Android, Web, and Backend remain explicit bootstrap-only targets
- Reproducible Python wheels and sdists for Python 3.11–3.14
- A compatibility-gated, incremental migration from Python to Rust
- Signed release review, provenance, SBOM, and fail-closed release gates
- GitHub Pages control plane with immutable GitHub Release assets
- No telemetry, credential collection, or implicit remote execution

## Status

The current Python implementation and validation suite are complete. An incremental Rust migration is now in progress: Rust components remain parallel and non-default until their behavior is proven byte-for-byte compatible with the existing contracts. The repository carries an MIT `LICENSE`, a `NOTICE`, and verified migration-audit hashes. The GitHub Pages control plane is deployed; public release assets and remote installation remain gated on an external release signature and GitHub environment approval.

## Requirements

- Python 3.11 or newer
- Rust 1.97.1 for native migration development only
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
anchors. The native lane can now assemble and validate a complete Doctor
Report v1 through `doctor-report`. Because v1 freezes the hosting Python
runtime, this compatibility command requires the host to attest
`--python-version`; it never discovers or executes an interpreter and does not
claim a no-Python production cutover. Mutating lifecycle transactions remain
on the Python path. As their first native prerequisite, `agent-lifecycle` now
exposes an identity-bound RAII directory lock with atomic exclusion, safe
missing-target creation, crash-residue visibility, and identity-checked
cleanup; it is not yet wired into production install or upgrade commands.
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
The session launcher remains an explicit caller-supplied payload until release
packaging binds a verified native executable. A separate `PublishedUninstall`
guard now freezes a complete
managed and external rollback point, moves all managed roots into a private
backup, removes only Activation-owned files, preserves local profiles,
`config.toml` semantics, and `skills/.system`, and supports explicit commit,
rollback, and drop-time recovery. Production command routing remains a later
lifecycle slice. The non-default
`lifecycle-uninstall` compatibility command now drives this guard, rejects a
missing target without creating it, and matches the successful Python JSON
report and resulting filesystem state on the supported POSIX source-installer
path. Windows keeps native unit coverage for target spelling, ownership, and
transaction recovery while the Python source installer remains POSIX-mode
only. The native command now also matches the source CLI's read-only dry-run,
human-readable success output, and canonical blocked JSON surface. The source
`uninstall.sh` has not switched to it yet.

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
Python. The non-default `lifecycle-install` command now adds a read-only
dry-run plus fresh-only staging, semantic verification, atomic publication,
post-publication verification, rollback-on-failure, and cleanup. Core-only and
Apple results and managed filesystem trees are differential-tested against
Python. It deliberately rejects replacement installs, does not run source
activation, and does not replace the production Python CLI; upgrades and
activation remain separate approval-bound phases.

The target parent namespace must remain trusted while portable name-based
release runs. Callers must expand `~` before using these APIs. The Doctor path
holds directory capabilities and opens contract files without following
symlinks; unlike the explicit lock API, it does not repair, install, upgrade,
roll back, uninstall, or otherwise write the target. Failed projected checks
keep canonical JSON on
stdout and return exit status 2. Core also does not create commits, change
staging, switch the production CLI, or make installation changes.
Activation and installed-package tree mode parity are currently POSIX
contracts; Windows-native Doctor still verifies the Lock shape, paths,
no-follow traversal, and content hashes but does not interpret POSIX mode
bits as a Windows ACL guarantee.

## Release governance

The `Publish verified release` workflow only accepts a successful qualification run from the protected `main` branch at the current workflow revision. It re-runs the final gate, rejects existing tags and releases, creates tags atomically, verifies Pages and Release assets by hash, and uses pinned GitHub Actions with job-level least privilege.

Before the first public release, repository administrators must configure branch protection, the `release` and `github-pages` environments, required reviewers, and the external review trust store. License/NOTICE evidence is now present and verified. See [`README.zh-CN.md`](README.zh-CN.md) for the Chinese guide, [`docs/architecture.md`](docs/architecture.md) for the public architecture overview, and [`docs/rust-migration.md`](docs/rust-migration.md) for the Rust migration plan.

## License

The repository-level License/NOTICE decision is recorded as MIT in the migration audit and the exact `NOTICE` hash is verified during the release gate. Any future change to licensing or attribution must update both files and regenerate the audit before release.
