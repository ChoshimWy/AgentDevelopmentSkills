# Verification Session Contract

Use one durable session per development task. Store it below:

```text
.codex/verification/sessions/<session-id>/
  session.json
  evidence-index.json
  test-manifest.json
  project-metadata.json
```

## Session identity

`session.json` is canonical UTF-8 JSON with a trailing newline:

```json
{
  "schema_version": "1.0",
  "session_id": "feature-device-control-193",
  "base_commit": "abc123",
  "current_diff_hash": "def456",
  "environment_fingerprint": "env:83af91",
  "project": {
    "workspace_or_project": "App.xcworkspace",
    "scheme": "App_TEST",
    "configuration": "Debug",
    "destination": "platform=iOS Simulator,name=iPhone 16 Pro",
    "test_plan": "AppTests"
  },
  "test_list_cache": {},
  "target_fingerprints": {},
  "evidence": {},
  "in_flight_requests": {},
  "failed_requests": {}
}
```

## Rules

- Validate `session_id` as a safe path component; reject traversal and aliases.
- Write atomically with a temporary sibling plus rename.
- Treat `base_commit` as task identity and `current_diff_hash` as freshness identity.
- Refresh project discovery only when workspace/project, scheme, test plan, lockfiles, xcconfig, Xcode/SDK, destination platform or relevant Build Settings change.
- Key in-flight requests by evidence fingerprint. Attach duplicate callers to the same request rather than creating another queue job.
- Store failure classification with the fingerprint. Reuse only deterministic code/link/test failures; never cache an environment or flaky failure as terminal.
- Evidence entries must point to existing structured artifacts and their hashes.
- Do not store credentials, provisioning contents, raw environment dumps or unredacted command lines.

Use `scripts/session_store.py` for create/read/update operations. Agents should not edit session JSON manually.
