use super::{
    LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE, PERSISTENT_PACKAGE_LOCK,
    load_json_file, open_child_directory, open_child_file, packages, post_install,
    same_content_state_cap, same_object_cap, valid_sha256, validate_activation_lock_contract,
};
use agent_contracts::canonical_sha256;
use agent_engine::{install_plan_identity_hash, validate_install_plan, validate_package_lock};
use cap_std::fs::Dir;
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path};

const ROLLBACK_POINT_DIRECTORY: &str = "rollback-point";
const EXTERNAL_ACTIVATION_LOCK: &str = "activation-lock.json";
const EXTERNAL_FILES_DIRECTORY: &str = "external-files";
const EXTERNAL_STATE_FILE: &str = "external-state.json";
const MANAGED_ROOTS: [&str; 4] = [
    ".agent-skills",
    ".agent-skills-lifecycle.lock",
    "AGENTS.md",
    "skills",
];

struct ExternalState {
    entries: Vec<Value>,
    fingerprint: String,
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
    let root_identity = root.dir_metadata()?;
    validate_root_entries(&root)?;

    for name in [
        "AGENTS.md",
        PERSISTENT_PACKAGE_LOCK,
        "install-lock.json",
        "rollback-point.json",
    ] {
        open_child_file(&root, name, MANAGED_FILE_MODE, "rollback point file").map_err(|_| {
            invalid_error(format!("rollback point file is missing or unsafe: {name}"))
        })?;
    }
    let packages_root = open_child_directory(
        &root,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point packages directory",
    )
    .map_err(|_| invalid_error("rollback point directory is missing or unsafe: packages"))?;
    let skills_root = open_child_directory(
        &root,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "rollback point skills directory",
    )
    .map_err(|_| invalid_error("rollback point directory is missing or unsafe: skills"))?;

    let install_lock = load_json_file(
        &root,
        "install-lock.json",
        MANAGED_FILE_MODE,
        "rollback point Install Lock",
    )?;
    let package_lock = load_json_file(
        &root,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "rollback point package Lockfile",
    )?;
    let point = load_json_file(
        &root,
        "rollback-point.json",
        MANAGED_FILE_MODE,
        "rollback point contract",
    )?;
    let external = validate_external_state(&root)?;
    validate_activation_snapshot(&root, &external.entries)?;

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
        &root,
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
    validate_semantics(&root, &install_lock, &package_lock, &semantics)?;

    let snapshot = snapshot_identity(&root)?;
    if point_object.get("snapshot_sha256").and_then(Value::as_str) != Some(snapshot.as_str()) {
        return invalid("rollback point snapshot digest is invalid");
    }
    let final_metadata = root.dir_metadata()?;
    if !same_object_cap(&root_identity, &final_metadata)
        || !same_content_state_cap(&root_identity, &final_metadata)
    {
        return invalid("rollback point directory changed while inspecting");
    }
    validate_root_entries(&root)?;

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
    if snapshot.directories.iter().any(|entry| {
        entry.get("mode").and_then(Value::as_u64) != Some(MANAGED_DIRECTORY_MODE.into())
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
        entries: entries.clone(),
        fingerprint: fingerprint.to_owned(),
    })
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
        if external.is_none_or(|entry| {
            entry.get("state").and_then(Value::as_str) != Some("file")
                || entry.get("mode") != record_object.get("mode")
                || entry.get("sha256") != record_object.get("sha256")
        }) {
            return invalid("rollback point activation lock differs from external snapshot");
        }
    }
    Ok(())
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
    fn open(directory: Dir, prefix: String) -> Result<Self, LifecycleError> {
        let mut names = directory
            .entries()?
            .map(|entry| entry.map(|entry| entry.file_name()))
            .collect::<Result<Vec<_>, _>>()?;
        names.sort();
        Ok(Self {
            directory,
            names,
            next: 0,
            prefix,
        })
    }
}

fn snapshot_identity(root: &Dir) -> Result<String, LifecycleError> {
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
    let mut frames = vec![SnapshotFrame::open(root.try_clone()?, String::new())?];
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
            frames.push(SnapshotFrame::open(child, relative)?);
        } else if metadata.is_file() {
            let sha256 = packages::hash_child_file(
                &frame.directory,
                text,
                mode,
                "rollback point snapshot file",
            )?;
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

fn validate_external_path(relative: &str) -> Result<(), LifecycleError> {
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
