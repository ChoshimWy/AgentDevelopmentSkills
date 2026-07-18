//! Read-only Git workspace and Worktree Session source-identity primitives.

use crate::RuntimeError;
use agent_contracts::canonical_sha256;
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
