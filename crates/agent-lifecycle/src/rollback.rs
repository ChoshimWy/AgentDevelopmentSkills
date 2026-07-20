use super::{
    LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE, PERSISTENT_PACKAGE_LOCK,
    configure_nofollow, external_stage, load_json_file, open_child_directory, open_child_file,
    packages, post_install, same_content_state_cap, same_object_cap, valid_sha256,
    validate_activation_lock_contract,
};
use agent_contracts::canonical_sha256;
use agent_engine::{install_plan_identity_hash, validate_install_plan, validate_package_lock};
use cap_fs_ext::{FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, Metadata, OpenOptions};
use serde_json::{Map, Value, json};
use sha2::Digest as _;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::io::{Read as _, Write as _};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const ROLLBACK_POINT_DIRECTORY: &str = "rollback-point";
const EXTERNAL_ACTIVATION_LOCK: &str = "activation-lock.json";
const EXTERNAL_FILES_DIRECTORY: &str = "external-files";
const EXTERNAL_STATE_FILE: &str = "external-state.json";
const MAX_ROLLBACK_DEPTH: usize = 128;
const MAX_ROLLBACK_ENTRIES: usize = 100_000;
const MAX_ROLLBACK_PATH_BYTES: usize = 4_096;
const MAX_ROLLBACK_RETAINED_BYTES: usize = super::MAX_CONTRACT_JSON_BYTES;
const MANAGED_ROOTS: [&str; 4] = [
    ".agent-skills",
    ".agent-skills-lifecycle.lock",
    "AGENTS.md",
    "skills",
];

struct ExternalState {
    directories: Vec<Value>,
    entries: Vec<Value>,
    fingerprint: String,
}

pub(super) struct PersistentRollbackPoint {
    external_paths: Vec<String>,
    point: Value,
    root: Dir,
}

impl PersistentRollbackPoint {
    pub(super) fn external_paths(&self) -> &[String] {
        &self.external_paths
    }

    pub(super) fn point(&self) -> &Value {
        &self.point
    }

    pub(super) fn root(&self) -> &Dir {
        &self.root
    }
}

static RESTORE_TEMPORARY_ID: AtomicU64 = AtomicU64::new(0);

pub(super) fn open_persistent_rollback_point(
    target: &Dir,
) -> Result<PersistentRollbackPoint, LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let root = open_child_directory(
        &managed,
        ROLLBACK_POINT_DIRECTORY,
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point directory",
    )?;
    validate_rollback_point_root(&root)?;
    let point = load_json_file(
        &root,
        "rollback-point.json",
        MANAGED_FILE_MODE,
        "rollback point contract",
    )?;
    let external = validate_external_state(&root)?;
    let external_paths = external
        .entries
        .iter()
        .map(|entry| value_path(entry, "external lifecycle file").map(str::to_owned))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(PersistentRollbackPoint {
        external_paths,
        point,
        root,
    })
}

pub(super) fn verify_external_target_state(root: &Dir, target: &Dir) -> Result<(), LifecycleError> {
    validate_rollback_point_root(root)?;
    validate_external_target_state(target, &validate_external_state(root)?)
}

#[allow(clippy::too_many_lines)]
pub(super) fn check_rollback_point(target: &Dir) -> Result<Value, LifecycleError> {
    match target.symlink_metadata(".agent-skills") {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(json!({"available": false}));
        }
        Err(error) => return Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return invalid("rollback point directory is missing or unsafe");
        }
        Ok(_) => {}
    }
    let managed = open_child_directory(target, ".agent-skills", None, "managed metadata directory")
        .map_err(|_| invalid_error("rollback point directory is missing or unsafe"))?;
    match managed.symlink_metadata(ROLLBACK_POINT_DIRECTORY) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(json!({"available": false}));
        }
        Err(error) => return Err(error.into()),
        Ok(_) => {}
    }
    let root = open_child_directory(
        &managed,
        ROLLBACK_POINT_DIRECTORY,
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point directory",
    )
    .map_err(|_| invalid_error("rollback point directory is missing or unsafe"))?;
    validate_rollback_point_root(&root)
}

pub(super) fn validate_rollback_point_root(root: &Dir) -> Result<Value, LifecycleError> {
    let root_identity = root.dir_metadata()?;
    validate_root_entries(root)?;

    for name in [
        "AGENTS.md",
        PERSISTENT_PACKAGE_LOCK,
        "install-lock.json",
        "rollback-point.json",
    ] {
        open_child_file(root, name, MANAGED_FILE_MODE, "rollback point file").map_err(|_| {
            invalid_error(format!("rollback point file is missing or unsafe: {name}"))
        })?;
    }
    let packages_root = open_child_directory(
        root,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point packages directory",
    )
    .map_err(|_| invalid_error("rollback point directory is missing or unsafe: packages"))?;
    let skills_root = open_child_directory(
        root,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point skills directory",
    )
    .map_err(|_| invalid_error("rollback point directory is missing or unsafe: skills"))?;

    let install_lock = load_json_file(
        root,
        "install-lock.json",
        MANAGED_FILE_MODE,
        "rollback point Install Lock",
    )?;
    let package_lock = load_json_file(
        root,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "rollback point package Lockfile",
    )?;
    let point = load_json_file(
        root,
        "rollback-point.json",
        MANAGED_FILE_MODE,
        "rollback point contract",
    )?;
    let external = validate_external_state(root)?;
    validate_activation_snapshot(root, &external.entries)?;

    validate_install_plan(&install_lock)?;
    validate_package_lock(&package_lock)?;
    validate_point_contract(&point)?;
    if install_lock.get("status").and_then(Value::as_str) != Some("installed") {
        return invalid("rollback point Install Lock is not installed");
    }
    let point_object = object(&point, "rollback point")?;
    if point_object.get("install_plan_fingerprint") != install_lock.get("fingerprint")
        || point_object.get("package_lock_hash") != package_lock.get("fingerprint")
        || install_lock.get("package_lock_hash") != package_lock.get("fingerprint")
        || package_lock
            .get("install_plan_identity_hash")
            .and_then(Value::as_str)
            != Some(install_plan_identity_hash(&install_lock)?.as_str())
        || point_object
            .get("external_state_sha256")
            .and_then(Value::as_str)
            != Some(external.fingerprint.as_str())
    {
        return invalid("rollback point Lockfile identities are inconsistent");
    }

    let agents_hash = packages::hash_child_file(
        root,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        "rollback point AGENTS",
    )?;
    if install_lock
        .pointer("/instructions/sha256")
        .and_then(Value::as_str)
        != Some(agents_hash.as_str())
    {
        return invalid("rollback point AGENTS.md differs from Install Lock");
    }

    let semantics = packages::derive_rollback_package_semantics(&packages_root, &install_lock)?;
    validate_skills(&skills_root, &install_lock)?;
    validate_semantics(root, &install_lock, &package_lock, &semantics)?;

    let snapshot = snapshot_identity(root)?;
    if point_object.get("snapshot_sha256").and_then(Value::as_str) != Some(snapshot.as_str()) {
        return invalid("rollback point snapshot digest is invalid");
    }
    let final_metadata = root.dir_metadata()?;
    if !same_object_cap(&root_identity, &final_metadata)
        || !same_content_state_cap(&root_identity, &final_metadata)
    {
        return invalid("rollback point directory changed while inspecting");
    }
    validate_root_entries(root)?;

    Ok(json!({
        "available": true,
        "package_lock_hash": point_object.get("package_lock_hash").cloned().unwrap_or(Value::Null),
        "point_id": point_object.get("point_id").cloned().unwrap_or(Value::Null),
    }))
}

fn validate_root_entries(root: &Dir) -> Result<(), LifecycleError> {
    let entries = entry_names(root)?;
    let mut expected = vec![
        "AGENTS.md".to_owned(),
        PERSISTENT_PACKAGE_LOCK.to_owned(),
        EXTERNAL_FILES_DIRECTORY.to_owned(),
        EXTERNAL_STATE_FILE.to_owned(),
        "install-lock.json".to_owned(),
        "packages".to_owned(),
        "rollback-point.json".to_owned(),
        "skills".to_owned(),
    ];
    if entries
        .iter()
        .any(|entry| entry == EXTERNAL_ACTIVATION_LOCK)
    {
        expected.push(EXTERNAL_ACTIVATION_LOCK.to_owned());
    }
    expected.sort();
    if entries != expected {
        return invalid("rollback point contains missing or unknown entries");
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn validate_external_state(root: &Dir) -> Result<ExternalState, LifecycleError> {
    let state = load_json_file(
        root,
        EXTERNAL_STATE_FILE,
        MANAGED_FILE_MODE,
        "rollback point external state",
    )
    .map_err(|_| invalid_error("rollback point external state is missing or unsafe"))?;
    let files_root = open_child_directory(
        root,
        EXTERNAL_FILES_DIRECTORY,
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point external files",
    )
    .map_err(|_| invalid_error("rollback point external state is missing or unsafe"))?;
    let state_object = state
        .as_object()
        .filter(|object| {
            exact_fields(
                object,
                &["directories", "entries", "fingerprint", "schema_version"],
            )
        })
        .ok_or_else(|| invalid_error("rollback point external state shape is invalid"))?;
    let directories = state_object
        .get("directories")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            invalid_error("rollback point external state version or entries are invalid")
        })?;
    let entries = state_object
        .get("entries")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            invalid_error("rollback point external state version or entries are invalid")
        })?;
    if state_object.get("schema_version").and_then(Value::as_str) != Some("1.0") {
        return invalid("rollback point external state version or entries are invalid");
    }

    let mut directory_paths = Vec::with_capacity(directories.len());
    for entry in directories {
        let entry_object = entry
            .as_object()
            .ok_or_else(|| invalid_error("rollback point external directory entry is invalid"))?;
        let state = entry_object.get("state").and_then(Value::as_str);
        if !matches!(state, Some("absent" | "directory")) {
            return invalid("rollback point external directory entry is invalid");
        }
        let expected = if state == Some("absent") {
            &["path", "state"][..]
        } else {
            &["mode", "path", "state"][..]
        };
        let path = entry_object.get("path").and_then(Value::as_str);
        if !exact_fields(entry_object, expected) || path.is_none() {
            return invalid("rollback point external directory entry shape is invalid");
        }
        let path = path.unwrap_or_default();
        validate_external_path(&format!("{path}/placeholder"))?;
        if state == Some("directory") && mode(entry_object.get("mode")).is_none() {
            return invalid("rollback point external directory mode is invalid");
        }
        directory_paths.push(path.to_owned());
    }
    if directory_paths.iter().collect::<BTreeSet<_>>().len() != directory_paths.len()
        || !directory_paths.windows(2).all(|pair| pair[0] <= pair[1])
    {
        return invalid("rollback point external directories must be sorted and unique");
    }

    let mut paths = Vec::with_capacity(entries.len());
    let mut expected_files = Vec::new();
    for entry in entries {
        let entry_object = entry
            .as_object()
            .ok_or_else(|| invalid_error("rollback point external state entry is invalid"))?;
        let state = entry_object.get("state").and_then(Value::as_str);
        if !matches!(state, Some("absent" | "file")) {
            return invalid("rollback point external state entry is invalid");
        }
        let expected = if state == Some("absent") {
            &["path", "state"][..]
        } else {
            &["mode", "path", "sha256", "state"][..]
        };
        if !exact_fields(entry_object, expected) {
            return invalid("rollback point external state entry shape is invalid");
        }
        let path = entry_object
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_error("rollback point external state path is invalid"))?;
        validate_external_path(path)?;
        paths.push(path.to_owned());
        if state == Some("absent") {
            if relative_exists(&files_root, path)? {
                return invalid(format!(
                    "absent external snapshot unexpectedly exists: {path}"
                ));
            }
            continue;
        }
        let expected_mode = mode(entry_object.get("mode")).ok_or_else(|| {
            invalid_error(format!("external snapshot file differs from state: {path}"))
        })?;
        let expected_hash = entry_object
            .get("sha256")
            .and_then(Value::as_str)
            .filter(|value| valid_sha256(value))
            .ok_or_else(|| {
                invalid_error(format!("external snapshot file differs from state: {path}"))
            })?;
        let (parent, name) = open_relative_parent(&files_root, path, "external snapshot file")
            .map_err(|_| {
                invalid_error(format!("external snapshot file differs from state: {path}"))
            })?;
        let actual_hash =
            packages::hash_child_file(&parent, &name, expected_mode, "external snapshot file")
                .map_err(|_| {
                    invalid_error(format!("external snapshot file differs from state: {path}"))
                })?;
        if actual_hash != expected_hash {
            return invalid(format!("external snapshot file differs from state: {path}"));
        }
        expected_files.push(path.to_owned());
    }
    if paths.iter().collect::<BTreeSet<_>>().len() != paths.len()
        || !paths.windows(2).all(|pair| pair[0] <= pair[1])
    {
        return invalid("rollback point external paths must be sorted and unique");
    }
    let snapshot = snapshot_tree(&files_root)?;
    let actual_files = snapshot
        .files
        .iter()
        .filter_map(|entry| entry.get("path").and_then(Value::as_str))
        .collect::<Vec<_>>();
    if actual_files
        != expected_files
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>()
    {
        return invalid("rollback point external snapshot contains unknown files");
    }
    let expected_directory_mode = rollback_external_directory_mode();
    if snapshot.directories.iter().any(|entry| {
        entry.get("mode").and_then(Value::as_u64) != Some(expected_directory_mode.into())
    }) {
        return invalid("rollback point external snapshot directory mode is invalid");
    }

    let fingerprint = state_object
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_error("rollback point external state fingerprint mismatch"))?;
    let mut identity = state_object.clone();
    identity.remove("fingerprint");
    if canonical_sha256(&Value::Object(identity))? != fingerprint {
        return invalid("rollback point external state fingerprint mismatch");
    }
    Ok(ExternalState {
        directories: directories.clone(),
        entries: entries.clone(),
        fingerprint: fingerprint.to_owned(),
    })
}

/// Restore the external file and ancestor-directory preimages frozen in one
/// validated rollback point.
///
/// The rollback point is reopened through directory capabilities, every
/// destination path is traversed without following symlinks, and replacement
/// files are written to single-link temporary files before an atomic rename.
/// Existing hard-linked files are rejected so rollback cannot modify an
/// unscoped alias. The final target state is revalidated against the snapshot.
pub(super) fn restore_external_state(
    root: &Dir,
    target: &Dir,
    target_path: &Path,
    quarantine: &Dir,
    quarantine_path: &Path,
) -> Result<(), LifecycleError> {
    restore_external_state_with(
        root,
        target,
        target_path,
        quarantine,
        quarantine_path,
        |_, _| Ok(()),
    )
}

pub(super) fn restore_external_state_with_hook(
    root: &Dir,
    target: &Dir,
    target_path: &Path,
    quarantine: &Dir,
    quarantine_path: &Path,
    hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    restore_external_state_with(root, target, target_path, quarantine, quarantine_path, hook)
}

fn restore_external_state_with(
    root: &Dir,
    target: &Dir,
    target_path: &Path,
    quarantine: &Dir,
    quarantine_path: &Path,
    mut hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    let state = validate_external_state(root)?;
    let files_root = open_child_directory(
        root,
        EXTERNAL_FILES_DIRECTORY,
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point external files",
    )?;

    for entry in &state.directories {
        if entry.get("state").and_then(Value::as_str) != Some("directory") {
            continue;
        }
        let relative = value_path(entry, "external lifecycle directory")?;
        let expected_mode = mode(entry.get("mode"))
            .ok_or_else(|| invalid_error("rollback point external directory mode is invalid"))?;
        ensure_target_directory(target, relative, expected_mode, true)?;
        hook(relative, "directory-prepared")?;
    }

    for entry in &state.entries {
        let relative = value_path(entry, "external lifecycle file")?;
        let (destination_parent, destination_parent_path, destination_name) =
            open_target_parent(target, target_path, relative, "external lifecycle file")?;
        match entry.get("state").and_then(Value::as_str) {
            Some("absent") => {
                quarantine_optional_regular_file(
                    &destination_parent,
                    &destination_parent_path,
                    &destination_name,
                    relative,
                    quarantine,
                    quarantine_path,
                )?;
            }
            Some("file") => {
                let expected_mode = mode(entry.get("mode")).ok_or_else(|| {
                    invalid_error(format!(
                        "external snapshot file differs from state: {relative}"
                    ))
                })?;
                let expected_hash =
                    entry.get("sha256").and_then(Value::as_str).ok_or_else(|| {
                        invalid_error(format!(
                            "external snapshot file differs from state: {relative}"
                        ))
                    })?;
                replace_from_snapshot(
                    &files_root,
                    &destination_parent,
                    &destination_parent_path,
                    &destination_name,
                    relative,
                    expected_mode,
                    expected_hash,
                    quarantine,
                    quarantine_path,
                )?;
            }
            _ => return invalid("rollback point external state entry is invalid"),
        }
        hook(relative, "entry-restored")?;
    }

    for entry in state.directories.iter().rev() {
        let relative = value_path(entry, "external lifecycle directory")?;
        match entry.get("state").and_then(Value::as_str) {
            Some("directory") => {
                let expected_mode = mode(entry.get("mode")).ok_or_else(|| {
                    invalid_error("rollback point external directory mode is invalid")
                })?;
                let directory =
                    open_target_directory(target, relative, "external lifecycle directory")?;
                set_directory_mode(&directory, expected_mode)?;
            }
            Some("absent") => quarantine_absent_directory(
                target,
                target_path,
                relative,
                quarantine,
                quarantine_path,
            )?,
            _ => return invalid("rollback point external directory entry is invalid"),
        }
        hook(relative, "directory-finalized")?;
    }

    validate_external_target_state(target, &state)
}

fn value_path<'a>(entry: &'a Value, label: &str) -> Result<&'a str, LifecycleError> {
    entry
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_error(format!("{label} path is invalid")))
}

fn ensure_target_directory(
    target: &Dir,
    relative: &str,
    expected_mode: u32,
    writable: bool,
) -> Result<(), LifecycleError> {
    let components = relative_components(relative)?;
    let mut directory = target.try_clone()?;
    for (index, component) in components.iter().enumerate() {
        let is_leaf = index + 1 == components.len();
        directory = match directory.symlink_metadata(component) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!(
                    "external lifecycle directory is unsafe: {}",
                    components[..=index].join("/")
                ));
            }
            Ok(_) => {
                let child = external_stage::open_directory(
                    &directory,
                    OsStr::new(component),
                    None,
                    "external lifecycle directory",
                )?;
                if writable {
                    set_directory_mode(&child, temporary_directory_mode(expected_mode))?;
                }
                child
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                external_stage::create_directory(
                    &directory,
                    OsStr::new(component),
                    Some(if writable {
                        temporary_directory_mode(expected_mode)
                    } else if is_leaf {
                        expected_mode
                    } else {
                        MANAGED_DIRECTORY_MODE
                    }),
                    "external lifecycle directory",
                )?
            }
            Err(error) => return Err(error.into()),
        };
    }
    set_directory_mode(
        &directory,
        if writable {
            temporary_directory_mode(expected_mode)
        } else {
            expected_mode
        },
    )
}

fn open_target_parent(
    target: &Dir,
    target_path: &Path,
    relative: &str,
    label: &str,
) -> Result<(Dir, PathBuf, String), LifecycleError> {
    let components = relative_components(relative)?;
    let (name, parents) = components
        .split_last()
        .ok_or_else(|| invalid_error(format!("{label} path is invalid")))?;
    let mut directory = target.try_clone()?;
    let mut directory_path = target_path.to_path_buf();
    for parent in parents {
        directory = match directory.symlink_metadata(parent) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!("{label} parent is unsafe: {relative}"));
            }
            Ok(_) => {
                let child =
                    external_stage::open_directory(&directory, OsStr::new(parent), None, label)?;
                make_directory_writable(&child)?;
                child
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                external_stage::create_directory(
                    &directory,
                    OsStr::new(parent),
                    Some(temporary_directory_mode(MANAGED_DIRECTORY_MODE)),
                    "external lifecycle directory",
                )?
            }
            Err(error) => return Err(error.into()),
        };
        directory_path.push(parent);
    }
    Ok((directory, directory_path, (*name).clone()))
}

fn open_target_directory(target: &Dir, relative: &str, label: &str) -> Result<Dir, LifecycleError> {
    let mut directory = target.try_clone()?;
    for component in relative_components(relative)? {
        directory =
            external_stage::open_directory(&directory, OsStr::new(&component), None, label)?;
    }
    Ok(directory)
}

fn quarantine_optional_regular_file(
    parent: &Dir,
    parent_path: &Path,
    name: &str,
    relative: &str,
    quarantine: &Dir,
    quarantine_path: &Path,
) -> Result<(), LifecycleError> {
    let Some(captured) = capture_optional_regular_file(parent, name, relative)? else {
        return Ok(());
    };
    quarantine_bound_entry(
        parent,
        parent_path,
        name,
        captured,
        quarantine,
        quarantine_path,
        relative,
    )
    .map(|_| ())
}

fn capture_optional_regular_file(
    parent: &Dir,
    name: &str,
    relative: &str,
) -> Result<Option<CapturedEntry>, LifecycleError> {
    match parent.symlink_metadata(name) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => invalid(
            format!("external lifecycle destination is unsafe: {relative}"),
        ),
        Ok(metadata) if metadata.nlink() != 1 => invalid(format!(
            "external lifecycle destination has an unsafe hard-link alias: {relative}"
        )),
        Ok(metadata) => {
            let file = external_stage::open_regular_file(
                parent,
                OsStr::new(name),
                None,
                "external lifecycle destination",
            )?;
            let opened = file.metadata()?;
            if !same_object_cap(&metadata, &opened)
                || !same_content_state_cap(&metadata, &opened)
                || opened.nlink() != 1
            {
                return invalid(format!(
                    "external lifecycle destination changed while opening: {relative}"
                ));
            }
            let mode = snapshot_mode(&opened);
            let sha256 =
                packages::hash_child_file(parent, name, mode, "external lifecycle destination")?;
            let current = parent.symlink_metadata(name)?;
            if !same_object_cap(&opened, &current)
                || !same_content_state_cap(&opened, &current)
                || current.nlink() != 1
            {
                return invalid(format!(
                    "external lifecycle destination changed while hashing: {relative}"
                ));
            }
            Ok(Some(CapturedEntry {
                identity: current,
                mode,
                sha256: Some(sha256),
            }))
        }
    }
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
fn replace_from_snapshot(
    files_root: &Dir,
    destination_parent: &Dir,
    destination_parent_path: &Path,
    destination_name: &str,
    relative: &str,
    expected_mode: u32,
    expected_hash: &str,
    quarantine: &Dir,
    quarantine_path: &Path,
) -> Result<(), LifecycleError> {
    let (source_parent, source_name) =
        open_relative_parent(files_root, relative, "external snapshot file")?;
    let mut source = external_stage::open_regular_file(
        &source_parent,
        OsStr::new(&source_name),
        Some(expected_mode),
        "external snapshot file",
    )?;
    let opened_source = source.metadata()?;
    let temporary_name = unique_temporary_name(quarantine, destination_name)?;
    let mut options = OpenOptions::new();
    options
        .write(true)
        .create_new(true)
        .follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt as _;
        options.mode(expected_mode);
    }
    let mut temporary = quarantine.open_with(&temporary_name, &options)?;
    let opened_temporary = temporary.metadata()?;
    let mut digest = sha2::Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    loop {
        let count = source.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        temporary.write_all(&buffer[..count])?;
    }
    temporary.flush()?;
    set_file_mode(&temporary, expected_mode)?;
    if format!("{:x}", digest.finalize()) != expected_hash {
        return invalid(format!(
            "external snapshot file changed while restoring: {relative}"
        ));
    }
    let completed = temporary.metadata()?;
    if !same_object_cap(&opened_temporary, &completed)
        || completed.nlink() != 1
        || snapshot_mode(&completed) != expected_mode
    {
        return invalid(format!(
            "external restore temporary file changed: {relative}"
        ));
    }
    drop(temporary);

    let quarantined =
        capture_optional_regular_file(destination_parent, destination_name, relative)?
            .map(|captured| {
                quarantine_bound_entry(
                    destination_parent,
                    destination_parent_path,
                    destination_name,
                    captured,
                    quarantine,
                    quarantine_path,
                    relative,
                )
            })
            .transpose()?;
    if let Err(error) = super::managed_swap::rename_no_replace(
        quarantine,
        quarantine_path,
        &temporary_name,
        destination_parent,
        destination_parent_path,
        destination_name,
    ) {
        let recovery = quarantined
            .as_ref()
            .map(|entry| {
                restore_quarantined_entry(
                    quarantine,
                    quarantine_path,
                    entry,
                    destination_parent,
                    destination_parent_path,
                    destination_name,
                )
            })
            .transpose();
        return match recovery {
            Ok(_) => invalid(format!(
                "external lifecycle destination changed before restore: {relative}: {error}"
            )),
            Err(recovery) => invalid(format!(
                "external lifecycle destination changed before restore: {relative}: {error}; quarantined preimage could not be restored: {recovery}"
            )),
        };
    }
    let published = destination_parent.symlink_metadata(destination_name)?;
    if !same_object_cap(&opened_temporary, &published)
        || published.nlink() != 1
        || snapshot_mode(&published) != expected_mode
    {
        return invalid(format!(
            "restored external lifecycle file identity changed: {relative}"
        ));
    }
    let actual_hash = packages::hash_child_file(
        destination_parent,
        destination_name,
        expected_mode,
        "restored external lifecycle file",
    )?;
    if actual_hash != expected_hash {
        return invalid(format!(
            "external lifecycle file was not restored: {relative}"
        ));
    }
    let after_source = source.metadata()?;
    if !same_object_cap(&opened_source, &after_source)
        || !same_content_state_cap(&opened_source, &after_source)
    {
        return invalid(format!(
            "external snapshot file changed while restoring: {relative}"
        ));
    }
    Ok(())
}

fn unique_temporary_name(parent: &Dir, destination: &str) -> Result<String, LifecycleError> {
    for _ in 0..1_024 {
        let id = RESTORE_TEMPORARY_ID.fetch_add(1, Ordering::Relaxed);
        let candidate = format!(".{destination}.restore-{}-{id}", std::process::id());
        match parent.symlink_metadata(&candidate) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(candidate),
            Err(error) => return Err(error.into()),
            Ok(_) => {}
        }
    }
    invalid("could not allocate an external restore temporary file")
}

struct CapturedEntry {
    identity: Metadata,
    mode: u32,
    sha256: Option<String>,
}

struct QuarantinedEntry {
    captured: CapturedEntry,
    name: String,
}

fn quarantine_bound_entry(
    source: &Dir,
    source_path: &Path,
    source_name: &str,
    captured: CapturedEntry,
    quarantine: &Dir,
    quarantine_path: &Path,
    relative: &str,
) -> Result<QuarantinedEntry, LifecycleError> {
    let quarantine_name = unique_temporary_name(quarantine, source_name)?;
    super::managed_swap::rename_no_replace(
        source,
        source_path,
        source_name,
        quarantine,
        quarantine_path,
        &quarantine_name,
    )
    .map_err(|error| {
        LifecycleError::Invalid(format!(
            "could not quarantine external lifecycle destination {relative}: {error}"
        ))
    })?;
    let moved = quarantine.symlink_metadata(&quarantine_name)?;
    let source_absent = matches!(
        source.symlink_metadata(source_name),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound
    );
    if !source_absent
        || !same_object_cap(&captured.identity, &moved)
        || !captured_entry_matches(quarantine, &quarantine_name, &captured)?
    {
        let entry = QuarantinedEntry {
            captured,
            name: quarantine_name,
        };
        let recovery = restore_quarantined_entry(
            quarantine,
            quarantine_path,
            &entry,
            source,
            source_path,
            source_name,
        );
        return match recovery {
            Ok(()) => invalid(format!(
                "external lifecycle destination changed while quarantining: {relative}"
            )),
            Err(error) => invalid(format!(
                "external lifecycle destination changed while quarantining: {relative}; moved entry could not be restored: {error}"
            )),
        };
    }
    Ok(QuarantinedEntry {
        captured,
        name: quarantine_name,
    })
}

fn restore_quarantined_entry(
    quarantine: &Dir,
    quarantine_path: &Path,
    entry: &QuarantinedEntry,
    destination: &Dir,
    destination_path: &Path,
    destination_name: &str,
) -> Result<(), LifecycleError> {
    let current = quarantine.symlink_metadata(&entry.name)?;
    if !same_object_cap(&entry.captured.identity, &current)
        || !captured_entry_matches(quarantine, &entry.name, &entry.captured)?
    {
        return invalid("quarantined external lifecycle entry changed");
    }
    super::managed_swap::rename_no_replace(
        quarantine,
        quarantine_path,
        &entry.name,
        destination,
        destination_path,
        destination_name,
    )?;
    let restored = destination.symlink_metadata(destination_name)?;
    if !same_object_cap(&entry.captured.identity, &restored)
        || !captured_entry_matches(destination, destination_name, &entry.captured)?
    {
        return invalid("quarantined external lifecycle entry was not restored");
    }
    Ok(())
}

fn captured_entry_matches(
    parent: &Dir,
    name: &str,
    captured: &CapturedEntry,
) -> Result<bool, LifecycleError> {
    let metadata = parent.symlink_metadata(name)?;
    if metadata.file_type().is_symlink()
        || !same_object_cap(&captured.identity, &metadata)
        || snapshot_mode(&metadata) != captured.mode
    {
        return Ok(false);
    }
    if let Some(expected_hash) = captured.sha256.as_deref() {
        if !metadata.is_file() || metadata.nlink() != 1 {
            return Ok(false);
        }
        return Ok(packages::hash_child_file(
            parent,
            name,
            captured.mode,
            "quarantined external lifecycle file",
        )? == expected_hash);
    }
    if !metadata.is_dir() {
        return Ok(false);
    }
    let directory = external_stage::open_directory(
        parent,
        OsStr::new(name),
        Some(captured.mode),
        "quarantined external lifecycle directory",
    )?;
    Ok(directory.entries()?.next().transpose()?.is_none())
}

fn quarantine_absent_directory(
    target: &Dir,
    target_path: &Path,
    relative: &str,
    quarantine: &Dir,
    quarantine_path: &Path,
) -> Result<(), LifecycleError> {
    let components = relative_components(relative)?;
    let (name, parents) = components
        .split_last()
        .ok_or_else(|| invalid_error("external lifecycle directory path is invalid"))?;
    let mut parent = target.try_clone()?;
    let mut parent_path = target_path.to_path_buf();
    for component in parents {
        match parent.symlink_metadata(component) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error.into()),
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!(
                    "external lifecycle directory is unsafe: {relative}"
                ));
            }
            Ok(_) => {
                parent = external_stage::open_directory(
                    &parent,
                    OsStr::new(component),
                    None,
                    "external lifecycle directory",
                )?;
                make_directory_writable(&parent)?;
                parent_path.push(component);
            }
        }
    }
    match parent.symlink_metadata(name) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => invalid(
            format!("external lifecycle directory is unsafe: {relative}"),
        ),
        Ok(metadata) => {
            let directory = external_stage::open_directory(
                &parent,
                OsStr::new(name),
                None,
                "external lifecycle directory",
            )?;
            if directory.entries()?.next().transpose()?.is_some() {
                return invalid(format!(
                    "new external lifecycle directory is not empty: {relative}"
                ));
            }
            let identity = directory.dir_metadata()?;
            if !same_object_cap(&metadata, &identity) {
                return invalid(format!(
                    "external lifecycle directory changed while opening: {relative}"
                ));
            }
            // Windows directory capabilities intentionally omit
            // FILE_SHARE_DELETE; release the leaf handle before its
            // identity-bound no-replace move into quarantine.
            drop(directory);
            quarantine_bound_entry(
                &parent,
                &parent_path,
                name,
                CapturedEntry {
                    mode: snapshot_mode(&identity),
                    identity,
                    sha256: None,
                },
                quarantine,
                quarantine_path,
                relative,
            )
            .map(|_| ())
        }
    }
}

fn validate_external_target_state(
    target: &Dir,
    state: &ExternalState,
) -> Result<(), LifecycleError> {
    for entry in &state.directories {
        let relative = value_path(entry, "external lifecycle directory")?;
        match entry.get("state").and_then(Value::as_str) {
            Some("absent") if relative_exists(target, relative)? => {
                return invalid(format!(
                    "external lifecycle directory was not restored: {relative}"
                ));
            }
            Some("directory") => {
                let expected_mode = mode(entry.get("mode")).ok_or_else(|| {
                    invalid_error("rollback point external directory mode is invalid")
                })?;
                let directory =
                    open_target_directory(target, relative, "external lifecycle directory")?;
                if snapshot_mode(&directory.dir_metadata()?) != expected_mode {
                    return invalid(format!(
                        "external lifecycle directory was not restored: {relative}"
                    ));
                }
            }
            Some("absent") => {}
            _ => return invalid("rollback point external directory entry is invalid"),
        }
    }
    for entry in &state.entries {
        let relative = value_path(entry, "external lifecycle file")?;
        match entry.get("state").and_then(Value::as_str) {
            Some("absent") if relative_exists(target, relative)? => {
                return invalid(format!(
                    "external lifecycle file was not restored: {relative}"
                ));
            }
            Some("file") => {
                let expected_mode = mode(entry.get("mode")).ok_or_else(|| {
                    invalid_error("rollback point external state entry is invalid")
                })?;
                let expected_hash =
                    entry.get("sha256").and_then(Value::as_str).ok_or_else(|| {
                        invalid_error("rollback point external state entry is invalid")
                    })?;
                let (parent, name) =
                    open_relative_parent(target, relative, "external lifecycle file")?;
                let actual_hash = packages::hash_child_file(
                    &parent,
                    &name,
                    expected_mode,
                    "external lifecycle file",
                )?;
                if actual_hash != expected_hash {
                    return invalid(format!(
                        "external lifecycle file was not restored: {relative}"
                    ));
                }
            }
            Some("absent") => {}
            _ => return invalid("rollback point external state entry is invalid"),
        }
    }
    Ok(())
}

#[cfg(unix)]
fn set_file_mode(file: &cap_std::fs::File, mode: u32) -> Result<(), LifecycleError> {
    use cap_std::fs::{Permissions, PermissionsExt as _};
    file.set_permissions(Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(windows)]
fn set_file_mode(file: &cap_std::fs::File, mode: u32) -> Result<(), LifecycleError> {
    let mut permissions = file.metadata()?.permissions();
    permissions.set_readonly(mode & 0o222 == 0);
    file.set_permissions(permissions)?;
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn set_file_mode(_file: &cap_std::fs::File, _mode: u32) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
fn set_directory_mode(directory: &Dir, mode: u32) -> Result<(), LifecycleError> {
    use cap_std::fs::{Permissions, PermissionsExt as _};
    directory.set_permissions(".", Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn set_directory_mode(_directory: &Dir, _mode: u32) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
fn temporary_directory_mode(mode: u32) -> u32 {
    mode | 0o700
}

#[cfg(not(unix))]
fn temporary_directory_mode(mode: u32) -> u32 {
    mode
}

#[cfg(unix)]
fn make_directory_writable(directory: &Dir) -> Result<(), LifecycleError> {
    set_directory_mode(
        directory,
        temporary_directory_mode(snapshot_mode(&directory.dir_metadata()?)),
    )
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn make_directory_writable(_directory: &Dir) -> Result<(), LifecycleError> {
    Ok(())
}

fn validate_activation_snapshot(
    root: &Dir,
    external_entries: &[Value],
) -> Result<(), LifecycleError> {
    match root.symlink_metadata(EXTERNAL_ACTIVATION_LOCK) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
        Ok(_) => {}
    }
    let activation = load_json_file(
        root,
        EXTERNAL_ACTIVATION_LOCK,
        MANAGED_FILE_MODE,
        "rollback point activation lock",
    )
    .map_err(|_| invalid_error("rollback point activation lock is unsafe"))?;
    let (_, records) = validate_activation_lock_contract(&activation)?;
    let external_by_path = external_entries
        .iter()
        .filter_map(|entry| Some((entry.get("path")?.as_str()?.to_owned(), entry.as_object()?)))
        .collect::<BTreeMap<_, _>>();
    for record in records {
        let record_object = record
            .as_object()
            .ok_or_else(|| invalid_error("rollback point activation lock record is invalid"))?;
        if !exact_fields(record_object, &["mode", "path", "sha256"]) {
            return invalid("rollback point activation lock record is invalid");
        }
        let path = record_object
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_error("rollback point activation lock record is invalid"))?;
        let external = external_by_path.get(path).copied();
        if external.is_none_or(|entry| !activation_snapshot_matches(entry, record_object)) {
            return invalid("rollback point activation lock differs from external snapshot");
        }
    }
    Ok(())
}

#[cfg(unix)]
fn activation_snapshot_matches(
    external: &Map<String, Value>,
    activation: &Map<String, Value>,
) -> bool {
    external.get("state").and_then(Value::as_str) == Some("file")
        && external.get("mode") == activation.get("mode")
        && external.get("sha256") == activation.get("sha256")
}

#[cfg(not(unix))]
fn activation_snapshot_matches(
    external: &Map<String, Value>,
    activation: &Map<String, Value>,
) -> bool {
    external.get("state").and_then(Value::as_str) == Some("file")
        && external.get("sha256") == activation.get("sha256")
}

fn validate_skills(skills_root: &Dir, install_lock: &Value) -> Result<(), LifecycleError> {
    let records = array(install_lock, "skills", "Install Lock Skills")?;
    let mut expected = records
        .iter()
        .map(|record| {
            record
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or_else(|| invalid_error("Skill name is invalid"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    expected.sort();
    if entry_names(skills_root)? != expected {
        return invalid("rollback point Skill set differs from Install Lock");
    }
    for record in records {
        let name = record
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_error("Skill name is invalid"))?;
        let root = open_child_directory(
            skills_root,
            name,
            record
                .get("root_mode")
                .and_then(Value::as_u64)
                .and_then(|value| u32::try_from(value).ok()),
            "rollback point Skill",
        )
        .map_err(|_| invalid_error(format!("rollback point Skill is missing or unsafe: {name}")))?;
        packages::validate_recorded_tree(
            &root,
            record,
            "sha256",
            &format!("rollback point Skill differs from Install Lock: {name}"),
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn validate_semantics(
    root: &Dir,
    install_lock: &Value,
    package_lock: &Value,
    semantics: &Value,
) -> Result<(), LifecycleError> {
    let selected = array(
        install_lock,
        "selected_packages",
        "Install Lock selected packages",
    )?;
    let locked = array(package_lock, "packages", "package Lock packages")?;
    let selected_ids = selected
        .iter()
        .filter_map(|value| value.get("id").and_then(Value::as_str))
        .collect::<Vec<_>>();
    let locked_ids = locked
        .iter()
        .filter_map(|value| value.get("id").and_then(Value::as_str))
        .collect::<Vec<_>>();
    if locked_ids != selected_ids {
        return invalid("rollback point package semantic closure differs between Lockfiles");
    }
    let selected_by_id = values_by_id(selected)?;
    let locked_by_id = values_by_id(locked)?;
    let identities = semantics
        .get("selected_package_identities")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_error("installed package identities are invalid"))?;
    let fields = [
        "core_compatibility",
        "kind",
        "provider_compatibility",
        "provider_version",
        "version",
    ];
    for (package_id, expected) in identities {
        let matches = selected_by_id
            .get(package_id)
            .zip(locked_by_id.get(package_id))
            .is_some_and(|(selected, locked)| {
                fields.iter().all(|field| {
                    selected.get(*field) == expected.get(*field)
                        && locked.get(*field) == expected.get(*field)
                })
            });
        if !matches {
            return invalid(format!(
                "rollback point package semantics differ from Manifests: {package_id}"
            ));
        }
    }

    if install_lock.get("bindings") != semantics.get("bindings")
        || install_lock.get("capability_providers") != semantics.get("capability_providers")
        || package_lock.get("capability_providers") != semantics.get("capability_providers")
        || package_lock.get("bindings_sha256")
            != Some(&Value::String(canonical_sha256(
                semantics.get("bindings").unwrap_or(&Value::Null),
            )?))
        || install_lock.get("permission_profiles") != semantics.get("permission_profiles")
        || package_lock.get("permission_profiles") != semantics.get("permission_profiles")
        || install_lock.get("resolved_dependencies") != semantics.get("dependencies")
        || package_lock.get("dependencies") != semantics.get("dependencies")
        || install_lock.get("side_effects") != semantics.get("side_effects")
        || package_lock.get("side_effects") != semantics.get("side_effects")
    {
        return invalid("rollback point runtime semantics differ from installed Manifests");
    }

    let skill_fields = ["file_count", "files", "name", "package", "sha256"];
    if project_array(install_lock, "skills", &skill_fields)?
        != project_array(semantics, "skills", &skill_fields)?
    {
        return invalid("rollback point Skill semantics differ from installed Manifests");
    }
    let instructions = semantics
        .get("instructions")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_error("installed instructions are invalid"))?;
    let locked_instructions = install_lock
        .get("instructions")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_error("Install Lock instructions are invalid"))?;
    let expected_package_instructions = json!({
        "rule_trace_sha256": canonical_sha256(
            instructions.get("rule_trace").unwrap_or(&Value::Null)
        )?,
        "sha256": instructions.get("sha256").cloned().unwrap_or(Value::Null),
    });
    let expected_content = instructions
        .get("content")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_error("installed instruction content is invalid"))?;
    if locked_instructions.get("fragments") != instructions.get("fragments")
        || locked_instructions.get("rule_trace") != instructions.get("rule_trace")
        || locked_instructions.get("sha256") != instructions.get("sha256")
        || package_lock.get("instructions") != Some(&expected_package_instructions)
        || !post_install::child_bytes_equal(
            root,
            "AGENTS.md",
            MANAGED_FILE_MODE,
            "rollback point AGENTS.md",
            expected_content.as_bytes(),
        )?
    {
        return invalid("rollback point AGENTS semantics differ from installed Manifests");
    }
    Ok(())
}

fn validate_point_contract(point: &Value) -> Result<(), LifecycleError> {
    let object = point
        .as_object()
        .filter(|object| {
            exact_fields(
                object,
                &[
                    "external_state_sha256",
                    "fingerprint",
                    "install_plan_fingerprint",
                    "manager",
                    "package_lock_hash",
                    "point_id",
                    "schema_version",
                    "snapshot_sha256",
                ],
            )
        })
        .ok_or_else(|| invalid_error("rollback-point must contain exactly the required fields"))?;
    if object.get("schema_version").and_then(Value::as_str) != Some("1.0") {
        return invalid("unsupported schema_version");
    }
    if object.get("manager").and_then(Value::as_str) != Some("agent-development-skills") {
        return invalid("rollback-point manager is invalid");
    }
    for field in [
        "install_plan_fingerprint",
        "package_lock_hash",
        "snapshot_sha256",
        "external_state_sha256",
        "fingerprint",
    ] {
        if !object
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(valid_sha256)
        {
            return invalid(format!("rollback-point {field} is invalid"));
        }
    }
    let package_hash = object
        .get("package_lock_hash")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if object.get("point_id").and_then(Value::as_str)
        != Some(format!("rollback-{}", &package_hash[..12]).as_str())
    {
        return invalid("rollback-point id is invalid");
    }
    let fingerprint = object
        .get("fingerprint")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let mut identity = object.clone();
    identity.remove("fingerprint");
    if canonical_sha256(&Value::Object(identity))? != fingerprint {
        return invalid("rollback-point fingerprint mismatch");
    }
    Ok(())
}

struct TreeSnapshot {
    directories: Vec<Value>,
    files: Vec<Value>,
}

struct SnapshotFrame {
    directory: Dir,
    names: Vec<std::ffi::OsString>,
    next: usize,
    prefix: String,
}

impl SnapshotFrame {
    fn open(directory: Dir, prefix: String, entries: &mut usize) -> Result<Self, LifecycleError> {
        let mut names = Vec::new();
        for entry in directory.entries()? {
            let entry = entry?;
            reserve_rollback_entry(entries)?;
            names.push(entry.file_name());
        }
        names.sort();
        Ok(Self {
            directory,
            names,
            next: 0,
            prefix,
        })
    }
}

pub(super) fn snapshot_identity(root: &Dir) -> Result<String, LifecycleError> {
    let mut snapshot = snapshot_tree(root)?;
    snapshot
        .files
        .retain(|entry| entry.get("path").and_then(Value::as_str) != Some("rollback-point.json"));
    canonical_sha256(&json!({
        "directories": snapshot.directories,
        "files": snapshot.files,
    }))
    .map_err(Into::into)
}

fn snapshot_tree(root: &Dir) -> Result<TreeSnapshot, LifecycleError> {
    let mut snapshot = TreeSnapshot {
        directories: Vec::new(),
        files: Vec::new(),
    };
    let mut entry_count = 0_usize;
    let mut retained_bytes = 0_usize;
    let mut frames = vec![SnapshotFrame::open(
        root.try_clone()?,
        String::new(),
        &mut entry_count,
    )?];
    while let Some(frame) = frames.last_mut() {
        if frame.next == frame.names.len() {
            frames.pop();
            continue;
        }
        let name = frame.names[frame.next].clone();
        frame.next += 1;
        let text = name
            .to_str()
            .ok_or_else(|| invalid_error("rollback point snapshot path is not UTF-8"))?;
        let relative = if frame.prefix.is_empty() {
            text.to_owned()
        } else {
            format!("{}/{text}", frame.prefix)
        };
        validate_rollback_snapshot_path(&relative)?;
        reserve_rollback_bytes(&mut retained_bytes, relative.len())?;
        let metadata = frame.directory.symlink_metadata(&name)?;
        if metadata.file_type().is_symlink() {
            return invalid(format!(
                "install tree must not contain symlinks: {relative}"
            ));
        }
        let mode = snapshot_mode(&metadata);
        if metadata.is_dir() {
            snapshot
                .directories
                .push(json!({"mode": mode, "path": relative}));
            let child = open_child_directory(
                &frame.directory,
                text,
                Some(mode),
                "rollback point snapshot directory",
            )?;
            frames.push(SnapshotFrame::open(child, relative, &mut entry_count)?);
        } else if metadata.is_file() {
            if metadata.nlink() != 1 {
                return invalid(format!(
                    "rollback point file has an unsafe hard-link alias: {relative}"
                ));
            }
            reserve_rollback_bytes(
                &mut retained_bytes,
                usize::try_from(metadata.len()).map_err(|_| {
                    LifecycleError::Invalid(
                        "rollback point file length does not fit this host".to_owned(),
                    )
                })?,
            )?;
            let sha256 = packages::hash_child_file(
                &frame.directory,
                text,
                mode,
                "rollback point snapshot file",
            )?;
            let current = frame.directory.symlink_metadata(&name)?;
            if current.file_type().is_symlink()
                || !current.is_file()
                || current.nlink() != 1
                || !same_object_cap(&metadata, &current)
                || !same_content_state_cap(&metadata, &current)
            {
                return invalid(format!(
                    "rollback point file changed while snapshotting: {relative}"
                ));
            }
            snapshot
                .files
                .push(json!({"mode": mode, "path": relative, "sha256": sha256}));
        } else {
            return invalid(format!(
                "install tree contains unsupported entry: {relative}"
            ));
        }
    }
    snapshot.directories.sort_by(|left, right| {
        left.get("path")
            .and_then(Value::as_str)
            .cmp(&right.get("path").and_then(Value::as_str))
    });
    snapshot.files.sort_by(|left, right| {
        left.get("path")
            .and_then(Value::as_str)
            .cmp(&right.get("path").and_then(Value::as_str))
    });
    Ok(snapshot)
}

fn reserve_rollback_bytes(total: &mut usize, additional: usize) -> Result<(), LifecycleError> {
    *total = total.checked_add(additional).ok_or_else(|| {
        LifecycleError::Invalid("rollback point retained byte counter overflow".to_owned())
    })?;
    if *total > MAX_ROLLBACK_RETAINED_BYTES {
        return invalid(format!(
            "rollback point exceeds maximum retained content of {MAX_ROLLBACK_RETAINED_BYTES} bytes"
        ));
    }
    Ok(())
}

fn reserve_rollback_entry(entries: &mut usize) -> Result<(), LifecycleError> {
    *entries = entries.checked_add(1).ok_or_else(|| {
        LifecycleError::Invalid("rollback point entry counter overflow".to_owned())
    })?;
    if *entries > MAX_ROLLBACK_ENTRIES {
        return invalid(format!(
            "rollback point exceeds maximum of {MAX_ROLLBACK_ENTRIES} entries"
        ));
    }
    Ok(())
}

fn validate_rollback_snapshot_path(relative: &str) -> Result<(), LifecycleError> {
    if relative.len() > MAX_ROLLBACK_PATH_BYTES {
        return invalid("rollback point snapshot path exceeds the length limit");
    }
    if relative.split('/').count() > MAX_ROLLBACK_DEPTH {
        return invalid("rollback point snapshot exceeds the depth limit");
    }
    Ok(())
}

#[cfg(unix)]
fn snapshot_mode(metadata: &cap_std::fs::Metadata) -> u32 {
    use cap_std::fs::MetadataExt as _;
    metadata.mode() & 0o777
}

#[cfg(windows)]
fn snapshot_mode(metadata: &cap_std::fs::Metadata) -> u32 {
    use cap_std::fs::MetadataExt as _;
    const FILE_ATTRIBUTE_READONLY: u32 = 0x1;
    if metadata.is_dir() {
        0o777
    } else if metadata.file_attributes() & FILE_ATTRIBUTE_READONLY == 0 {
        0o666
    } else {
        0o444
    }
}

#[cfg(not(any(unix, windows)))]
fn snapshot_mode(metadata: &cap_std::fs::Metadata) -> u32 {
    if metadata.is_dir() { 0o777 } else { 0o666 }
}

#[cfg(unix)]
const fn rollback_external_directory_mode() -> u32 {
    MANAGED_DIRECTORY_MODE
}

#[cfg(not(unix))]
const fn rollback_external_directory_mode() -> u32 {
    0o777
}

fn open_relative_parent(
    root: &Dir,
    relative: &str,
    label: &str,
) -> Result<(Dir, String), LifecycleError> {
    let components = relative_components(relative)?;
    let (name, parents) = components
        .split_last()
        .ok_or_else(|| invalid_error(format!("{label} must be a package-relative path")))?;
    let mut directory = root.try_clone()?;
    for parent in parents {
        directory = open_child_directory(&directory, parent, None, label)?;
    }
    Ok((directory, (*name).clone()))
}

fn relative_exists(root: &Dir, relative: &str) -> Result<bool, LifecycleError> {
    let components = relative_components(relative)?;
    let (name, parents) = components
        .split_last()
        .ok_or_else(|| invalid_error("external snapshot path is invalid"))?;
    let mut directory = root.try_clone()?;
    for parent in parents {
        match directory.symlink_metadata(parent) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error.into()),
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return Ok(true);
            }
            Ok(_) => {
                directory =
                    open_child_directory(&directory, parent, None, "external snapshot path")?;
            }
        }
    }
    match directory.symlink_metadata(name) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn validate_external_path(relative: &str) -> Result<(), LifecycleError> {
    let parts = relative_components(relative)?;
    if parts
        .first()
        .is_some_and(|part| MANAGED_ROOTS.contains(&part.as_str()))
    {
        return invalid(format!(
            "external lifecycle file overlaps a managed root: {relative}"
        ));
    }
    Ok(())
}

fn relative_components(relative: &str) -> Result<Vec<String>, LifecycleError> {
    if relative.is_empty() || relative.starts_with('/') {
        return invalid("external lifecycle file must be a package-relative path");
    }
    let mut parts = Vec::new();
    for component in Path::new(relative).components() {
        match component {
            Component::Normal(part) => parts.push(
                part.to_str()
                    .ok_or_else(|| invalid_error("external lifecycle path is not UTF-8"))?
                    .to_owned(),
            ),
            Component::CurDir => {}
            Component::ParentDir | Component::Prefix(_) | Component::RootDir => {
                return invalid("external lifecycle file must be a package-relative path");
            }
        }
    }
    if parts.is_empty() {
        return invalid("external lifecycle file must be a package-relative path");
    }
    Ok(parts)
}

fn entry_names(directory: &Dir) -> Result<Vec<String>, LifecycleError> {
    let mut names = directory
        .entries()?
        .map(|entry| {
            entry.map_err(LifecycleError::from).and_then(|entry| {
                entry
                    .file_name()
                    .to_str()
                    .map(str::to_owned)
                    .ok_or_else(|| invalid_error("rollback point path is not UTF-8"))
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    names.sort();
    Ok(names)
}

fn values_by_id(values: &[Value]) -> Result<BTreeMap<String, &Value>, LifecycleError> {
    values
        .iter()
        .map(|value| {
            Ok((
                value
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_error("package id is invalid"))?
                    .to_owned(),
                value,
            ))
        })
        .collect()
}

fn project_array(
    value: &Value,
    field: &str,
    fields: &[&str],
) -> Result<Vec<Value>, LifecycleError> {
    array(value, field, field)?
        .iter()
        .map(|item| {
            let mut projected = Map::new();
            for field in fields {
                projected.insert(
                    (*field).to_owned(),
                    item.get(*field)
                        .cloned()
                        .ok_or_else(|| invalid_error("semantic field is missing"))?,
                );
            }
            Ok(Value::Object(projected))
        })
        .collect()
}

fn array<'a>(value: &'a Value, field: &str, label: &str) -> Result<&'a [Value], LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| invalid_error(format!("{label} is invalid")))
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, LifecycleError> {
    value
        .as_object()
        .ok_or_else(|| invalid_error(format!("{label} is invalid")))
}

fn exact_fields(object: &Map<String, Value>, expected: &[&str]) -> bool {
    object.len() == expected.len() && expected.iter().all(|field| object.contains_key(*field))
}

fn mode(value: Option<&Value>) -> Option<u32> {
    value
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .filter(|value| *value <= 0o777)
}

fn invalid_error(message: impl Into<String>) -> LifecycleError {
    LifecycleError::Invalid(message.into())
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(invalid_error(message))
}

#[cfg(test)]
mod tests {
    use super::*;
    use cap_std::ambient_authority;

    #[test]
    fn rollback_snapshot_limits_are_inclusive_and_overflow_safe() {
        let mut entries = MAX_ROLLBACK_ENTRIES - 1;
        reserve_rollback_entry(&mut entries).expect("exact entry limit");
        assert!(reserve_rollback_entry(&mut entries).is_err());
        let mut overflowed_entries = usize::MAX;
        assert!(reserve_rollback_entry(&mut overflowed_entries).is_err());

        let mut retained = MAX_ROLLBACK_RETAINED_BYTES - 1;
        reserve_rollback_bytes(&mut retained, 1).expect("exact retained-byte limit");
        assert!(reserve_rollback_bytes(&mut retained, 1).is_err());
        let mut overflowed_bytes = usize::MAX;
        assert!(reserve_rollback_bytes(&mut overflowed_bytes, 1).is_err());

        validate_rollback_snapshot_path(&"x".repeat(MAX_ROLLBACK_PATH_BYTES))
            .expect("exact path limit");
        assert!(validate_rollback_snapshot_path(&"x".repeat(MAX_ROLLBACK_PATH_BYTES + 1)).is_err());
        validate_rollback_snapshot_path(&vec!["x"; MAX_ROLLBACK_DEPTH].join("/"))
            .expect("exact depth limit");
        assert!(
            validate_rollback_snapshot_path(&vec!["x"; MAX_ROLLBACK_DEPTH + 1].join("/")).is_err()
        );
    }

    #[test]
    fn rollback_snapshot_rejects_oversized_file_before_hashing() {
        let temporary = tempfile::tempdir().expect("create rollback limit fixture");
        let path = temporary.path().join("oversized");
        let file = std::fs::File::create(&path).expect("create sparse rollback file");
        file.set_len((MAX_ROLLBACK_RETAINED_BYTES + 1) as u64)
            .expect("size sparse rollback file");
        drop(file);
        let root = Dir::open_ambient_dir(temporary.path(), ambient_authority())
            .expect("open rollback limit fixture");

        let error = snapshot_identity(&root).expect_err("oversized snapshot must fail");

        assert!(
            error.to_string().contains("maximum retained content"),
            "{error}"
        );
    }
}
