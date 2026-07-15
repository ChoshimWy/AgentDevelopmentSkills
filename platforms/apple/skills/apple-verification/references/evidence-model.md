# Evidence Model and Final Gate

## Evidence requirement

Each requirement has a stable `evidence_id`, kind, identity, reason and minimum capabilities:

```json
{
  "evidence_id": "test:DeviceControlTests/DeviceControlViewModelTests",
  "kind": "unit-test",
  "identity": {"selectors": ["DeviceControlTests/DeviceControlViewModelTests"]},
  "reason": "DeviceControlViewModel.swift changed",
  "minimum_capabilities": ["compile-test-bundle", "execute-selected-tests"]
}
```

## Accepted evidence

Accepted evidence must record:

- `status=passed`
- evidence/environment/source fingerprints
- capabilities proved
- destination and configuration
- structured artifact ids plus hashes
- producer, source job and completion time
- `cached` and reuse source when reused

## Same-or-stronger comparison

Evidence A satisfies requirement R only when:

1. A is passed and current.
2. A has the same environment/source compatibility identity required by R.
3. A capabilities are a superset of R minimum capabilities.
4. A target, test bundle, selectors, UI scenario/screen and destination constraints cover R.
5. All referenced artifacts still exist and match hashes.

Do not compare evidence with a single linear number. A device build is not automatically stronger than a simulator unit test; the capability set and requested identity decide.

## Missing evidence

`final-gate` computes:

```text
missing_evidence = required_evidence - accepted_same_or_stronger_evidence
```

It must not run the missing items itself. Return the missing set and the lane/owner needed to produce it.

## Review evidence

Independent Review is its own evidence item. It must bind to the current Diff and identify distinct implementation/reviewer actors. Blocking findings invalidate the Final Gate even if build/test evidence passes.

## Final decision

- `accepted_existing_evidence`: no missing evidence and no stale/high-risk gap.
- `needs_additional_evidence`: explicit missing items remain.
- `blocked_review`: reviewer unavailable or blocking findings exist.
- `blocked_stale_evidence`: fingerprints or artifact hashes no longer match.

An unexplained `no_test_reason`, UI change without suitable UI evidence, or project/dependency/release change without consumer/final evidence must remain blocked.
