# Skill Naming Convention

Skill identifiers are stable public contract names. New identifiers must be unique, deterministic, and scoped to their owning platform or discipline.

## Rules

- Use lowercase kebab-case names.
- Do not add runtime, provider, or implementation-specific prefixes to shared skills.
- Keep platform-specific skills under the platform package.
- Deprecated skills remain readable until their removal window closes; new writers must not emit deprecated identifiers.

The machine policy is [`skill-naming-policy.json`](../skill-naming-policy.json), validated by `scripts/validate_skill_naming.py`.
