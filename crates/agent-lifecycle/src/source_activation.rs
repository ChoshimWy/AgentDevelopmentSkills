use super::{
    EXTERNAL_ACTIVATION_LOCK, LifecycleError, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    configure_nofollow, open_child_directory, same_content_state_cap, same_object_cap,
    validate_activation_lock_contract,
};
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, parse_json};
use cap_fs_ext::{FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, Metadata, OpenOptions};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::io::{Read as _, Write as _};
use std::path::{Component, Path};
use std::sync::atomic::{AtomicU64, Ordering};

pub(super) const DEACTIVATION_HANDLER_ID: &str = "core.source-deactivation.apple-codex-v1";

static CONFIG_TEMPORARY_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Debug)]
pub(super) struct SourceDeactivation {
    activation_lock: Vec<u8>,
    config: ConfigDeactivation,
    records: Vec<ActivationRecord>,
    scope: Vec<String>,
}

#[derive(Debug)]
struct ActivationRecord {
    mode: u32,
    path: String,
    sha256: String,
}

#[derive(Debug)]
enum ConfigDeactivation {
    Missing,
    Preserved(FileSnapshot),
    Replace {
        candidate: Vec<u8>,
        original: FileSnapshot,
    },
}

#[derive(Debug)]
struct FileSnapshot {
    bytes: Vec<u8>,
    identity: Metadata,
    mode: u32,
}

impl SourceDeactivation {
    pub(super) fn prepare(target: &Dir, target_path: &Path) -> Result<Self, LifecycleError> {
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        let activation_lock = read_required_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            Some(MANAGED_FILE_MODE),
            "source activation Lock",
        )?
        .bytes;
        if activation_lock.len() > MAX_CONTRACT_JSON_BYTES {
            return Err(agent_contracts::ContractError::InputTooLarge {
                maximum: MAX_CONTRACT_JSON_BYTES,
            }
            .into());
        }
        let lock = parse_json(&activation_lock)?;
        let (_, values) = validate_activation_lock_contract(&lock)?;
        let mut records = Vec::with_capacity(values.len());
        for value in values {
            let path = value
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_error("source activation Lock path is invalid"))?
                .to_owned();
            let mode = value
                .get("mode")
                .and_then(Value::as_u64)
                .and_then(|mode| u32::try_from(mode).ok())
                .ok_or_else(|| invalid_error("source activation Lock mode is invalid"))?;
            let sha256 = value
                .get("sha256")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_error("source activation Lock hash is invalid"))?
                .to_owned();
            verify_activation_file(target, &path, mode, &sha256)?;
            records.push(ActivationRecord { mode, path, sha256 });
        }
        records.sort_by(|left, right| left.path.cmp(&right.path));
        let config = prepare_config_deactivation(target, target_path)?;
        let mut scope = records
            .iter()
            .map(|record| record.path.clone())
            .collect::<Vec<_>>();
        scope.push("config.toml".to_owned());
        scope.sort();
        scope.dedup();
        Ok(Self {
            activation_lock,
            config,
            records,
            scope,
        })
    }

    pub(super) fn scope(&self) -> &[String] {
        &self.scope
    }

    pub(super) fn apply_with_hook(
        self,
        target: &Dir,
        scratch: &Dir,
        mut handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        self.revalidate(target)?;
        for record in &self.records {
            remove_activation_file(target, record)?;
            handler_hook(&record.path, "owned-file-removed")?;
        }
        let config_action = match &self.config {
            ConfigDeactivation::Missing => "missing",
            ConfigDeactivation::Preserved(_) => "preserved",
            ConfigDeactivation::Replace {
                candidate,
                original,
            } => {
                replace_config(target, scratch, original, candidate, &mut handler_hook)?;
                "removed-managed-instructions-path"
            }
        };
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        let current = read_required_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            Some(MANAGED_FILE_MODE),
            "source activation Lock",
        )?;
        if current.bytes != self.activation_lock {
            return invalid("source activation Lock changed during deactivation");
        }
        managed.remove_file(EXTERNAL_ACTIVATION_LOCK)?;
        Ok(json!({
            "config_action": config_action,
            "handler": DEACTIVATION_HANDLER_ID,
            "removed_files": self.records.into_iter().map(|record| record.path).collect::<Vec<_>>(),
        }))
    }

    pub(super) fn revalidate(&self, target: &Dir) -> Result<(), LifecycleError> {
        for record in &self.records {
            verify_activation_file(target, &record.path, record.mode, &record.sha256)?;
        }
        match &self.config {
            ConfigDeactivation::Missing => {
                if target.symlink_metadata("config.toml").is_ok() {
                    return invalid("config.toml changed before source deactivation");
                }
            }
            ConfigDeactivation::Preserved(snapshot)
            | ConfigDeactivation::Replace {
                original: snapshot, ..
            } => verify_file_snapshot(target, "config.toml", snapshot, "config.toml")?,
        }
        let managed = open_child_directory(
            target,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )?;
        if read_required_file(
            &managed,
            EXTERNAL_ACTIVATION_LOCK,
            Some(MANAGED_FILE_MODE),
            "source activation Lock",
        )?
        .bytes
            != self.activation_lock
        {
            return invalid("source activation Lock changed before deactivation");
        }
        Ok(())
    }
}

fn prepare_config_deactivation(
    target: &Dir,
    target_path: &Path,
) -> Result<ConfigDeactivation, LifecycleError> {
    let Some(original) = read_optional_file(target, "config.toml", None, "config.toml")? else {
        return Ok(ConfigDeactivation::Missing);
    };
    let text = std::str::from_utf8(&original.bytes)
        .map_err(|_| invalid_error("config.toml must be valid UTF-8 TOML"))?;
    let parsed = text
        .parse::<toml::Value>()
        .map_err(|_| invalid_error("config.toml must be valid UTF-8 TOML"))?;
    let expected_instructions = target_path.join("AGENTS.md");
    let expected_instructions = expected_instructions
        .to_str()
        .ok_or_else(|| invalid_error("source deactivation target path must be valid UTF-8"))?;
    if parsed
        .get("model_instructions_file")
        .and_then(toml::Value::as_str)
        != Some(expected_instructions)
    {
        return Ok(ConfigDeactivation::Preserved(original));
    }
    let (candidate, matches) = remove_root_assignment(text);
    if matches != 1 {
        return invalid("managed model_instructions_file must be one root-level assignment");
    }
    let reparsed = candidate
        .parse::<toml::Value>()
        .map_err(|_| invalid_error("targeted config deactivation is not valid TOML"))?;
    let mut expected = parsed;
    let expected_table = expected
        .as_table_mut()
        .ok_or_else(|| invalid_error("config.toml root must be a TOML table"))?;
    expected_table.remove("model_instructions_file");
    if reparsed != expected {
        return invalid("targeted config deactivation changed unmanaged values");
    }
    Ok(ConfigDeactivation::Replace {
        candidate: candidate.into_bytes(),
        original,
    })
}

fn remove_root_assignment(text: &str) -> (String, usize) {
    let mut candidate = String::with_capacity(text.len());
    let mut matches = 0_usize;
    let mut root = true;
    for line in text.split_inclusive('\n') {
        let trimmed = line.trim_start_matches([' ', '\t']);
        if root && trimmed.starts_with('[') {
            root = false;
        }
        if root && !trimmed.starts_with('#') && is_model_instructions_assignment(trimmed) {
            matches += 1;
        } else {
            candidate.push_str(line);
        }
    }
    (candidate, matches)
}

fn is_model_instructions_assignment(line: &str) -> bool {
    [
        "model_instructions_file",
        "\"model_instructions_file\"",
        "'model_instructions_file'",
    ]
    .iter()
    .any(|key| {
        line.strip_prefix(key)
            .is_some_and(|suffix| suffix.trim_start_matches([' ', '\t']).starts_with('='))
    })
}

fn verify_activation_file(
    target: &Dir,
    relative: &str,
    mode: u32,
    expected_sha256: &str,
) -> Result<(), LifecycleError> {
    let (parent, name) = open_relative_parent(target, relative, "activated file")?;
    let snapshot = read_required_file(&parent, name, Some(mode), "activated file")?;
    if format!("{:x}", Sha256::digest(&snapshot.bytes)) != expected_sha256 {
        return invalid(format!(
            "activated file preimage differs from Lock: {relative}"
        ));
    }
    Ok(())
}

fn remove_activation_file(target: &Dir, record: &ActivationRecord) -> Result<(), LifecycleError> {
    verify_activation_file(target, &record.path, record.mode, &record.sha256)?;
    let (parent, name) = open_relative_parent(target, &record.path, "activated file")?;
    parent.remove_file(name)?;
    Ok(())
}

fn replace_config(
    target: &Dir,
    scratch: &Dir,
    original: &FileSnapshot,
    candidate: &[u8],
    handler_hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    verify_file_snapshot(target, "config.toml", original, "config.toml")?;
    let (temporary, prepared) = prepare_config_temporary(scratch, original, candidate)?;
    let result = (|| {
        verify_file_snapshot(target, "config.toml", original, "config.toml")?;
        let current_temporary = read_required_file(
            scratch,
            &temporary,
            Some(original.mode),
            "config replacement temporary file",
        )?;
        if !same_object_cap(&prepared.identity, &current_temporary.identity)
            || !same_content_state_cap(&prepared.identity, &current_temporary.identity)
            || current_temporary.identity.nlink() != 1
            || current_temporary.bytes != candidate
        {
            return invalid("config replacement temporary file changed before publication");
        }
        handler_hook(&temporary, "config-temporary-prepared")?;
        scratch.rename(&temporary, target, "config.toml")?;
        let current =
            read_required_file(target, "config.toml", Some(original.mode), "config.toml")?;
        if !same_object_cap(&prepared.identity, &current.identity)
            || current.identity.nlink() != 1
            || current.bytes != candidate
        {
            return invalid("config.toml replacement differs after source deactivation");
        }
        Ok(())
    })();
    if let Err(error) = result {
        return cleanup_temporary_after_error(scratch, &temporary, error);
    }
    Ok(())
}

fn prepare_config_temporary(
    scratch: &Dir,
    original: &FileSnapshot,
    candidate: &[u8],
) -> Result<(String, FileSnapshot), LifecycleError> {
    let mut temporary = None;
    for attempt in 0..128_u64 {
        let name = format!(
            ".config.toml.agent-skills-{}-{}-{}",
            std::process::id(),
            CONFIG_TEMPORARY_ID.fetch_add(1, Ordering::Relaxed),
            attempt
        );
        let mut options = OpenOptions::new();
        options
            .write(true)
            .create_new(true)
            .follow(FollowSymlinks::No);
        configure_nofollow(&mut options);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        match scratch.open_with(&name, &options) {
            Ok(mut file) => {
                let opened = file.metadata()?;
                let prepared = (|| {
                    if opened.nlink() != 1 || !mode_matches(file_mode(&opened), Some(0o600)) {
                        return invalid(
                            "config replacement temporary file is not private or has a hard-link alias",
                        );
                    }
                    file.write_all(candidate)?;
                    #[cfg(unix)]
                    {
                        use cap_std::fs::{Permissions, PermissionsExt as _};
                        file.set_permissions(Permissions::from_mode(original.mode))?;
                    }
                    file.sync_all()?;
                    let completed = file.metadata()?;
                    if !same_object_cap(&opened, &completed)
                        || completed.nlink() != 1
                        || !mode_matches(file_mode(&completed), Some(original.mode))
                    {
                        return invalid("config replacement temporary file changed while writing");
                    }
                    drop(file);
                    let reopened = read_required_file(
                        scratch,
                        &name,
                        Some(original.mode),
                        "config replacement temporary file",
                    )?;
                    if !same_object_cap(&completed, &reopened.identity)
                        || !same_content_state_cap(&completed, &reopened.identity)
                        || reopened.identity.nlink() != 1
                        || reopened.bytes != candidate
                    {
                        return invalid("config replacement temporary file changed after writing");
                    }
                    Ok(reopened)
                })();
                match prepared {
                    Ok(prepared) => temporary = Some((name, prepared)),
                    Err(error) => return cleanup_temporary_after_error(scratch, &name, error),
                }
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
    }
    temporary.ok_or_else(|| invalid_error("could not allocate config replacement"))
}

fn cleanup_temporary_after_error<T>(
    scratch: &Dir,
    temporary: &str,
    error: LifecycleError,
) -> Result<T, LifecycleError> {
    match scratch.remove_file(temporary) {
        Ok(()) => Err(error),
        Err(cleanup) if cleanup.kind() == std::io::ErrorKind::NotFound => Err(error),
        Err(cleanup) => invalid(format!(
            "{error}; config replacement temporary cleanup is incomplete: {cleanup}"
        )),
    }
}

fn verify_file_snapshot(
    parent: &Dir,
    name: &str,
    expected: &FileSnapshot,
    label: &str,
) -> Result<(), LifecycleError> {
    let current = read_required_file(parent, name, Some(expected.mode), label)?;
    if !same_object_cap(&expected.identity, &current.identity)
        || !same_content_state_cap(&expected.identity, &current.identity)
        || expected.bytes != current.bytes
    {
        return invalid(format!("{label} changed before source deactivation"));
    }
    Ok(())
}

fn read_optional_file(
    parent: &Dir,
    name: &str,
    mode: Option<u32>,
    label: &str,
) -> Result<Option<FileSnapshot>, LifecycleError> {
    match parent.symlink_metadata(name) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            invalid(format!("{label} must be a regular file"))
        }
        Ok(_) => read_required_file(parent, name, mode, label).map(Some),
    }
}

fn read_required_file(
    parent: &Dir,
    name: &str,
    expected_mode: Option<u32>,
    label: &str,
) -> Result<FileSnapshot, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| invalid_error(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_file() {
        return invalid(format!("{label} is missing or unsafe"));
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    let mut file = parent
        .open_with(name, &options)
        .map_err(|_| invalid_error(format!("{label} is missing or unsafe")))?;
    let identity = file.metadata()?;
    let mode = file_mode(&identity);
    if !mode_matches(mode, expected_mode) {
        return invalid(format!("{label} mode is not canonical"));
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    let reopened = parent
        .open_with(name, &options)
        .and_then(|file| file.metadata())
        .map_err(|_| invalid_error(format!("{label} changed while reading")))?;
    if !same_object_cap(&identity, &after)
        || !same_object_cap(&identity, &reopened)
        || !same_content_state_cap(&identity, &after)
        || !same_content_state_cap(&identity, &reopened)
    {
        return invalid(format!("{label} changed while reading"));
    }
    Ok(FileSnapshot {
        bytes,
        identity,
        mode,
    })
}

fn open_relative_parent<'a>(
    root: &Dir,
    relative: &'a str,
    label: &str,
) -> Result<(Dir, &'a str), LifecycleError> {
    let path = Path::new(relative);
    if path.is_absolute() {
        return invalid(format!("{label} must be a package-relative path"));
    }
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => parts.push(
                part.to_str()
                    .ok_or_else(|| invalid_error(format!("{label} path is invalid")))?,
            ),
            Component::CurDir => {}
            Component::ParentDir | Component::Prefix(_) | Component::RootDir => {
                return invalid(format!("{label} must be a package-relative path"));
            }
        }
    }
    let (name, parents) = parts
        .split_last()
        .ok_or_else(|| invalid_error(format!("{label} must be a package-relative path")))?;
    let mut directory = root.try_clone()?;
    for parent in parents {
        directory = open_child_directory(&directory, parent, None, label)?;
    }
    Ok((directory, name))
}

#[cfg(unix)]
fn file_mode(metadata: &Metadata) -> u32 {
    use cap_std::fs::MetadataExt as _;
    metadata.mode() & 0o777
}

#[cfg(not(unix))]
fn file_mode(_metadata: &Metadata) -> u32 {
    MANAGED_FILE_MODE
}

#[cfg(unix)]
fn mode_matches(actual: u32, expected: Option<u32>) -> bool {
    expected.is_none_or(|expected| expected == actual)
}

#[cfg(not(unix))]
fn mode_matches(_actual: u32, _expected: Option<u32>) -> bool {
    true
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(invalid_error(message))
}

fn invalid_error(message: impl Into<String>) -> LifecycleError {
    LifecycleError::Invalid(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use cap_std::ambient_authority;
    use std::sync::atomic::AtomicU64;

    static TEST_ID: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn root_assignment_removal_preserves_every_other_byte() {
        let input = concat!(
            "# heading\n",
            "\"model_instructions_file\" = \"/tmp/AGENTS.md\" # owned\n",
            "model = \"gpt\"\n",
            "[features]\n",
            "model_instructions_file = \"nested\"\n",
        );
        let (candidate, matches) = remove_root_assignment(input);
        assert_eq!(matches, 1);
        assert_eq!(
            candidate,
            concat!(
                "# heading\n",
                "model = \"gpt\"\n",
                "[features]\n",
                "model_instructions_file = \"nested\"\n",
            )
        );
    }

    #[test]
    fn all_owned_preimages_are_revalidated_before_the_first_delete() {
        let target_path = std::env::temp_dir().join(format!(
            "agent-source-deactivation-{}-{}",
            std::process::id(),
            TEST_ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&target_path).expect("create target");
        std::fs::create_dir(target_path.join(".agent-skills")).expect("create managed root");
        std::fs::create_dir(target_path.join("bin")).expect("create bin");
        std::fs::write(target_path.join("bin/a"), b"a\n").expect("write first asset");
        std::fs::write(target_path.join("bin/b"), b"b\n").expect("write second asset");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            for path in [target_path.join(".agent-skills"), target_path.join("bin")] {
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
                    .expect("set directory mode");
            }
            for path in [target_path.join("bin/a"), target_path.join("bin/b")] {
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
                    .expect("set file mode");
            }
        }
        let lock = json!({
            "files": [
                {"mode": 0o755, "path": "bin/a", "sha256": format!("{:x}", Sha256::digest(b"a\n"))},
                {"mode": 0o755, "path": "bin/b", "sha256": format!("{:x}", Sha256::digest(b"b\n"))},
            ],
            "handler": "core.source-activation.apple-codex-v1",
            "manager": "agent-development-skills",
            "schema_version": "2.0",
        });
        let lock_path = target_path.join(".agent-skills/activation-lock.json");
        std::fs::write(
            &lock_path,
            agent_contracts::canonical_json(&lock).expect("encode lock"),
        )
        .expect("write lock");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::set_permissions(&lock_path, std::fs::Permissions::from_mode(0o644))
                .expect("set lock mode");
        }
        let target_path = target_path.canonicalize().expect("canonical target");
        let target = Dir::open_ambient_dir(&target_path, ambient_authority()).expect("open target");
        let prepared =
            SourceDeactivation::prepare(&target, &target_path).expect("prepare deactivation");
        std::fs::write(target_path.join("bin/b"), b"changed\n").expect("drift second asset");
        let error = prepared
            .apply_with_hook(&target, &target, |_, _| Ok(()))
            .expect_err("drift must fail before deletion");
        assert!(
            error
                .to_string()
                .contains("activated file preimage differs"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(target_path.join("bin/a")).expect("read first asset"),
            b"a\n"
        );
        assert!(lock_path.exists());
        drop(target);
        std::fs::remove_dir_all(&target_path).expect("remove target");
    }
}
