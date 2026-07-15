# Verification Coordinator and build-queue Protocol

This is the normative target contract. The current wrapper implements exact-request dedupe/cache and atomic queue publication; lane priority, Session integration, cross-request dominance, failure caching, request coalescing and automatic `.xctestrun` registration remain scaffolded follow-up work.

## Request lifecycle

```text
planned -> attached | queued -> running -> succeeded | failed | blocked
```

Every request carries:

- session id and request id
- evidence fingerprint
- lane and priority
- workspace/project, scheme, configuration, test plan and destination identity
- required capabilities and selectors/scenario
- source/environment fingerprints
- `force` / `no_cache` flags

## In-flight dedupe

- Serialize fingerprint registration before queue insertion.
- If an identical compatible fingerprint is `queued` or `running`, return `attached` with the original job id.
- Every attached caller observes the same final structured artifacts; do not copy or mutate the source job.
- Destination and DerivedData locks remain daemon-owned.

## Evidence reuse

- Reuse a completed job only after the evidence model accepts freshness, compatibility, capability coverage and artifact hashes.
- Return `cached=true`, source job id and source evidence fingerprint.
- Never turn a partial/blocked result into passed cache evidence.

## Failure cache

- Deterministic compiler, linker and test assertion failures may be returned for an unchanged fingerprint.
- Environment, destination, signing-service, tool-bootstrap, timeout and flaky failures are retryable and must not become terminal cache hits.
- `--force` bypasses both success and failure reuse but does not bypass queue/destination locks.

## Request coalescing

Coalesce only when environment/source/test-plan/destination fingerprints match. A build request and selected test request may become:

```text
build-for-testing
-> cached .xctestrun
-> test-without-building -only-testing:<selector>
```

Never widen selectors or silently change destination to make requests merge.

## Priority

1. repaired first-error recovery validation
2. current interactive targeted test
3. required UI smoke/snapshot
4. final regression
5. background prewarm

Priority does not permit two jobs to share a locked destination or DerivedData build slot concurrently.

## Structured response

Return `agent-summary.json` first. It includes status, fingerprint, cached/attached state, source job, required/accepted/missing evidence, first blocker, artifact paths and next action. Raw logs remain path-only unless `needs_raw_log=true`.
