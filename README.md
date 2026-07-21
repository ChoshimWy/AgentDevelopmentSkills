# AgentDevelopmentSkills

AgentDevelopmentSkills is an offline-first, fail-closed workflow core for
coding agents. It discovers repository capabilities, resolves explicit
platform and discipline contracts, produces deterministic execution plans,
and records auditable evidence.

## What it provides

- Conservative repository discovery and capability-based routing
- Deterministic plans, manifests, lockfiles, migrations, and release artifacts
- Guarded install, upgrade, rollback, doctor, and uninstall transactions
- Apple and Desktop packages; Android, Web, and Backend are currently
  `bootstrap-only`
- Python compatibility tooling and a gradually expanding Rust native runtime
- Reproducible release artifacts, provenance, SBOM, and fail-closed gates
- No telemetry, credential collection, or implicit remote execution

## Status

The project is in a controlled Rust migration. The source-checkout installer
can use the pinned Rust toolchain offline for eligible fresh Apple/Desktop
installs; compatibility-only paths still use Python. Release Manifest v3 and
the six-target native binary matrix are implemented, but hosted installation
is not a production entry point until the signed release and required GitHub
environment approvals are complete.

See the [Rust migration plan](docs/rust-migration.md) for the exact feature
matrix and cutover gates.

## Requirements

- Python 3.11+ for the compatibility implementation
- Rust 1.97.1 for native development and offline source builds
- macOS, Linux, or WSL2 for the supported POSIX bootstrap path
- Windows bootstrap is CI-validated; hosted Windows installation remains gated

## Install from a checkout

The safest way to install before a signed release is available is from a local
checkout:

```bash
./install.sh
```

Examples:

```bash
./install.sh --platform apple
./install.sh --platform desktop
./install.sh --platform all
./install.sh --platform apple --discipline qa --runtime-config codex --dry-run
```

For a fresh install, the selector uses Rust when `cargo` (or an active
`rustup` toolchain) and the required offline dependencies are available. To
select the implementation explicitly:

```bash
AGENT_SKILLS_INSTALL_ENGINE=rust ./install.sh --platform apple
AGENT_SKILLS_INSTALL_ENGINE=python ./install.sh --platform apple
```

Once native execution has started, a Rust failure never silently falls back to
Python. The installer also recognizes the exact legacy `iOSAgentSkills`
symlink layout on POSIX and handles it inside the guarded adoption transaction.

## Remote installation

The Pages bootstrap is intentionally separate from immutable GitHub Release
assets. Do not use it as a production installer until a signed release is
published:

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://choshimwy.github.io/AgentDevelopmentSkills/install.sh \
  | bash -s -- --platform apple
```

Windows PowerShell:

```powershell
iwr -useb https://choshimwy.github.io/AgentDevelopmentSkills/install.ps1 | iex
```

The hosted upgrade and uninstall flows require the same release provenance and
approval gates. They are operator-invoked, never background updates.

## Development

Run the complete conformance suite:

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
```

Run focused Python tests:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_pages_distribution \
  tests.test_github_publication
```

Validate the Rust workspace:

```bash
cargo fmt --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
```

The native CLI is still a non-default migration surface. Examples and
compatibility details are documented in [docs/rust-migration.md](docs/rust-migration.md).

## Repository map

| Path | Purpose |
| --- | --- |
| `src/` | Python compatibility implementation |
| `crates/` | Rust contracts, engine, lifecycle, runtime, registry, and release packages |
| `platforms/` | Platform packages and manifests |
| `disciplines/` | Cross-platform workflow disciplines |
| `runtime-configs/` | Explicit runtime configuration packages |
| `schemas/` | Versioned machine-readable contracts |
| `scripts/` | Install, conformance, release, and validation tooling |
| `docs/` | Architecture, migration, implementation phases, and operational details |

## Design and security principles

1. **Explicit selection:** platform packages and runtime configurations are
   selected by the caller; they are never activated implicitly.
2. **Deterministic output:** machine-readable output is canonical UTF-8 JSON
   with stable ordering and no NaN values.
3. **Fail closed:** missing capabilities, dependency cycles, schema mismatches,
   permission expansion, tampering, and unsafe layouts stop the transaction.
4. **Bounded trust:** discovery is read-only; Core does not read provider
   credentials, execute provider code, or make implicit network calls.
5. **Transactional lifecycle:** installation changes are staged, verified,
   published atomically where supported, and recoverable on failure.

## Documentation

- [Architecture overview](docs/architecture.md)
- [Rust migration and cutover policy](docs/rust-migration.md)
- [Cross-platform workflow architecture](docs/cross-platform-agent-workflow-architecture.html)
- [Phase 4 QA Core and Desktop status](docs/implementation/phase-4-qa-core-and-desktop-minimum.html)
- [Multi-session worktree architecture](docs/multi-session-worktree.md)
- [Skill naming convention](docs/skill-naming.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [中文指南](README.zh-CN.md)

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
