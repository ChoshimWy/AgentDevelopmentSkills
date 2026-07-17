# Contributing

Thank you for contributing to AgentDevelopmentSkills.

## Before opening a pull request

1. Keep changes scoped and explain the contract or user-facing behavior affected.
2. Do not add credentials, private keys, machine-specific exports, or generated local state.
3. Preserve fail-closed behavior and deterministic output.
4. Update the relevant Markdown documentation and tests.

Run focused tests for your change and, when touching contracts, packaging, installation, or release code, run:

```bash
PYTHONPATH=src python3 scripts/run_conformance.py
```

## Pull requests

Use a clear summary, list validation commands, and call out platform or environment constraints. Changes to release governance, schemas, permissions, or trust boundaries require explicit negative-case tests.

## License

By contributing, you agree that your contribution is provided under the MIT License in `LICENSE`.
