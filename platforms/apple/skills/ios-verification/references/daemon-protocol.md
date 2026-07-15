# Verification Coordinator and build-queue Protocol

This is the normative target contract. The current wrapper implements exact-request dedupe/cache, queue schema/generation checks, atomic ready publication, startup reconciliation and structured queue repair; lane priority, Session integration, cross-request dominance, failure caching, request coalescing and automatic `.xctestrun` registration remain scaffolded follow-up work.

## Queue integrity and recovery

- The queue root carries canonical `queue-meta.json` with the supported schema and generation.
- A job becomes executable only after all metadata and `command.args0` are hashed into canonical `job-manifest.json`, its digest is frozen, `ready=true` and finally `state=queued` are atomically published from staging. The daemon revalidates the complete inventory and strict NUL-argument shape before claim and immediately before command execution.
- Missing or unknown state is `invalid`; it must never be interpreted as `queued`.
- A running daemon owns canonical `daemon-owner.json` and `daemon-heartbeat.json`; its PID is accepted only when generation, 256-bit instance token, queue root and fresh heartbeat all match. Automatic and direct daemon entry share one token-bound `start.lockdir` adoption gate, so only the winning process may publish the owner record. Each `running` job binds the same PID/token and the controlled `active_job` pointer.
- Before a daemon starts, offline reconciliation marks orphaned `running` jobs failed, rejects expired, incomplete or tampered `queued` jobs, removes stale runtime locks/leases, and quarantines invalid legacy entries by default. The live daemon quarantines an invalid queued job itself instead of invoking offline repair.
- An incompatible queue schema/generation blocks daemon startup rather than guessing migration compatibility.
- Missing queue metadata is initialized automatically only for an offline queue containing no staging, active or nonterminal/invalid legacy state. Otherwise publication fails closed and requires an explicit doctor/repair decision.
- `--queue-doctor` is read-only and safe while the daemon is active. Mutating `--repair` refuses with exit 75 while a validated daemon (or an untrusted live PID) exists; stop the daemon first. Offline `--repair` quarantines invalid entries, while `--repair --delete-invalid` is the explicit destructive variant.
- `--queue-status --json` emits the same structured health inventory without repairing it.
- Terminal `succeeded` and `failed` history is retained and never re-executed. A terminal success is cacheable only when its v2 publication manifest/generation and structured artifacts still validate; legacy terminal history is diagnostic-only. Invalid history is removed from the executable set so it cannot delay a newly published valid job, and an attached waiter exits 70 if its job is deleted or quarantined.
- Every runtime pointer (`active_job`, Slot lease owner) must resolve to a direct, real child of the queue-owned `jobs/` directory. Recovery never writes through an external or symlinked path.

## Request lifecycle

```text
planned -> attached | queued -> running -> succeeded | failed | blocked
```

### History incident runbook

1. Run `codex_verify --queue-status --json` or read-only `--queue-doctor`; do not delete records based only on directory age.
2. If the daemon is healthy and active, let it finish or stop that validated instance first. Online repair is intentionally refused.
3. Run offline `--queue-doctor --repair` to quarantine invalid/missing-state/stale queued records. Use `--delete-invalid` only when explicit destruction is intended.
4. Re-run read-only doctor and require `healthy=true` before submitting new verification.
5. If metadata/generation is incompatible or a live PID has no valid owner heartbeat, keep publication blocked. Investigate/stop the legacy process rather than rewriting metadata or killing an unverified PID.

This makes bad history non-executable and non-cacheable. A new valid job is never required to scan or consume quarantined history, while terminal history may be pruned later under an independent retention policy.

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
