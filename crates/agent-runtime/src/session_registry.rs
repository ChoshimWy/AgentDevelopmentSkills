//! Persistent Worktree Session Registry operations.

use crate::RuntimeError;
use crate::git_workspace::resolve_worktree;
use crate::sessions::{transition_session_context, validate_worktree_session_context};
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json, parse_json};
use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt as _};
use cap_std::ambient_authority;
use cap_std::fs::{Dir, OpenOptions};
use serde_json::{Map, Value, json};
use std::fs::File;
use std::io::{Read as _, Write as _};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// Create one validated Registry entry in `created` state.
///
/// # Errors
/// Returns an error for an unsafe Registry, duplicate id, foreign primary
/// repository, stale dependency, or invalid Session Context.
pub fn registry_create(repository: &Path, context: &Value) -> Result<Value, RuntimeError> {
    validate_worktree_session_context(context)?;
    if context.pointer("/lifecycle/state").and_then(Value::as_str) != Some("created") {
        return contract("new worktree session registry entries must start in created state");
    }
    with_registry(repository, |registry| {
        registry.validate_owner(context)?;
        registry.validate_stacked_dependencies(context)?;
        let session_id = context
            .get("session_id")
            .and_then(Value::as_str)
            .ok_or_else(|| RuntimeError::Contract("worktree session id is invalid".to_owned()))?;
        let file_name = session_file_name(session_id)?;
        if registry.entry_exists(&file_name)? {
            return contract(format!("worktree session already exists: {session_id}"));
        }
        registry.atomic_write(&file_name, context, false)?;
        Ok(context.clone())
    })
}

/// Load and validate one Registry entry.
///
/// # Errors
/// Returns an error for an unsafe/missing file, contract mismatch, or foreign
/// primary repository.
pub fn registry_load(repository: &Path, session_id: &str) -> Result<Value, RuntimeError> {
    let common_dir = registry_common(repository)?;
    let Some(registry) = open_existing_registry(common_dir)? else {
        return contract(format!("worktree session does not exist: {session_id}"));
    };
    registry.load(session_id)
}

/// List validated Registry entries sorted by filename.
///
/// # Errors
/// Returns an error for any unsafe entry, malformed context, or foreign owner.
pub fn registry_list(repository: &Path) -> Result<Value, RuntimeError> {
    let common_dir = registry_common(repository)?;
    let Some(registry) = open_existing_registry(common_dir)? else {
        return Ok(Value::Array(Vec::new()));
    };
    Ok(Value::Array(registry.list()?))
}

/// Replace one existing entry after immutable-identity and transition checks.
///
/// # Errors
/// Returns an error for stale dependencies, immutable drift, illegal state
/// changes, or unsafe persistence.
pub fn registry_write(repository: &Path, context: &Value) -> Result<Value, RuntimeError> {
    validate_worktree_session_context(context)?;
    with_registry(repository, |registry| {
        let session_id = context
            .get("session_id")
            .and_then(Value::as_str)
            .ok_or_else(|| RuntimeError::Contract("worktree session id is invalid".to_owned()))?;
        let current = registry.load(session_id)?;
        if current.get("created_at") != context.get("created_at")
            || current.get("project_id") != context.get("project_id")
            || immutable_identity(&current)? != immutable_identity(context)?
        {
            return contract("worktree session immutable registry identity changed");
        }
        let current_state = state(&current)?;
        let next_state = state(context)?;
        if current_state != next_state && !legal_transition(current_state, next_state) {
            return contract(format!(
                "illegal worktree session transition: {current_state} -> {next_state}"
            ));
        }
        registry.validate_stacked_dependencies(context)?;
        registry.atomic_write(&session_file_name(session_id)?, context, true)?;
        Ok(context.clone())
    })
}

/// Apply and persist one legal non-gated Registry lifecycle transition.
///
/// # Errors
/// Returns an error for illegal transitions, unsafe persistence, or live
/// repository refresh failures.
pub fn registry_transition(
    repository: &Path,
    session_id: &str,
    target: &str,
) -> Result<Value, RuntimeError> {
    with_registry(repository, |registry| {
        let context = registry.load(session_id)?;
        let candidate = transition_session_context(&context, target)?;
        registry.atomic_write(&session_file_name(session_id)?, &candidate, true)?;
        Ok(candidate)
    })
}

struct Registry {
    common_dir: PathBuf,
    directory: Dir,
    _lock: Option<File>,
}

impl Registry {
    fn load(&self, session_id: &str) -> Result<Value, RuntimeError> {
        let file_name = session_file_name(session_id)?;
        let value = self.read_entry(&file_name).map_err(|error| match error {
            RuntimeError::Io(ref source) if source.kind() == std::io::ErrorKind::NotFound => {
                RuntimeError::Contract(format!("worktree session does not exist: {session_id}"))
            }
            other => other,
        })?;
        validate_worktree_session_context(&value)?;
        if value.get("session_id").and_then(Value::as_str) != Some(session_id) {
            return contract("worktree session registry identity mismatch");
        }
        self.validate_owner(&value)?;
        Ok(value)
    }

    fn list(&self) -> Result<Vec<Value>, RuntimeError> {
        let mut names = Vec::new();
        for entry in self.directory.entries()? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_str().ok_or_else(|| {
                RuntimeError::Contract(
                    "worktree session registry filename is not valid UTF-8".to_owned(),
                )
            })?;
            #[allow(
                clippy::case_sensitive_file_extension_comparisons,
                reason = "Registry contract owns the exact lowercase .json suffix"
            )]
            if name.ends_with(".json") {
                names.push(name.to_owned());
            }
        }
        names.sort();
        let mut result = Vec::with_capacity(names.len());
        for name in names {
            let metadata = self.directory.symlink_metadata(&name)?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return contract(format!("worktree session registry entry is unsafe: {name}"));
            }
            let value = self.read_entry(&name)?;
            validate_worktree_session_context(&value)?;
            let expected_id = name.strip_suffix(".json").unwrap_or_default();
            if value.get("session_id").and_then(Value::as_str) != Some(expected_id) {
                return contract("worktree session registry filename mismatch");
            }
            self.validate_owner(&value)?;
            result.push(value);
        }
        Ok(result)
    }

    fn validate_owner(&self, context: &Value) -> Result<(), RuntimeError> {
        let repositories = context
            .get("repositories")
            .and_then(Value::as_array)
            .ok_or_else(|| RuntimeError::Contract("session repositories are invalid".to_owned()))?;
        let primary = repositories
            .iter()
            .find(|repository| repository.get("role").and_then(Value::as_str) == Some("primary"))
            .ok_or_else(|| {
                RuntimeError::Contract(
                    "worktree-session-context requires exactly one primary repository".to_owned(),
                )
            })?;
        let common = primary
            .get("git_common_dir")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                RuntimeError::Contract("session primary git common dir is invalid".to_owned())
            })?;
        let resolved = std::fs::canonicalize(common)?;
        if resolved != self.common_dir {
            return contract(
                "worktree session primary repository does not belong to this registry",
            );
        }
        Ok(())
    }

    fn validate_stacked_dependencies(&self, context: &Value) -> Result<(), RuntimeError> {
        let dependencies = context
            .get("dependencies")
            .and_then(Value::as_array)
            .ok_or_else(|| RuntimeError::Contract("session dependencies are invalid".to_owned()))?;
        for dependency in dependencies {
            let session_id = dependency
                .get("session_id")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    RuntimeError::Contract("session dependency id is invalid".to_owned())
                })?;
            let upstream = self.load(session_id)?;
            if upstream
                .pointer("/source_identity/mode")
                .and_then(Value::as_str)
                != Some("committed")
            {
                return contract("stacked dependency must reference a committed checkpoint");
            }
            if upstream.pointer("/source_identity/value")
                != dependency.get("required_source_identity")
            {
                return contract("stacked dependency source identity is stale");
            }
        }
        Ok(())
    }

    fn entry_exists(&self, file_name: &str) -> Result<bool, RuntimeError> {
        match self.directory.symlink_metadata(file_name) {
            Ok(_) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(RuntimeError::Io(error)),
        }
    }

    fn read_entry(&self, file_name: &str) -> Result<Value, RuntimeError> {
        let metadata = self.directory.symlink_metadata(file_name)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return contract(format!(
                "worktree session registry entry is unsafe: {file_name}"
            ));
        }
        if metadata.len() > MAX_CONTRACT_JSON_BYTES as u64 {
            return contract(format!(
                "contract input has more than {MAX_CONTRACT_JSON_BYTES} bytes"
            ));
        }
        let mut options = OpenOptions::new();
        options.read(true);
        configure_private_nofollow(&mut options);
        let mut file = self.directory.open_with(file_name, &options)?.into_std();
        let opened = file.metadata()?;
        if metadata_identity_cap(&metadata) != metadata_identity_std(&opened) {
            return contract("worktree session registry entry changed while opening");
        }
        let mut bytes =
            Vec::with_capacity(usize::try_from(metadata.len()).unwrap_or(MAX_CONTRACT_JSON_BYTES));
        std::io::Read::by_ref(&mut file)
            .take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        if bytes.len() > MAX_CONTRACT_JSON_BYTES {
            return contract(format!(
                "contract input has more than {MAX_CONTRACT_JSON_BYTES} bytes"
            ));
        }
        let current = self.directory.symlink_metadata(file_name)?;
        if current.file_type().is_symlink()
            || !current.is_file()
            || metadata_identity_std(&opened) != metadata_identity_cap(&current)
        {
            return contract("worktree session registry entry changed while reading");
        }
        Ok(parse_json(&bytes)?)
    }

    fn atomic_write(
        &self,
        file_name: &str,
        value: &Value,
        replace: bool,
    ) -> Result<(), RuntimeError> {
        let bytes = canonical_json(value)?;
        let temporary = format!(
            ".{file_name}.{}.{}.tmp",
            std::process::id(),
            TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        );
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        configure_private_nofollow(&mut options);
        let temporary_file = self.directory.open_with(&temporary, &options);
        let mut temporary_file = temporary_file.map_err(RuntimeError::Io)?.into_std();
        let write_result = (|| -> Result<(), RuntimeError> {
            temporary_file.write_all(&bytes)?;
            temporary_file.sync_all()?;
            match self.directory.symlink_metadata(file_name) {
                Ok(metadata) => {
                    if metadata.file_type().is_symlink() || !metadata.is_file() {
                        return contract("worktree session registry destination is unsafe");
                    }
                    if !replace {
                        return contract("worktree session registry destination already exists");
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    if replace {
                        return contract("worktree session registry destination is missing");
                    }
                }
                Err(error) => return Err(RuntimeError::Io(error)),
            }
            self.directory
                .rename(&temporary, &self.directory, file_name)?;
            self.directory.try_clone()?.into_std_file().sync_all()?;
            Ok(())
        })();
        if write_result.is_err() {
            let _ = self.directory.remove_file(&temporary);
        }
        write_result
    }
}

fn with_registry<T>(
    repository: &Path,
    operation: impl FnOnce(&Registry) -> Result<T, RuntimeError>,
) -> Result<T, RuntimeError> {
    let common_dir = registry_common(repository)?;
    let directory_path = common_dir.join("agent-sessions");
    create_private_directory(&directory_path)?;
    let directory_metadata = std::fs::symlink_metadata(&directory_path)?;
    if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
        return contract("worktree session registry directory is unsafe");
    }
    let directory = Dir::open_ambient_dir(&directory_path, ambient_authority())?;
    if metadata_identity_std(&directory_metadata)
        != metadata_identity_cap(&directory.dir_metadata()?)
    {
        return contract("worktree session registry directory changed while opening");
    }
    let lock_name = ".registry.lock";
    match directory.symlink_metadata(lock_name) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return contract("worktree session registry lock is unsafe");
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(RuntimeError::Io(error)),
    }
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    configure_private_nofollow(&mut options);
    let lock = directory.open_with(lock_name, &options)?.into_std();
    let opened = lock.metadata()?;
    let current = directory.symlink_metadata(lock_name)?;
    if current.file_type().is_symlink()
        || !current.is_file()
        || metadata_identity_std(&opened) != metadata_identity_cap(&current)
    {
        return contract("worktree session registry lock is unsafe");
    }
    lock.lock()
        .map_err(|_| RuntimeError::Contract("worktree session registry lock failed".to_owned()))?;
    let registry = Registry {
        common_dir,
        directory,
        _lock: Some(lock),
    };
    operation(&registry)
}

fn open_existing_registry(common_dir: PathBuf) -> Result<Option<Registry>, RuntimeError> {
    let directory_path = common_dir.join("agent-sessions");
    let metadata = match std::fs::symlink_metadata(&directory_path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(RuntimeError::Io(error)),
    };
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return contract("worktree session registry directory is unsafe");
    }
    let directory = Dir::open_ambient_dir(&directory_path, ambient_authority())?;
    if metadata_identity_std(&metadata) != metadata_identity_cap(&directory.dir_metadata()?) {
        return contract("worktree session registry directory changed while opening");
    }
    Ok(Some(Registry {
        common_dir,
        directory,
        _lock: None,
    }))
}

fn registry_common(repository: &Path) -> Result<PathBuf, RuntimeError> {
    let candidate = std::fs::canonicalize(repository)?;
    let metadata = std::fs::symlink_metadata(&candidate)?;
    if candidate.file_name().and_then(|name| name.to_str()) == Some(".git")
        && metadata.is_dir()
        && !metadata.file_type().is_symlink()
    {
        Ok(candidate)
    } else {
        resolve_worktree(&candidate).map(|(_, common)| common)
    }
}

fn session_file_name(session_id: &str) -> Result<String, RuntimeError> {
    if !valid_identifier(session_id) {
        return contract("worktree session id is invalid");
    }
    Ok(format!("{session_id}.json"))
}

fn immutable_identity(context: &Value) -> Result<Value, RuntimeError> {
    let context = object(context, "worktree-session-context")?;
    let repositories = context
        .get("repositories")
        .and_then(Value::as_array)
        .ok_or_else(|| RuntimeError::Contract("session repositories are invalid".to_owned()))?;
    let repositories = repositories
        .iter()
        .map(|repository| {
            let repository = object(repository, "session repository")?;
            Ok(json!({
                "base": repository.get("base").cloned().unwrap_or(Value::Null),
                "branch": repository.get("branch").cloned().unwrap_or(Value::Null),
                "git_common_dir": repository.get("git_common_dir").cloned().unwrap_or(Value::Null),
                "repository_id": repository.get("repository_id").cloned().unwrap_or(Value::Null),
                "role": repository.get("role").cloned().unwrap_or(Value::Null),
                "worktree_path": repository.get("worktree_path").cloned().unwrap_or(Value::Null),
            }))
        })
        .collect::<Result<Vec<_>, RuntimeError>>()?;
    Ok(json!({
        "created_at": context.get("created_at").cloned().unwrap_or(Value::Null),
        "capability_closure": context.get("capability_closure").cloned().unwrap_or(Value::Null),
        "dependencies": context.get("dependencies").cloned().unwrap_or(Value::Null),
        "project_id": context.get("project_id").cloned().unwrap_or(Value::Null),
        "platform_contexts": context.get("platform_contexts").cloned().unwrap_or(Value::Null),
        "repositories": repositories,
        "selected_platforms": context.get("selected_platforms").cloned().unwrap_or(Value::Null),
        "session_id": context.get("session_id").cloned().unwrap_or(Value::Null),
    }))
}

fn state(context: &Value) -> Result<&str, RuntimeError> {
    context
        .pointer("/lifecycle/state")
        .and_then(Value::as_str)
        .ok_or_else(|| RuntimeError::Contract("session lifecycle is missing".to_owned()))
}

fn legal_transition(current: &str, target: &str) -> bool {
    match current {
        "created" => matches!(target, "active" | "blocked"),
        "active" => matches!(target, "checkpointed" | "blocked"),
        "checkpointed" => matches!(target, "active" | "gated" | "blocked"),
        "gated" => matches!(target, "integrated" | "blocked"),
        "integrated" => matches!(target, "closed" | "blocked"),
        "blocked" => matches!(target, "active" | "closed"),
        _ => false,
    }
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value != "."
        && value != ".."
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        })
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, RuntimeError> {
    value
        .as_object()
        .ok_or_else(|| RuntimeError::Contract(format!("{label} must be an object")))
}

#[cfg(unix)]
fn metadata_identity_std(metadata: &std::fs::Metadata) -> (u64, u64, u32, u64, i64, i64, i64, i64) {
    use std::os::unix::fs::MetadataExt as _;
    (
        metadata.dev(),
        metadata.ino(),
        metadata.mode(),
        metadata.size(),
        metadata.mtime(),
        metadata.mtime_nsec(),
        metadata.ctime(),
        metadata.ctime_nsec(),
    )
}

#[cfg(unix)]
fn metadata_identity_cap(
    metadata: &cap_std::fs::Metadata,
) -> (u64, u64, u32, u64, i64, i64, i64, i64) {
    use cap_std::fs::MetadataExt as _;
    (
        metadata.dev(),
        metadata.ino(),
        metadata.mode(),
        metadata.size(),
        metadata.mtime(),
        metadata.mtime_nsec(),
        metadata.ctime(),
        metadata.ctime_nsec(),
    )
}

#[cfg(not(unix))]
fn metadata_identity_std(
    metadata: &std::fs::Metadata,
) -> (u64, bool, Option<std::time::SystemTime>) {
    (
        metadata.len(),
        metadata.permissions().readonly(),
        metadata.modified().ok(),
    )
}

#[cfg(not(unix))]
fn metadata_identity_cap(
    metadata: &cap_std::fs::Metadata,
) -> (u64, bool, Option<std::time::SystemTime>) {
    (
        metadata.len(),
        metadata.permissions().readonly(),
        metadata.modified().ok(),
    )
}

#[cfg(unix)]
fn configure_private_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    options
        .follow(FollowSymlinks::No)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
}

#[cfg(windows)]
fn configure_private_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    options
        .follow(FollowSymlinks::No)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(unix, windows)))]
fn configure_private_nofollow(options: &mut OpenOptions) {
    options.follow(FollowSymlinks::No);
}

#[cfg(unix)]
fn create_private_directory(path: &Path) -> Result<(), RuntimeError> {
    use std::os::unix::fs::DirBuilderExt as _;
    let mut builder = std::fs::DirBuilder::new();
    builder.mode(0o700);
    match builder.create(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(RuntimeError::Io(error)),
    }
}

#[cfg(not(unix))]
fn create_private_directory(path: &Path) -> Result<(), RuntimeError> {
    match std::fs::create_dir(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(RuntimeError::Io(error)),
    }
}

fn contract<T>(message: impl Into<String>) -> Result<T, RuntimeError> {
    Err(RuntimeError::Contract(message.into()))
}
