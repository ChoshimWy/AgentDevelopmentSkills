use super::{
    LIFECYCLE_LOCK_DIRECTORY, LifecycleError, MANAGED_DIRECTORY_MODE, absolute_path,
    open_absolute_directory_nofollow, open_child_directory, same_object_cap,
};
use cap_std::fs::Dir;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

/// Exclusive directory lock for one installation lifecycle transaction.
///
/// The lock directory lives outside all swapped managed roots. Its creation is
/// atomic across processes, and a crashed process intentionally leaves the
/// directory behind so Doctor reports recovery attention instead of guessing
/// that the transaction is stale.
///
/// The target parent namespace must be trusted against concurrent adversarial
/// replacement while a portable name-based release is in progress.
#[must_use = "the lifecycle lock guard must be held for the full transaction"]
pub struct LifecycleLock {
    active: bool,
    contract_target: PathBuf,
    identity: cap_std::fs::Metadata,
    lock_directory: Option<Dir>,
    target: PathBuf,
    target_directory: Dir,
}

impl LifecycleLock {
    /// Acquire the lifecycle lock, securely creating missing target directories.
    ///
    /// # Errors
    /// Fails when the target traverses a symlink or non-directory, when another
    /// lifecycle operation owns the lock, or when the lock cannot be opened as
    /// the same directory that was atomically created.
    pub fn acquire(target_root: impl AsRef<Path>) -> Result<Self, LifecycleError> {
        reject_unexpanded_home(target_root.as_ref())?;
        let requested_target = absolute_path(target_root.as_ref())?;
        let target_directory = open_or_create_directory(&requested_target)?;
        Self::acquire_opened_target(&requested_target, target_directory)
    }

    /// Acquire the lifecycle lock without creating a missing target.
    ///
    /// Destructive lifecycle commands use this entry point so a misspelled or
    /// concurrently removed target cannot be recreated as an empty directory.
    ///
    /// # Errors
    /// Fails when the target does not already exist as a real directory, when
    /// it traverses a symlink, or when normal lifecycle lock acquisition fails.
    pub fn acquire_existing(target_root: impl AsRef<Path>) -> Result<Self, LifecycleError> {
        let (target_directory, target, contract_target) =
            inspect_existing_target(target_root.as_ref())?;
        Self::acquire_resolved_target(target_directory, target, contract_target)
    }

    fn acquire_opened_target(
        requested_target: &Path,
        target_directory: Dir,
    ) -> Result<Self, LifecycleError> {
        let target = canonical_path_for_directory(requested_target, &target_directory)?;
        let contract_target =
            contract_path_for_directory(requested_target, &target, &target_directory);
        Self::acquire_resolved_target(target_directory, target, contract_target)
    }

    fn acquire_resolved_target(
        target_directory: Dir,
        target: PathBuf,
        contract_target: PathBuf,
    ) -> Result<Self, LifecycleError> {
        match create_lock_directory(&target_directory) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                return invalid(format!(
                    "lifecycle operation is already active or recovery is required: {}",
                    target.join(LIFECYCLE_LOCK_DIRECTORY).display()
                ));
            }
            Err(error) => return Err(error.into()),
        }
        let lock = open_child_directory(
            &target_directory,
            LIFECYCLE_LOCK_DIRECTORY,
            None,
            "lifecycle lock",
        )?;
        let identity = lock.dir_metadata()?;
        let prepared = (|| {
            #[cfg(unix)]
            {
                use cap_std::fs::{Permissions, PermissionsExt as _};
                lock.set_permissions(".", Permissions::from_mode(MANAGED_DIRECTORY_MODE))?;
            }
            Ok::<_, LifecycleError>(
                open_child_directory(
                    &target_directory,
                    LIFECYCLE_LOCK_DIRECTORY,
                    Some(MANAGED_DIRECTORY_MODE),
                    "lifecycle lock",
                )?
                .dir_metadata()?,
            )
        })();
        let current = match prepared {
            Ok(current) => current,
            Err(error) => {
                remove_owned_lock(&target_directory, &identity);
                return Err(error);
            }
        };
        if !same_object_cap(&identity, &current) {
            return invalid("lifecycle lock changed while acquiring");
        }
        canonical_path_for_directory(&target, &target_directory)?;
        Ok(Self {
            active: true,
            contract_target,
            identity: current,
            lock_directory: Some(lock),
            target,
            target_directory,
        })
    }

    /// Return the absolute target identity frozen by this lock.
    #[must_use]
    pub fn target(&self) -> &Path {
        &self.target
    }

    /// Return the target spelling exposed through Python-compatible contracts.
    #[must_use]
    pub fn contract_target(&self) -> &Path {
        &self.contract_target
    }

    pub(super) fn target_directory(&self) -> Result<Dir, LifecycleError> {
        Ok(self.target_directory.try_clone()?)
    }

    pub(super) fn directory(&self) -> Result<Dir, LifecycleError> {
        self.validate()?;
        Ok(self
            .lock_directory
            .as_ref()
            .ok_or_else(|| {
                LifecycleError::Invalid("lifecycle lock token is no longer active".into())
            })?
            .try_clone()?)
    }

    /// Prove that the lock directory still denotes the acquired object.
    ///
    /// # Errors
    /// Fails if the lock was released, removed, replaced, or changed mode.
    pub fn validate(&self) -> Result<(), LifecycleError> {
        if !self.active {
            return invalid("lifecycle lock token is no longer active");
        }
        canonical_path_for_directory(&self.target, &self.target_directory)?;
        let held_lock = self.lock_directory.as_ref().ok_or_else(|| {
            LifecycleError::Invalid("lifecycle lock token is no longer active".into())
        })?;
        if !same_object_cap(&self.identity, &held_lock.dir_metadata()?) {
            return invalid("lifecycle lock token is no longer active");
        }
        let current = open_child_directory(
            &self.target_directory,
            LIFECYCLE_LOCK_DIRECTORY,
            Some(MANAGED_DIRECTORY_MODE),
            "lifecycle lock token",
        )?
        .dir_metadata()?;
        if !same_object_cap(&self.identity, &current) {
            return invalid("lifecycle lock token is no longer active");
        }
        Ok(())
    }

    /// Validate both the live lock object and its binding to an operation target.
    ///
    /// # Errors
    /// Fails if the supplied target differs from the one used for acquisition or
    /// if the directory token is no longer active.
    pub fn validate_for(&self, target_root: impl AsRef<Path>) -> Result<(), LifecycleError> {
        let requested = absolute_path(target_root.as_ref())?;
        let canonical =
            canonical_path_for_directory(&requested, &self.target_directory).map_err(|_| {
                LifecycleError::Invalid("lifecycle lock token does not match target".into())
            })?;
        if canonical != self.target {
            return invalid("lifecycle lock token does not match target");
        }
        self.validate()
    }

    /// Release the lock only if it is still the acquired empty directory.
    ///
    /// Portable removal is name-based after immediate identity revalidation and
    /// therefore relies on the trusted-parent requirement documented on the guard.
    ///
    /// # Errors
    /// Fails closed when the lock was replaced or contains unexpected residue.
    pub fn release(mut self) -> Result<(), LifecycleError> {
        self.validate()?;
        #[cfg(windows)]
        drop(self.lock_directory.take());
        self.target_directory.remove_dir(LIFECYCLE_LOCK_DIRECTORY)?;
        self.active = false;
        Ok(())
    }
}

pub(super) fn inspect_existing_target(
    target_root: &Path,
) -> Result<(Dir, PathBuf, PathBuf), LifecycleError> {
    reject_unexpanded_home(target_root)?;
    let requested_target = absolute_path(target_root)?;
    let target_directory = open_absolute_directory_nofollow(&requested_target)?;
    let target = canonical_path_for_directory(&requested_target, &target_directory)?;
    let contract_target =
        contract_path_for_directory(&requested_target, &target, &target_directory);
    Ok((target_directory, target, contract_target))
}

pub(super) fn inspect_optional_target(target_root: &Path) -> Result<Option<Dir>, LifecycleError> {
    reject_unexpanded_home(target_root)?;
    let requested_target = absolute_path(target_root)?;
    match std::fs::symlink_metadata(&requested_target) {
        Ok(_) => {
            let (target, _, _) = inspect_existing_target(&requested_target)?;
            Ok(Some(target))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut existing = requested_target.as_path();
            loop {
                match std::fs::symlink_metadata(existing) {
                    Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                        return invalid(format!(
                            "lifecycle target must not traverse a symlink or non-directory: {}",
                            requested_target.display()
                        ));
                    }
                    Ok(_) => {
                        open_absolute_directory_nofollow(existing)?;
                        return Ok(None);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        existing = existing.parent().ok_or_else(|| {
                            LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                        })?;
                    }
                    Err(error) => return Err(error.into()),
                }
            }
        }
        Err(error) => Err(error.into()),
    }
}

pub(super) struct LifecycleTargetSnapshot {
    contract_target: PathBuf,
    state: LifecycleTargetState,
}

enum LifecycleTargetState {
    Existing {
        canonical: PathBuf,
        directory: Dir,
        requested: PathBuf,
    },
    Missing {
        ancestor: Dir,
        ancestor_canonical: PathBuf,
        ancestor_requested: PathBuf,
        first_missing: OsString,
    },
}

impl LifecycleTargetSnapshot {
    pub(super) fn capture(target_root: &Path) -> Result<Self, LifecycleError> {
        reject_unexpanded_home(target_root)?;
        let requested = absolute_path(target_root)?;
        let mut existing = requested.as_path();
        let mut missing = Vec::new();
        loop {
            match std::fs::symlink_metadata(existing) {
                Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                    return invalid(format!(
                        "lifecycle target must not traverse a symlink or non-directory: {}",
                        requested.display()
                    ));
                }
                Ok(_) => {
                    let directory = open_absolute_directory_nofollow(existing)?;
                    let canonical = canonical_path_for_directory(existing, &directory)?;
                    if missing.is_empty() {
                        let contract_target =
                            contract_path_for_directory(&requested, &canonical, &directory);
                        return Ok(Self {
                            contract_target,
                            state: LifecycleTargetState::Existing {
                                canonical,
                                directory,
                                requested,
                            },
                        });
                    }
                    let first_missing = missing.last().cloned().ok_or_else(|| {
                        LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                    })?;
                    let mut contract_target = canonical.clone();
                    for component in missing.iter().rev() {
                        contract_target.push(component);
                    }
                    #[cfg(windows)]
                    if !has_explicit_verbatim_prefix(&requested) {
                        contract_target = strip_verbatim_prefix(&contract_target);
                    }
                    return Ok(Self {
                        contract_target,
                        state: LifecycleTargetState::Missing {
                            ancestor: directory,
                            ancestor_canonical: canonical,
                            ancestor_requested: existing.to_path_buf(),
                            first_missing,
                        },
                    });
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    let component = existing.file_name().ok_or_else(|| {
                        LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                    })?;
                    missing.push(component.to_owned());
                    existing = existing.parent().ok_or_else(|| {
                        LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                    })?;
                }
                Err(error) => return Err(error.into()),
            }
        }
    }

    pub(super) fn directory(&self) -> Option<&Dir> {
        match &self.state {
            LifecycleTargetState::Existing { directory, .. } => Some(directory),
            LifecycleTargetState::Missing { .. } => None,
        }
    }

    pub(super) fn contract_target(&self) -> &Path {
        &self.contract_target
    }

    pub(super) fn validate(&self) -> Result<(), LifecycleError> {
        match &self.state {
            LifecycleTargetState::Existing {
                canonical,
                directory,
                requested,
            } => {
                let current = canonical_path_for_directory(requested, directory)?;
                if &current != canonical {
                    return invalid("lifecycle target changed while previewing install");
                }
            }
            LifecycleTargetState::Missing {
                ancestor,
                ancestor_canonical,
                ancestor_requested,
                first_missing,
            } => {
                let current = canonical_path_for_directory(ancestor_requested, ancestor)?;
                if &current != ancestor_canonical {
                    return invalid("lifecycle target changed while previewing install");
                }
                match ancestor.symlink_metadata(first_missing) {
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                    Ok(_) => {
                        return invalid("lifecycle target appeared while previewing install");
                    }
                }
            }
        }
        Ok(())
    }
}

impl Drop for LifecycleLock {
    fn drop(&mut self) {
        if self.active && self.validate().is_ok() {
            #[cfg(windows)]
            drop(self.lock_directory.take());
            if self
                .target_directory
                .remove_dir(LIFECYCLE_LOCK_DIRECTORY)
                .is_ok()
            {
                self.active = false;
            }
        }
    }
}

fn create_lock_directory(target: &Dir) -> std::io::Result<()> {
    #[cfg(all(unix, not(target_os = "wasi")))]
    {
        use cap_std::fs::{DirBuilder, DirBuilderExt as _};

        let mut builder = DirBuilder::new();
        builder.mode(MANAGED_DIRECTORY_MODE);
        target.create_dir_with(LIFECYCLE_LOCK_DIRECTORY, &builder)
    }
    #[cfg(any(not(unix), target_os = "wasi"))]
    {
        target.create_dir(LIFECYCLE_LOCK_DIRECTORY)
    }
}

/// Resolve the Python-compatible absolute spelling for an existing or missing lifecycle target.
///
/// Existing ancestors must be real directories. Missing suffix components are
/// appended to the canonical nearest existing ancestor without creating them.
///
/// # Errors
/// Returns a fail-closed error for unexpanded home shorthand, symlink or
/// non-directory traversal, invalid roots, or canonicalization failure.
pub fn normalize_lifecycle_target(
    target_root: impl AsRef<Path>,
) -> Result<PathBuf, LifecycleError> {
    reject_unexpanded_home(target_root.as_ref())?;
    let requested = absolute_path(target_root.as_ref())?;
    let mut existing = requested.as_path();
    let mut missing = Vec::new();
    loop {
        match std::fs::symlink_metadata(existing) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!(
                    "lifecycle target must not traverse a symlink or non-directory: {}",
                    requested.display()
                ));
            }
            Ok(_) => {
                open_absolute_directory_nofollow(existing)?;
                let mut normalized = std::fs::canonicalize(existing)?;
                for component in missing.iter().rev() {
                    normalized.push(component);
                }
                #[cfg(windows)]
                if !has_explicit_verbatim_prefix(&requested) {
                    normalized = strip_verbatim_prefix(&normalized);
                }
                return Ok(normalized);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let component = existing.file_name().ok_or_else(|| {
                    LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                })?;
                missing.push(component.to_owned());
                existing = existing.parent().ok_or_else(|| {
                    LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                })?;
            }
            Err(error) => return Err(error.into()),
        }
    }
}

fn open_or_create_directory(path: &Path) -> Result<Dir, LifecycleError> {
    let target = absolute_path(path)?;
    let mut existing = target.as_path();
    let mut missing = Vec::new();
    loop {
        match std::fs::symlink_metadata(existing) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return invalid(format!(
                    "lifecycle target must not traverse a symlink or non-directory: {}",
                    target.display()
                ));
            }
            Ok(_) => break,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let name = existing.file_name().ok_or_else(|| {
                    LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                })?;
                missing.push(name.to_owned());
                existing = existing.parent().ok_or_else(|| {
                    LifecycleError::Invalid("lifecycle target is invalid".to_owned())
                })?;
            }
            Err(error) => return Err(error.into()),
        }
    }

    let mut directory = open_absolute_directory_nofollow(existing)?;
    for name in missing.iter().rev() {
        let name = name
            .to_str()
            .ok_or_else(|| LifecycleError::Invalid("lifecycle target is not UTF-8".to_owned()))?;
        match directory.create_dir(name) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
        directory = open_child_directory(&directory, name, None, "lifecycle target directory")?;
    }
    Ok(directory)
}

fn canonical_path_for_directory(path: &Path, expected: &Dir) -> Result<PathBuf, LifecycleError> {
    let expected_identity = expected.dir_metadata()?;
    let before = open_absolute_directory_nofollow(path)?;
    if !same_object_cap(&expected_identity, &before.dir_metadata()?) {
        return invalid("lifecycle target changed while resolving");
    }
    let canonical = std::fs::canonicalize(path)?;
    let canonical_directory = open_absolute_directory_nofollow(&canonical)?;
    if !same_object_cap(&expected_identity, &canonical_directory.dir_metadata()?) {
        return invalid("lifecycle target changed while resolving");
    }
    let after = open_absolute_directory_nofollow(path)?;
    if !same_object_cap(&expected_identity, &after.dir_metadata()?) {
        return invalid("lifecycle target changed while resolving");
    }
    Ok(canonical)
}

#[cfg(not(windows))]
fn contract_path_for_directory(_requested: &Path, canonical: &Path, _expected: &Dir) -> PathBuf {
    canonical.to_path_buf()
}

#[cfg(windows)]
fn contract_path_for_directory(requested: &Path, canonical: &Path, expected: &Dir) -> PathBuf {
    if has_explicit_verbatim_prefix(requested) {
        return canonical.to_path_buf();
    }
    let candidate = strip_verbatim_prefix(canonical);
    if candidate == canonical {
        return candidate;
    }
    let Ok(candidate_directory) = open_absolute_directory_nofollow(&candidate) else {
        return canonical.to_path_buf();
    };
    let Ok(candidate_identity) = candidate_directory.dir_metadata() else {
        return canonical.to_path_buf();
    };
    let Ok(expected_identity) = expected.dir_metadata() else {
        return canonical.to_path_buf();
    };
    if same_object_cap(&expected_identity, &candidate_identity) {
        candidate
    } else {
        canonical.to_path_buf()
    }
}

#[cfg(windows)]
fn has_explicit_verbatim_prefix(path: &Path) -> bool {
    use std::path::{Component, Prefix};

    matches!(
        path.components().next(),
        Some(Component::Prefix(prefix))
            if matches!(
                prefix.kind(),
                Prefix::Verbatim(_)
                    | Prefix::VerbatimDisk(_)
                    | Prefix::VerbatimUNC(_, _)
            )
    )
}

#[cfg(windows)]
pub(super) fn strip_verbatim_prefix(path: &Path) -> PathBuf {
    use std::ffi::OsString;
    use std::os::windows::ffi::{OsStrExt as _, OsStringExt as _};
    use std::path::{Component, Prefix};

    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return path.to_path_buf();
    };
    let mut normalized = match prefix.kind() {
        Prefix::VerbatimDisk(drive) => PathBuf::from(format!("{}:", char::from(drive))),
        Prefix::VerbatimUNC(server, share) => {
            let mut value = vec![u16::from(b'\\'), u16::from(b'\\')];
            value.extend(server.encode_wide());
            value.push(u16::from(b'\\'));
            value.extend(share.encode_wide());
            PathBuf::from(OsString::from_wide(&value))
        }
        _ => return path.to_path_buf(),
    };
    for component in components {
        normalized.push(component.as_os_str());
    }
    normalized
}

fn reject_unexpanded_home(path: &Path) -> Result<(), LifecycleError> {
    if path
        .components()
        .next()
        .and_then(|component| match component {
            std::path::Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .is_some_and(|value| value.starts_with('~'))
    {
        return invalid("lifecycle target home shorthand must be expanded by the caller");
    }
    Ok(())
}

fn remove_owned_lock(target: &Dir, identity: &cap_std::fs::Metadata) {
    let owned = open_child_directory(
        target,
        LIFECYCLE_LOCK_DIRECTORY,
        None,
        "lifecycle lock cleanup",
    )
    .and_then(|lock| Ok(same_object_cap(identity, &lock.dir_metadata()?)))
    .unwrap_or(false);
    if owned {
        let _ = target.remove_dir(LIFECYCLE_LOCK_DIRECTORY);
    }
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inspect_doctor_baseline;
    use serde_json::Value;
    use std::process::Command;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn temporary_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agent-lifecycle-lock-{label}-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    fn check_status<'a>(value: &'a Value, check_id: &str) -> Option<&'a str> {
        value
            .get("checks")?
            .as_array()?
            .iter()
            .find(|check| check.get("id").and_then(Value::as_str) == Some(check_id))?
            .get("status")?
            .as_str()
    }

    #[cfg(windows)]
    #[test]
    fn contract_target_matches_python_for_normal_and_explicit_verbatim_inputs() {
        let root = temporary_path("windows-contract-target");
        std::fs::create_dir(&root).expect("create Windows contract target");
        let canonical = std::fs::canonicalize(&root).expect("canonical Windows target");
        assert!(!has_explicit_verbatim_prefix(&root));
        assert!(has_explicit_verbatim_prefix(&canonical));
        assert!(has_explicit_verbatim_prefix(&PathBuf::from(
            r"\\?\UNC\server\share\agent"
        )));

        let normal = LifecycleLock::acquire_existing(&root).expect("acquire normal target");
        assert_eq!(normal.target(), canonical);
        assert_eq!(normal.contract_target(), strip_verbatim_prefix(&canonical));
        drop(normal);

        let explicit =
            LifecycleLock::acquire_existing(&canonical).expect("acquire explicit verbatim target");
        assert_eq!(explicit.target(), canonical);
        assert_eq!(explicit.contract_target(), canonical);
        drop(explicit);

        std::fs::remove_dir_all(&root).expect("remove Windows contract target");
    }

    #[test]
    fn normalized_missing_target_uses_the_canonical_existing_parent_without_writes() {
        let root = temporary_path("normalized-target");
        std::fs::create_dir(&root).expect("create normalized target parent");
        let requested = root.join("missing/nested");
        let normalized =
            normalize_lifecycle_target(&requested).expect("normalize missing lifecycle target");
        let expected = std::fs::canonicalize(&root).expect("canonical target parent");
        #[cfg(windows)]
        let expected = strip_verbatim_prefix(&expected);
        assert_eq!(normalized, expected.join("missing/nested"));
        assert!(!requested.exists());
        std::fs::remove_dir_all(&root).expect("remove normalized target parent");
    }

    #[test]
    fn missing_target_snapshot_rejects_a_concurrent_path_appearance() {
        let root = temporary_path("missing-target-snapshot");
        std::fs::create_dir(&root).expect("create snapshot parent");
        let requested = root.join("missing/nested");
        let snapshot =
            LifecycleTargetSnapshot::capture(&requested).expect("capture missing target");
        std::fs::create_dir(root.join("missing")).expect("create concurrent target prefix");
        let error = snapshot
            .validate()
            .expect_err("appeared target must invalidate snapshot");
        assert!(
            error
                .to_string()
                .contains("appeared while previewing install")
        );
        drop(snapshot);
        std::fs::remove_dir_all(&root).expect("remove snapshot parent");
    }

    #[cfg(unix)]
    #[test]
    fn existing_target_snapshot_rejects_a_concurrent_path_replacement() {
        let root = temporary_path("existing-target-snapshot");
        let requested = root.join("target");
        std::fs::create_dir_all(&requested).expect("create snapshot target");
        let snapshot =
            LifecycleTargetSnapshot::capture(&requested).expect("capture existing target");
        std::fs::rename(&requested, root.join("replaced")).expect("rename captured target");
        std::fs::create_dir(&requested).expect("replace captured target");
        let error = snapshot
            .validate()
            .expect_err("replaced target must invalidate snapshot");
        assert!(error.to_string().contains("changed while resolving"));
        drop(snapshot);
        std::fs::remove_dir_all(&root).expect("remove snapshot root");
    }

    #[cfg(unix)]
    #[test]
    fn lock_directory_is_created_with_managed_mode_atomically() {
        use std::os::unix::fs::PermissionsExt as _;

        const CHILD_TARGET: &str = "AGENT_LIFECYCLE_LOCK_MODE_CHILD_TARGET";
        if let Some(target) = std::env::var_os(CHILD_TARGET) {
            let target_path = PathBuf::from(target);
            let target_directory =
                Dir::open_ambient_dir(&target_path, cap_std::ambient_authority())
                    .expect("open lock mode-test target");
            create_lock_directory(&target_directory).expect("create private lock directory");
            assert_eq!(
                std::fs::metadata(target_path.join(LIFECYCLE_LOCK_DIRECTORY))
                    .expect("inspect initial lock mode")
                    .permissions()
                    .mode()
                    & 0o777,
                MANAGED_DIRECTORY_MODE
            );
            return;
        }

        let root = temporary_path("initial-mode");
        std::fs::create_dir(&root).expect("create lock mode-test target");
        let test_name =
            "transaction_lock::tests::lock_directory_is_created_with_managed_mode_atomically";
        let output = Command::new("/bin/sh")
            .arg("-c")
            .arg("umask 000; exec \"$TEST_EXE\" --exact \"$TEST_NAME\" --nocapture")
            .env(
                "TEST_EXE",
                std::env::current_exe().expect("resolve test executable"),
            )
            .env("TEST_NAME", test_name)
            .env(CHILD_TARGET, &root)
            .output()
            .expect("run lock mode child");
        assert!(
            output.status.success(),
            "lock mode child failed:\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
        std::fs::remove_dir_all(&root).expect("remove lock mode-test target");
    }

    #[test]
    fn lock_is_exclusive_visible_to_doctor_and_released_by_identity() {
        let root = temporary_path("exclusive").join("nested/target");
        let lock = LifecycleLock::acquire(&root).expect("acquire lifecycle lock");
        assert_eq!(
            lock.target(),
            std::fs::canonicalize(&root).expect("canonical lifecycle target")
        );
        lock.validate().expect("validate lifecycle lock");
        let other = root.parent().unwrap_or(&root).join("other");
        assert!(lock.validate_for(&other).is_err());
        lock.validate_for(&root)
            .expect("validate lifecycle lock target binding");
        let error = LifecycleLock::acquire(&root)
            .err()
            .expect("second lifecycle lock must fail");
        assert!(
            error
                .to_string()
                .contains("already active or recovery is required")
        );

        let report = inspect_doctor_baseline(&root, root.join("schemas"))
            .expect("inspect active lifecycle lock");
        assert_eq!(check_status(&report, "recovery.residue"), Some("failed"));
        assert_eq!(
            report.pointer("/recovery/candidates"),
            Some(&serde_json::json!([{
                "kind": "lifecycle-lock",
                "path": LIFECYCLE_LOCK_DIRECTORY,
            }]))
        );

        lock.release().expect("release lifecycle lock");
        assert!(!root.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        let report =
            inspect_doctor_baseline(&root, root.join("schemas")).expect("inspect released lock");
        assert_eq!(check_status(&report, "recovery.residue"), Some("passed"));
        std::fs::remove_dir_all(root.parent().and_then(Path::parent).unwrap_or(&root))
            .expect("remove lifecycle lock root");
    }

    #[test]
    fn existing_only_lock_never_creates_a_missing_target() {
        let root = temporary_path("existing-only").join("missing/target");
        let error = LifecycleLock::acquire_existing(&root)
            .err()
            .expect("missing destructive target must fail");
        assert!(
            matches!(
                &error,
                LifecycleError::Io(source)
                    if source.kind() == std::io::ErrorKind::NotFound
            ),
            "{error}"
        );
        assert!(!root.exists());
        assert!(!root.parent().expect("target parent").exists());
    }

    #[test]
    fn lock_exclusion_and_crash_residue_cross_process_boundaries() {
        const CHILD_MODE: &str = "AGENT_LIFECYCLE_LOCK_CHILD_MODE";
        const CHILD_TARGET: &str = "AGENT_LIFECYCLE_LOCK_CHILD_TARGET";

        if let Some(mode) = std::env::var_os(CHILD_MODE) {
            let target = PathBuf::from(
                std::env::var_os(CHILD_TARGET).expect("child lifecycle target must be provided"),
            );
            match mode.to_str().expect("child mode must be UTF-8") {
                "contend" => {
                    assert!(LifecycleLock::acquire(target).is_err());
                }
                "leak" => {
                    let lock =
                        LifecycleLock::acquire(target).expect("child acquires lifecycle lock");
                    std::mem::forget(lock);
                }
                other => panic!("unexpected child mode: {other}"),
            }
            return;
        }

        let root = temporary_path("process-boundary");
        std::fs::create_dir(&root).expect("create lifecycle target");
        let lock = LifecycleLock::acquire(&root).expect("parent acquires lifecycle lock");
        run_lock_child("contend", &root);
        lock.release().expect("parent releases lifecycle lock");

        run_lock_child("leak", &root);
        let report =
            inspect_doctor_baseline(&root, root.join("schemas")).expect("inspect leaked lock");
        assert_eq!(check_status(&report, "recovery.residue"), Some("failed"));
        assert!(LifecycleLock::acquire(&root).is_err());

        std::fs::remove_dir(root.join(LIFECYCLE_LOCK_DIRECTORY))
            .expect("remove simulated crash residue");
        std::fs::remove_dir(&root).expect("remove lifecycle target");
    }

    fn run_lock_child(mode: &str, target: &Path) {
        let test_name =
            "transaction_lock::tests::lock_exclusion_and_crash_residue_cross_process_boundaries";
        let output = Command::new(std::env::current_exe().expect("resolve test executable"))
            .arg("--exact")
            .arg(test_name)
            .arg("--nocapture")
            .env("AGENT_LIFECYCLE_LOCK_CHILD_MODE", mode)
            .env("AGENT_LIFECYCLE_LOCK_CHILD_TARGET", target)
            .output()
            .expect("run lifecycle lock child");
        assert!(
            output.status.success(),
            "lifecycle child failed:\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
    }

    #[cfg(unix)]
    #[test]
    fn replaced_lock_is_not_accepted_or_removed_by_drop() {
        let root = temporary_path("replacement");
        std::fs::create_dir(&root).expect("create lifecycle target");
        let lock = LifecycleLock::acquire(&root).expect("acquire lifecycle lock");
        let path = root.join(LIFECYCLE_LOCK_DIRECTORY);
        std::fs::remove_dir(&path).expect("remove acquired lock directory");
        std::fs::create_dir(&path).expect("create replacement lock directory");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
                .expect("set replacement mode");
        }
        assert!(lock.validate().is_err());
        drop(lock);
        assert!(path.is_dir(), "drop must preserve a replacement lock");
        std::fs::remove_dir_all(&root).expect("remove replacement test root");
    }

    #[cfg(unix)]
    #[test]
    fn replaced_target_path_invalidates_old_lock_and_preserves_new_lock() {
        let root = temporary_path("target-replacement");
        let moved = root.with_extension("moved");
        std::fs::create_dir(&root).expect("create lifecycle target");
        let old_lock = LifecycleLock::acquire(&root).expect("acquire old lifecycle lock");
        std::fs::rename(&root, &moved).expect("move locked lifecycle target");
        std::fs::create_dir(&root).expect("replace lifecycle target path");
        let new_lock = LifecycleLock::acquire(&root).expect("acquire replacement lifecycle lock");

        assert!(old_lock.validate().is_err());
        assert!(old_lock.release().is_err());
        new_lock
            .validate()
            .expect("replacement lifecycle lock remains active");
        new_lock
            .release()
            .expect("release replacement lifecycle lock");

        assert!(
            moved.join(LIFECYCLE_LOCK_DIRECTORY).is_dir(),
            "invalidated old lock must remain visible as recovery residue"
        );
        std::fs::remove_dir(moved.join(LIFECYCLE_LOCK_DIRECTORY))
            .expect("remove old lifecycle lock residue");
        std::fs::remove_dir(&moved).expect("remove moved lifecycle target");
        std::fs::remove_dir(&root).expect("remove replacement lifecycle target");
    }

    #[test]
    fn unexpanded_home_shorthand_is_rejected_without_writes() {
        let shorthand = PathBuf::from(format!(
            "~agent-lifecycle-home-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let result = LifecycleLock::acquire(shorthand.join("target"));
        assert!(result.is_err());
        assert!(!shorthand.exists());
    }

    #[cfg(unix)]
    #[test]
    fn missing_target_creation_rejects_intermediate_symlink() {
        use std::os::unix::fs::symlink;

        let root = temporary_path("symlink");
        let outside = temporary_path("outside");
        std::fs::create_dir(&root).expect("create symlink test root");
        std::fs::create_dir(&outside).expect("create outside root");
        symlink(&outside, root.join("linked")).expect("create intermediate symlink");
        assert!(LifecycleLock::acquire(root.join("linked/target")).is_err());
        assert!(!outside.join("target").exists());
        std::fs::remove_dir_all(&root).expect("remove symlink test root");
        std::fs::remove_dir_all(&outside).expect("remove outside root");
    }
}
