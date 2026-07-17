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
and compile deterministic plans without executing them:

```bash
cargo run --locked -p agent-skills-rs -- \
  repository-discover tests/fixtures/apple-app
cargo run --locked -p agent-skills-rs -- \
  policy-resolve /path/to/profile.json "implement the requested feature"
cargo run --locked -p agent-skills-rs -- \
  plan-compile /path/to/profile.json /path/to/policy.json \
  --manifests platforms
```

The migration sequence and cutover gates are documented in
[`docs/rust-migration.md`](docs/rust-migration.md). The Python CLI remains the
production entry point until every relevant differential test and release gate
passes. The current native lane includes canonical contracts and a read-only
manifest registry, repository discovery, policy resolution, and plan
compilation. It does not execute plans, package code, or installation changes.

## Release governance

The `Publish verified release` workflow only accepts a successful qualification run from the protected `main` branch at the current workflow revision. It re-runs the final gate, rejects existing tags and releases, creates tags atomically, verifies Pages and Release assets by hash, and uses pinned GitHub Actions with job-level least privilege.

Before the first public release, repository administrators must configure branch protection, the `release` and `github-pages` environments, required reviewers, and the external review trust store. License/NOTICE evidence is now present and verified. See [`README.zh-CN.md`](README.zh-CN.md) for the Chinese guide, [`docs/architecture.md`](docs/architecture.md) for the public architecture overview, and [`docs/rust-migration.md`](docs/rust-migration.md) for the Rust migration plan.

## License

The repository-level License/NOTICE decision is recorded as MIT in the migration audit and the exact `NOTICE` hash is verified during the release gate. Any future change to licensing or attribution must update both files and regenerate the audit before release.
