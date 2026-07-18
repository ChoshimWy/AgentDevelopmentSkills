use super::{
    EXTERNAL_ACTIVATION_LOCK, LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    check_activation, configure_nofollow, open_child_directory, same_content_state_cap,
    same_object_cap, staged_install,
};
use agent_contracts::MAX_CONTRACT_JSON_BYTES;
use cap_fs_ext::{DirExt as _, FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, OpenOptions};
use sha2::{Digest as _, Sha256};
use std::ffi::OsStr;
use std::io::{Read as _, Write as _};
use std::path::{Component, Path, PathBuf};

const MAX_EXTERNAL_TREE_DEPTH: usize = 128;
const MAX_EXTERNAL_TREE_ENTRIES: usize = 100_000;
const MAX_EXTERNAL_PATH_BYTES: usize = 4_096;
const SYSTEM_SKILLS_DIRECTORY: &str = ".system";

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct ExternalStageSnapshot {
    activation: Option<Vec<u8>>,
    system_skills: Option<SystemTreeSnapshot>,
}

impl ExternalStageSnapshot {
    pub(super) fn layout(&self) -> staged_install::ExternalLayout {
        staged_install::ExternalLayout {
            activation: self.activation.is_some(),
            system_skills: self.system_skills.is_some(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SystemTreeSnapshot {
    entries: Vec<TreeEntry>,
    root_mode: Option<u32>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct TreeEntry {
    kind: EntryKind,
    mode: Option<u32>,
    path: PathBuf,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum EntryKind {
    Directory,
    File {
        length: u64,
        sha256: String,
    },
    #[cfg(not(windows))]
    Symlink {
        target: PathBuf,
    },
}

pub(super) fn stage(target: &Dir, stage: &Dir) -> Result<ExternalStageSnapshot, LifecycleError> {
    let activation = inspect_activation(target)?;
    let system_skills = inspect_system_skills(target)?;
    if let Some(bytes) = activation.as_deref() {
        let managed = open_child_directory(
            stage,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "staged managed metadata",
        )?;
        write_independent_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            bytes,
            MANAGED_FILE_MODE,
            "staged Activation Lock",
        )?;
    }
    if let Some(snapshot) = &system_skills {
        copy_system_skills(target, stage, snapshot)?;
    }
    let snapshot = ExternalStageSnapshot {
        activation,
        system_skills,
    };
    verify(target, stage, &snapshot)?;
    Ok(snapshot)
}

pub(super) fn verify(
    target: &Dir,
    stage: &Dir,
    expected: &ExternalStageSnapshot,
) -> Result<(), LifecycleError> {
    if inspect_activation(target)? != expected.activation {
        return invalid("target Activation state changed after staging");
    }
    if inspect_system_skills(target)? != expected.system_skills {
        return invalid("target .system Skills changed after staging");
    }
    if inspect_staged_activation(stage)? != expected.activation {
        return invalid("staged Activation Lock differs from preserved state");
    }
    if inspect_staged_system_skills(stage)? != expected.system_skills {
        return invalid("staged .system Skills differ from preserved state");
    }
    Ok(())
}

fn inspect_activation(target: &Dir) -> Result<Option<Vec<u8>>, LifecycleError> {
    let Some(managed) = open_optional_directory(
        target,
        OsStr::new(".agent-skills"),
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?
    else {
        return Ok(None);
    };
    match managed.symlink_metadata(EXTERNAL_ACTIVATION_LOCK) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            return invalid("activation Lock is missing or unsafe");
        }
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    }
    check_activation(target)?;
    read_single_link_file(
        &managed,
        OsStr::new(EXTERNAL_ACTIVATION_LOCK),
        Some(MANAGED_FILE_MODE),
        Some(MAX_CONTRACT_JSON_BYTES),
        "activation Lock",
    )
    .map(Some)
}

fn inspect_staged_activation(stage: &Dir) -> Result<Option<Vec<u8>>, LifecycleError> {
    let managed = open_child_directory(
        stage,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged managed metadata",
    )?;
    match managed.symlink_metadata(EXTERNAL_ACTIVATION_LOCK) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            invalid("staged Activation Lock is missing or unsafe")
        }
        Ok(_) => read_single_link_file(
            &managed,
            OsStr::new(EXTERNAL_ACTIVATION_LOCK),
            Some(MANAGED_FILE_MODE),
            Some(MAX_CONTRACT_JSON_BYTES),
            "staged Activation Lock",
        )
        .map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn inspect_system_skills(target: &Dir) -> Result<Option<SystemTreeSnapshot>, LifecycleError> {
    let Some(skills) =
        open_optional_directory(target, OsStr::new("skills"), None, "target Skills root")?
    else {
        return Ok(None);
    };
    inspect_optional_system_tree(&skills, "target .system Skills", false)
}

fn inspect_staged_system_skills(stage: &Dir) -> Result<Option<SystemTreeSnapshot>, LifecycleError> {
    let skills = open_child_directory(
        stage,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged Skills root",
    )?;
    inspect_optional_system_tree(&skills, "staged .system Skills", true)
}

fn inspect_optional_system_tree(
    skills: &Dir,
    label: &str,
    require_single_link: bool,
) -> Result<Option<SystemTreeSnapshot>, LifecycleError> {
    let Some(root) =
        open_optional_directory(skills, OsStr::new(SYSTEM_SKILLS_DIRECTORY), None, label)?
    else {
        return Ok(None);
    };
    let opened = root.dir_metadata()?;
    let mut entries = Vec::new();
    scan_directory(&root, Path::new(""), 0, require_single_link, &mut entries)?;
    let current = open_directory(skills, OsStr::new(SYSTEM_SKILLS_DIRECTORY), None, label)?;
    let current_metadata = current.dir_metadata()?;
    if !same_object_cap(&opened, &current_metadata)
        || !same_content_state_cap(&opened, &current_metadata)
    {
        return invalid(format!("{label} changed while inspecting"));
    }
    Ok(Some(SystemTreeSnapshot {
        entries,
        root_mode: preserved_mode(&opened),
    }))
}

fn scan_directory(
    directory: &Dir,
    relative: &Path,
    depth: usize,
    require_single_link: bool,
    entries: &mut Vec<TreeEntry>,
) -> Result<(), LifecycleError> {
    let opened = directory.dir_metadata()?;
    let mut names = directory
        .entries()?
        .map(|entry| entry.map(|entry| entry.file_name()))
        .collect::<Result<Vec<_>, _>>()?;
    names.sort();
    for name in names {
        if entries.len() >= MAX_EXTERNAL_TREE_ENTRIES {
            return invalid("external .system tree exceeds the entry limit");
        }
        let path = relative.join(&name);
        if path_bytes(&path) > MAX_EXTERNAL_PATH_BYTES {
            return invalid("external .system tree path exceeds the length limit");
        }
        let metadata = directory.symlink_metadata(&name)?;
        let file_type = metadata.file_type();
        if file_type.is_dir() {
            if depth >= MAX_EXTERNAL_TREE_DEPTH {
                return invalid("external .system tree exceeds the depth limit");
            }
            let child = open_directory(directory, &name, None, "external .system directory")?;
            entries.push(TreeEntry {
                kind: EntryKind::Directory,
                mode: preserved_mode(&metadata),
                path: path.clone(),
            });
            scan_directory(&child, &path, depth + 1, require_single_link, entries)?;
        } else if file_type.is_file() {
            let (length, sha256) = hash_regular_file(
                directory,
                &name,
                require_single_link,
                "external .system file",
            )?;
            entries.push(TreeEntry {
                kind: EntryKind::File { length, sha256 },
                mode: preserved_mode(&metadata),
                path,
            });
        } else if file_type.is_symlink() {
            #[cfg(windows)]
            {
                return invalid(
                    "external .system symlinks are unsupported on Windows without following them",
                );
            }
            #[cfg(not(windows))]
            {
                let target = stable_link_target(directory, &name, "external .system symlink")?;
                entries.push(TreeEntry {
                    kind: EntryKind::Symlink { target },
                    mode: None,
                    path,
                });
            }
        } else {
            return invalid("external .system tree contains an unsupported filesystem object");
        }
    }
    let after = directory.dir_metadata()?;
    if !same_object_cap(&opened, &after) || !same_content_state_cap(&opened, &after) {
        return invalid("external .system directory changed while inspecting");
    }
    Ok(())
}

fn copy_system_skills(
    target: &Dir,
    stage: &Dir,
    snapshot: &SystemTreeSnapshot,
) -> Result<(), LifecycleError> {
    let source_skills = open_directory(target, OsStr::new("skills"), None, "target Skills root")?;
    let source_root = open_directory(
        &source_skills,
        OsStr::new(SYSTEM_SKILLS_DIRECTORY),
        snapshot.root_mode,
        "target .system Skills",
    )?;
    let destination_skills = open_child_directory(
        stage,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "staged Skills root",
    )?;
    let destination_root = create_directory(
        &destination_skills,
        OsStr::new(SYSTEM_SKILLS_DIRECTORY),
        working_directory_mode(),
        "staged .system Skills",
    )?;
    for entry in &snapshot.entries {
        let (name, parent_path) = split_entry_path(&entry.path)?;
        let source_parent =
            open_relative_directory(&source_root, parent_path, "external .system source parent")?;
        let destination_parent = open_relative_directory(
            &destination_root,
            parent_path,
            "external .system destination parent",
        )?;
        match &entry.kind {
            EntryKind::Directory => {
                create_directory(
                    &destination_parent,
                    name,
                    working_directory_mode(),
                    "staged .system directory",
                )?;
            }
            EntryKind::File { length, sha256 } => copy_regular_file(
                &source_parent,
                &destination_parent,
                name,
                entry.mode,
                *length,
                sha256,
            )?,
            #[cfg(not(windows))]
            EntryKind::Symlink { target } => {
                require_source_link(&source_parent, name, target)?;
                create_symlink(&destination_parent, name, target)?;
            }
        }
    }
    for entry in snapshot.entries.iter().rev() {
        if entry.kind == EntryKind::Directory {
            let directory = open_relative_directory(
                &destination_root,
                &entry.path,
                "staged .system directory",
            )?;
            set_directory_mode(&directory, entry.mode)?;
            require_mode(
                &directory.dir_metadata()?,
                entry.mode,
                "staged .system directory",
            )?;
        }
    }
    set_directory_mode(&destination_root, snapshot.root_mode)?;
    require_mode(
        &destination_root.dir_metadata()?,
        snapshot.root_mode,
        "staged .system Skills",
    )?;
    Ok(())
}

fn copy_regular_file(
    source_parent: &Dir,
    destination_parent: &Dir,
    name: &OsStr,
    mode: Option<u32>,
    expected_length: u64,
    expected_sha256: &str,
) -> Result<(), LifecycleError> {
    let before = source_parent.symlink_metadata(name)?;
    if before.file_type().is_symlink() || !before.is_file() {
        return invalid("external .system source file is missing or unsafe");
    }
    let mut source_options = OpenOptions::new();
    source_options.read(true).follow(FollowSymlinks::No);
    configure_nofollow(&mut source_options);
    let mut source = source_parent.open_with(name, &source_options)?;
    let opened_source = source.metadata()?;
    if !same_object_cap(&before, &opened_source) {
        return invalid("external .system source file changed while opening");
    }
    require_mode(&opened_source, mode, "external .system source file")?;

    let mut destination_options = OpenOptions::new();
    destination_options
        .write(true)
        .create_new(true)
        .follow(FollowSymlinks::No);
    configure_nofollow(&mut destination_options);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt as _;
        destination_options.mode(mode.unwrap_or(0o600));
    }
    let mut destination = destination_parent.open_with(name, &destination_options)?;
    let opened_destination = destination.metadata()?;
    let mut digest = Sha256::new();
    let mut length = 0_u64;
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    loop {
        let count = source.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        length = length
            .checked_add(u64::try_from(count).map_err(|_| {
                LifecycleError::Invalid("external .system file length overflow".to_owned())
            })?)
            .ok_or_else(|| {
                LifecycleError::Invalid("external .system file length overflow".to_owned())
            })?;
        digest.update(&buffer[..count]);
        destination.write_all(&buffer[..count])?;
    }
    destination.flush()?;
    set_file_mode(&destination, mode)?;
    let actual_sha256 = format!("{:x}", digest.finalize());
    if length != expected_length || actual_sha256 != expected_sha256 {
        return invalid("external .system source file changed before copying");
    }
    let completed_destination = destination.metadata()?;
    let current_destination =
        open_regular_file(destination_parent, name, mode, "staged .system file")?.metadata()?;
    if !same_object_cap(&opened_destination, &completed_destination)
        || !same_object_cap(&opened_destination, &current_destination)
        || !same_content_state_cap(&completed_destination, &current_destination)
        || completed_destination.nlink() != 1
        || current_destination.nlink() != 1
    {
        return invalid("staged .system file changed while copying");
    }
    let after_source = source.metadata()?;
    let current_source =
        open_regular_file(source_parent, name, mode, "external .system source file")?.metadata()?;
    if !same_object_cap(&opened_source, &after_source)
        || !same_object_cap(&opened_source, &current_source)
        || !same_content_state_cap(&opened_source, &after_source)
        || !same_content_state_cap(&opened_source, &current_source)
    {
        return invalid("external .system source file changed while copying");
    }
    Ok(())
}

fn inspect_file(
    parent: &Dir,
    name: &OsStr,
    mode: Option<u32>,
    limit: Option<usize>,
    label: &str,
) -> Result<(Vec<u8>, cap_std::fs::Metadata), LifecycleError> {
    let mut file = open_regular_file(parent, name, mode, label)?;
    let opened = file.metadata()?;
    if opened.nlink() != 1 {
        return invalid(format!("{label} has an unsafe hard-link alias"));
    }
    if limit.is_some_and(|maximum| opened.len() > maximum as u64) {
        return invalid(format!("{label} exceeds the size limit"));
    }
    let mut bytes = Vec::with_capacity(
        usize::try_from(opened.len())
            .unwrap_or_else(|_| limit.unwrap_or_default())
            .min(limit.unwrap_or(usize::MAX)),
    );
    match limit {
        Some(maximum) => {
            std::io::Read::by_ref(&mut file)
                .take((maximum + 1) as u64)
                .read_to_end(&mut bytes)?;
            if bytes.len() > maximum {
                return invalid(format!("{label} exceeds the size limit"));
            }
        }
        None => {
            file.read_to_end(&mut bytes)?;
        }
    }
    let after = file.metadata()?;
    let current = open_regular_file(parent, name, mode, label)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
        || current.nlink() != 1
    {
        return invalid(format!("{label} changed while reading"));
    }
    Ok((bytes, opened))
}

fn read_single_link_file(
    parent: &Dir,
    name: &OsStr,
    mode: Option<u32>,
    limit: Option<usize>,
    label: &str,
) -> Result<Vec<u8>, LifecycleError> {
    inspect_file(parent, name, mode, limit, label).map(|(bytes, _)| bytes)
}

fn hash_regular_file(
    parent: &Dir,
    name: &OsStr,
    require_single_link: bool,
    label: &str,
) -> Result<(u64, String), LifecycleError> {
    let mut file = open_regular_file(parent, name, None, label)?;
    let opened = file.metadata()?;
    if require_single_link && opened.nlink() != 1 {
        return invalid(format!("{label} has an unsafe hard-link alias"));
    }
    let mut digest = Sha256::new();
    let mut length = 0_u64;
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        length = length
            .checked_add(u64::try_from(count).map_err(|_| {
                LifecycleError::Invalid("external .system file length overflow".to_owned())
            })?)
            .ok_or_else(|| {
                LifecycleError::Invalid("external .system file length overflow".to_owned())
            })?;
        digest.update(&buffer[..count]);
    }
    let after = file.metadata()?;
    let current = open_regular_file(parent, name, None, label)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
        || (require_single_link && (after.nlink() != 1 || current.nlink() != 1))
    {
        return invalid(format!("{label} changed while hashing"));
    }
    Ok((length, format!("{:x}", digest.finalize())))
}

fn open_regular_file(
    parent: &Dir,
    name: &OsStr,
    mode: Option<u32>,
    label: &str,
) -> Result<cap_std::fs::File, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_file() {
        return invalid(format!("{label} is missing or unsafe"));
    }
    require_mode(&before, mode, label)?;
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
        || !same_object_cap(&opened, &after)
    {
        return invalid(format!("{label} changed while opening"));
    }
    require_mode(&opened, mode, label)?;
    require_mode(&after, mode, label)?;
    Ok(file)
}

fn open_optional_directory(
    parent: &Dir,
    name: &OsStr,
    mode: Option<u32>,
    label: &str,
) -> Result<Option<Dir>, LifecycleError> {
    match parent.symlink_metadata(name) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            invalid(format!("{label} is missing or unsafe"))
        }
        Ok(_) => open_directory(parent, name, mode, label).map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn open_directory(
    parent: &Dir,
    name: &OsStr,
    mode: Option<u32>,
    label: &str,
) -> Result<Dir, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_dir() {
        return invalid(format!("{label} is missing or unsafe"));
    }
    require_mode(&before, mode, label)?;
    let directory = parent
        .open_dir_nofollow(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    let opened = directory.dir_metadata()?;
    let after = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    let current = parent
        .open_dir_nofollow(name)
        .and_then(|directory| directory.dir_metadata())
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    if after.file_type().is_symlink()
        || !after.is_dir()
        || !same_object_cap(&before, &opened)
        || !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
    {
        return invalid(format!("{label} changed while opening"));
    }
    require_mode(&opened, mode, label)?;
    require_mode(&after, mode, label)?;
    require_mode(&current, mode, label)?;
    Ok(directory)
}

fn open_relative_directory(
    root: &Dir,
    relative: &Path,
    label: &str,
) -> Result<Dir, LifecycleError> {
    let mut directory = root.try_clone()?;
    for component in relative.components() {
        let Component::Normal(name) = component else {
            return invalid(format!("{label} path is invalid"));
        };
        directory = open_directory(&directory, name, None, label)?;
    }
    Ok(directory)
}

fn create_directory(
    parent: &Dir,
    name: &OsStr,
    mode: Option<u32>,
    label: &str,
) -> Result<Dir, LifecycleError> {
    let result = {
        #[cfg(all(unix, not(target_os = "wasi")))]
        {
            use cap_std::fs::{DirBuilder, DirBuilderExt as _};

            let mut builder = DirBuilder::new();
            builder.mode(mode.unwrap_or(0o700));
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
    let directory = open_directory(parent, name, None, label)?;
    set_directory_mode(&directory, mode)?;
    let current = open_directory(parent, name, mode, label)?;
    let opened = directory.dir_metadata()?;
    if !same_object_cap(&opened, &current.dir_metadata()?) {
        return invalid(format!("{label} changed while creating"));
    }
    Ok(directory)
}

fn write_independent_file(
    parent: &Dir,
    name: &str,
    bytes: &[u8],
    mode: u32,
    label: &str,
) -> Result<(), LifecycleError> {
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
    let mut file = parent.open_with(name, &options).map_err(|error| {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            LifecycleError::Invalid(format!("{label} already exists"))
        } else {
            error.into()
        }
    })?;
    let opened = file.metadata()?;
    file.write_all(bytes)?;
    file.flush()?;
    set_file_mode(&file, Some(mode))?;
    let completed = file.metadata()?;
    let current = open_regular_file(parent, OsStr::new(name), Some(mode), label)?.metadata()?;
    if !same_object_cap(&opened, &completed)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&completed, &current)
        || completed.nlink() != 1
        || current.nlink() != 1
    {
        return invalid(format!("{label} changed while writing"));
    }
    Ok(())
}

#[cfg(not(windows))]
fn stable_link_target(parent: &Dir, name: &OsStr, label: &str) -> Result<PathBuf, LifecycleError> {
    let before = parent.symlink_metadata(name)?;
    if !before.file_type().is_symlink() {
        return invalid(format!("{label} is missing or unsafe"));
    }
    let target = parent.read_link_contents(name)?;
    let after = parent.symlink_metadata(name)?;
    let current_target = parent.read_link_contents(name)?;
    if !after.file_type().is_symlink()
        || !same_object_cap(&before, &after)
        || !same_content_state_cap(&before, &after)
        || target != current_target
    {
        return invalid(format!("{label} changed while reading"));
    }
    Ok(target)
}

#[cfg(not(windows))]
fn require_source_link(parent: &Dir, name: &OsStr, expected: &Path) -> Result<(), LifecycleError> {
    let metadata = parent.symlink_metadata(name)?;
    if !metadata.file_type().is_symlink()
        || stable_link_target(parent, name, "external .system symlink")? != expected
    {
        return invalid("external .system symlink changed before copying");
    }
    Ok(())
}

#[cfg(not(windows))]
fn create_symlink(parent: &Dir, name: &OsStr, target: &Path) -> Result<(), LifecycleError> {
    parent.symlink_contents(target, name)?;
    if stable_link_target(parent, name, "staged .system symlink")? != target {
        return invalid("staged .system symlink differs after creation");
    }
    Ok(())
}

fn split_entry_path(path: &Path) -> Result<(&OsStr, &Path), LifecycleError> {
    let name = path.file_name().ok_or_else(|| {
        LifecycleError::Invalid("external .system entry path is invalid".to_owned())
    })?;
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    Ok((name, parent))
}

#[cfg(unix)]
#[allow(clippy::unnecessary_wraps)]
fn preserved_mode(metadata: &cap_std::fs::Metadata) -> Option<u32> {
    use cap_std::fs::MetadataExt as _;
    Some(metadata.mode() & 0o7777)
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn preserved_mode(_metadata: &cap_std::fs::Metadata) -> Option<u32> {
    None
}

#[cfg(unix)]
fn require_mode(
    metadata: &cap_std::fs::Metadata,
    expected: Option<u32>,
    label: &str,
) -> Result<(), LifecycleError> {
    use cap_std::fs::MetadataExt as _;
    if expected.is_some_and(|mode| metadata.mode() & 0o7777 != mode) {
        return invalid(format!("{label} mode changed"));
    }
    Ok(())
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn require_mode(
    _metadata: &cap_std::fs::Metadata,
    _expected: Option<u32>,
    _label: &str,
) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
fn set_file_mode(file: &cap_std::fs::File, mode: Option<u32>) -> Result<(), LifecycleError> {
    use cap_std::fs::{Permissions, PermissionsExt as _};
    if let Some(mode) = mode {
        file.set_permissions(Permissions::from_mode(mode))?;
    }
    Ok(())
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn set_file_mode(_file: &cap_std::fs::File, _mode: Option<u32>) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
fn set_directory_mode(directory: &Dir, mode: Option<u32>) -> Result<(), LifecycleError> {
    use cap_std::fs::{Permissions, PermissionsExt as _};
    if let Some(mode) = mode {
        directory.set_permissions(".", Permissions::from_mode(mode))?;
    }
    Ok(())
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn set_directory_mode(_directory: &Dir, _mode: Option<u32>) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
#[allow(clippy::unnecessary_wraps)]
fn working_directory_mode() -> Option<u32> {
    Some(0o700)
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn working_directory_mode() -> Option<u32> {
    None
}

#[cfg(unix)]
fn path_bytes(path: &Path) -> usize {
    use std::os::unix::ffi::OsStrExt as _;
    path.as_os_str().as_bytes().len()
}

#[cfg(windows)]
fn path_bytes(path: &Path) -> usize {
    use std::os::windows::ffi::OsStrExt as _;
    path.as_os_str().encode_wide().count().saturating_mul(2)
}

#[cfg(not(any(unix, windows)))]
fn path_bytes(path: &Path) -> usize {
    path.as_os_str().to_string_lossy().len()
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}
