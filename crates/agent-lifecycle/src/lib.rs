//! Read-only lifecycle diagnostics for the native migration path.
//!
//! This crate deliberately starts with the non-mutating Doctor boundary. It
//! does not install, upgrade, roll back, or remove managed content.

use agent_contracts::{ContractError, MAX_CONTRACT_JSON_BYTES, canonical_sha256, parse_json};
use agent_engine::{
    install_plan_identity_hash, schema_inventory, validate_install_plan, validate_package_lock,
};
use cap_fs_ext::{DirExt as _, FollowSymlinks, OpenOptionsFollowExt as _};
use cap_std::ambient_authority;
use cap_std::fs::{Dir, OpenOptions};
use serde_json::{Value, json};
use std::io::Read as _;
use std::path::{Path, PathBuf};
use thiserror::Error;

const MANAGED_DIRECTORY_MODE: u32 = 0o755;
const MANAGED_FILE_MODE: u32 = 0o644;
const PERSISTENT_PACKAGE_LOCK: &str = "agent-skills.lock";
const EXTERNAL_ACTIVATION_LOCK: &str = "activation-lock.json";
const ROLLBACK_POINT_DIRECTORY: &str = "rollback-point";
const LIFECYCLE_LOCK_DIRECTORY: &str = ".agent-skills-lifecycle.lock";

const RECOVERY_PREFIXES: [(&str, &str); 3] = [
    (".agent-skills-backup-", "install-backup"),
    (".agent-skills-stage-", "install-stage"),
    (".agent-skills-uninstall-backup-", "uninstall-backup"),
];

/// Native lifecycle inspection failures.
#[derive(Debug, Error)]
pub enum LifecycleError {
    #[error(transparent)]
    Contract(#[from] ContractError),
    #[error(transparent)]
    Engine(#[from] agent_engine::EngineError),
    #[error("lifecycle input cannot be read: {0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Invalid(String),
}

#[derive(Debug)]
struct BaselineState {
    checks: Vec<Value>,
    install_lock: Option<Value>,
    package_lock: Option<Value>,
    recovery_candidates: Vec<Value>,
    recovery_unknown: bool,
}

impl BaselineState {
    fn new() -> Self {
        Self {
            checks: Vec::new(),
            install_lock: None,
            package_lock: None,
            recovery_candidates: Vec::new(),
            recovery_unknown: true,
        }
    }

    fn record(
        &mut self,
        check_id: &str,
        category: &str,
        status: &str,
        summary: &str,
        details: Value,
    ) {
        let mut check = json!({
            "category": category,
            "details": null,
            "id": check_id,
            "status": status,
            "summary": summary,
        });
        check["details"] = details;
        self.checks.push(check);
    }

    fn failed(
        &mut self,
        check_id: &str,
        category: &str,
        summary: &str,
        error: impl std::fmt::Display,
    ) {
        let message = error.to_string();
        self.record(
            check_id,
            category,
            "failed",
            summary,
            json!({
                "errors": [
                    if message.is_empty() {
                        "lifecycle inspection failed"
                    } else {
                        &message
                    }
                ]
            }),
        );
    }
}

/// Inspect the first fail-closed Doctor boundary without modifying the target.
///
/// The returned projection contains the existing Doctor check records for the
/// safe-target, recovery-residue, managed-layout, Install Lock, persistent
/// Lockfile, and Schema inventory checks. It is intentionally a compatibility
/// probe rather than a new public artifact schema.
///
/// Target and managed directories are held as directory capabilities. Contract
/// files are opened without following symlinks and their identities are checked
/// before and after the open, so a concurrent replacement cannot redirect a
/// diagnostic read outside the inspected tree.
///
/// # Errors
/// Returns an error only when the canonical projection itself cannot be
/// constructed. Individual diagnostic failures are represented as check
/// records, matching the Python Doctor behavior.
#[allow(clippy::too_many_lines)]
pub fn inspect_doctor_baseline(
    target_root: impl AsRef<Path>,
    schema_root: impl AsRef<Path>,
) -> Result<Value, LifecycleError> {
    let target = absolute_path(target_root.as_ref())?;
    let schemas = absolute_path(schema_root.as_ref())?;
    let mut state = BaselineState::new();

    let target_directory =
        match open_root_directory(&target, None, "install target").and_then(|directory| {
            directory.entries()?;
            Ok(directory)
        }) {
            Ok(directory) => {
                state.record(
                    "filesystem.target",
                    "filesystem",
                    "passed",
                    "Install target is a safe directory",
                    json!({"path": target}),
                );
                Some(directory)
            }
            Err(error) => {
                state.failed(
                    "filesystem.target",
                    "filesystem",
                    "Install target is a safe directory",
                    error,
                );
                None
            }
        };

    inspect_recovery(target_directory.as_ref(), &mut state);

    if let Some(target_directory) = target_directory.as_ref() {
        match check_layout(target_directory) {
            Ok(details) => state.record(
                "filesystem.layout",
                "filesystem",
                "passed",
                "Managed root layout and modes are canonical",
                details,
            ),
            Err(error) => state.failed(
                "filesystem.layout",
                "filesystem",
                "Managed root layout and modes are canonical",
                error,
            ),
        }
    } else {
        state.record(
            "filesystem.layout",
            "filesystem",
            "skipped",
            "Managed root layout requires a safe install target",
            json!({}),
        );
    }

    if let Some(target_directory) = target_directory.as_ref() {
        let result = open_child_directory(
            target_directory,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )
        .and_then(|managed| load_install_lock(&managed));
        match result {
            Ok(value) => {
                let details = json!({
                    "fingerprint": value.get("fingerprint").cloned().unwrap_or(Value::Null),
                    "lock_schema_version": value.get("lock_schema_version").cloned().unwrap_or(Value::Null),
                });
                state.install_lock = Some(value);
                state.record(
                    "install.lock",
                    "install",
                    "passed",
                    "Install Lock is valid and installed",
                    details,
                );
            }
            Err(error) => state.failed(
                "install.lock",
                "install",
                "Install Lock is valid and installed",
                error,
            ),
        }
    } else {
        state.record(
            "install.lock",
            "install",
            "skipped",
            "Install Lock check requires a safe install target",
            json!({}),
        );
    }

    if let (Some(target_directory), Some(install_lock)) =
        (target_directory.as_ref(), state.install_lock.clone())
    {
        let result = open_child_directory(
            target_directory,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )
        .and_then(|managed| load_package_lock(&managed, &install_lock));
        match result {
            Ok(value) => {
                let details = json!({
                    "fingerprint": value.get("fingerprint").cloned().unwrap_or(Value::Null),
                    "previous": value.pointer("/lineage/previous_lock_hash").cloned().unwrap_or(Value::Null),
                });
                state.package_lock = Some(value);
                state.record(
                    "lock.persistent",
                    "lock",
                    "passed",
                    "Persistent Lockfile is valid and anchored",
                    details,
                );
            }
            Err(error) => state.failed(
                "lock.persistent",
                "lock",
                "Persistent Lockfile is valid and anchored",
                error,
            ),
        }
    } else {
        state.record(
            "lock.persistent",
            "lock",
            "skipped",
            "Persistent Lockfile check requires a valid Install Lock",
            json!({}),
        );
    }

    if let Some(package_lock) = state.package_lock.as_ref() {
        match check_schema_inventory(&schemas, package_lock) {
            Ok(details) => state.record(
                "schema.inventory",
                "schema",
                "passed",
                "Runtime Schema inventory matches the package Lockfile",
                details,
            ),
            Err(error) => state.failed(
                "schema.inventory",
                "schema",
                "Runtime Schema inventory matches the package Lockfile",
                error,
            ),
        }
    } else {
        state.record(
            "schema.inventory",
            "schema",
            "skipped",
            "Schema inventory check requires a valid package Lockfile",
            json!({}),
        );
    }

    let install = json!({
        "install_plan_fingerprint": state.install_lock.as_ref()
            .and_then(|value| value.get("fingerprint")).cloned().unwrap_or(Value::Null),
        "package_lock_hash": state.package_lock.as_ref()
            .and_then(|value| value.get("fingerprint")).cloned().unwrap_or(Value::Null),
        "selected_disciplines": state.install_lock.as_ref()
            .and_then(|value| value.get("selected_disciplines")).cloned().unwrap_or_else(|| json!([])),
        "selected_platforms": state.install_lock.as_ref()
            .and_then(|value| value.get("selected_platforms")).cloned().unwrap_or_else(|| json!([])),
        "selected_runtime_configs": state.install_lock.as_ref()
            .and_then(|value| value.get("selected_runtime_configs")).cloned().unwrap_or_else(|| json!([])),
    });
    let recovery_status = if state.recovery_unknown {
        "unknown"
    } else if state.recovery_candidates.is_empty() {
        "clean"
    } else {
        "attention"
    };
    let recovery = json!({
        "candidates": state.recovery_candidates,
        "status": recovery_status,
    });
    let mut projection = json!({
        "checks": state.checks,
        "install": install,
        "recovery": recovery,
        "target_root": target,
    });
    let fingerprint = canonical_sha256(&projection)?;
    projection
        .as_object_mut()
        .ok_or_else(|| LifecycleError::Invalid("Doctor baseline projection is invalid".to_owned()))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    Ok(projection)
}

fn inspect_recovery(target: Option<&Dir>, state: &mut BaselineState) {
    let Some(target) = target else {
        state.record(
            "recovery.residue",
            "recovery",
            "skipped",
            "Recovery residue check requires a safe install target",
            json!({}),
        );
        return;
    };

    match recovery_candidates(target) {
        Ok(candidates) => {
            state.recovery_unknown = false;
            state.recovery_candidates = candidates;
            if state.recovery_candidates.is_empty() {
                state.record(
                    "recovery.residue",
                    "recovery",
                    "passed",
                    "No interrupted lifecycle transaction residue was found",
                    json!({}),
                );
            } else {
                state.record(
                    "recovery.residue",
                    "recovery",
                    "failed",
                    "Interrupted lifecycle transaction residue requires attention",
                    json!({"candidates": state.recovery_candidates}),
                );
            }
        }
        Err(error) => {
            state.recovery_unknown = true;
            state.failed(
                "recovery.residue",
                "recovery",
                "Lifecycle recovery residue could not be inspected",
                error,
            );
        }
    }
}

fn recovery_candidates(target: &Dir) -> Result<Vec<Value>, LifecycleError> {
    let mut candidates = Vec::new();
    for entry in target.entries()? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name == LIFECYCLE_LOCK_DIRECTORY {
            candidates.push(json!({"kind": "lifecycle-lock", "path": name}));
            continue;
        }
        if let Some((_, kind)) = RECOVERY_PREFIXES
            .iter()
            .find(|(prefix, _)| name.starts_with(prefix))
        {
            candidates.push(json!({"kind": kind, "path": name}));
        }
    }
    candidates.sort_by(|left, right| {
        left.get("path")
            .and_then(Value::as_str)
            .cmp(&right.get("path").and_then(Value::as_str))
    });
    Ok(candidates)
}

fn check_layout(target: &Dir) -> Result<Value, LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    drop(open_child_file(
        target,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        "global AGENTS.md",
    )?);
    drop(open_child_directory(
        target,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed skills directory",
    )?);

    let mut entries = Vec::new();
    for entry in managed.entries()? {
        let entry = entry?;
        if ignored_os_metadata(&managed, &entry.file_name())? {
            continue;
        }
        entries.push(entry.file_name().to_string_lossy().into_owned());
    }
    entries.sort();
    let mut expected = vec![
        PERSISTENT_PACKAGE_LOCK.to_owned(),
        "install-lock.json".to_owned(),
        "packages".to_owned(),
    ];
    if entries
        .iter()
        .any(|entry| entry == EXTERNAL_ACTIVATION_LOCK)
    {
        expected.push(EXTERNAL_ACTIVATION_LOCK.to_owned());
    }
    if entries
        .iter()
        .any(|entry| entry == ROLLBACK_POINT_DIRECTORY)
    {
        expected.push(ROLLBACK_POINT_DIRECTORY.to_owned());
    }
    expected.sort();
    if entries != expected {
        return Err(LifecycleError::Invalid(
            "managed metadata contains missing or unknown entries".to_owned(),
        ));
    }
    Ok(json!({"managed_entries": entries}))
}

fn load_install_lock(managed: &Dir) -> Result<Value, LifecycleError> {
    let value = load_json_file(
        managed,
        "install-lock.json",
        MANAGED_FILE_MODE,
        "Install Lock",
    )?;
    validate_install_plan(&value)?;
    if value.get("status").and_then(Value::as_str) != Some("installed") {
        return Err(LifecycleError::Invalid(
            "Install Lock status is not installed".to_owned(),
        ));
    }
    Ok(value)
}

fn load_package_lock(managed: &Dir, install_lock: &Value) -> Result<Value, LifecycleError> {
    let value = load_json_file(
        managed,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "persistent package Lockfile",
    )?;
    validate_package_lock(&value)?;
    if value.get("fingerprint") != install_lock.get("package_lock_hash") {
        return Err(LifecycleError::Invalid(
            "persistent package Lockfile fingerprint differs from Install Lock".to_owned(),
        ));
    }
    if value
        .get("install_plan_identity_hash")
        .and_then(Value::as_str)
        != Some(install_plan_identity_hash(install_lock)?.as_str())
    {
        return Err(LifecycleError::Invalid(
            "persistent package Lockfile Install Plan identity differs".to_owned(),
        ));
    }
    Ok(value)
}

fn check_schema_inventory(schemas: &Path, package_lock: &Value) -> Result<Value, LifecycleError> {
    let current = schema_inventory(schemas)?;
    if package_lock.get("schema_inventory") != Some(&current) {
        return Err(LifecycleError::Invalid(
            "runtime Schema inventory differs from persistent package Lockfile".to_owned(),
        ));
    }
    Ok(json!({
        "content_sha256": current.get("content_sha256").cloned().unwrap_or(Value::Null),
        "file_count": current.get("files").and_then(Value::as_array).map_or(0, Vec::len),
    }))
}

fn open_root_directory(path: &Path, mode: Option<u32>, label: &str) -> Result<Dir, LifecycleError> {
    let before = std::fs::symlink_metadata(path)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_dir() {
        return Err(LifecycleError::Invalid(format!(
            "{label} is missing or unsafe"
        )));
    }
    if let Some(mode) = mode {
        require_std_mode(&before, mode, label)?;
    }
    let directory = Dir::open_ambient_dir(path, ambient_authority())
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    let opened = directory.dir_metadata()?;
    let after = std::fs::symlink_metadata(path)
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    if after.file_type().is_symlink()
        || !same_object_std_cap(&before, &opened)
        || !same_object_std_cap(&after, &opened)
    {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while opening"
        )));
    }
    Ok(directory)
}

fn open_child_directory(
    parent: &Dir,
    name: &str,
    mode: Option<u32>,
    label: &str,
) -> Result<Dir, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_dir() {
        return Err(LifecycleError::Invalid(format!(
            "{label} is missing or unsafe"
        )));
    }
    if let Some(mode) = mode {
        require_cap_mode(&before, mode, label)?;
    }
    let directory = parent
        .open_dir_nofollow(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    let opened = directory.dir_metadata()?;
    let after = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    if after.file_type().is_symlink()
        || !same_object_cap(&before, &opened)
        || !same_object_cap(&after, &opened)
    {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while opening"
        )));
    }
    Ok(directory)
}

fn open_child_file(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<cap_std::fs::File, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_file() {
        return Err(LifecycleError::Invalid(format!(
            "{label} is missing or unsafe"
        )));
    }
    require_cap_mode(&before, mode, label)?;
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    let file = parent
        .open_with(name, &options)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    let opened = file.metadata()?;
    let after = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    if after.file_type().is_symlink()
        || !after.is_file()
        || !same_object_cap(&before, &opened)
        || !same_object_cap(&after, &opened)
    {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while opening"
        )));
    }
    Ok(file)
}

fn load_json_file(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<Value, LifecycleError> {
    let mut file = open_child_file(parent, name, mode, label)?;
    let opened = file.metadata()?;
    let length = opened.len();
    if length > MAX_CONTRACT_JSON_BYTES as u64 {
        return Err(ContractError::InputTooLarge {
            maximum: MAX_CONTRACT_JSON_BYTES,
        }
        .into());
    }
    let mut bytes = Vec::with_capacity(
        usize::try_from(length)
            .unwrap_or(MAX_CONTRACT_JSON_BYTES)
            .min(MAX_CONTRACT_JSON_BYTES),
    );
    file.by_ref()
        .take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_CONTRACT_JSON_BYTES {
        return Err(ContractError::InputTooLarge {
            maximum: MAX_CONTRACT_JSON_BYTES,
        }
        .into());
    }
    let after_file = file.metadata()?;
    let after_path = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while reading")))?;
    if after_path.file_type().is_symlink()
        || !after_path.is_file()
        || !same_object_cap(&opened, &after_file)
        || !same_object_cap(&opened, &after_path)
        || !same_content_state_cap(&opened, &after_file)
        || !same_content_state_cap(&opened, &after_path)
    {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while reading"
        )));
    }
    Ok(parse_json(&bytes)?)
}

fn ignored_os_metadata(parent: &Dir, name: &std::ffi::OsStr) -> Result<bool, LifecycleError> {
    if name != ".DS_Store" {
        return Ok(false);
    }
    let metadata = parent.symlink_metadata(name)?;
    Ok(!metadata.file_type().is_symlink() && metadata.is_file())
}

#[cfg(unix)]
fn configure_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
}

#[cfg(windows)]
fn configure_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(unix, windows)))]
fn configure_nofollow(_options: &mut OpenOptions) {}

#[cfg(unix)]
fn require_std_mode(
    metadata: &std::fs::Metadata,
    expected: u32,
    label: &str,
) -> Result<(), LifecycleError> {
    use std::os::unix::fs::MetadataExt as _;
    if metadata.mode() & 0o777 != expected {
        return Err(LifecycleError::Invalid(format!(
            "{label} mode is not canonical"
        )));
    }
    Ok(())
}

#[cfg(not(unix))]
fn require_std_mode(
    _metadata: &std::fs::Metadata,
    _expected: u32,
    _label: &str,
) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
fn require_cap_mode(
    metadata: &cap_std::fs::Metadata,
    expected: u32,
    label: &str,
) -> Result<(), LifecycleError> {
    use cap_std::fs::MetadataExt as _;
    if metadata.mode() & 0o777 != expected {
        return Err(LifecycleError::Invalid(format!(
            "{label} mode is not canonical"
        )));
    }
    Ok(())
}

#[cfg(not(unix))]
fn require_cap_mode(
    _metadata: &cap_std::fs::Metadata,
    _expected: u32,
    _label: &str,
) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
fn same_object_std_cap(standard: &std::fs::Metadata, capability: &cap_std::fs::Metadata) -> bool {
    use cap_std::fs::MetadataExt as _;
    use std::os::unix::fs::MetadataExt as _;
    standard.dev() == capability.dev() && standard.ino() == capability.ino()
}

#[cfg(unix)]
fn same_object_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    use cap_std::fs::MetadataExt as _;
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(unix)]
fn same_content_state_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    use cap_std::fs::MetadataExt as _;
    left.size() == right.size()
        && left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime() == right.ctime()
        && left.ctime_nsec() == right.ctime_nsec()
}

#[cfg(windows)]
fn same_object_std_cap(standard: &std::fs::Metadata, capability: &cap_std::fs::Metadata) -> bool {
    use cap_std::fs::MetadataExt as _;
    use std::os::windows::fs::MetadataExt as _;
    matches!(
        (
            standard.volume_serial_number(),
            standard.file_index(),
            capability.volume_serial_number(),
            capability.file_index(),
        ),
        (Some(left_volume), Some(left_index), Some(right_volume), Some(right_index))
            if left_volume == right_volume && left_index == right_index
    )
}

#[cfg(windows)]
fn same_object_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    use cap_std::fs::MetadataExt as _;
    matches!(
        (
            left.volume_serial_number(),
            left.file_index(),
            right.volume_serial_number(),
            right.file_index(),
        ),
        (Some(left_volume), Some(left_index), Some(right_volume), Some(right_index))
            if left_volume == right_volume && left_index == right_index
    )
}

#[cfg(windows)]
fn same_content_state_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    left.len() == right.len()
        && left.modified().ok().is_some()
        && left.modified().ok() == right.modified().ok()
}

#[cfg(not(any(unix, windows)))]
fn same_object_std_cap(_standard: &std::fs::Metadata, _capability: &cap_std::fs::Metadata) -> bool {
    false
}

#[cfg(not(any(unix, windows)))]
fn same_object_cap(_left: &cap_std::fs::Metadata, _right: &cap_std::fs::Metadata) -> bool {
    false
}

#[cfg(not(any(unix, windows)))]
fn same_content_state_cap(_left: &cap_std::fs::Metadata, _right: &cap_std::fs::Metadata) -> bool {
    false
}

fn absolute_path(path: &Path) -> Result<PathBuf, LifecycleError> {
    if path.is_absolute() {
        Ok(path.to_path_buf())
    } else {
        Ok(std::env::current_dir()?.join(path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn temporary_root(label: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "agent-lifecycle-{label}-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).expect("create lifecycle test root");
        root
    }

    fn check<'a>(value: &'a Value, check_id: &str) -> &'a Value {
        value
            .get("checks")
            .and_then(Value::as_array)
            .and_then(|checks| {
                checks
                    .iter()
                    .find(|check| check.get("id").and_then(Value::as_str) == Some(check_id))
            })
            .expect("Doctor baseline check")
    }

    #[test]
    fn empty_target_is_read_only_and_reports_dependencies_as_skipped() {
        let root = temporary_root("empty");
        let before = std::fs::read_dir(&root)
            .expect("read lifecycle test root")
            .count();
        let value = inspect_doctor_baseline(&root, root.join("schemas"))
            .expect("inspect empty lifecycle target");
        assert_eq!(
            check(&value, "filesystem.target").get("status"),
            Some(&json!("passed"))
        );
        assert_eq!(
            check(&value, "filesystem.layout").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            check(&value, "lock.persistent").get("status"),
            Some(&json!("skipped"))
        );
        assert_eq!(
            check(&value, "schema.inventory").get("status"),
            Some(&json!("skipped"))
        );
        assert_eq!(
            std::fs::read_dir(&root)
                .expect("re-read lifecycle test root")
                .count(),
            before
        );
        std::fs::remove_dir(&root).expect("remove lifecycle test root");
    }

    #[test]
    fn recovery_residue_is_sorted_and_fail_closed() {
        let root = temporary_root("recovery");
        for name in [
            ".agent-skills-uninstall-backup-z",
            ".agent-skills-lifecycle.lock",
            ".agent-skills-stage-a",
        ] {
            std::fs::create_dir(root.join(name)).expect("create recovery residue");
        }
        let value = inspect_doctor_baseline(&root, root.join("schemas"))
            .expect("inspect lifecycle residue");
        assert_eq!(
            check(&value, "recovery.residue").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            value.pointer("/recovery/candidates"),
            Some(&json!([
                {"kind": "lifecycle-lock", "path": ".agent-skills-lifecycle.lock"},
                {"kind": "install-stage", "path": ".agent-skills-stage-a"},
                {"kind": "uninstall-backup", "path": ".agent-skills-uninstall-backup-z"},
            ]))
        );
        std::fs::remove_dir_all(&root).expect("remove lifecycle test root");
    }

    #[cfg(unix)]
    #[test]
    fn symlink_target_and_managed_root_are_rejected() {
        use std::os::unix::fs::symlink;

        let root = temporary_root("symlink");
        let real = root.join("real");
        std::fs::create_dir(&real).expect("create real target");
        let linked = root.join("linked");
        symlink(&real, &linked).expect("create target symlink");
        let linked_value =
            inspect_doctor_baseline(&linked, root.join("schemas")).expect("inspect linked target");
        assert_eq!(
            check(&linked_value, "filesystem.target").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            linked_value.pointer("/recovery/status"),
            Some(&json!("unknown"))
        );

        let target = root.join("target");
        std::fs::create_dir(&target).expect("create target");
        symlink(&real, target.join(".agent-skills")).expect("create managed-root symlink");
        let managed_value = inspect_doctor_baseline(&target, root.join("schemas"))
            .expect("inspect linked managed root");
        assert_eq!(
            check(&managed_value, "filesystem.layout").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            check(&managed_value, "install.lock").get("status"),
            Some(&json!("failed"))
        );
        std::fs::remove_dir_all(&root).expect("remove lifecycle test root");
    }
}
