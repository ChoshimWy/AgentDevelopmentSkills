# Security Policy

## Supported versions

Only the latest `main` revision and the most recent signed public release are supported for security fixes.

## Reporting a vulnerability

Please do not open a public issue for an undisclosed vulnerability. Use a private GitHub security advisory or contact the repository owner through a private GitHub channel with:

- a concise description and impact;
- affected commit, release, or component;
- reproducible steps or a minimal proof of concept;
- any suggested mitigation.

Do not include credentials, private keys, personal data, or customer data in a report.

## Release security boundary

Release candidates are subject to provenance, SBOM, external review, and a fail-closed release gate. The release gate is not a hostile-code sandbox; official candidate execution must occur on a disposable isolated worker.
