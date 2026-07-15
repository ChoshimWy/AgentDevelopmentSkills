# Fingerprint Rules

Use three independent fingerprints. A whole-repository diff hash alone is insufficient.

## Environment fingerprint

Include only execution compatibility inputs:

- Xcode and SDK version
- workspace/project, scheme, configuration, test plan and destination platform
- `Package.resolved`, `Podfile.lock`, `Pods/Manifest.lock`
- relevant xcconfig and selected Build Settings

Do not include unrelated README or source files.

## Target source fingerprint

Hash the actual target inputs:

- source files and generated source inputs
- target membership and resources
- compiler flags that affect the target
- dependency target fingerprints

Normalize paths relative to the project root. Record missing files explicitly so deletion changes the digest.

## Evidence fingerprint

Bind one evidence request to:

- evidence kind and logical identity
- environment fingerprint
- target/test/fixture/scenario fingerprints used by that evidence
- XCTest selectors, UI screen/scenario, appearance/locale/dynamic type when applicable

Examples:

```text
compile:App
test:DeviceControlTests/DeviceControlViewModelTests
ui:device-connected:device-control
review:current-diff
```

## Invalidation

Invalidate only related evidence when any of these change:

- target or test source inputs
- scheme or test plan
- destination platform
- Xcode/SDK or relevant build settings
- dependency lockfiles
- snapshot baseline
- fixture/scenario/locale/theme/dynamic type
- current Diff for review evidence

Use `scripts/fingerprint.py`. Canonicalize JSON with sorted keys, compact separators, UTF-8, no NaN and a trailing newline for persisted objects.
