# AgentDevelopmentSkills Architecture

## Purpose

AgentDevelopmentSkills is an offline-first workflow core for coding agents. It discovers repository capabilities, resolves explicit contracts, creates deterministic plans, and records auditable evidence.

## Core flow

```text
repository discovery
        ↓
capability and platform policy
        ↓
deterministic workflow plan
        ↓
approval / execution / evidence
        ↓
doctor, rollback, and release gate
```

## Trust boundaries

- Discovery is read-only and must not execute repository scripts or read credentials.
- Package sources, manifests, schemas, bindings, permissions, and assets are hash-checked.
- Unknown platforms and providers fail closed; Android, Web, and Backend are currently bootstrap-only.
- Install, upgrade, rollback, and uninstall operations use ownership checks and transactional recovery.
- Release candidates require provenance, SBOM, external review, Python compatibility evidence, and a passed final gate.

## Supported installation targets

| Target | Status |
| --- | --- |
| macOS | Production bootstrap |
| Linux / WSL2 | Production bootstrap |
| Windows | CI/bootstrap validation; production install gated |
| Apple package | Implemented |
| Desktop package | Implemented |
| Android / Web / Backend | Bootstrap-only |

## Public interfaces

- [`README.md`](../README.md): user-facing overview and quick start
- [`README.zh-CN.md`](../README.zh-CN.md): Chinese guide
- [`schemas/`](../schemas/): machine-readable contracts
- [`scripts/run_conformance.py`](../scripts/run_conformance.py): offline conformance entry point
- [`scripts/run_release_gate.py`](../scripts/run_release_gate.py): release gate

## Release architecture

GitHub Pages is the small public control plane. Versioned ZIP, wheel, and sdist assets are hosted by immutable GitHub Releases. The publish workflow verifies the qualification run, protected `main` revision, external review, release identity, tag uniqueness, and deployed asset hashes before publication.

## Security and privacy

Telemetry is disabled by default. The project does not provide implicit remote execution or credential collection. Report security issues privately according to the repository's security policy before public disclosure.

For the historical implementation record, see the local-only `docs/implementation/` directory when working from a development checkout.
