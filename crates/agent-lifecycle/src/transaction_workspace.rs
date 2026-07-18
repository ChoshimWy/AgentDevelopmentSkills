use super::{
    INSTALL_BACKUP_PREFIX, INSTALL_STAGE_PREFIX, LifecycleError, LifecycleLock,
    open_child_directory, same_object_cap,
};
use cap_std::fs::Dir;
use std::collections::hash_map::RandomState;
use std::hash::BuildHasher as _;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

const WORKSPACE_ATTEMPTS: u64 = 128;
const WORKSPACE_DIRECTORY_MODE: u32 = 0o700;
static WORKSPACE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// Staging and backup directories held under one exclusive lifecycle lock.
///
/// This is a transaction foundation rather than an install implementation. It
/// creates only the two recovery-visible workspace directories and keeps their
/// identities bound to the locked target. Production install/rollback commands
/// are not yet routed through it.
#[must_use = "the lifecycle workspace must be held for the full transaction"]
pub struct LifecycleWorkspace {
    backup: WorkspaceEntry,
    lock: Option<LifecycleLock>,
    stage: WorkspaceEntry,
    target: PathBuf,
    target_directory: Dir,
}

impl LifecycleWorkspace {
    /// Acquire the target lock and create one stage/backup workspace pair.
    ///
    /// # Errors
    /// Fails when lock acquisition, atomic workspace creation, mode
    /// canonicalization, or identity revalidation fails.
    pub fn begin(target_root: impl AsRef<Path>) -> Result<Self, LifecycleError> {
        Self::from_lock(LifecycleLock::acquire(target_root)?)
    }

    /// Create a workspace under an already-held lifecycle lock.
    ///
    /// # Errors
    /// Fails when the lock is stale or a safe workspace pair cannot be created.
    pub fn from_lock(lock: LifecycleLock) -> Result<Self, LifecycleError> {
        lock.validate()?;
        let target = lock.target().to_path_buf();
        let target_directory = lock.target_directory()?;
        for attempt in 0..WORKSPACE_ATTEMPTS {
            lock.validate()?;
            let suffix = workspace_suffix(attempt);
            let stage_name = format!("{INSTALL_STAGE_PREFIX}{suffix}");
            let Some(mut stage) = WorkspaceEntry::create(&target_directory, stage_name)? else {
                continue;
            };
            let backup_name = format!("{INSTALL_BACKUP_PREFIX}{suffix}");
            let backup = match WorkspaceEntry::create(&target_directory, backup_name) {
                Ok(Some(backup)) => backup,
                Ok(None) => {
                    stage.cleanup(&target_directory)?;
                    continue;
                }
                Err(error) => {
                    if let Err(cleanup_error) = stage.cleanup(&target_directory) {
                        return invalid(format!(
                            "lifecycle workspace creation failed ({error}); stage cleanup is incomplete: {cleanup_error}"
                        ));
                    }
                    return Err(error);
                }
            };
            let workspace = Self {
                backup,
                lock: Some(lock),
                stage,
                target,
                target_directory,
            };
            workspace.validate()?;
            return Ok(workspace);
        }
        invalid("could not allocate a unique lifecycle workspace")
    }

    /// Return the canonical locked target.
    #[must_use]
    pub fn target(&self) -> &Path {
        &self.target
    }

    /// Return the stage directory path for diagnostics and recovery reporting.
    #[must_use]
    pub fn stage_path(&self) -> PathBuf {
        self.target.join(&self.stage.name)
    }

    /// Return the backup directory path for diagnostics and recovery reporting.
    #[must_use]
    pub fn backup_path(&self) -> PathBuf {
        self.target.join(&self.backup.name)
    }

    /// Borrow the held stage directory capability after identity validation.
    ///
    /// # Errors
    /// Fails if the workspace is no longer valid.
    pub fn stage_directory(&self) -> Result<&Dir, LifecycleError> {
        self.validate()?;
        self.stage.directory()
    }

    /// Borrow the held backup directory capability after identity validation.
    ///
    /// # Errors
    /// Fails if the workspace is no longer valid.
    pub fn backup_directory(&self) -> Result<&Dir, LifecycleError> {
        self.validate()?;
        self.backup.directory()
    }

    /// Prove the lock, target, stage, and backup identities are still bound.
    ///
    /// # Errors
    /// Fails if any namespace entry is missing, replaced, or has a noncanonical
    /// mode, or if the target lock is no longer valid.
    pub fn validate(&self) -> Result<(), LifecycleError> {
        self.lock()?.validate()?;
        self.stage.validate(&self.target_directory)?;
        self.backup.validate(&self.target_directory)?;
        Ok(())
    }

    /// Remove stage and backup recursively, then release the lifecycle lock.
    ///
    /// Removal is capability-relative and does not follow symlink entries.
    /// The trusted-target-parent boundary documented by [`LifecycleLock`]
    /// remains in force for portable name-based removal.
    ///
    /// # Errors
    /// Fails closed if an identity changed or cleanup/release is incomplete.
    pub fn cleanup(mut self) -> Result<(), LifecycleError> {
        self.lock()?.validate()?;
        let mut errors = Vec::new();
        if let Err(error) = self.stage.cleanup(&self.target_directory) {
            errors.push(("stage", error));
        }
        match self.lock()?.validate() {
            Ok(()) => {
                if let Err(error) = self.backup.cleanup(&self.target_directory) {
                    errors.push(("backup", error));
                }
            }
            Err(error) => errors.push(("lock revalidation", error)),
        }
        if let Err(error) = self.release_lock() {
            errors.push(("lock release", error));
        }
        finish_cleanup("lifecycle workspace cleanup", errors)
    }

    /// Remove only the stage, preserve the backup as recovery evidence, and
    /// release the lifecycle lock.
    ///
    /// # Errors
    /// Fails closed when the workspace changed or stage cleanup/lock release
    /// cannot complete.
    pub fn preserve_backup(mut self) -> Result<PathBuf, LifecycleError> {
        self.lock()?.validate()?;
        self.backup.validate(&self.target_directory)?;
        let path = self.backup_path();
        self.backup.preserve();
        let mut errors = Vec::new();
        if let Err(error) = self.stage.cleanup(&self.target_directory) {
            errors.push(("stage", error));
        }
        if let Err(error) = self.release_lock() {
            errors.push(("lock release", error));
        }
        finish_cleanup("lifecycle workspace backup preservation", errors)?;
        Ok(path)
    }

    fn lock(&self) -> Result<&LifecycleLock, LifecycleError> {
        self.lock
            .as_ref()
            .ok_or_else(|| LifecycleError::Invalid("lifecycle workspace lock is inactive".into()))
    }

    fn release_lock(&mut self) -> Result<(), LifecycleError> {
        self.lock
            .take()
            .ok_or_else(|| LifecycleError::Invalid("lifecycle workspace lock is inactive".into()))?
            .release()
    }
}

impl Drop for LifecycleWorkspace {
    fn drop(&mut self) {
        let Some(lock) = self.lock.as_ref() else {
            return;
        };
        if lock.validate().is_err() {
            return;
        }
        let _ = self.stage.cleanup(&self.target_directory);
        if lock.validate().is_err() {
            return;
        }
        let _ = self.backup.cleanup(&self.target_directory);
    }
}

struct WorkspaceEntry {
    active: bool,
    handle: Option<Dir>,
    identity: cap_std::fs::Metadata,
    name: String,
}

impl WorkspaceEntry {
    fn create(target: &Dir, name: String) -> Result<Option<Self>, LifecycleError> {
        match create_workspace_directory(target, &name) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return Ok(None),
            Err(error) => return Err(error.into()),
        }
        let handle = open_child_directory(target, &name, None, "lifecycle workspace directory")?;
        let identity = handle.dir_metadata()?;
        #[cfg(unix)]
        {
            use cap_std::fs::{Permissions, PermissionsExt as _};
            handle.set_permissions(".", Permissions::from_mode(WORKSPACE_DIRECTORY_MODE))?;
        }
        let current = open_child_directory(
            target,
            &name,
            Some(WORKSPACE_DIRECTORY_MODE),
            "lifecycle workspace directory",
        )?
        .dir_metadata()?;
        if !same_object_cap(&identity, &current) {
            return invalid("lifecycle workspace changed while creating");
        }
        Ok(Some(Self {
            active: true,
            handle: Some(handle),
            identity: current,
            name,
        }))
    }

    fn validate(&self, target: &Dir) -> Result<(), LifecycleError> {
        if !self.active {
            return invalid("lifecycle workspace directory is inactive");
        }
        let handle = self.handle.as_ref().ok_or_else(|| {
            LifecycleError::Invalid("lifecycle workspace handle is inactive".into())
        })?;
        if !same_object_cap(&self.identity, &handle.dir_metadata()?) {
            return invalid("lifecycle workspace directory is inactive");
        }
        let current = open_child_directory(
            target,
            &self.name,
            Some(WORKSPACE_DIRECTORY_MODE),
            "lifecycle workspace directory",
        )?
        .dir_metadata()?;
        if !same_object_cap(&self.identity, &current) {
            return invalid("lifecycle workspace directory is inactive");
        }
        Ok(())
    }

    fn directory(&self) -> Result<&Dir, LifecycleError> {
        self.handle
            .as_ref()
            .filter(|_| self.active)
            .ok_or_else(|| LifecycleError::Invalid("lifecycle workspace handle is inactive".into()))
    }

    fn cleanup(&mut self, target: &Dir) -> Result<(), LifecycleError> {
        if !self.active {
            return Ok(());
        }
        self.validate(target)?;
        #[cfg(windows)]
        drop(self.handle.take());
        target.remove_dir_all(&self.name)?;
        self.active = false;
        drop(self.handle.take());
        Ok(())
    }

    fn preserve(&mut self) {
        self.active = false;
        drop(self.handle.take());
    }
}

fn create_workspace_directory(target: &Dir, name: &str) -> std::io::Result<()> {
    #[cfg(all(unix, not(target_os = "wasi")))]
    {
        use cap_std::fs::{DirBuilder, DirBuilderExt as _};

        let mut builder = DirBuilder::new();
        builder.mode(WORKSPACE_DIRECTORY_MODE);
        target.create_dir_with(name, &builder)
    }
    #[cfg(any(not(unix), target_os = "wasi"))]
    {
        target.create_dir(name)
    }
}

fn workspace_suffix(attempt: u64) -> String {
    let sequence = WORKSPACE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let entropy =
        RandomState::new().hash_one((u64::from(std::process::id()), sequence, timestamp, attempt));
    format!("{entropy:016x}")
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

fn finish_cleanup(
    operation: &str,
    mut errors: Vec<(&'static str, LifecycleError)>,
) -> Result<(), LifecycleError> {
    match errors.len() {
        0 => Ok(()),
        1 => Err(errors.pop().expect("one cleanup error").1),
        _ => invalid(format!(
            "{operation} is incomplete: {}",
            errors
                .into_iter()
                .map(|(step, error)| format!("{step}: {error}"))
                .collect::<Vec<_>>()
                .join("; ")
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{LIFECYCLE_LOCK_DIRECTORY, inspect_doctor_baseline};
    use serde_json::Value;
    use std::process::Command;

    fn temporary_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agent-lifecycle-workspace-{label}-{}-{}",
            std::process::id(),
            WORKSPACE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    fn recovery_candidates(value: &Value) -> Vec<(String, String)> {
        value
            .pointer("/recovery/candidates")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|candidate| {
                (
                    candidate
                        .get("kind")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                    candidate
                        .get("path")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                )
            })
            .collect()
    }

    #[cfg(unix)]
    #[test]
    fn workspace_directory_is_created_private_before_normalization() {
        use std::os::unix::fs::PermissionsExt as _;

        const CHILD_TARGET: &str = "AGENT_LIFECYCLE_WORKSPACE_MODE_CHILD_TARGET";
        if let Some(target) = std::env::var_os(CHILD_TARGET) {
            let target_path = PathBuf::from(target);
            let target_directory =
                Dir::open_ambient_dir(&target_path, cap_std::ambient_authority())
                    .expect("open mode-test target");
            create_workspace_directory(&target_directory, "probe")
                .expect("create private workspace directory");
            assert_eq!(
                std::fs::metadata(target_path.join("probe"))
                    .expect("inspect initial workspace mode")
                    .permissions()
                    .mode()
                    & 0o777,
                WORKSPACE_DIRECTORY_MODE
            );
            return;
        }

        let root = temporary_path("initial-mode");
        std::fs::create_dir(&root).expect("create mode-test target");
        let test_name = "transaction_workspace::tests::workspace_directory_is_created_private_before_normalization";
        let output = Command::new("/bin/sh")
            .arg("-c")
            .arg("umask 022; exec \"$TEST_EXE\" --exact \"$TEST_NAME\" --nocapture")
            .env(
                "TEST_EXE",
                std::env::current_exe().expect("resolve test executable"),
            )
            .env("TEST_NAME", test_name)
            .env(CHILD_TARGET, &root)
            .output()
            .expect("run workspace mode child");
        assert!(
            output.status.success(),
            "workspace mode child failed:\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
        std::fs::remove_dir_all(&root).expect("remove mode-test target");
    }

    #[test]
    fn workspace_is_recovery_visible_and_cleanup_releases_everything() {
        let root = temporary_path("cleanup");
        std::fs::create_dir(&root).expect("create workspace target");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        assert_eq!(workspace.target(), std::fs::canonicalize(&root).unwrap());
        assert!(workspace.stage_path().is_dir());
        assert!(workspace.backup_path().is_dir());
        assert!(workspace.stage_directory().unwrap().dir_metadata().is_ok());
        assert!(workspace.backup_directory().unwrap().dir_metadata().is_ok());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            assert_eq!(
                std::fs::metadata(workspace.stage_path())
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                WORKSPACE_DIRECTORY_MODE
            );
            assert_eq!(
                std::fs::metadata(workspace.backup_path())
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                WORKSPACE_DIRECTORY_MODE
            );
        }
        workspace.validate().expect("validate lifecycle workspace");
        let report = inspect_doctor_baseline(&root, root.join("schemas"))
            .expect("inspect workspace residue");
        let candidates = recovery_candidates(&report);
        assert!(candidates.iter().any(|(kind, _)| kind == "install-stage"));
        assert!(candidates.iter().any(|(kind, _)| kind == "install-backup"));
        assert!(candidates.iter().any(|(kind, _)| kind == "lifecycle-lock"));

        workspace.cleanup().expect("cleanup lifecycle workspace");
        let report =
            inspect_doctor_baseline(&root, root.join("schemas")).expect("inspect clean target");
        assert!(recovery_candidates(&report).is_empty());
        std::fs::remove_dir(&root).expect("remove workspace target");
    }

    #[test]
    fn preserve_backup_removes_stage_and_releases_lock() {
        let root = temporary_path("preserve");
        std::fs::create_dir(&root).expect("create workspace target");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        let stage = workspace.stage_path();
        let backup = workspace.backup_path();
        workspace
            .backup_directory()
            .expect("borrow backup capability")
            .write("evidence", b"recovery")
            .expect("write backup evidence");
        let preserved = workspace
            .preserve_backup()
            .expect("preserve lifecycle backup");
        assert_eq!(preserved, backup);
        assert!(!stage.exists());
        assert_eq!(
            std::fs::read(backup.join("evidence")).expect("read backup evidence"),
            b"recovery"
        );
        assert!(!root.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        std::fs::remove_dir_all(&backup).expect("remove preserved backup");
        std::fs::remove_dir(&root).expect("remove workspace target");
    }

    #[test]
    fn crash_residue_survives_a_child_process() {
        const CHILD_TARGET: &str = "AGENT_LIFECYCLE_WORKSPACE_CHILD_TARGET";
        if let Some(target) = std::env::var_os(CHILD_TARGET) {
            let workspace =
                LifecycleWorkspace::begin(PathBuf::from(target)).expect("begin child workspace");
            std::mem::forget(workspace);
            return;
        }

        let root = temporary_path("crash");
        std::fs::create_dir(&root).expect("create workspace target");
        let test_name = "transaction_workspace::tests::crash_residue_survives_a_child_process";
        let output = Command::new(std::env::current_exe().expect("resolve test executable"))
            .arg("--exact")
            .arg(test_name)
            .arg("--nocapture")
            .env(CHILD_TARGET, &root)
            .output()
            .expect("run workspace child");
        assert!(
            output.status.success(),
            "workspace child failed:\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );

        let report =
            inspect_doctor_baseline(&root, root.join("schemas")).expect("inspect crash residue");
        let candidates = recovery_candidates(&report);
        assert_eq!(candidates.len(), 3);
        for (_, path) in candidates {
            std::fs::remove_dir_all(root.join(path)).expect("remove crash residue");
        }
        std::fs::remove_dir(&root).expect("remove workspace target");
    }

    #[cfg(unix)]
    #[test]
    fn recursive_cleanup_does_not_follow_symlink_entries() {
        use std::os::unix::fs::symlink;

        let root = temporary_path("symlink");
        let outside = temporary_path("outside");
        std::fs::create_dir(&root).expect("create workspace target");
        std::fs::create_dir(&outside).expect("create outside target");
        std::fs::write(outside.join("evidence"), b"safe").expect("write outside evidence");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        symlink(&outside, workspace.stage_path().join("escape")).expect("create stage symlink");
        workspace.cleanup().expect("cleanup lifecycle workspace");
        assert_eq!(
            std::fs::read(outside.join("evidence")).expect("read outside evidence"),
            b"safe"
        );
        std::fs::remove_dir_all(&outside).expect("remove outside target");
        std::fs::remove_dir(&root).expect("remove workspace target");
    }

    #[cfg(unix)]
    #[test]
    fn replaced_stage_is_rejected_while_owned_backup_is_cleaned() {
        let root = temporary_path("replacement");
        std::fs::create_dir(&root).expect("create workspace target");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        let stage = workspace.stage_path();
        let backup = workspace.backup_path();
        std::fs::remove_dir(&stage).expect("remove original stage");
        std::fs::create_dir(&stage).expect("create replacement stage");
        std::fs::set_permissions(&stage, {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::Permissions::from_mode(WORKSPACE_DIRECTORY_MODE)
        })
        .expect("set replacement mode");
        assert!(workspace.validate().is_err());
        assert!(workspace.cleanup().is_err());
        assert!(stage.is_dir());
        assert!(!backup.exists());
        assert!(!root.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        std::fs::remove_dir_all(stage).expect("remove replacement stage");
        std::fs::remove_dir(&root).expect("remove workspace target");
    }

    #[cfg(unix)]
    #[test]
    fn preserve_backup_keeps_backup_when_stage_was_replaced() {
        let root = temporary_path("preserve-replacement");
        std::fs::create_dir(&root).expect("create workspace target");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        let stage = workspace.stage_path();
        let backup = workspace.backup_path();
        std::fs::remove_dir(&stage).expect("remove original stage");
        std::fs::create_dir(&stage).expect("create replacement stage");
        std::fs::set_permissions(&stage, {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::Permissions::from_mode(WORKSPACE_DIRECTORY_MODE)
        })
        .expect("set replacement mode");

        assert!(workspace.preserve_backup().is_err());
        assert!(stage.is_dir());
        assert!(backup.is_dir());
        assert!(!root.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        std::fs::remove_dir_all(stage).expect("remove replacement stage");
        std::fs::remove_dir_all(backup).expect("remove preserved backup");
        std::fs::remove_dir(&root).expect("remove workspace target");
    }
}
