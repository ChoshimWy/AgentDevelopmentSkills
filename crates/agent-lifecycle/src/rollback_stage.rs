use super::{
    EXTERNAL_ACTIVATION_LOCK, LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    PERSISTENT_PACKAGE_LOCK, check_activation, configure_nofollow, external_stage,
    ignored_os_metadata, load_json_file, open_child_directory, open_child_file, packages,
    post_install, rollback, same_content_state_cap, same_object_cap, staged_tree,
};
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256};
use agent_engine::{install_plan_identity_hash, validate_install_plan, validate_package_lock};
use cap_fs_ext::{FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, OpenOptions};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::io::{Read as _, Write as _};
use std::path::{Component, Path};

const INSTALL_LOCK: &str = "install-lock.json";
const ROLLBACK_POINT_DIRECTORY: &str = "rollback-point";
const ROLLBACK_POINT_FILE: &str = "rollback-point.json";
const EXTERNAL_FILES_DIRECTORY: &str = "external-files";
const EXTERNAL_STATE_FILE: &str = "external-state.json";
const MAX_EXTERNAL_PATHS: usize = 100_000;
const MAX_EXTERNAL_PATH_BYTES: usize = 4_096;

#[derive(Clone, Debug)]
pub(super) struct RollbackStageSnapshot {
    point: Value,
    source: SourceInstallSnapshot,
}

impl RollbackStageSnapshot {
    pub(super) fn fingerprint(&self) -> Result<&str, LifecycleError> {
        self.point
            .get("fingerprint")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                LifecycleError::Invalid("staged rollback point fingerprint is invalid".to_owned())
            })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SourceInstallSnapshot {
    external_state: Value,
    managed: ManagedInstallSnapshot,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ManagedInstallSnapshot {
    activation: Option<Vec<u8>>,
    agents: Vec<u8>,
    install_lock: Value,
    install_lock_bytes: Vec<u8>,
    package_lock: Value,
    package_lock_bytes: Vec<u8>,
}

#[allow(clippy::too_many_lines)]
pub(super) fn stage(
    target: &Dir,
    stage: &Dir,
    external_paths: &[String],
) -> Result<RollbackStageSnapshot, LifecycleError> {
    let normalized = normalize_external_paths(external_paths)?;
    let source = inspect_source_install(target, &normalized)?;
    let managed = open_child_directory(
        stage,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    let root = external_stage::create_directory(
        &managed,
        OsStr::new(ROLLBACK_POINT_DIRECTORY),
        Some(MANAGED_DIRECTORY_MODE),
        "staged rollback point",
    )?;
    let packages_root = external_stage::create_directory(
        &root,
        OsStr::new("packages"),
        Some(MANAGED_DIRECTORY_MODE),
        "staged rollback packages",
    )?;
    let skills_root = external_stage::create_directory(
        &root,
        OsStr::new("skills"),
        Some(MANAGED_DIRECTORY_MODE),
        "staged rollback Skills",
    )?;
    let external_files = external_stage::create_directory(
        &root,
        OsStr::new(EXTERNAL_FILES_DIRECTORY),
        Some(MANAGED_DIRECTORY_MODE),
        "staged rollback external files",
    )?;

    external_stage::write_independent_file(
        &root,
        "AGENTS.md",
        &source.managed.agents,
        MANAGED_FILE_MODE,
        "staged rollback AGENTS.md",
    )?;
    external_stage::write_independent_file(
        &root,
        INSTALL_LOCK,
        &source.managed.install_lock_bytes,
        MANAGED_FILE_MODE,
        "staged rollback Install Lock",
    )?;
    external_stage::write_independent_file(
        &root,
        PERSISTENT_PACKAGE_LOCK,
        &source.managed.package_lock_bytes,
        MANAGED_FILE_MODE,
        "staged rollback package Lockfile",
    )?;
    if let Some(bytes) = source.managed.activation.as_deref() {
        external_stage::write_independent_file(
            &root,
            EXTERNAL_ACTIVATION_LOCK,
            bytes,
            MANAGED_FILE_MODE,
            "staged rollback Activation Lock",
        )?;
    }

    let source_managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "source managed metadata",
    )?;
    let source_packages = open_child_directory(
        &source_managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "source packages",
    )?;
    for record in records(&source.managed.install_lock, "packages", "source packages")? {
        let id = record
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| LifecycleError::Invalid("source package id is invalid".to_owned()))?;
        let package = open_child_directory(
            &source_packages,
            id,
            record_mode(record, "root_mode")?,
            "source package",
        )?;
        staged_tree::stage_package_at(&packages_root, &package, record)?;
    }
    let source_skills = open_child_directory(
        target,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "source Skills root",
    )?;
    for record in records(&source.managed.install_lock, "skills", "source Skills")? {
        let name = record
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| LifecycleError::Invalid("source Skill name is invalid".to_owned()))?;
        let skill = open_child_directory(
            &source_skills,
            name,
            record_mode(record, "root_mode")?,
            "source Skill",
        )?;
        staged_tree::stage_skill_at(&skills_root, &skill, record)?;
    }

    copy_external_files(target, &external_files, &source.external_state)?;
    external_stage::write_independent_file(
        &root,
        EXTERNAL_STATE_FILE,
        &canonical_json(&source.external_state)?,
        MANAGED_FILE_MODE,
        "staged rollback external state",
    )?;
    let snapshot_sha256 = rollback::snapshot_identity(&root)?;
    let package_lock_hash = source
        .managed
        .package_lock
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("source package Lockfile fingerprint is invalid".to_owned())
        })?;
    let mut point = json!({
        "external_state_sha256": source.external_state.get("fingerprint").cloned().unwrap_or(Value::Null),
        "install_plan_fingerprint": source.managed.install_lock.get("fingerprint").cloned().unwrap_or(Value::Null),
        "manager": "agent-development-skills",
        "package_lock_hash": package_lock_hash,
        "point_id": format!("rollback-{}", &package_lock_hash[..12]),
        "schema_version": "1.0",
        "snapshot_sha256": snapshot_sha256,
    });
    point["fingerprint"] = Value::String(canonical_sha256(&point)?);
    external_stage::write_independent_file(
        &root,
        ROLLBACK_POINT_FILE,
        &canonical_json(&point)?,
        MANAGED_FILE_MODE,
        "staged rollback point contract",
    )?;
    rollback::validate_rollback_point_root(&root)?;
    let snapshot = RollbackStageSnapshot { point, source };
    verify(target, stage, &snapshot, &normalized)?;
    Ok(snapshot)
}

pub(super) fn verify(
    target: &Dir,
    stage: &Dir,
    expected: &RollbackStageSnapshot,
    external_paths: &[String],
) -> Result<(), LifecycleError> {
    let normalized = normalize_external_paths(external_paths)?;
    if inspect_source_install(target, &normalized)? != expected.source {
        return invalid("rollback point source installation changed after staging");
    }
    let managed = open_child_directory(
        stage,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    let root = open_child_directory(
        &managed,
        ROLLBACK_POINT_DIRECTORY,
        Some(MANAGED_DIRECTORY_MODE),
        "staged rollback point",
    )?;
    rollback::validate_rollback_point_root(&root)?;
    if load_json_file(
        &root,
        ROLLBACK_POINT_FILE,
        MANAGED_FILE_MODE,
        "staged rollback point contract",
    )? != expected.point
    {
        return invalid("staged rollback point identity changed after staging");
    }
    if inspect_source_install(target, &normalized)? != expected.source {
        return invalid("rollback point source installation changed while verifying");
    }
    Ok(())
}

pub(super) fn verify_published(
    target: &Dir,
    expected: &RollbackStageSnapshot,
) -> Result<(), LifecycleError> {
    verify_staged(target, expected)
}

pub(super) fn verify_staged(
    target: &Dir,
    expected: &RollbackStageSnapshot,
) -> Result<(), LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    let root = open_child_directory(
        &managed,
        ROLLBACK_POINT_DIRECTORY,
        Some(MANAGED_DIRECTORY_MODE),
        "staged rollback point",
    )?;
    rollback::validate_rollback_point_root(&root)?;
    if load_json_file(
        &root,
        ROLLBACK_POINT_FILE,
        MANAGED_FILE_MODE,
        "staged rollback point contract",
    )? != expected.point
    {
        return invalid("staged rollback point identity changed after swap");
    }
    Ok(())
}

fn inspect_source_install(
    target: &Dir,
    external_paths: &[String],
) -> Result<SourceInstallSnapshot, LifecycleError> {
    let managed = inspect_managed_install(target)?;
    check_activation(target)?;
    let external_state = inspect_external_state(target, external_paths)?;
    Ok(SourceInstallSnapshot {
        external_state,
        managed,
    })
}

pub(super) fn verify_backup(
    backup: &Dir,
    expected: &RollbackStageSnapshot,
) -> Result<(), LifecycleError> {
    validate_backup_entries(backup)?;
    verify_restored(backup, expected)
}

pub(super) fn verify_restored(
    target: &Dir,
    expected: &RollbackStageSnapshot,
) -> Result<(), LifecycleError> {
    if inspect_managed_install(target)? != expected.source.managed {
        return invalid("restored managed installation differs from rollback source");
    }
    Ok(())
}

fn inspect_managed_install(target: &Dir) -> Result<ManagedInstallSnapshot, LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "source managed metadata",
    )?;
    let install_lock = load_json_file(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        "source Install Lock",
    )?;
    let package_lock = load_json_file(
        &managed,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "source package Lockfile",
    )?;
    validate_install_plan(&install_lock)?;
    validate_package_lock(&package_lock)?;
    if install_lock.get("status").and_then(Value::as_str) != Some("installed") {
        return invalid("persistent rollback requires an installed source Install Lock");
    }
    if install_lock.get("package_lock_hash") != package_lock.get("fingerprint")
        || package_lock
            .get("install_plan_identity_hash")
            .and_then(Value::as_str)
            != Some(install_plan_identity_hash(&install_lock)?.as_str())
    {
        return invalid("persistent rollback source Lockfile identities are inconsistent");
    }
    validate_managed_entries(&managed)?;
    rollback::check_rollback_point(target)?;

    let mut semantics = None;
    packages::check_package_integrity(target, &install_lock, &package_lock, &mut semantics)?;
    post_install::check_skill_integrity(target, &install_lock, semantics.as_ref())?;
    post_install::check_global_instructions(
        target,
        &install_lock,
        &package_lock,
        semantics.as_ref(),
    )?;
    post_install::check_binding_freeze(&install_lock, &package_lock, semantics.as_ref())?;
    post_install::check_permission_freeze(&install_lock, &package_lock, semantics.as_ref())?;

    let agents = read_stable_file(
        target,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        None,
        "source AGENTS.md",
    )?;
    let expected_agents_hash = install_lock
        .pointer("/instructions/sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("source Install Lock instructions are invalid".to_owned())
        })?;
    if bytes_sha256(&agents) != expected_agents_hash {
        return invalid("source AGENTS.md differs from Install Lock");
    }
    let install_lock_bytes = read_stable_file(
        &managed,
        INSTALL_LOCK,
        MANAGED_FILE_MODE,
        Some(MAX_CONTRACT_JSON_BYTES),
        "source Install Lock",
    )?;
    let package_lock_bytes = read_stable_file(
        &managed,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        Some(MAX_CONTRACT_JSON_BYTES),
        "source package Lockfile",
    )?;
    let activation = match managed.symlink_metadata(EXTERNAL_ACTIVATION_LOCK) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            return invalid("source Activation Lock is missing or unsafe");
        }
        Ok(_) => Some(read_stable_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            MANAGED_FILE_MODE,
            Some(MAX_CONTRACT_JSON_BYTES),
            "source Activation Lock",
        )?),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error.into()),
    };
    Ok(ManagedInstallSnapshot {
        activation,
        agents,
        install_lock,
        install_lock_bytes,
        package_lock,
        package_lock_bytes,
    })
}

fn validate_backup_entries(backup: &Dir) -> Result<(), LifecycleError> {
    let mut actual = BTreeSet::new();
    for entry in backup.entries()? {
        let entry = entry?;
        let name = entry.file_name();
        if ignored_os_metadata(backup, &name)? {
            continue;
        }
        actual.insert(
            name.to_str()
                .ok_or_else(|| {
                    LifecycleError::Invalid("recovery backup contains a non-UTF-8 entry".to_owned())
                })?
                .to_owned(),
        );
    }
    if actual
        != BTreeSet::from([
            ".agent-skills".to_owned(),
            "AGENTS.md".to_owned(),
            "skills".to_owned(),
        ])
    {
        return invalid("recovery backup managed roots are incomplete");
    }
    Ok(())
}

fn validate_managed_entries(managed: &Dir) -> Result<(), LifecycleError> {
    let mut actual = BTreeSet::new();
    for entry in managed.entries()? {
        let entry = entry?;
        let name = entry.file_name();
        if ignored_os_metadata(managed, &name)? {
            continue;
        }
        actual.insert(
            name.to_str()
                .ok_or_else(|| {
                    LifecycleError::Invalid(
                        "source managed metadata contains a non-UTF-8 entry".to_owned(),
                    )
                })?
                .to_owned(),
        );
    }
    let mut expected = BTreeSet::from([
        INSTALL_LOCK.to_owned(),
        PERSISTENT_PACKAGE_LOCK.to_owned(),
        "packages".to_owned(),
    ]);
    for optional in [EXTERNAL_ACTIVATION_LOCK, ROLLBACK_POINT_DIRECTORY] {
        if actual.contains(optional) {
            expected.insert(optional.to_owned());
        }
    }
    if actual != expected {
        return invalid("persistent rollback source contains unknown managed metadata");
    }
    Ok(())
}

fn inspect_external_state(target: &Dir, paths: &[String]) -> Result<Value, LifecycleError> {
    let mut directory_set = BTreeSet::new();
    for path in paths {
        let components = normalized_components(path)?;
        for count in 1..components.len() {
            directory_set.insert(components[..count].join("/"));
        }
    }
    let mut directories = Vec::with_capacity(directory_set.len());
    for path in directory_set {
        match inspect_directory_path(target, &path)? {
            Some(mode) => directories.push(json!({
                "mode": mode,
                "path": path,
                "state": "directory",
            })),
            None => directories.push(json!({"path": path, "state": "absent"})),
        }
    }
    let mut entries = Vec::with_capacity(paths.len());
    for path in paths {
        match inspect_file_path(target, path)? {
            Some((mode, sha256)) => entries.push(json!({
                "mode": mode,
                "path": path,
                "sha256": sha256,
                "state": "file",
            })),
            None => entries.push(json!({"path": path, "state": "absent"})),
        }
    }
    let mut state = json!({
        "directories": directories,
        "entries": entries,
        "schema_version": "1.0",
    });
    state["fingerprint"] = Value::String(canonical_sha256(&state)?);
    Ok(state)
}

fn inspect_directory_path(root: &Dir, relative: &str) -> Result<Option<u32>, LifecycleError> {
    let components = normalized_components(relative)?;
    let mut directory = root.try_clone()?;
    for (index, component) in components.iter().enumerate() {
        match directory.symlink_metadata(component) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!(
                    "external lifecycle directory is unsafe: {}",
                    components[..=index].join("/")
                ));
            }
            Ok(_) => {
                directory = external_stage::open_directory(
                    &directory,
                    OsStr::new(component),
                    None,
                    "external lifecycle directory",
                )?;
            }
        }
    }
    Ok(Some(snapshot_mode(&directory.dir_metadata()?, true)))
}

fn inspect_file_path(root: &Dir, relative: &str) -> Result<Option<(u32, String)>, LifecycleError> {
    let components = normalized_components(relative)?;
    let (name, parents) = components.split_last().ok_or_else(|| {
        LifecycleError::Invalid("external lifecycle file path is invalid".to_owned())
    })?;
    let mut directory = root.try_clone()?;
    for (index, component) in parents.iter().enumerate() {
        match directory.symlink_metadata(component) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!(
                    "external lifecycle directory is unsafe: {}",
                    parents[..=index].join("/")
                ));
            }
            Ok(_) => {
                directory = external_stage::open_directory(
                    &directory,
                    OsStr::new(component),
                    None,
                    "external lifecycle directory",
                )?;
            }
        }
    }
    match directory.symlink_metadata(name) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => invalid(
            format!("external lifecycle file is not a regular file: {relative}"),
        ),
        Ok(metadata) => {
            let mode = snapshot_mode(&metadata, false);
            let sha256 =
                packages::hash_child_file(&directory, name, mode, "external lifecycle file")?;
            Ok(Some((mode, sha256)))
        }
    }
}

fn copy_external_files(
    target: &Dir,
    destination: &Dir,
    state: &Value,
) -> Result<(), LifecycleError> {
    let entries = state
        .get("entries")
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid("external state entries are invalid".to_owned()))?;
    for entry in entries {
        if entry.get("state").and_then(Value::as_str) != Some("file") {
            continue;
        }
        let relative = entry
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| LifecycleError::Invalid("external state path is invalid".to_owned()))?;
        let expected_mode = record_mode(entry, "mode")?.ok_or_else(|| {
            LifecycleError::Invalid("external state file mode is invalid".to_owned())
        })?;
        let expected_hash = entry.get("sha256").and_then(Value::as_str).ok_or_else(|| {
            LifecycleError::Invalid("external state file hash is invalid".to_owned())
        })?;
        let components = normalized_components(relative)?;
        let (name, parents) = components.split_last().ok_or_else(|| {
            LifecycleError::Invalid("external state file path is invalid".to_owned())
        })?;
        let source_parent = open_existing_parents(target, parents, "external lifecycle file")?;
        let destination_parent = ensure_destination_parents(destination, parents)?;
        copy_expected_file(
            &source_parent,
            &destination_parent,
            name,
            expected_mode,
            expected_hash,
            relative,
        )?;
    }
    Ok(())
}

fn copy_expected_file(
    source_parent: &Dir,
    destination_parent: &Dir,
    name: &str,
    mode: u32,
    expected_hash: &str,
    relative: &str,
) -> Result<(), LifecycleError> {
    let mut source =
        external_stage::open_regular_file(source_parent, OsStr::new(name), Some(mode), relative)?;
    let opened_source = source.metadata()?;
    let mut options = OpenOptions::new();
    options
        .write(true)
        .create_new(true)
        .follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt as _;
        options.mode(mode);
    }
    let mut destination = destination_parent.open_with(name, &options)?;
    let opened_destination = destination.metadata()?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    loop {
        let count = source.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        destination.write_all(&buffer[..count])?;
    }
    destination.flush()?;
    set_snapshot_file_mode(&destination, mode)?;
    if format!("{:x}", digest.finalize()) != expected_hash {
        return invalid(format!(
            "external lifecycle file changed before snapshot: {relative}"
        ));
    }
    let completed = destination.metadata()?;
    if snapshot_mode(&completed, false) != mode {
        return invalid(format!(
            "external snapshot file mode differs from source: {relative}"
        ));
    }
    let current = external_stage::open_regular_file(
        destination_parent,
        OsStr::new(name),
        Some(mode),
        relative,
    )?
    .metadata()?;
    if !same_object_cap(&opened_destination, &completed)
        || !same_object_cap(&opened_destination, &current)
        || !same_content_state_cap(&completed, &current)
        || completed.nlink() != 1
        || current.nlink() != 1
    {
        return invalid(format!(
            "external snapshot file changed while copying: {relative}"
        ));
    }
    let after_source = source.metadata()?;
    let current_source =
        external_stage::open_regular_file(source_parent, OsStr::new(name), Some(mode), relative)?
            .metadata()?;
    if !same_object_cap(&opened_source, &after_source)
        || !same_object_cap(&opened_source, &current_source)
        || !same_content_state_cap(&opened_source, &after_source)
        || !same_content_state_cap(&opened_source, &current_source)
    {
        return invalid(format!(
            "external lifecycle file changed while snapshotting: {relative}"
        ));
    }
    Ok(())
}

fn open_existing_parents(
    root: &Dir,
    components: &[String],
    label: &str,
) -> Result<Dir, LifecycleError> {
    let mut directory = root.try_clone()?;
    for component in components {
        directory = external_stage::open_directory(&directory, OsStr::new(component), None, label)?;
    }
    Ok(directory)
}

fn ensure_destination_parents(root: &Dir, components: &[String]) -> Result<Dir, LifecycleError> {
    let mut directory = root.try_clone()?;
    for component in components {
        directory = match directory.symlink_metadata(component) {
            Ok(_) => external_stage::open_directory(
                &directory,
                OsStr::new(component),
                Some(MANAGED_DIRECTORY_MODE),
                "staged rollback external directory",
            )?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                external_stage::create_directory(
                    &directory,
                    OsStr::new(component),
                    Some(MANAGED_DIRECTORY_MODE),
                    "staged rollback external directory",
                )?
            }
            Err(error) => return Err(error.into()),
        };
    }
    Ok(directory)
}

fn normalize_external_paths(values: &[String]) -> Result<Vec<String>, LifecycleError> {
    if values.len() > MAX_EXTERNAL_PATHS {
        return invalid("external lifecycle files exceed the path limit");
    }
    let mut normalized = Vec::with_capacity(values.len());
    for value in values {
        let path = normalized_components(value)?.join("/");
        if path.len() > MAX_EXTERNAL_PATH_BYTES {
            return invalid("external lifecycle file path exceeds the length limit");
        }
        rollback::validate_external_path(&path)?;
        normalized.push(path);
    }
    if !normalized.windows(2).all(|pair| pair[0] < pair[1]) {
        return invalid("external lifecycle files must be sorted and unique");
    }
    Ok(normalized)
}

fn normalized_components(relative: &str) -> Result<Vec<String>, LifecycleError> {
    if relative.is_empty() || Path::new(relative).is_absolute() {
        return invalid("external lifecycle file must be a package-relative path");
    }
    let mut components = Vec::new();
    for component in Path::new(relative).components() {
        match component {
            Component::Normal(part) => components.push(
                part.to_str()
                    .ok_or_else(|| {
                        LifecycleError::Invalid("external lifecycle path is not UTF-8".to_owned())
                    })?
                    .to_owned(),
            ),
            Component::CurDir => {}
            Component::ParentDir | Component::Prefix(_) | Component::RootDir => {
                return invalid("external lifecycle file must be a package-relative path");
            }
        }
    }
    if components.is_empty() {
        return invalid("external lifecycle file must be a package-relative path");
    }
    Ok(components)
}

fn read_stable_file(
    parent: &Dir,
    name: &str,
    mode: u32,
    maximum: Option<usize>,
    label: &str,
) -> Result<Vec<u8>, LifecycleError> {
    let mut file = open_child_file(parent, name, mode, label)?;
    let opened = file.metadata()?;
    if maximum.is_some_and(|limit| opened.len() > limit as u64) {
        return invalid(format!("{label} exceeds the size limit"));
    }
    let mut bytes = Vec::with_capacity(
        usize::try_from(opened.len())
            .unwrap_or_else(|_| maximum.unwrap_or_default())
            .min(maximum.unwrap_or(usize::MAX)),
    );
    match maximum {
        Some(limit) => {
            std::io::Read::by_ref(&mut file)
                .take((limit + 1) as u64)
                .read_to_end(&mut bytes)?;
            if bytes.len() > limit {
                return invalid(format!("{label} exceeds the size limit"));
            }
        }
        None => {
            file.read_to_end(&mut bytes)?;
        }
    }
    let after = file.metadata()?;
    let current = open_child_file(parent, name, mode, label)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
    {
        return invalid(format!("{label} changed while reading"));
    }
    Ok(bytes)
}

fn records<'a>(value: &'a Value, field: &str, label: &str) -> Result<&'a [Value], LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} are invalid")))
}

fn record_mode(value: &Value, field: &str) -> Result<Option<u32>, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .and_then(|mode| u32::try_from(mode).ok())
        .filter(|mode| *mode <= 0o777)
        .map(Some)
        .ok_or_else(|| LifecycleError::Invalid(format!("{field} is invalid")))
}

#[cfg(unix)]
fn snapshot_mode(metadata: &cap_std::fs::Metadata, _directory: bool) -> u32 {
    use cap_std::fs::MetadataExt as _;
    metadata.mode() & 0o777
}

#[cfg(windows)]
fn snapshot_mode(metadata: &cap_std::fs::Metadata, directory: bool) -> u32 {
    use cap_std::fs::MetadataExt as _;
    const FILE_ATTRIBUTE_READONLY: u32 = 0x1;
    if directory {
        0o777
    } else if metadata.file_attributes() & FILE_ATTRIBUTE_READONLY == 0 {
        0o666
    } else {
        0o444
    }
}

#[cfg(not(any(unix, windows)))]
fn snapshot_mode(_metadata: &cap_std::fs::Metadata, directory: bool) -> u32 {
    if directory { 0o777 } else { 0o666 }
}

#[cfg(unix)]
fn set_snapshot_file_mode(file: &cap_std::fs::File, mode: u32) -> Result<(), LifecycleError> {
    use cap_std::fs::{Permissions, PermissionsExt as _};
    file.set_permissions(Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(windows)]
fn set_snapshot_file_mode(file: &cap_std::fs::File, mode: u32) -> Result<(), LifecycleError> {
    let mut permissions = file.metadata()?.permissions();
    permissions.set_readonly(mode & 0o222 == 0);
    file.set_permissions(permissions)?;
    Ok(())
}

#[cfg(not(any(unix, windows)))]
#[allow(clippy::unnecessary_wraps)]
fn set_snapshot_file_mode(_file: &cap_std::fs::File, _mode: u32) -> Result<(), LifecycleError> {
    Ok(())
}

fn bytes_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}
