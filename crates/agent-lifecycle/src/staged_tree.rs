use super::{
    LifecycleError, MANAGED_DIRECTORY_MODE, configure_nofollow, open_child_directory,
    open_child_file, packages, same_content_state_cap, same_object_cap,
};
use agent_contracts::canonical_sha256;
use cap_fs_ext::{FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, OpenOptions};
use serde_json::Value;
use std::collections::BTreeSet;
use std::io::{Read as _, Write as _};

const MAX_STAGED_TREE_ENTRIES: usize = 100_000;
const MAX_STAGED_PATH_BYTES: usize = 4_096;

#[derive(Clone, Copy)]
enum TreeKind {
    Package,
    Skill,
}

struct DirectoryRecord {
    mode: u32,
    path: String,
}

struct FileRecord {
    mode: u32,
    path: String,
}

struct RecordedTree {
    directories: Vec<DirectoryRecord>,
    files: Vec<FileRecord>,
    name: String,
    root_mode: u32,
}

pub(super) fn stage_package(
    stage: &Dir,
    source: &Dir,
    record: &Value,
) -> Result<(), LifecycleError> {
    stage_tree(stage, source, record, TreeKind::Package)
}

pub(super) fn stage_skill(stage: &Dir, source: &Dir, record: &Value) -> Result<(), LifecycleError> {
    stage_tree(stage, source, record, TreeKind::Skill)
}

pub(super) fn verify_package(stage: &Dir, record: &Value) -> Result<(), LifecycleError> {
    verify_tree(stage, record, TreeKind::Package)
}

pub(super) fn verify_skill(stage: &Dir, record: &Value) -> Result<(), LifecycleError> {
    verify_tree(stage, record, TreeKind::Skill)
}

fn stage_tree(
    stage: &Dir,
    source: &Dir,
    value: &Value,
    kind: TreeKind,
) -> Result<(), LifecycleError> {
    let record = parse_recorded_tree(value, kind)?;
    let parent = destination_parent(stage, kind)?;
    let root = create_directory(&parent, &record.name, record.root_mode, "staged tree root")?;
    for directory in &record.directories {
        let components = portable_components(&directory.path, "staged directory")?;
        let (name, parents) = components
            .split_last()
            .ok_or_else(|| LifecycleError::Invalid("staged directory path is empty".to_owned()))?;
        let parent = open_parents(&root, parents, "staged directory parent")?;
        create_directory(&parent, name, directory.mode, "staged directory")?;
    }
    for file in &record.files {
        copy_recorded_file(source, &root, file)?;
    }
    validate_destination(&root, value, kind, &record.name)?;
    verify_tree(stage, value, kind)
}

fn verify_tree(stage: &Dir, value: &Value, kind: TreeKind) -> Result<(), LifecycleError> {
    let record = parse_recorded_tree(value, kind)?;
    let parent = open_destination_parent(stage, kind)?;
    let root = open_child_directory(
        &parent,
        &record.name,
        Some(record.root_mode),
        "staged tree root",
    )?;
    let identity = root.dir_metadata()?;
    validate_destination(&root, value, kind, &record.name)?;
    let current_parent = open_destination_parent(stage, kind)?;
    let current = open_child_directory(
        &current_parent,
        &record.name,
        Some(record.root_mode),
        "staged tree root",
    )?;
    let current_identity = current.dir_metadata()?;
    if !same_object_cap(&identity, &current_identity)
        || !same_content_state_cap(&identity, &current_identity)
    {
        return invalid("staged tree changed while verifying");
    }
    validate_destination(&current, value, kind, &record.name)?;
    let final_parent = open_destination_parent(stage, kind)?;
    let final_root = open_child_directory(
        &final_parent,
        &record.name,
        Some(record.root_mode),
        "staged tree root",
    )?;
    let final_identity = final_root.dir_metadata()?;
    if !same_object_cap(&current_identity, &final_identity)
        || !same_content_state_cap(&current_identity, &final_identity)
    {
        return invalid("staged tree changed while verifying");
    }
    Ok(())
}

fn validate_destination(
    root: &Dir,
    value: &Value,
    kind: TreeKind,
    name: &str,
) -> Result<(), LifecycleError> {
    let (digest_field, label) = match kind {
        TreeKind::Package => ("files_sha256", "staged package"),
        TreeKind::Skill => ("sha256", "staged Skill"),
    };
    packages::validate_recorded_tree_strict(
        root,
        value,
        digest_field,
        &format!("{label} differs from Install Plan: {name}"),
    )
}

fn destination_parent(stage: &Dir, kind: TreeKind) -> Result<Dir, LifecycleError> {
    match kind {
        TreeKind::Package => {
            let managed = ensure_directory(
                stage,
                ".agent-skills",
                MANAGED_DIRECTORY_MODE,
                "staged managed metadata",
            )?;
            ensure_directory(
                &managed,
                "packages",
                MANAGED_DIRECTORY_MODE,
                "staged package parent",
            )
        }
        TreeKind::Skill => ensure_directory(
            stage,
            "skills",
            MANAGED_DIRECTORY_MODE,
            "staged Skill parent",
        ),
    }
}

fn open_destination_parent(stage: &Dir, kind: TreeKind) -> Result<Dir, LifecycleError> {
    match kind {
        TreeKind::Package => {
            let managed = open_child_directory(
                stage,
                ".agent-skills",
                Some(MANAGED_DIRECTORY_MODE),
                "staged managed metadata",
            )?;
            open_child_directory(
                &managed,
                "packages",
                Some(MANAGED_DIRECTORY_MODE),
                "staged package parent",
            )
        }
        TreeKind::Skill => open_child_directory(
            stage,
            "skills",
            Some(MANAGED_DIRECTORY_MODE),
            "staged Skill parent",
        ),
    }
}

fn ensure_directory(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<Dir, LifecycleError> {
    match parent.symlink_metadata(name) {
        Ok(_) => open_child_directory(parent, name, Some(mode), label),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            create_directory(parent, name, mode, label)
        }
        Err(error) => Err(error.into()),
    }
}

fn create_directory(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<Dir, LifecycleError> {
    create_directory_with_hook(parent, name, mode, label, || Ok(()))
}

fn create_directory_with_hook(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
    before_revalidation: impl FnOnce() -> Result<(), LifecycleError>,
) -> Result<Dir, LifecycleError> {
    let result = {
        #[cfg(all(unix, not(target_os = "wasi")))]
        {
            use cap_std::fs::{DirBuilder, DirBuilderExt as _};

            let mut builder = DirBuilder::new();
            builder.mode(mode);
            parent.create_dir_with(name, &builder)
        }
        #[cfg(any(not(unix), target_os = "wasi"))]
        {
            parent.create_dir(name)
        }
    };
    match result {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            return invalid(format!("{label} already exists"));
        }
        Err(error) => return Err(error.into()),
    }
    let directory = open_child_directory(parent, name, None, label)?;
    let identity = directory.dir_metadata()?;
    before_revalidation()?;
    #[cfg(unix)]
    {
        use cap_std::fs::{Permissions, PermissionsExt as _};
        directory.set_permissions(".", Permissions::from_mode(mode))?;
    }
    let current = open_child_directory(parent, name, Some(mode), label)?.dir_metadata()?;
    if !same_object_cap(&identity, &current) {
        return invalid(format!("{label} changed while creating"));
    }
    Ok(directory)
}

fn copy_recorded_file(
    source_root: &Dir,
    destination_root: &Dir,
    record: &FileRecord,
) -> Result<(), LifecycleError> {
    copy_recorded_file_with_hook(source_root, destination_root, record, || Ok(()))
}

fn copy_recorded_file_with_hook(
    source_root: &Dir,
    destination_root: &Dir,
    record: &FileRecord,
    before_destination_revalidation: impl FnOnce() -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    let components = portable_components(&record.path, "staged file")?;
    let (name, parents) = components
        .split_last()
        .ok_or_else(|| LifecycleError::Invalid("staged file path is empty".to_owned()))?;
    let source_parent = open_parents(source_root, parents, "installation source directory")?;
    let destination_parent =
        open_parents(destination_root, parents, "staged file parent directory")?;
    let mut source = open_source_file(&source_parent, name, &record.path)?;
    let opened_source = source.metadata()?;
    let mut destination =
        create_destination_file(&destination_parent, name, record.mode, &record.path)?;
    let opened_destination = destination.metadata()?;
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    loop {
        let count = source.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        destination.write_all(&buffer[..count])?;
    }
    destination.flush()?;
    #[cfg(unix)]
    {
        use cap_std::fs::{Permissions, PermissionsExt as _};
        destination.set_permissions(Permissions::from_mode(record.mode))?;
    }
    let completed_destination = destination.metadata()?;
    before_destination_revalidation()?;
    let current_destination = open_child_file(
        &destination_parent,
        name,
        record.mode,
        "staged destination file",
    )?
    .metadata()?;
    if !same_object_cap(&opened_destination, &completed_destination)
        || !same_object_cap(&opened_destination, &current_destination)
        || !same_content_state_cap(&completed_destination, &current_destination)
    {
        return invalid(format!(
            "staged destination changed while copying: {}",
            record.path
        ));
    }
    if completed_destination.nlink() != 1 || current_destination.nlink() != 1 {
        return invalid(format!(
            "staged destination has an unsafe hard-link alias: {}",
            record.path
        ));
    }
    let after_source = source.metadata()?;
    let current_source = open_source_file(&source_parent, name, &record.path)?.metadata()?;
    if !same_object_cap(&opened_source, &after_source)
        || !same_object_cap(&opened_source, &current_source)
        || !same_content_state_cap(&opened_source, &after_source)
        || !same_content_state_cap(&opened_source, &current_source)
    {
        return invalid(format!(
            "installation source changed while staging: {}",
            record.path
        ));
    }
    Ok(())
}

fn open_source_file(
    parent: &Dir,
    name: &str,
    relative: &str,
) -> Result<cap_std::fs::File, LifecycleError> {
    let before = parent.symlink_metadata(name).map_err(|_| {
        LifecycleError::Invalid(format!(
            "installation source changed or became unsafe: {relative}"
        ))
    })?;
    if before.file_type().is_symlink() || !before.is_file() {
        return invalid(format!(
            "installation source changed or became unsafe: {relative}"
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    let file = parent.open_with(name, &options).map_err(|_| {
        LifecycleError::Invalid(format!(
            "installation source changed or became unsafe: {relative}"
        ))
    })?;
    let opened = file.metadata()?;
    let after = parent.symlink_metadata(name).map_err(|_| {
        LifecycleError::Invalid(format!(
            "installation source changed or became unsafe: {relative}"
        ))
    })?;
    let current = parent
        .open_with(name, &options)
        .and_then(|candidate| candidate.metadata())
        .map_err(|_| {
            LifecycleError::Invalid(format!(
                "installation source changed or became unsafe: {relative}"
            ))
        })?;
    if after.file_type().is_symlink() || !after.is_file() || !same_object_cap(&opened, &current) {
        return invalid(format!(
            "installation source changed or became unsafe: {relative}"
        ));
    }
    Ok(file)
}

fn create_destination_file(
    parent: &Dir,
    name: &str,
    mode: u32,
    relative: &str,
) -> Result<cap_std::fs::File, LifecycleError> {
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
    parent.open_with(name, &options).map_err(|error| {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            LifecycleError::Invalid(format!("staged file already exists: {relative}"))
        } else {
            error.into()
        }
    })
}

fn open_parents(root: &Dir, components: &[String], label: &str) -> Result<Dir, LifecycleError> {
    let mut directory = root.try_clone()?;
    for component in components {
        directory = open_child_directory(&directory, component, None, label)?;
    }
    Ok(directory)
}

fn parse_recorded_tree(value: &Value, kind: TreeKind) -> Result<RecordedTree, LifecycleError> {
    let object = value
        .as_object()
        .ok_or_else(|| LifecycleError::Invalid("staged tree record is invalid".to_owned()))?;
    let (name_field, digest_field, require_manifest) = match kind {
        TreeKind::Package => ("id", "files_sha256", true),
        TreeKind::Skill => ("name", "sha256", false),
    };
    let name = object
        .get(name_field)
        .and_then(Value::as_str)
        .filter(|name| safe_id(name))
        .ok_or_else(|| LifecycleError::Invalid("staged tree name is invalid".to_owned()))?
        .to_owned();
    let root_mode = mode_field(value, "root_mode", "staged tree root mode")?;
    let raw_files = object
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid("staged tree files are invalid".to_owned()))?;
    let raw_directories = object
        .get("directories")
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid("staged tree directories are invalid".to_owned()))?;
    if raw_files.len() > MAX_STAGED_TREE_ENTRIES || raw_directories.len() > MAX_STAGED_TREE_ENTRIES
    {
        return invalid("staged tree exceeds maximum entries");
    }
    if object.get("file_count").and_then(Value::as_u64) != u64::try_from(raw_files.len()).ok()
        || object.get(digest_field).and_then(Value::as_str)
            != Some(canonical_sha256(&Value::Array(raw_files.clone()))?.as_str())
    {
        return invalid("staged tree identity is invalid");
    }

    let mut files = Vec::with_capacity(raw_files.len());
    let mut file_paths = Vec::with_capacity(raw_files.len());
    for entry in raw_files {
        let path = path_field(entry, "staged file path")?;
        let digest = entry.get("sha256").and_then(Value::as_str);
        if !digest.is_some_and(valid_sha256) {
            return invalid("staged file hash is invalid");
        }
        files.push(FileRecord {
            mode: mode_field(entry, "mode", "staged file mode")?,
            path: path.clone(),
        });
        file_paths.push(path);
    }
    let mut directories = Vec::with_capacity(raw_directories.len());
    let mut directory_paths = Vec::with_capacity(raw_directories.len());
    for entry in raw_directories {
        let path = path_field(entry, "staged directory path")?;
        directories.push(DirectoryRecord {
            mode: mode_field(entry, "mode", "staged directory mode")?,
            path: path.clone(),
        });
        directory_paths.push(path);
    }
    if !sorted_unique(&file_paths)
        || !sorted_unique(&directory_paths)
        || file_paths
            .iter()
            .any(|path| directory_paths.binary_search(path).is_ok())
    {
        return invalid("staged tree paths are invalid");
    }
    if require_manifest
        && file_paths
            .binary_search(&"manifest.json".to_owned())
            .is_err()
    {
        return invalid("staged package tree must contain manifest.json");
    }
    Ok(RecordedTree {
        directories,
        files,
        name,
        root_mode,
    })
}

fn mode_field(value: &Value, field: &str, label: &str) -> Result<u32, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .filter(|mode| *mode <= 0o777)
        .and_then(|mode| u32::try_from(mode).ok())
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn path_field(value: &Value, label: &str) -> Result<String, LifecycleError> {
    let path = value
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))?;
    portable_components(path, label)?;
    Ok(path.to_owned())
}

fn portable_components(value: &str, label: &str) -> Result<Vec<String>, LifecycleError> {
    if value.is_empty()
        || value.starts_with('/')
        || value.contains('\\')
        || value.len() > MAX_STAGED_PATH_BYTES
        || value
            .as_bytes()
            .get(0..2)
            .is_some_and(|prefix| prefix[0].is_ascii_alphabetic() && prefix[1] == b':')
    {
        return invalid(format!("{label} is unsafe"));
    }
    let components = value
        .split('/')
        .filter(|component| !component.is_empty() && *component != ".")
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if components.is_empty()
        || components.iter().any(|component| component == "..")
        || components.join("/") != value
    {
        return invalid(format!("{label} is unsafe"));
    }
    Ok(components)
}

fn safe_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn sorted_unique(values: &[String]) -> bool {
    values.windows(2).all(|pair| pair[0] < pair[1])
        && values.iter().collect::<BTreeSet<_>>().len() == values.len()
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::LifecycleWorkspace;
    use serde_json::json;
    use sha2::{Digest as _, Sha256};
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn temporary_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agent-lifecycle-staged-tree-{label}-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    fn digest(bytes: &[u8]) -> String {
        format!("{:x}", Sha256::digest(bytes))
    }

    fn package_record() -> Value {
        let files = vec![
            json!({"mode": 0o644, "path": "manifest.json", "sha256": digest(b"{}")}),
            json!({"mode": 0o755, "path": "scripts/run.sh", "sha256": digest(b"#!/bin/sh\n")}),
        ];
        json!({
            "directories": [{"mode": 0o755, "path": "scripts"}],
            "file_count": files.len(),
            "files_sha256": canonical_sha256(&Value::Array(files.clone())).unwrap(),
            "files": files,
            "id": "core",
            "root_mode": 0o755,
        })
    }

    fn skill_record() -> Value {
        let files = vec![json!({"mode": 0o644, "path": "SKILL.md", "sha256": digest(b"# Test\n")})];
        json!({
            "directories": [],
            "file_count": files.len(),
            "files": files,
            "name": "test-skill",
            "root_mode": 0o755,
            "sha256": canonical_sha256(&Value::Array(files.clone())).unwrap(),
        })
    }

    fn open_directory(path: &Path) -> Dir {
        Dir::open_ambient_dir(path, cap_std::ambient_authority()).expect("open test directory")
    }

    fn assert_extra_metadata_is_rejected(
        workspace: &LifecycleWorkspace,
        package_record: &Value,
        skill_record: &Value,
    ) {
        let staged_package = workspace.stage_path().join(".agent-skills/packages/core");
        let staged_skill = workspace.stage_path().join("skills/test-skill");
        std::fs::write(staged_package.join(".DS_Store"), b"metadata")
            .expect("write package metadata");
        assert!(
            workspace
                .verify_staged_package_tree(package_record)
                .is_err(),
            "staged package verification must not ignore extra OS metadata"
        );
        std::fs::remove_file(staged_package.join(".DS_Store")).expect("remove package metadata");
        std::fs::write(staged_skill.join(".DS_Store"), b"metadata").expect("write Skill metadata");
        assert!(
            workspace.verify_staged_skill_tree(skill_record).is_err(),
            "staged Skill verification must not ignore extra OS metadata"
        );
        std::fs::remove_file(staged_skill.join(".DS_Store")).expect("remove Skill metadata");
    }

    fn assert_post_stage_hard_link_is_rejected(
        workspace: &LifecycleWorkspace,
        package_record: &Value,
        alias: &Path,
    ) {
        std::fs::hard_link(
            workspace
                .stage_path()
                .join(".agent-skills/packages/core/manifest.json"),
            alias,
        )
        .expect("create post-stage hard-link alias");
        assert!(
            workspace
                .verify_staged_package_tree(package_record)
                .is_err(),
            "staged verification must reject a post-copy hard-link alias"
        );
        std::fs::remove_file(alias).expect("remove hard-link alias");
    }

    #[test]
    fn package_and_skill_trees_are_copied_and_revalidated() {
        let root = temporary_path("success");
        let package = temporary_path("package");
        let skill = temporary_path("skill");
        std::fs::create_dir(&root).expect("create target");
        std::fs::create_dir_all(package.join("scripts")).expect("create package source");
        std::fs::write(package.join("manifest.json"), b"{}").expect("write Manifest");
        std::fs::write(package.join("scripts/run.sh"), b"#!/bin/sh\n").expect("write script");
        std::fs::create_dir(&skill).expect("create Skill source");
        std::fs::write(skill.join("SKILL.md"), b"# Test\n").expect("write Skill");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::set_permissions(
                package.join("manifest.json"),
                std::fs::Permissions::from_mode(0o600),
            )
            .expect("set noncanonical source mode");
            std::fs::set_permissions(
                package.join("scripts/run.sh"),
                std::fs::Permissions::from_mode(0o700),
            )
            .expect("set executable source mode");
        }

        let mut workspace = LifecycleWorkspace::begin(&root).expect("begin workspace");
        let package_record = package_record();
        let skill_record = skill_record();
        assert!(
            workspace
                .verify_staged_package_tree(&package_record)
                .is_err()
        );
        assert!(
            !workspace.stage_path().join(".agent-skills").exists(),
            "verification must not create missing staged parents"
        );
        workspace
            .stage_package_tree(&open_directory(&package), &package_record)
            .expect("stage package");
        workspace
            .stage_skill_tree(&open_directory(&skill), &skill_record)
            .expect("stage Skill");
        assert!(
            workspace
                .stage_package_tree(&open_directory(&package), &package_record)
                .is_err(),
            "duplicate staging must not merge into an existing tree"
        );
        workspace
            .verify_staged_package_tree(&package_record)
            .expect("verify package");
        workspace
            .verify_staged_skill_tree(&skill_record)
            .expect("verify Skill");
        assert_extra_metadata_is_rejected(&workspace, &package_record, &skill_record);
        assert_post_stage_hard_link_is_rejected(
            &workspace,
            &package_record,
            &root.join("external-alias"),
        );
        assert_eq!(
            std::fs::read(
                workspace
                    .stage_path()
                    .join(".agent-skills/packages/core/scripts/run.sh")
            )
            .expect("read staged script"),
            b"#!/bin/sh\n"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            assert_eq!(
                std::fs::metadata(
                    workspace
                        .stage_path()
                        .join(".agent-skills/packages/core/manifest.json")
                )
                .expect("inspect staged Manifest")
                .permissions()
                .mode()
                    & 0o777,
                0o644
            );
        }

        std::fs::write(
            workspace
                .stage_path()
                .join(".agent-skills/packages/core/manifest.json"),
            b"{\"tampered\":true}",
        )
        .expect("tamper staged Manifest");
        assert!(
            workspace
                .verify_staged_package_tree(&package_record)
                .is_err()
        );
        workspace.cleanup().expect("cleanup workspace");
        std::fs::remove_dir_all(package).expect("remove package source");
        std::fs::remove_dir_all(skill).expect("remove Skill source");
        std::fs::remove_dir(root).expect("remove target");
    }

    #[test]
    fn staged_file_rejects_external_hard_link_alias() {
        let root = temporary_path("hard-link");
        let source = root.join("source");
        let destination = root.join("destination");
        std::fs::create_dir_all(&source).expect("create source");
        std::fs::create_dir(&destination).expect("create destination");
        std::fs::write(source.join("evidence"), b"content").expect("write source");
        let source_directory = open_directory(&source);
        let destination_directory = open_directory(&destination);
        let result = copy_recorded_file_with_hook(
            &source_directory,
            &destination_directory,
            &FileRecord {
                mode: 0o644,
                path: "evidence".to_owned(),
            },
            || {
                std::fs::hard_link(
                    destination.join("evidence"),
                    destination.join("external-alias"),
                )?;
                Ok(())
            },
        );
        assert!(
            destination.join("external-alias").exists(),
            "hard-link fixture must create an alias"
        );
        assert!(result.is_err(), "hard-link alias must not be accepted");
        std::fs::remove_dir_all(root).expect("remove hard-link fixture");
    }

    #[cfg(unix)]
    #[test]
    fn atomically_created_entries_reject_namespace_replacement() {
        use std::os::unix::fs::PermissionsExt as _;

        let root = temporary_path("entry-replacement");
        let source = root.join("source");
        let destination = root.join("destination");
        std::fs::create_dir_all(&source).expect("create source");
        std::fs::create_dir(&destination).expect("create destination");
        std::fs::write(source.join("evidence"), b"content").expect("write source");
        let source_directory = open_directory(&source);
        let destination_directory = open_directory(&destination);
        let result = copy_recorded_file_with_hook(
            &source_directory,
            &destination_directory,
            &FileRecord {
                mode: 0o644,
                path: "evidence".to_owned(),
            },
            || {
                std::fs::rename(
                    destination.join("evidence"),
                    destination.join("original-evidence"),
                )?;
                std::fs::write(destination.join("evidence"), b"content")?;
                std::fs::set_permissions(
                    destination.join("evidence"),
                    std::fs::Permissions::from_mode(0o644),
                )?;
                Ok(())
            },
        );
        assert!(result.is_err(), "replacement file must not be accepted");

        let result = create_directory_with_hook(
            &destination_directory,
            "created",
            0o755,
            "test directory",
            || {
                std::fs::rename(
                    destination.join("created"),
                    destination.join("original-created"),
                )?;
                std::fs::create_dir(destination.join("created"))?;
                std::fs::set_permissions(
                    destination.join("created"),
                    std::fs::Permissions::from_mode(0o755),
                )?;
                Ok(())
            },
        );
        assert!(
            result.is_err(),
            "replacement directory must not be accepted"
        );
        std::fs::remove_dir_all(root).expect("remove replacement fixture");
    }

    #[cfg(unix)]
    #[test]
    fn source_symlink_and_escape_record_fail_closed() {
        use std::os::unix::fs::symlink;

        let root = temporary_path("unsafe");
        let package = temporary_path("unsafe-package");
        let outside = temporary_path("outside");
        std::fs::create_dir(&root).expect("create target");
        std::fs::create_dir(&package).expect("create package source");
        std::fs::create_dir(&outside).expect("create outside source");
        std::fs::write(package.join("manifest.json"), b"{}").expect("write Manifest");
        std::fs::write(outside.join("evidence"), b"outside").expect("write outside evidence");
        symlink(outside.join("evidence"), package.join("linked")).expect("create source symlink");
        let linked_files = vec![
            json!({"mode": 0o644, "path": "linked", "sha256": digest(b"outside")}),
            json!({"mode": 0o644, "path": "manifest.json", "sha256": digest(b"{}")}),
        ];
        let linked_record = json!({
            "directories": [],
            "file_count": linked_files.len(),
            "files_sha256": canonical_sha256(&Value::Array(linked_files.clone())).unwrap(),
            "files": linked_files,
            "id": "linked",
            "root_mode": 0o755,
        });
        let mut workspace = LifecycleWorkspace::begin(&root).expect("begin workspace");
        assert!(
            workspace
                .stage_package_tree(&open_directory(&package), &linked_record)
                .is_err()
        );
        assert_eq!(
            std::fs::read(outside.join("evidence")).expect("read outside evidence"),
            b"outside"
        );

        let escape_files = vec![
            json!({"mode": 0o644, "path": "../escape", "sha256": digest(b"outside")}),
            json!({"mode": 0o644, "path": "manifest.json", "sha256": digest(b"{}")}),
        ];
        let escape_record = json!({
            "directories": [],
            "file_count": escape_files.len(),
            "files_sha256": canonical_sha256(&Value::Array(escape_files.clone())).unwrap(),
            "files": escape_files,
            "id": "escape",
            "root_mode": 0o755,
        });
        assert!(
            workspace
                .stage_package_tree(&open_directory(&package), &escape_record)
                .is_err()
        );
        assert!(
            !workspace
                .stage_path()
                .join(".agent-skills/packages/escape")
                .exists()
        );
        workspace.cleanup().expect("cleanup workspace");
        std::fs::remove_dir_all(package).expect("remove package source");
        std::fs::remove_dir_all(outside).expect("remove outside source");
        std::fs::remove_dir(root).expect("remove target");
    }
}
