//! Git workspace and Worktree Session source-identity primitives.

use crate::RuntimeError;
use agent_contracts::canonical_sha256;
#[cfg(unix)]
use cap_fs_ext::DirExt as _;
#[cfg(unix)]
use cap_std::ambient_authority;
#[cfg(unix)]
use cap_std::fs::Dir;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::fs::{File, Metadata};
use std::io::Read as _;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::thread;

const MAX_GIT_STDOUT_BYTES: usize = 64 * 1024 * 1024;
const MAX_GIT_STDERR_BYTES: usize = 1024 * 1024;
const MAX_GIT_PATHS: usize = 100_000;

struct GitOutput {
    status: ExitStatus,
    stdout: Vec<u8>,
}

#[derive(Debug, Eq, PartialEq)]
struct PatchSnapshot {
    changed_files: Vec<String>,
    payload: Value,
    untracked_files: Vec<String>,
}

/// Resolve a path to its canonical Git Worktree root and common directory.
///
/// # Errors
/// Returns an error when Git cannot resolve the Worktree or either canonical
/// directory is missing, unsafe, or not UTF-8 representable.
pub fn resolve_worktree(path: &Path) -> Result<(PathBuf, PathBuf), RuntimeError> {
    let candidate = std::fs::canonicalize(path).map_err(|_| {
        RuntimeError::Contract(format!(
            "worktree path is missing or unsafe: {}",
            path.display()
        ))
    })?;
    ensure_real_directory(&candidate, "worktree path")?;
    let root = canonical_git_path(&candidate, &["rev-parse", "--show-toplevel"])?;
    ensure_real_directory(&root, "worktree root")?;
    let common_raw = git_text(&root, &["rev-parse", "--git-common-dir"])?;
    let common_candidate = PathBuf::from(common_raw);
    let common_path = if common_candidate.is_absolute() {
        common_candidate
    } else {
        root.join(common_candidate)
    };
    let common = std::fs::canonicalize(&common_path).map_err(|_| {
        RuntimeError::Contract(format!(
            "git common dir is missing or unsafe: {}",
            common_path.display()
        ))
    })?;
    ensure_real_directory(&common, "git common dir")?;
    Ok((root, common))
}

/// Resolve a Git ref to one full SHA-1 or SHA-256 commit object id.
///
/// # Errors
/// Returns an error for an invalid ref or non-commit Git object.
pub fn resolve_commit(root: &Path, reference: &str) -> Result<String, RuntimeError> {
    let (worktree, _) = resolve_worktree(root)?;
    if reference.is_empty() || reference.starts_with('-') {
        return Err(RuntimeError::Contract("base ref is invalid".to_owned()));
    }
    let commit = git_text(
        &worktree,
        &["rev-parse", "--verify", &format!("{reference}^{{commit}}")],
    )?;
    if !valid_git_oid(&commit) {
        return Err(RuntimeError::Contract(
            "resolved base is not a full Git object id".to_owned(),
        ));
    }
    Ok(commit)
}

/// Return staged, unstaged, and untracked Worktree status.
///
/// # Errors
/// Returns an error when status cannot be classified or Git paths are unsafe.
pub fn worktree_status(root: &Path) -> Result<Value, RuntimeError> {
    let (worktree, _) = resolve_worktree(root)?;
    let staged = git_run(
        &worktree,
        &[
            "diff",
            "--cached",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
        ],
        false,
    )?;
    let unstaged = git_run(
        &worktree,
        &["diff", "--quiet", "--no-ext-diff", "--no-textconv"],
        false,
    )?;
    let staged_code = staged.status.code();
    let unstaged_code = unstaged.status.code();
    if !matches!(staged_code, Some(0 | 1)) || !matches!(unstaged_code, Some(0 | 1)) {
        return Err(RuntimeError::Contract(
            "unable to classify worktree status".to_owned(),
        ));
    }
    let untracked = untracked_paths(&worktree)?;
    Ok(json!({
        "dirty": staged_code == Some(1) || unstaged_code == Some(1) || !untracked.is_empty(),
        "staged": staged_code == Some(1),
        "unstaged": unstaged_code == Some(1),
        "untracked": untracked,
    }))
}

/// Compute a deterministic `repository-patch-v1` identity.
///
/// # Errors
/// Returns an error for Gitlinks, unsafe paths, dirty checkpoints, concurrent
/// source changes, unsupported untracked file types, or Git failures.
pub fn repository_patch(
    root: &Path,
    repository_id: &str,
    base_commit: &str,
    checkpoint_commit: Option<&str>,
) -> Result<Value, RuntimeError> {
    validate_identifier(repository_id, "repository id")?;
    let (worktree, _) = resolve_worktree(root)?;
    let base = resolve_commit(&worktree, base_commit)?;
    let checkpoint = checkpoint_commit
        .map(|value| resolve_commit(&worktree, value))
        .transpose()?;
    if has_gitlinks(&worktree)? {
        return Err(RuntimeError::Contract(
            "repository-patch-v1 does not support Git submodules/gitlinks".to_owned(),
        ));
    }
    let first = repository_patch_once(&worktree, repository_id, &base, checkpoint.as_deref())?;
    let second = repository_patch_once(&worktree, repository_id, &base, checkpoint.as_deref())?;
    if first != second {
        return Err(RuntimeError::Contract(
            "repository changed while computing the patch fingerprint".to_owned(),
        ));
    }
    Ok(json!({
        "algorithm": "repository-patch-v1",
        "changed_files": first.changed_files,
        "patch_hash": format!("repository-patch:{}", canonical_sha256(&first.payload)?),
        "untracked_files": first.untracked_files,
    }))
}

/// Inspect one repository and bind its base, current patch, and optional clean
/// checkpoint identity.
///
/// # Errors
/// Returns an error for invalid metadata, unsafe paths, divergent history,
/// dirty committed state, or any repository patch failure.
#[allow(clippy::too_many_arguments)]
pub fn inspect_repository(
    root: &Path,
    repository_id: &str,
    role: &str,
    base_ref: &str,
    base_source: &str,
    committed: bool,
) -> Result<Value, RuntimeError> {
    validate_identifier(repository_id, "repository id")?;
    if !matches!(role, "primary" | "dependency") {
        return Err(RuntimeError::Contract(
            "repository role is invalid".to_owned(),
        ));
    }
    if !matches!(
        base_source,
        "explicit" | "integration-checkpoint" | "stacked-checkpoint" | "clean-head"
    ) {
        return Err(RuntimeError::Contract(
            "repository base source is invalid".to_owned(),
        ));
    }
    let (worktree, common) = resolve_worktree(root)?;
    let base_commit = resolve_commit(&worktree, base_ref)?;
    let head = resolve_commit(&worktree, "HEAD")?;
    let ancestor = git_run(
        &worktree,
        &["merge-base", "--is-ancestor", &base_commit, &head],
        false,
    )?;
    if !ancestor.status.success() {
        return Err(RuntimeError::Contract(
            "repository HEAD does not descend from the frozen base commit".to_owned(),
        ));
    }
    let branch_result = git_run(&worktree, &["symbolic-ref", "--short", "-q", "HEAD"], false)?;
    let branch = if branch_result.status.success() {
        Some(decode_trimmed(branch_result.stdout, "Git branch")?)
    } else {
        None
    };
    let checkpoint = if committed {
        let status = worktree_status(&worktree)?;
        if status.get("dirty").and_then(Value::as_bool) != Some(false) {
            return Err(RuntimeError::Contract(
                "committed repository identity requires a clean worktree".to_owned(),
            ));
        }
        Some(json!({
            "commit": head,
            "tree": git_text(&worktree, &["rev-parse", &format!("{head}^{{tree}}")])?,
        }))
    } else {
        None
    };
    let change_set = repository_patch(
        &worktree,
        repository_id,
        &base_commit,
        committed.then_some(head.as_str()),
    )?;
    Ok(json!({
        "base": {
            "commit": base_commit,
            "dirty_worktree_inherited": false,
            "ref": base_ref,
            "source": base_source,
        },
        "branch": branch,
        "change_set": change_set,
        "checkpoint": checkpoint,
        "git_common_dir": path_text(&common, "git common dir")?,
        "repository_id": repository_id,
        "role": role,
        "worktree_path": path_text(&worktree, "worktree path")?,
    }))
}

/// Create one isolated Worktree and branch from a frozen Commit.
///
/// The source Worktree may be dirty only when the caller supplies an explicit
/// base. Source changes are never copied into the new Worktree.
///
/// # Errors
/// Returns an error for an unsafe target, invalid or existing branch, Gitlink
/// base, failed Git operation, or post-create identity mismatch. A
/// post-create validation failure triggers exact compensation and reports when
/// compensation cannot prove the created Worktree is still pristine.
#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)]
pub fn create_session_worktree(
    repository: &Path,
    name: &str,
    repository_id: &str,
    base_ref: Option<&str>,
    base_source: Option<&str>,
    worktree_root: Option<&Path>,
    branch: Option<&str>,
) -> Result<(Value, Value), RuntimeError> {
    validate_identifier(name, "session name")?;
    validate_identifier(repository_id, "repository id")?;
    let (source_root, common) = resolve_worktree(repository)?;
    let status = worktree_status(&source_root)?;
    let source_dirty = status
        .get("dirty")
        .and_then(Value::as_bool)
        .ok_or_else(|| RuntimeError::Contract("worktree status is invalid".to_owned()))?;
    let (effective_ref, effective_source) = match base_ref {
        None => {
            if base_source.is_some() {
                return contract("base_source requires an explicit base ref");
            }
            if source_dirty {
                return contract(
                    "cannot infer session base from a dirty worktree; specify an explicit base commit or ref",
                );
            }
            ("HEAD", "clean-head")
        }
        Some(reference) => {
            if base_source == Some("clean-head") {
                return contract("clean-head base_source is reserved for an inferred clean HEAD");
            }
            (reference, base_source.unwrap_or("explicit"))
        }
    };
    if !matches!(
        effective_source,
        "explicit" | "integration-checkpoint" | "stacked-checkpoint" | "clean-head"
    ) {
        return contract("repository base source is invalid");
    }
    let base_commit = resolve_commit(&source_root, effective_ref)?;
    if commit_has_gitlinks(&source_root, &base_commit)? {
        return contract("repository-patch-v1 does not support Git submodules/gitlinks");
    }
    let branch_name = branch.map_or_else(|| format!("agent/{name}"), ToOwned::to_owned);
    if branch_name.is_empty() || branch_name.starts_with('-') || branch_name.contains("..") {
        return contract("session branch is invalid");
    }
    if !git_run(
        &source_root,
        &["check-ref-format", "--branch", &branch_name],
        false,
    )?
    .status
    .success()
    {
        return contract("session branch is invalid");
    }
    let branch_ref = format!("refs/heads/{branch_name}");
    let branch_exists = git_run(
        &source_root,
        &["show-ref", "--verify", "--quiet", &branch_ref],
        false,
    )?;
    match branch_exists.status.code() {
        Some(0) => {
            return contract(format!("session branch already exists: {branch_name}"));
        }
        Some(1) => {}
        _ => return contract("unable to inspect session branch"),
    }

    let parent_input = worktree_root.map_or_else(
        || {
            source_root
                .parent()
                .unwrap_or(&source_root)
                .join(".agent-worktrees")
                .join(
                    source_root
                        .file_name()
                        .unwrap_or_else(|| OsStr::new("repository")),
                )
        },
        Path::to_path_buf,
    );
    let parent = prepare_worktree_parent(&parent_input, &source_root)?;
    let target = parent.join(name);
    if target == source_root || target.starts_with(&source_root) {
        return contract("session worktree must not be nested inside the source worktree");
    }
    match std::fs::symlink_metadata(&target) {
        Ok(_) => {
            return contract(format!(
                "session worktree path already exists: {}",
                target.display()
            ));
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(RuntimeError::Io(error)),
    }
    let target_text = path_text(&target, "session worktree path")?;
    git_run(
        &source_root,
        &["update-ref", "--no-deref", &branch_ref, &base_commit, ""],
        true,
    )?;
    if let Err(error) = git_run(
        &source_root,
        &["worktree", "add", target_text, &branch_name],
        true,
    ) {
        if let Err(cleanup_error) = compensate_failed_worktree_add(
            &source_root,
            &common,
            &target,
            &branch_name,
            &base_commit,
        ) {
            return contract(format!(
                "worktree creation failed ({error}); exact compensation was blocked ({cleanup_error})"
            ));
        }
        return Err(error);
    }
    let record_result = (|| {
        let (created, created_common) = resolve_worktree(&target)?;
        if created != target || created_common != common {
            return contract("created worktree does not belong to the expected Git common dir");
        }
        validate_direct_branch_ref(&source_root, &branch_ref, &base_commit)?;
        let record = inspect_repository(
            &created,
            repository_id,
            "primary",
            &base_commit,
            effective_source,
            false,
        )?;
        if record.get("branch").and_then(Value::as_str) != Some(branch_name.as_str()) {
            return contract("created worktree branch identity is invalid");
        }
        Ok(record)
    })();
    let record = match record_result {
        Ok(record) => record,
        Err(error) => {
            if let Err(cleanup_error) = remove_exact_created_worktree(
                &source_root,
                &common,
                &target,
                &branch_name,
                &base_commit,
            ) {
                return contract(format!(
                    "worktree creation validation failed ({error}); exact compensation was blocked ({cleanup_error})"
                ));
            }
            return Err(error);
        }
    };
    Ok((
        record,
        json!({
            "base_commit": base_commit,
            "base_ref": effective_ref,
            "source_worktree_dirty": source_dirty,
            "source_worktree_changes_inherited": false,
        }),
    ))
}

/// Remove only a pristine Worktree/branch pair created by this workflow.
///
/// # Errors
/// Returns an error instead of removing anything when live Worktree, branch,
/// Commit, common-directory, or dirty-state identity differs from the record.
pub fn remove_created_session_worktree(
    record: &Value,
    source_repository: &Path,
) -> Result<(), RuntimeError> {
    let record = object(record, "session repository")?;
    let worktree_path = PathBuf::from(required_str(record, "worktree_path", "session repository")?);
    let (worktree, common) = resolve_worktree(&worktree_path)?;
    let expected_common = required_str(record, "git_common_dir", "session repository")?;
    if path_text(&common, "git common dir")? != expected_common
        || worktree_status(&worktree)?
            .get("dirty")
            .and_then(Value::as_bool)
            != Some(false)
    {
        return contract("created worktree changed; refusing automatic compensation");
    }
    let base = object(
        required_value(record, "base", "session repository")?,
        "session repository base",
    )?;
    let base_commit = required_str(base, "commit", "session repository base")?;
    let branch = required_str(record, "branch", "session repository")?;
    let head = resolve_commit(&worktree, "HEAD")?;
    let active_branch = git_text(&worktree, &["symbolic-ref", "--short", "-q", "HEAD"])?;
    if head != base_commit || active_branch != branch {
        return contract("created worktree identity changed; refusing automatic compensation");
    }
    let (source_root, source_common) = resolve_worktree(source_repository)?;
    if source_common != common {
        return contract("created worktree compensation source is invalid");
    }
    remove_exact_created_worktree(&source_root, &common, &worktree, branch, base_commit)
}

/// Derive the order-independent `session-source-v1` identity.
///
/// # Errors
/// Returns an error for an invalid mode, missing patch/checkpoint identity, or
/// empty/duplicate repository ids.
pub fn session_source_identity(repositories: &Value, mode: &str) -> Result<String, RuntimeError> {
    if !matches!(mode, "working" | "committed") {
        return Err(RuntimeError::Contract(
            "session source identity mode is invalid".to_owned(),
        ));
    }
    let repositories = repositories.as_array().ok_or_else(|| {
        RuntimeError::Contract("session repositories must be an array".to_owned())
    })?;
    let mut ordered = repositories.iter().collect::<Vec<_>>();
    ordered.sort_by(|left, right| {
        repository_id(left)
            .unwrap_or_default()
            .cmp(repository_id(right).unwrap_or_default())
    });
    let mut ids = BTreeSet::new();
    let mut payload = Vec::with_capacity(ordered.len());
    for repository in ordered {
        let repository = object(repository, "session repository")?;
        let repository_id = required_str(repository, "repository_id", "session repository")?;
        let change_set = object(
            required_value(repository, "change_set", "session repository")?,
            "session repository change_set",
        )?;
        let patch_hash = required_str(change_set, "patch_hash", "session repository change_set")?;
        if !valid_prefixed_hash(patch_hash, "repository-patch:") {
            return Err(RuntimeError::Contract(
                "repository patch identity is missing".to_owned(),
            ));
        }
        let checkpoint = repository.get("checkpoint").cloned().unwrap_or(Value::Null);
        if mode == "committed" && !checkpoint.is_object() {
            return Err(RuntimeError::Contract(
                "committed session source identity requires repository checkpoints".to_owned(),
            ));
        }
        ids.insert(repository_id.to_owned());
        payload.push(json!({
            "base_commit": repository
                .get("base")
                .and_then(Value::as_object)
                .and_then(|base| base.get("commit"))
                .cloned()
                .unwrap_or(Value::Null),
            "checkpoint": checkpoint,
            "patch_hash": patch_hash,
            "repository_id": repository_id,
            "role": repository.get("role").cloned().unwrap_or(Value::Null),
        }));
    }
    if payload.is_empty() || ids.len() != payload.len() {
        return Err(RuntimeError::Contract(
            "session repositories must be non-empty with unique identities".to_owned(),
        ));
    }
    let digest = canonical_sha256(&json!({
        "algorithm": "session-source-v1",
        "mode": mode,
        "repositories": payload,
    }))?;
    Ok(format!("session-source:{digest}"))
}

fn repository_patch_once(
    root: &Path,
    repository_id: &str,
    base_commit: &str,
    checkpoint_commit: Option<&str>,
) -> Result<PatchSnapshot, RuntimeError> {
    let (diff_args, untracked) = if let Some(checkpoint) = checkpoint_commit {
        let status = worktree_status(root)?;
        if status.get("dirty").and_then(Value::as_bool) != Some(false) {
            return Err(RuntimeError::Contract(
                "checkpoint fingerprint requires a clean worktree".to_owned(),
            ));
        }
        (
            vec![
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                base_commit,
                checkpoint,
                "--",
            ],
            Vec::new(),
        )
    } else {
        (
            vec![
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                base_commit,
                "--",
            ],
            untracked_paths(root)?,
        )
    };
    let diff = git_run(root, &diff_args, true)?.stdout;
    let changed_files = changed_paths(root, base_commit, checkpoint_commit)?;
    let mut inventory = Vec::with_capacity(untracked.len());
    for path in &untracked {
        inventory.push(untracked_entry(root, path)?);
    }
    let payload = json!({
        "base_commit": base_commit,
        "changed_files": changed_files,
        "checkpoint_commit": checkpoint_commit,
        "repository_id": repository_id,
        "tracked_diff_sha256": hex_digest(&diff),
        "tracked_diff_size": diff.len(),
        "untracked_files": untracked,
        "untracked_inventory": inventory,
    });
    Ok(PatchSnapshot {
        changed_files,
        payload,
        untracked_files: untracked,
    })
}

fn changed_paths(
    root: &Path,
    base: &str,
    checkpoint: Option<&str>,
) -> Result<Vec<String>, RuntimeError> {
    let mut args = vec![
        "diff",
        "--name-only",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        base,
    ];
    if let Some(checkpoint) = checkpoint {
        args.push(checkpoint);
    }
    args.push("--");
    let mut paths = decode_git_paths(&git_run(root, &args, true)?.stdout)?;
    if checkpoint.is_none() {
        paths.extend(untracked_paths(root)?);
    }
    paths.sort();
    paths.dedup();
    Ok(paths)
}

fn untracked_paths(root: &Path) -> Result<Vec<String>, RuntimeError> {
    let output = git_run(
        root,
        &["ls-files", "-z", "--others", "--exclude-standard"],
        true,
    )?;
    let mut paths = decode_git_paths(&output.stdout)?;
    paths.sort();
    Ok(paths)
}

fn decode_git_paths(raw: &[u8]) -> Result<Vec<String>, RuntimeError> {
    let mut result = Vec::new();
    for item in raw.split(|byte| *byte == 0).filter(|item| !item.is_empty()) {
        let value = std::str::from_utf8(item)
            .map_err(|_| RuntimeError::Contract("Git path is not valid UTF-8".to_owned()))?;
        if value.starts_with('/')
            || value.contains('\\')
            || Path::new(value).components().any(|part| {
                matches!(
                    part,
                    Component::CurDir
                        | Component::ParentDir
                        | Component::RootDir
                        | Component::Prefix(_)
                )
            })
        {
            return Err(RuntimeError::Contract(format!(
                "Git returned an unsafe path: {value:?}"
            )));
        }
        result.push(value.to_owned());
        if result.len() > MAX_GIT_PATHS {
            return Err(RuntimeError::Contract(format!(
                "Git path inventory exceeds maximum {MAX_GIT_PATHS}"
            )));
        }
    }
    Ok(result)
}

fn untracked_entry(root: &Path, relative: &str) -> Result<Value, RuntimeError> {
    let path = root.join(relative);
    let before = std::fs::symlink_metadata(&path)?;
    let mode = metadata_mode(&before);
    if before.is_file() {
        let mut handle = open_read_nofollow(&path)?;
        let opened = handle.metadata()?;
        let mut digest = Sha256::new();
        let mut size = 0_u64;
        let mut buffer = vec![0_u8; 1024 * 1024];
        loop {
            let count = handle.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            size = size.saturating_add(count as u64);
            digest.update(&buffer[..count]);
        }
        let after = std::fs::symlink_metadata(&path)?;
        if metadata_identity(&before) != metadata_identity(&opened)
            || metadata_identity(&opened) != metadata_identity(&after)
        {
            return Err(RuntimeError::Contract(format!(
                "untracked file changed while hashing: {relative}"
            )));
        }
        return Ok(json!({
            "kind": "file",
            "mode": mode,
            "path": relative,
            "sha256": format!("{:x}", digest.finalize()),
            "size": size,
        }));
    }
    if before.file_type().is_symlink() {
        let target = std::fs::read_link(&path)?;
        let after = std::fs::symlink_metadata(&path)?;
        if metadata_identity(&before) != metadata_identity(&after) {
            return Err(RuntimeError::Contract(format!(
                "untracked symlink changed while hashing: {relative}"
            )));
        }
        let data = os_str_bytes(target.as_os_str())?;
        return Ok(json!({
            "kind": "symlink",
            "mode": mode,
            "path": relative,
            "sha256": hex_digest(&data),
            "size": data.len(),
        }));
    }
    Err(RuntimeError::Contract(format!(
        "unsupported untracked file type: {relative}"
    )))
}

fn has_gitlinks(root: &Path) -> Result<bool, RuntimeError> {
    let raw = git_run(root, &["ls-files", "-s", "-z"], true)?.stdout;
    Ok(raw
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
        .any(|record| record.starts_with(b"160000 ")))
}

fn commit_has_gitlinks(root: &Path, commit: &str) -> Result<bool, RuntimeError> {
    let raw = git_run(root, &["ls-tree", "-r", "-z", commit], true)?.stdout;
    Ok(raw
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
        .any(|record| record.starts_with(b"160000 ")))
}

fn remove_exact_created_worktree(
    source_root: &Path,
    expected_common: &Path,
    worktree_path: &Path,
    branch: &str,
    base_commit: &str,
) -> Result<(), RuntimeError> {
    let (worktree, common) = resolve_worktree(worktree_path)?;
    if worktree != worktree_path
        || common != expected_common
        || worktree_status(&worktree)?
            .get("dirty")
            .and_then(Value::as_bool)
            != Some(false)
    {
        return contract("created worktree changed; refusing automatic compensation");
    }
    if resolve_commit(&worktree, "HEAD")? != base_commit {
        return contract("created worktree HEAD changed; refusing automatic compensation");
    }
    let active_branch = git_text(&worktree, &["symbolic-ref", "--short", "-q", "HEAD"])?;
    if active_branch != branch
        || resolve_commit(source_root, &format!("refs/heads/{branch}"))? != base_commit
    {
        return contract("created worktree branch changed; refusing automatic compensation");
    }
    validate_direct_branch_ref(source_root, &format!("refs/heads/{branch}"), base_commit)?;
    let worktree_text = path_text(&worktree, "session worktree path")?;
    git_run(source_root, &["worktree", "remove", worktree_text], true)?;
    delete_branch_exact(source_root, branch, base_commit)?;
    Ok(())
}

fn compensate_failed_worktree_add(
    source_root: &Path,
    expected_common: &Path,
    worktree_path: &Path,
    branch: &str,
    base_commit: &str,
) -> Result<(), RuntimeError> {
    match std::fs::symlink_metadata(worktree_path) {
        Ok(_) => remove_exact_created_worktree(
            source_root,
            expected_common,
            worktree_path,
            branch,
            base_commit,
        ),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            delete_branch_exact(source_root, branch, base_commit)
        }
        Err(error) => Err(RuntimeError::Io(error)),
    }
}

fn delete_branch_exact(
    source_root: &Path,
    branch: &str,
    base_commit: &str,
) -> Result<(), RuntimeError> {
    let branch_ref = format!("refs/heads/{branch}");
    let current = git_run(
        source_root,
        &["show-ref", "--verify", "--quiet", &branch_ref],
        false,
    )?;
    match current.status.code() {
        Some(1) => return Ok(()),
        Some(0) => {}
        _ => return contract("unable to inspect created worktree branch"),
    }
    if resolve_commit(source_root, &branch_ref)? != base_commit {
        return contract("created worktree branch changed; refusing automatic compensation");
    }
    validate_direct_branch_ref(source_root, &branch_ref, base_commit)?;
    let deleted = git_run(
        source_root,
        &["update-ref", "--no-deref", "-d", &branch_ref, base_commit],
        false,
    )?;
    if !deleted.status.success() {
        return contract("created worktree branch changed; refusing automatic compensation");
    }
    let remaining = git_run(
        source_root,
        &["show-ref", "--verify", "--quiet", &branch_ref],
        false,
    )?;
    if remaining.status.code() != Some(1) {
        return contract("created worktree branch changed; refusing automatic compensation");
    }
    Ok(())
}

fn validate_direct_branch_ref(
    source_root: &Path,
    branch_ref: &str,
    base_commit: &str,
) -> Result<(), RuntimeError> {
    let symbolic = git_run(source_root, &["symbolic-ref", "-q", branch_ref], false)?;
    if symbolic.status.code() != Some(1) || resolve_commit(source_root, branch_ref)? != base_commit
    {
        return contract("created worktree branch identity is invalid");
    }
    Ok(())
}

fn prepare_worktree_parent(input: &Path, source_root: &Path) -> Result<PathBuf, RuntimeError> {
    let path = canonicalize_missing_directory(input)?;
    if path == source_root || path.starts_with(source_root) {
        return contract("session worktree must not be nested inside the source worktree");
    }
    prepare_real_directory(&path)?;
    Ok(path)
}

fn canonicalize_missing_directory(input: &Path) -> Result<PathBuf, RuntimeError> {
    let absolute = lexical_absolute_path(input)?;
    let mut existing = absolute.as_path();
    let mut missing = Vec::new();
    loop {
        match std::fs::symlink_metadata(existing) {
            Ok(metadata) => {
                if existing == absolute && metadata.file_type().is_symlink() {
                    return contract(format!(
                        "session worktree root is missing or unsafe: {}",
                        absolute.display()
                    ));
                }
                if !metadata.is_dir() && !metadata.file_type().is_symlink() {
                    return contract(format!(
                        "session worktree root is missing or unsafe: {}",
                        absolute.display()
                    ));
                }
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let name = existing.file_name().ok_or_else(|| {
                    RuntimeError::Contract("session worktree root is invalid".to_owned())
                })?;
                missing.push(name.to_owned());
                existing = existing.parent().ok_or_else(|| {
                    RuntimeError::Contract("session worktree root is invalid".to_owned())
                })?;
            }
            Err(error) => return Err(RuntimeError::Io(error)),
        }
    }
    let mut path = std::fs::canonicalize(existing)?;
    for component in missing.iter().rev() {
        path.push(component);
    }
    Ok(path)
}

#[cfg(unix)]
fn prepare_real_directory(path: &Path) -> Result<(), RuntimeError> {
    let mut directory = Dir::open_ambient_dir("/", ambient_authority())?;
    for component in path.components() {
        match component {
            Component::RootDir | Component::CurDir => {}
            Component::Normal(part) => {
                directory = match directory.open_dir_nofollow(part) {
                    Ok(next) => next,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        match directory.create_dir(part) {
                            Ok(()) => {}
                            Err(race) if race.kind() == std::io::ErrorKind::AlreadyExists => {}
                            Err(other) => return Err(RuntimeError::Io(other)),
                        }
                        directory.open_dir_nofollow(part).map_err(|_| {
                            RuntimeError::Contract(format!(
                                "session worktree root is missing or unsafe: {}",
                                path.display()
                            ))
                        })?
                    }
                    Err(_) => {
                        return contract(format!(
                            "session worktree root is missing or unsafe: {}",
                            path.display()
                        ));
                    }
                };
            }
            Component::ParentDir | Component::Prefix(_) => {
                return contract(format!(
                    "session worktree root is missing or unsafe: {}",
                    path.display()
                ));
            }
        }
    }
    Ok(())
}

#[cfg(not(unix))]
fn prepare_real_directory(path: &Path) -> Result<(), RuntimeError> {
    std::fs::create_dir_all(path)?;
    ensure_real_directory(path, "session worktree root")
}

fn lexical_absolute_path(path: &Path) -> Result<PathBuf, RuntimeError> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::Prefix(prefix) => normalized.push(prefix.as_os_str()),
            Component::RootDir => normalized.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                if !normalized.pop() {
                    return contract("session worktree root is invalid");
                }
            }
            Component::Normal(part) => normalized.push(part),
        }
    }
    Ok(normalized)
}

fn canonical_git_path(root: &Path, args: &[&str]) -> Result<PathBuf, RuntimeError> {
    let raw = git_text(root, args)?;
    let path = PathBuf::from(raw);
    std::fs::canonicalize(&path).map_err(RuntimeError::Io)
}

fn git_text(root: &Path, args: &[&str]) -> Result<String, RuntimeError> {
    let output = git_run(root, args, true)?;
    decode_trimmed(output.stdout, "Git output")
}

fn decode_trimmed(bytes: Vec<u8>, label: &str) -> Result<String, RuntimeError> {
    let value = String::from_utf8(bytes)
        .map_err(|_| RuntimeError::Contract(format!("{label} is not valid UTF-8")))?;
    Ok(value.trim().to_owned())
}

fn git_run(root: &Path, args: &[&str], check: bool) -> Result<GitOutput, RuntimeError> {
    let mut command = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    let mut child = command
        .arg("--no-optional-locks")
        .args(["-c", "core.fsmonitor=false"])
        .args(["-c", "core.hooksPath=/dev/null"])
        .args(args)
        .current_dir(root)
        .env("LC_ALL", "C")
        .env("LANG", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(RuntimeError::Io)?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| RuntimeError::Contract("Git stdout pipe is unavailable".to_owned()))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| RuntimeError::Contract("Git stderr pipe is unavailable".to_owned()))?;
    let stdout_reader = thread::spawn(move || read_bounded(stdout, MAX_GIT_STDOUT_BYTES));
    let stderr_reader = thread::spawn(move || read_bounded(stderr, MAX_GIT_STDERR_BYTES));
    let status = child.wait()?;
    let (stdout, stdout_exceeded) = join_reader(stdout_reader)?;
    let (stderr, stderr_exceeded) = join_reader(stderr_reader)?;
    if stdout_exceeded {
        return Err(RuntimeError::Contract(format!(
            "Git stdout exceeds maximum {MAX_GIT_STDOUT_BYTES} bytes"
        )));
    }
    if stderr_exceeded {
        return Err(RuntimeError::Contract(format!(
            "Git stderr exceeds maximum {MAX_GIT_STDERR_BYTES} bytes"
        )));
    }
    if check && !status.success() {
        let summary = String::from_utf8_lossy(&stderr).trim().to_owned();
        return Err(RuntimeError::Contract(format!(
            "git command failed ({}): {}",
            args.join(" "),
            if summary.is_empty() {
                "unknown error"
            } else {
                &summary
            }
        )));
    }
    Ok(GitOutput { status, stdout })
}

fn read_bounded(
    mut reader: impl std::io::Read,
    maximum: usize,
) -> std::io::Result<(Vec<u8>, bool)> {
    let mut retained = Vec::new();
    let mut exceeded = false;
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        let remaining = maximum.saturating_add(1).saturating_sub(retained.len());
        let keep = remaining.min(count);
        retained.extend_from_slice(&buffer[..keep]);
        if keep < count || retained.len() > maximum {
            exceeded = true;
        }
    }
    if retained.len() > maximum {
        retained.truncate(maximum);
    }
    Ok((retained, exceeded))
}

fn join_reader(
    reader: thread::JoinHandle<std::io::Result<(Vec<u8>, bool)>>,
) -> Result<(Vec<u8>, bool), RuntimeError> {
    reader
        .join()
        .map_err(|_| RuntimeError::Contract("Git output reader panicked".to_owned()))?
        .map_err(RuntimeError::Io)
}

fn ensure_real_directory(path: &Path, label: &str) -> Result<(), RuntimeError> {
    let metadata = std::fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(RuntimeError::Contract(format!(
            "{label} is missing or unsafe: {}",
            path.display()
        )));
    }
    Ok(())
}

fn path_text<'a>(path: &'a Path, label: &str) -> Result<&'a str, RuntimeError> {
    path.to_str()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} is not valid UTF-8")))
}

fn validate_identifier(value: &str, label: &str) -> Result<(), RuntimeError> {
    let valid = !value.is_empty()
        && value.len() <= 128
        && value != "."
        && value != ".."
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        });
    if !valid {
        return Err(RuntimeError::Contract(format!("{label} is invalid")));
    }
    Ok(())
}

fn valid_git_oid(value: &str) -> bool {
    matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_prefixed_hash(value: &str, prefix: &str) -> bool {
    value.strip_prefix(prefix).is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    })
}

fn contract<T>(message: impl Into<String>) -> Result<T, RuntimeError> {
    Err(RuntimeError::Contract(message.into()))
}

fn repository_id(value: &Value) -> Option<&str> {
    value.get("repository_id").and_then(Value::as_str)
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, RuntimeError> {
    value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} must be an object")))
}

fn required_value<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a Value, RuntimeError> {
    value
        .get(field)
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is required")))
}

fn required_str<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a str, RuntimeError> {
    required_value(value, field, label)?
        .as_str()
        .filter(|item| !item.is_empty())
        .ok_or_else(|| RuntimeError::Contract(format!("{label} {field} is required")))
}

fn hex_digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[cfg(unix)]
fn open_read_nofollow(path: &Path) -> Result<File, RuntimeError> {
    use std::os::unix::fs::OpenOptionsExt as _;
    let mut options = std::fs::OpenOptions::new();
    options
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    Ok(options.open(path)?)
}

#[cfg(windows)]
fn open_read_nofollow(path: &Path) -> Result<File, RuntimeError> {
    use std::os::windows::fs::OpenOptionsExt as _;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    let mut options = std::fs::OpenOptions::new();
    options
        .read(true)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    Ok(options.open(path)?)
}

#[cfg(not(any(unix, windows)))]
fn open_read_nofollow(path: &Path) -> Result<File, RuntimeError> {
    Ok(File::open(path)?)
}

#[cfg(unix)]
fn metadata_mode(metadata: &Metadata) -> u32 {
    use std::os::unix::fs::MetadataExt as _;
    metadata.mode() & 0o7777
}

#[cfg(not(unix))]
fn metadata_mode(metadata: &Metadata) -> u32 {
    if metadata.permissions().readonly() {
        0o444
    } else {
        0o666
    }
}

#[cfg(unix)]
fn metadata_identity(metadata: &Metadata) -> (u64, u64, u32, u64, i64, i64) {
    use std::os::unix::fs::MetadataExt as _;
    (
        metadata.dev(),
        metadata.ino(),
        metadata.mode(),
        metadata.size(),
        metadata.mtime(),
        metadata.mtime_nsec(),
    )
}

#[cfg(not(unix))]
fn metadata_identity(metadata: &Metadata) -> (u64, bool, Option<std::time::SystemTime>) {
    (
        metadata.len(),
        metadata.permissions().readonly(),
        metadata.modified().ok(),
    )
}

#[cfg(unix)]
#[allow(clippy::unnecessary_wraps, reason = "cross-platform signature")]
fn os_str_bytes(value: &OsStr) -> Result<Vec<u8>, RuntimeError> {
    use std::os::unix::ffi::OsStrExt as _;
    Ok(value.as_bytes().to_vec())
}

#[cfg(not(unix))]
fn os_str_bytes(value: &OsStr) -> Result<Vec<u8>, RuntimeError> {
    value
        .to_str()
        .map(|text| text.as_bytes().to_vec())
        .ok_or_else(|| RuntimeError::Contract("symlink target is not valid UTF-8".to_owned()))
}
