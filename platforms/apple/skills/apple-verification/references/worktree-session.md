# Apple Worktree Session Verification

## Boundary

The shared Git/Workflow capabilities own Worktree creation, multi-repository source identity, Registry lifecycle and Final Gate. Apple consumes only a committed, currently fresh `worktree-session-context-v1`.

Use `scripts/worktree_session.py` to create the Apple daemon request. It:

- requires `apple` in `selected_platforms`;
- requires every repository to have a clean Checkpoint Commit/tree;
- refreshes the shared source identity before creating a request;
- passes primary Worktree/Common Dir plus all dependency repository identities;
- freezes destination, test plan and target fingerprints in the daemon request;
- derives the path-free lease Slot as `<project-id>/env-<sha256(environment-fingerprint)>`;
- stores artifacts under `sessions/<session-id>/<source-identity>/<attempt-id>`.

Submit that frozen request through the only supported verification entry point:

```bash
python3 scripts/worktree_session.py \
  --context .codex/worktree-sessions/<session-id>.json \
  --attempt-id checkpoint-1 \
  --mode checkpoint \
  --environment-fingerprint '<environment-fingerprint>' \
  --derived-data-slot '<project-id>/env-<sha256(environment-fingerprint)>' \
  --destination '<frozen-destination>' \
  --test-plan '<test-plan>' \
  --target-fingerprint '<target-fingerprint>' \
  > /tmp/apple-worktree-request.json

./codex_verify.sh \
  --worktree-session-request /tmp/apple-worktree-request.json \
  --build-check <build-check.sh> <session-worktree> <selectors-and-action>
```

`codex_verify` reads, validates and canonicalizes one immutable request snapshot before extracting any execution field. The daemon hashes the copied request, validates it, takes the deterministic DerivedData lease Slot, validates it again under the lease, exports the frozen destination/test plan and `CODEX_WORKTREE_SESSION_*` identity to `build_check.py`, and validates the repository closure again after execution. `build_check.py` artifacts are rooted below the queue-owned `artifacts/<artifact-namespace>/<job-id>` directory; the daemon freezes their relative paths, modes, sizes and SHA-256 values into a hashed manifest, and cache reuse revalidates both that manifest and the current directory inventory. A stale/dirty repository, changed request or artifact digest, path mismatch, destination/test-plan conflict or unavailable Slot blocks the job before its result can become reusable evidence. `agent-summary.json` records the request hash, Session identity, artifact manifest/directory and daemon-validation result.

Assurance is intentionally split: the daemon recomputes the complete committed multi-repository source identity and binds destination/test plan to the executed command path. `environment_fingerprint` and `target_fingerprints` are currently caller-frozen assertions; this adapter does not rediscover Xcode build settings or recompute target source fingerprints. `agent-summary.json.worktree_session.identity_assurance` records that limitation. Those two fields become evidence only when an upstream coordinator/provider supplies independently derived fingerprints; their presence in a request or summary alone is not proof.

## Shared DerivedData safety

The Environment Slot is a lease identity over the shared system DerivedData root, not evidence or a separate DerivedData path. Its environment segment is deterministically derived from the frozen environment fingerprint, and the daemon remains globally serialized. The current integration holds that lease for one queue job only, so Worktree Session `test-without-building` is fail-closed until the wrapper/daemon also consumes a validated immutable build artifact identity. The helper can freeze `.xctestrun`, test bundles, target fingerprints, destination, test plan and the complete runtime product closure with `immutable_build_artifact_identity`, but creating that document alone does not enable reuse. `product_artifacts` must include every referenced host/UITarget app and every standalone Framework/dylib/product not already contained in a hashed app or test bundle. Absolute paths and product placeholders outside `__TESTROOT__/...` are rejected; `__PLATFORMS__/...` is the only external placeholder allowed because it is frozen by the Apple environment fingerprint. Validation compares the identity with the current request, re-parses `.xctestrun`, and re-hashes every file/directory. After the lease is released, path-only reuse from Shared DerivedData is forbidden.

## Local dependencies

Writable local Pods, Swift packages, Framework/Unity sources or generated-source repositories must be separate dependency Worktrees in the same Session source identity. If a stable Commit/content identity cannot be proven, verification is blocked.
