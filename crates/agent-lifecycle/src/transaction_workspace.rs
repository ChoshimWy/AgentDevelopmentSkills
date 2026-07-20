use super::{
    INSTALL_BACKUP_PREFIX, INSTALL_STAGE_PREFIX, LifecycleError, LifecycleLock,
    ValidatedInstallPlan, external_stage, load_json_file, open_child_directory, open_child_file,
    rollback, rollback_stage, same_content_state_cap, same_object_cap, source_activation,
    staged_install, staged_tree,
};
use agent_contracts::MAX_CONTRACT_JSON_BYTES;
use cap_std::fs::Dir;
use serde_json::Value;
use std::collections::hash_map::RandomState;
use std::ffi::OsStr;
use std::hash::BuildHasher as _;
use std::io::Read as _;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

const WORKSPACE_ATTEMPTS: u64 = 128;
const WORKSPACE_DIRECTORY_MODE: u32 = 0o700;
static WORKSPACE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// Staging and backup directories held under one exclusive lifecycle lock.
///
/// This is a transaction foundation rather than an install implementation. It
/// creates two recovery-visible workspace directories, keeps their identities
/// bound to the locked target, and can assemble a complete managed stage from
/// one [`ValidatedInstallPlan`], including a frozen external `.system` and
/// Activation snapshot plus a persistent rollback point for an intact current
/// installation or a split-source rollback point for fresh activation.
/// Managed roots can then be published through an identity-bound
/// [`super::PublishedInstall`] guard. That guard owns external rollback
/// restoration once its internal mutation boundary starts; trusted handlers
/// now cover source deactivation, rollback-backed replacement/fresh
/// activation, rollback-backed legacy adoption, and rollback-backed full
/// uninstall for the production lifecycle routes that have passed their gates.
#[must_use = "the lifecycle workspace must be held for the full transaction"]
pub struct LifecycleWorkspace {
    backup: WorkspaceEntry,
    contract_target: PathBuf,
    lock: Option<LifecycleLock>,
    requires_persistent_rollback_restore: bool,
    stage: WorkspaceEntry,
    staged_external_state: Option<external_stage::ExternalStageSnapshot>,
    staged_legacy_skills: Option<Dir>,
    staged_install_identity: Option<StagedInstallIdentity>,
    staged_rollback_point: Option<rollback_stage::RollbackStageSnapshot>,
    staged_rollback_point_is_fresh: bool,
    target: PathBuf,
    target_directory: Dir,
    rollback_external_paths: Vec<String>,
}

struct StagedInstallIdentity {
    install_plan_fingerprint: String,
    package_lock_fingerprint: String,
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

    /// Acquire an existing target and create one stage/backup workspace pair.
    ///
    /// Unlike [`Self::begin`], this never creates the target root. Destructive
    /// lifecycle commands should use this entry point.
    ///
    /// # Errors
    /// Fails when the target is missing or unsafe, or when normal workspace
    /// acquisition fails.
    pub fn begin_existing(target_root: impl AsRef<Path>) -> Result<Self, LifecycleError> {
        Self::from_lock(LifecycleLock::acquire_existing(target_root)?)
    }

    /// Create a workspace under an already-held lifecycle lock.
    ///
    /// # Errors
    /// Fails when the lock is stale or a safe workspace pair cannot be created.
    pub fn from_lock(lock: LifecycleLock) -> Result<Self, LifecycleError> {
        lock.validate()?;
        let target = lock.target().to_path_buf();
        let contract_target = lock.contract_target().to_path_buf();
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
                contract_target,
                lock: Some(lock),
                requires_persistent_rollback_restore: false,
                stage,
                staged_external_state: None,
                staged_legacy_skills: None,
                staged_install_identity: None,
                staged_rollback_point: None,
                staged_rollback_point_is_fresh: false,
                target,
                target_directory,
                rollback_external_paths: Vec::new(),
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

    /// Return the target spelling used by compatibility reports and config.
    #[must_use]
    pub fn contract_target(&self) -> &Path {
        &self.contract_target
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

    /// Create the complete managed staging layout and freeze its metadata.
    ///
    /// The supplied token owns an already validated Install Plan and persistent
    /// package Lockfile. `instructions` must be the exact global `AGENTS.md`
    /// bytes frozen by that plan. This operation creates all three managed roots
    /// and writes canonical Lockfiles before any plan-bound package or Skill is
    /// accepted.
    ///
    /// External `.system` Skills, Activation state, rollback points, and root
    /// swaps are staged by later transaction steps.
    ///
    /// # Errors
    /// Fails closed if the workspace is not empty, the instructions differ from
    /// the plan, or any managed directory/file cannot be created atomically.
    pub fn stage_install_layout(
        &mut self,
        plan: &ValidatedInstallPlan,
        instructions: &[u8],
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        if self.staged_install_identity.is_some() {
            return invalid("lifecycle workspace already has a staged Install Plan");
        }
        staged_install::stage_layout(self.stage.directory()?, plan, instructions)?;
        staged_install::validate_layout(self.stage.directory()?, plan)?;
        self.staged_install_identity = Some(StagedInstallIdentity {
            install_plan_fingerprint: plan.fingerprint().to_owned(),
            package_lock_fingerprint: plan.package_lock_fingerprint().to_owned(),
        });
        self.validate()
    }

    /// Stage one package selected by the validated Install Plan.
    ///
    /// # Errors
    /// Fails closed for an unknown package ID, unsafe source, duplicate
    /// destination, or content that differs from the plan-owned record.
    pub fn stage_plan_package(
        &mut self,
        plan: &ValidatedInstallPlan,
        package_id: &str,
        source: &Dir,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        self.require_external_state_not_staged()?;
        staged_install::validate_layout(self.stage.directory()?, plan)?;
        staged_install::stage_package(self.stage.directory()?, plan, package_id, source)?;
        staged_install::validate_layout(self.stage.directory()?, plan)?;
        self.validate()
    }

    /// Stage one Skill selected by the validated Install Plan.
    ///
    /// # Errors
    /// Fails closed for an unknown Skill name, unsafe source, duplicate
    /// destination, or content that differs from the plan-owned record.
    pub fn stage_plan_skill(
        &mut self,
        plan: &ValidatedInstallPlan,
        skill_name: &str,
        source: &Dir,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        self.require_external_state_not_staged()?;
        staged_install::validate_layout(self.stage.directory()?, plan)?;
        staged_install::stage_skill(self.stage.directory()?, plan, skill_name, source)?;
        staged_install::validate_layout(self.stage.directory()?, plan)?;
        self.validate()
    }

    /// Preserve the target's external `.system` Skills and Activation Lock.
    ///
    /// All plan-owned package and Skill trees must already form a complete,
    /// verified managed stage. The external snapshot is copied without
    /// following symlinks, frozen in memory, and revalidated against both the
    /// locked target and stage before a later swap.
    ///
    /// # Errors
    /// Fails closed for incomplete plan staging, unsafe external roots,
    /// Activation drift, excessive trees, unsupported filesystem objects, or a
    /// second preservation attempt.
    pub fn stage_external_state(
        &mut self,
        plan: &ValidatedInstallPlan,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        self.require_external_state_not_staged()?;
        staged_install::verify(
            self.stage.directory()?,
            plan,
            staged_install::ExternalLayout::default(),
        )?;
        let snapshot = external_stage::stage(&self.target_directory, self.stage.directory()?)?;
        external_stage::verify(&self.target_directory, self.stage.directory()?, &snapshot)?;
        self.staged_external_state = Some(snapshot);
        self.validate()
    }

    /// Preserve a legacy iOSAgentSkills `.system` tree in the managed stage.
    ///
    /// The supplied directory is the already capability-opened legacy `skills`
    /// root. Its identity stays held through publication so moving the target
    /// symlink cannot redirect or invalidate the frozen source.
    #[cfg(not(windows))]
    pub(super) fn stage_legacy_external_state(
        &mut self,
        plan: &ValidatedInstallPlan,
        legacy_skills: &Dir,
    ) -> Result<bool, LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        self.require_external_state_not_staged()?;
        staged_install::verify(
            self.stage.directory()?,
            plan,
            staged_install::ExternalLayout::default(),
        )?;
        let snapshot =
            external_stage::stage_from_legacy_skills(legacy_skills, self.stage.directory()?)?;
        let preserved = snapshot.layout().system_skills;
        external_stage::verify_from_legacy_skills(
            legacy_skills,
            self.stage.directory()?,
            &snapshot,
        )?;
        self.staged_legacy_skills = Some(legacy_skills.try_clone()?);
        self.staged_external_state = Some(snapshot);
        self.validate()?;
        Ok(preserved)
    }

    pub(super) fn stage_persistent_rollback_install(
        &mut self,
        persistent: &rollback::PersistentRollbackPoint,
    ) -> Result<(ValidatedInstallPlan, Value), LifecycleError> {
        self.validate()?;
        if self.staged_install_identity.is_some()
            || self.staged_external_state.is_some()
            || self.staged_rollback_point.is_some()
        {
            return invalid("persistent rollback requires an empty lifecycle workspace");
        }
        rollback::validate_rollback_point_root(persistent.root())?;
        let install_lock = load_json_file(
            persistent.root(),
            "install-lock.json",
            super::MANAGED_FILE_MODE,
            "rollback point Install Lock",
        )?;
        let package_lock = load_json_file(
            persistent.root(),
            super::PERSISTENT_PACKAGE_LOCK,
            super::MANAGED_FILE_MODE,
            "rollback point package Lockfile",
        )?;
        let plan = ValidatedInstallPlan::new(install_lock.clone(), package_lock)?;
        let instructions = read_persistent_agents(persistent.root())?;
        self.stage_install_layout(&plan, &instructions)?;

        self.stage_persistent_packages(&plan, persistent.root(), &install_lock)?;
        self.stage_persistent_skills(&plan, persistent.root(), &install_lock)?;
        let external = external_stage::stage_for_rollback(
            &self.target_directory,
            persistent.root(),
            self.stage.directory()?,
        )?;
        self.verify_external_stage(&external)?;
        self.staged_external_state = Some(external);
        self.requires_persistent_rollback_restore = true;
        self.stage_rollback_point(&plan, persistent.external_paths())?;
        rollback::validate_rollback_point_root(persistent.root())?;
        external_stage::verify_rollback_source(
            persistent.root(),
            self.staged_external_state.as_ref().ok_or_else(|| {
                LifecycleError::Invalid("rollback external snapshot is missing".to_owned())
            })?,
        )?;
        if load_json_file(
            persistent.root(),
            "rollback-point.json",
            super::MANAGED_FILE_MODE,
            "rollback point contract",
        )? != *persistent.point()
        {
            return invalid("persistent rollback point changed while staging");
        }
        let reverse = self
            .staged_rollback_point
            .as_ref()
            .ok_or_else(|| LifecycleError::Invalid("reverse rollback point is missing".to_owned()))?
            .point()
            .clone();
        self.verify_staged_install(&plan)?;
        Ok((plan, reverse))
    }

    fn stage_persistent_packages(
        &mut self,
        plan: &ValidatedInstallPlan,
        root: &Dir,
        install_lock: &Value,
    ) -> Result<(), LifecycleError> {
        let packages = open_child_directory(
            root,
            "packages",
            Some(super::MANAGED_DIRECTORY_MODE),
            "rollback point packages",
        )?;
        for record in install_lock
            .get("packages")
            .and_then(Value::as_array)
            .ok_or_else(|| LifecycleError::Invalid("rollback packages are invalid".to_owned()))?
        {
            let id = record.get("id").and_then(Value::as_str).ok_or_else(|| {
                LifecycleError::Invalid("rollback package id is invalid".to_owned())
            })?;
            let package =
                open_child_directory(&packages, id, None, "rollback point package source")?;
            self.stage_plan_package(plan, id, &package)?;
        }
        Ok(())
    }

    fn stage_persistent_skills(
        &mut self,
        plan: &ValidatedInstallPlan,
        root: &Dir,
        install_lock: &Value,
    ) -> Result<(), LifecycleError> {
        let skills = open_child_directory(
            root,
            "skills",
            Some(super::MANAGED_DIRECTORY_MODE),
            "rollback point Skills",
        )?;
        for record in install_lock
            .get("skills")
            .and_then(Value::as_array)
            .ok_or_else(|| LifecycleError::Invalid("rollback Skills are invalid".to_owned()))?
        {
            let name = record.get("name").and_then(Value::as_str).ok_or_else(|| {
                LifecycleError::Invalid("rollback Skill name is invalid".to_owned())
            })?;
            let skill = open_child_directory(&skills, name, None, "rollback point Skill source")?;
            self.stage_plan_skill(plan, name, &skill)?;
        }
        Ok(())
    }

    /// Snapshot the currently installed managed state as a staged rollback point.
    ///
    /// The new managed stage and external `.system`/Activation state must
    /// already be complete. `external_paths` is the sorted, unique set of
    /// package-owned lifecycle files outside the managed roots. Existing files,
    /// absent files, and their parent-directory states are frozen into the
    /// rollback contract without following symlinks.
    ///
    /// # Errors
    /// Fails closed when the current target is not an intact managed install,
    /// the staged replacement is incomplete, external paths are unsafe or
    /// unsorted, source state changes while copying, or rollback staging was
    /// already attempted successfully.
    pub fn stage_rollback_point(
        &mut self,
        plan: &ValidatedInstallPlan,
        external_paths: &[String],
    ) -> Result<String, LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        if self.staged_rollback_point.is_some() {
            return invalid("lifecycle workspace rollback point is already staged");
        }
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        self.verify_external_stage(external)?;
        staged_install::verify(self.stage.directory()?, plan, external.layout())?;
        let snapshot = rollback_stage::stage(
            &self.target_directory,
            self.stage.directory()?,
            external_paths,
        )?;
        let fingerprint = snapshot.fingerprint()?.to_owned();
        rollback_stage::verify(
            &self.target_directory,
            self.stage.directory()?,
            &snapshot,
            external_paths,
        )?;
        self.rollback_external_paths = external_paths.to_vec();
        self.staged_rollback_point = Some(snapshot);
        self.staged_rollback_point_is_fresh = false;
        self.verify_staged_install(plan)?;
        Ok(fingerprint)
    }

    /// Freeze fresh-install source activation preimages before publication.
    ///
    /// Installed activation assets are read from the complete managed stage,
    /// while unmanaged destinations, profiles, and `config.toml` are read from
    /// the still-unmodified target. The resulting exact external scope is
    /// persisted inside the staged rollback point so a failed post-publication
    /// activation can remove the new managed roots and restore every external
    /// preimage.
    ///
    /// # Errors
    /// Fails closed unless the staged installation and preserved external
    /// state are complete, all target managed roots are absent, activation
    /// assets are valid, unmanaged destinations are compatible, and the fresh
    /// rollback point can be copied and revalidated without drift.
    pub fn stage_fresh_source_activation(
        &mut self,
        plan: &ValidatedInstallPlan,
        session_launcher: &[u8],
    ) -> Result<String, LifecycleError> {
        self.stage_source_activation(plan, session_launcher, false)
    }

    #[cfg(not(windows))]
    pub(super) fn stage_legacy_source_activation(
        &mut self,
        plan: &ValidatedInstallPlan,
        session_launcher: &[u8],
    ) -> Result<String, LifecycleError> {
        self.stage_source_activation(plan, session_launcher, true)
    }

    fn stage_source_activation(
        &mut self,
        plan: &ValidatedInstallPlan,
        session_launcher: &[u8],
        legacy_adoption: bool,
    ) -> Result<String, LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        if self.staged_rollback_point.is_some() {
            return invalid("lifecycle workspace rollback point is already staged");
        }
        require_fresh_managed_target(&self.target_directory)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        self.verify_external_stage(external)?;
        staged_install::verify(self.stage.directory()?, plan, external.layout())?;
        let prepared = if legacy_adoption {
            #[cfg(not(windows))]
            {
                source_activation::SourceActivation::prepare_legacy_adoption(
                    self.stage.directory()?,
                    &self.target_directory,
                    &self.contract_target,
                    session_launcher,
                )?
            }
            #[cfg(windows)]
            {
                return invalid("legacy adoption is unavailable on Windows");
            }
        } else {
            source_activation::SourceActivation::prepare_fresh(
                self.stage.directory()?,
                &self.target_directory,
                &self.contract_target,
                session_launcher,
            )?
        };
        let external_paths = prepared.scope().to_vec();
        let snapshot = rollback_stage::stage_fresh(
            self.stage.directory()?,
            &self.target_directory,
            self.stage.directory()?,
            &external_paths,
        )?;
        let fingerprint = snapshot.fingerprint()?.to_owned();
        rollback_stage::verify_fresh(
            self.stage.directory()?,
            &self.target_directory,
            self.stage.directory()?,
            &snapshot,
            &external_paths,
        )?;
        self.rollback_external_paths = external_paths;
        self.staged_rollback_point = Some(snapshot);
        self.staged_rollback_point_is_fresh = true;
        self.verify_staged_install(plan)?;
        Ok(fingerprint)
    }

    /// Freeze a complete rollback point for a full managed uninstall.
    ///
    /// Unlike replacement staging, uninstall has no candidate managed roots.
    /// The private stage therefore contains only the rollback snapshot used to
    /// recover the current managed installation and its exact external scope.
    pub(super) fn stage_uninstall_rollback(
        &mut self,
        external_paths: &[String],
    ) -> Result<String, LifecycleError> {
        self.validate()?;
        if self.staged_install_identity.is_some()
            || self.staged_external_state.is_some()
            || self.staged_rollback_point.is_some()
        {
            return invalid("uninstall rollback staging requires an empty lifecycle workspace");
        }
        external_stage::create_directory(
            self.stage.directory()?,
            OsStr::new(".agent-skills"),
            Some(super::MANAGED_DIRECTORY_MODE),
            "uninstall rollback metadata",
        )?;
        let snapshot = rollback_stage::stage(
            &self.target_directory,
            self.stage.directory()?,
            external_paths,
        )?;
        let fingerprint = snapshot.fingerprint()?.to_owned();
        rollback_stage::verify(
            &self.target_directory,
            self.stage.directory()?,
            &snapshot,
            external_paths,
        )?;
        self.rollback_external_paths = external_paths.to_vec();
        self.staged_rollback_point = Some(snapshot);
        self.staged_rollback_point_is_fresh = false;
        self.validate()?;
        Ok(fingerprint)
    }

    /// Verify the complete staged install against one validated plan token.
    ///
    /// This native pre-swap gate checks exact root topology, canonical Lockfile
    /// bytes, all package/Skill trees, preserved `.system` and Activation
    /// state, Manifest-derived semantics, AGENTS composition, Bindings,
    /// permissions, and plan/Lockfile identity.
    ///
    /// # Errors
    /// Fails closed for missing, extra, replaced, or semantically inconsistent
    /// staged content.
    pub fn verify_staged_install(&self, plan: &ValidatedInstallPlan) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        self.verify_external_stage(external)?;
        let mut layout = external.layout();
        if let Some(rollback) = self.staged_rollback_point.as_ref() {
            if self.staged_rollback_point_is_fresh {
                require_fresh_managed_target(&self.target_directory)?;
                rollback_stage::verify_fresh(
                    self.stage.directory()?,
                    &self.target_directory,
                    self.stage.directory()?,
                    rollback,
                    &self.rollback_external_paths,
                )?;
            } else {
                rollback_stage::verify(
                    &self.target_directory,
                    self.stage.directory()?,
                    rollback,
                    &self.rollback_external_paths,
                )?;
            }
            layout.rollback_point = true;
        }
        staged_install::verify(self.stage.directory()?, plan, layout)?;
        if let Some(rollback) = self.staged_rollback_point.as_ref() {
            if self.staged_rollback_point_is_fresh {
                rollback_stage::verify_fresh(
                    self.stage.directory()?,
                    &self.target_directory,
                    self.stage.directory()?,
                    rollback,
                    &self.rollback_external_paths,
                )?;
            } else {
                rollback_stage::verify(
                    &self.target_directory,
                    self.stage.directory()?,
                    rollback,
                    &self.rollback_external_paths,
                )?;
            }
        }
        self.verify_external_stage(external)?;
        self.validate()
    }

    /// Revalidate a managed installation after its staged roots were moved
    /// into the target while the transaction backup remains held.
    pub(super) fn verify_published_install(
        &self,
        plan: &ValidatedInstallPlan,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        let mut layout = external.layout();
        if self.staged_rollback_point.is_some() {
            layout.rollback_point = true;
        }
        staged_install::verify_published(&self.target_directory, plan, layout)?;
        external_stage::verify_published(&self.target_directory, external)?;
        if let Some(rollback) = self.staged_rollback_point.as_ref() {
            rollback_stage::verify_published(&self.target_directory, rollback)?;
        }
        self.validate()
    }

    pub(super) fn verify_published_after_handler(
        &self,
        plan: &ValidatedInstallPlan,
        activation: external_stage::PublishedActivation,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        let mut layout = external.layout();
        layout.activation = activation != external_stage::PublishedActivation::Absent;
        if self.staged_rollback_point.is_some() {
            layout.rollback_point = true;
        }
        staged_install::verify_published(&self.target_directory, plan, layout)?;
        external_stage::verify_published_after_handler(
            &self.target_directory,
            external,
            activation,
        )?;
        if let Some(rollback) = self.staged_rollback_point.as_ref() {
            rollback_stage::verify_published(&self.target_directory, rollback)?;
        }
        self.validate()
    }

    pub(super) fn verify_published_during_handler(
        &self,
        plan: &ValidatedInstallPlan,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        let mut layout = external.layout();
        layout.activation =
            external_stage::verify_published_during_handler(&self.target_directory, external)?;
        if self.staged_rollback_point.is_some() {
            layout.rollback_point = true;
        }
        staged_install::verify_published(&self.target_directory, plan, layout)?;
        if let Some(rollback) = self.staged_rollback_point.as_ref() {
            rollback_stage::verify_published(&self.target_directory, rollback)?;
        }
        self.validate()
    }

    fn verify_external_stage(
        &self,
        expected: &external_stage::ExternalStageSnapshot,
    ) -> Result<(), LifecycleError> {
        match self.staged_legacy_skills.as_ref() {
            Some(legacy_skills) => external_stage::verify_from_legacy_skills(
                legacy_skills,
                self.stage.directory()?,
                expected,
            ),
            None => {
                external_stage::verify(&self.target_directory, self.stage.directory()?, expected)
            }
        }
    }

    pub(super) fn target_directory(&self) -> Result<Dir, LifecycleError> {
        self.validate()?;
        Ok(self.target_directory.try_clone()?)
    }

    pub(super) fn require_rollback_external_paths(
        &self,
        expected: &[String],
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        if self.staged_rollback_point.is_none() {
            return invalid("external mutation requires a verified rollback point");
        }
        if self.rollback_external_paths != expected {
            return invalid("external handler scope differs from the frozen rollback point");
        }
        let rollback = self.staged_rollback_point.as_ref().ok_or_else(|| {
            LifecycleError::Invalid("lifecycle workspace has no frozen rollback point".to_owned())
        })?;
        rollback_stage::verify_published_external_preimage(
            &self.target_directory,
            rollback,
            expected,
        )?;
        self.validate()
    }

    pub(super) fn verify_reinstatement_stage(
        &self,
        plan: &ValidatedInstallPlan,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        let mut layout = external.layout();
        if self.staged_rollback_point.is_some() {
            layout.rollback_point = true;
        }
        staged_install::verify(self.stage.directory()?, plan, layout)?;
        external_stage::verify_staged(self.stage.directory()?, external)?;
        if let Some(rollback) = self.staged_rollback_point.as_ref() {
            rollback_stage::verify_staged(self.stage.directory()?, rollback)?;
        }
        self.validate()
    }

    pub(super) fn verify_reinstatement_stage_during_handler(
        &self,
        plan: &ValidatedInstallPlan,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.validate_staged_install_identity(plan)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        let mut layout = external.layout();
        layout.activation =
            external_stage::verify_staged_during_handler(self.stage.directory()?, external)?;
        if self.staged_rollback_point.is_some() {
            layout.rollback_point = true;
        }
        staged_install::verify(self.stage.directory()?, plan, layout)?;
        if let Some(rollback) = self.staged_rollback_point.as_ref() {
            rollback_stage::verify_staged(self.stage.directory()?, rollback)?;
        }
        self.validate()
    }

    pub(super) fn verify_staged_rollback_point(&self) -> Result<(), LifecycleError> {
        self.validate()?;
        let rollback = self.staged_rollback_point.as_ref().ok_or_else(|| {
            LifecycleError::Invalid("lifecycle workspace has no frozen rollback point".to_owned())
        })?;
        rollback_stage::verify_staged(self.stage.directory()?, rollback)?;
        self.validate()
    }

    pub(super) fn restore_uninstall_external_state(&self) -> Result<(), LifecycleError> {
        self.verify_staged_rollback_point()?;
        let managed = open_child_directory(
            self.stage.directory()?,
            ".agent-skills",
            Some(super::MANAGED_DIRECTORY_MODE),
            "uninstall rollback metadata",
        )?;
        let rollback = open_child_directory(
            &managed,
            super::ROLLBACK_POINT_DIRECTORY,
            Some(super::MANAGED_DIRECTORY_MODE),
            "uninstall rollback point",
        )?;
        let quarantine = external_stage::create_directory(
            self.stage.directory()?,
            OsStr::new("external-recovery"),
            Some(0o700),
            "uninstall external recovery quarantine",
        )?;
        super::rollback::restore_external_state(
            &rollback,
            &self.target_directory,
            self.target(),
            &quarantine,
            &self.stage_path().join("external-recovery"),
        )
    }

    pub(super) fn restore_persistent_rollback_external_state_with_hook(
        &self,
        approved_point: &str,
        hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<(), LifecycleError> {
        self.restore_persistent_rollback_external_state_with(approved_point, hook)
    }

    fn restore_persistent_rollback_external_state_with(
        &self,
        approved_point: &str,
        hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        let persistent = rollback::open_persistent_rollback_point(self.backup_directory()?)?;
        if persistent
            .point()
            .get("fingerprint")
            .and_then(Value::as_str)
            != Some(approved_point)
        {
            return invalid("backup rollback point differs from the approved identity");
        }
        if persistent.external_paths() != self.rollback_external_paths {
            return invalid("backup rollback external scope differs from the staged transaction");
        }
        self.require_rollback_external_paths(persistent.external_paths())?;
        let quarantine = external_stage::create_directory(
            self.stage.directory()?,
            OsStr::new("rollback-forward-recovery"),
            Some(0o700),
            "rollback forward recovery quarantine",
        )?;
        rollback::restore_external_state_with_hook(
            persistent.root(),
            &self.target_directory,
            self.target(),
            &quarantine,
            &self.stage_path().join("rollback-forward-recovery"),
            hook,
        )?;
        rollback::verify_external_target_state(persistent.root(), &self.target_directory)?;
        self.validate()
    }

    pub(super) fn verify_persistent_rollback_result(
        &self,
        approved_point: &str,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        let persistent = rollback::open_persistent_rollback_point(self.backup_directory()?)?;
        if persistent
            .point()
            .get("fingerprint")
            .and_then(Value::as_str)
            != Some(approved_point)
            || persistent.external_paths() != self.rollback_external_paths
        {
            return invalid("published rollback differs from the approved rollback point");
        }
        rollback::verify_external_target_state(persistent.root(), &self.target_directory)?;
        let external = self.staged_external_state.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace external state has not been staged".to_owned(),
            )
        })?;
        external_stage::verify_published_rollback(&self.target_directory, external)?;
        self.validate()
    }

    /// Copy and verify one caller-supplied package tree record.
    ///
    /// The destination is derived from `record.id` under
    /// `.agent-skills/packages`; source paths are opened relative to the
    /// supplied directory capability and symlinks are never followed. This
    /// validates the tree-local Install Plan shape and identity, but does not
    /// prove record membership in a complete validated plan.
    ///
    /// # Errors
    /// Fails closed when the workspace, record, source, or staged copy differs
    /// from the recorded tree identity.
    pub fn stage_package_tree(
        &mut self,
        source: &Dir,
        record: &Value,
    ) -> Result<(), LifecycleError> {
        self.validate()?;
        self.require_external_state_not_staged()?;
        staged_tree::stage_package(self.stage.directory()?, source, record)?;
        self.validate()
    }

    /// Copy and verify one caller-supplied Skill tree record.
    ///
    /// The destination is derived from `record.name` under `skills`; source
    /// paths are capability-relative and symlinks are never followed. This
    /// validates the tree-local Install Plan shape and identity, but does not
    /// prove record membership in a complete validated plan.
    ///
    /// # Errors
    /// Fails closed when the workspace, record, source, or staged copy differs
    /// from the recorded tree identity.
    pub fn stage_skill_tree(&mut self, source: &Dir, record: &Value) -> Result<(), LifecycleError> {
        self.validate()?;
        self.require_external_state_not_staged()?;
        staged_tree::stage_skill(self.stage.directory()?, source, record)?;
        self.validate()
    }

    /// Revalidate one previously staged package tree against its record.
    ///
    /// # Errors
    /// Fails if the workspace or staged package identity changed.
    pub fn verify_staged_package_tree(&self, record: &Value) -> Result<(), LifecycleError> {
        self.validate()?;
        staged_tree::verify_package(self.stage.directory()?, record)
    }

    /// Revalidate one previously staged Skill tree against its record.
    ///
    /// # Errors
    /// Fails if the workspace or staged Skill identity changed.
    pub fn verify_staged_skill_tree(&self, record: &Value) -> Result<(), LifecycleError> {
        self.validate()?;
        staged_tree::verify_skill(self.stage.directory()?, record)
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
        if let Err(error) = self.cleanup_handler_scratch() {
            errors.push(("handler scratch", error));
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
        if let Err(error) = self.cleanup_handler_scratch() {
            errors.push(("handler scratch", error));
        }
        if let Err(error) = self.release_lock() {
            errors.push(("lock release", error));
        }
        finish_cleanup("lifecycle workspace backup preservation", errors)?;
        Ok(path)
    }

    /// Preserve both stage and backup as recovery evidence, then release the
    /// lifecycle lock.
    ///
    /// This is used when recovery may have quarantined an external namespace
    /// entry in the private stage. Deleting either workspace could otherwise
    /// destroy the only retained copy of that entry.
    ///
    /// # Errors
    /// Fails closed when either workspace identity changed or lock release is
    /// incomplete.
    pub(super) fn preserve_recovery_workspace(
        mut self,
    ) -> Result<(PathBuf, PathBuf), LifecycleError> {
        let stage = self.stage_path();
        let backup = self.backup_path();
        let mut errors = Vec::new();
        match self.lock() {
            Ok(lock) => {
                if let Err(error) = lock.validate() {
                    errors.push(("lock validation", error));
                }
            }
            Err(error) => errors.push(("lock validation", error)),
        }
        if let Err(error) = self.stage.validate(&self.target_directory) {
            errors.push(("stage validation", error));
        }
        if let Err(error) = self.backup.validate(&self.target_directory) {
            errors.push(("backup validation", error));
        }
        self.stage.preserve();
        self.backup.preserve();
        if let Err(error) = self.cleanup_handler_scratch() {
            errors.push(("handler scratch", error));
        }
        if let Err(error) = self.release_lock() {
            errors.push(("lock release", error));
        }
        finish_cleanup("lifecycle recovery workspace preservation", errors)?;
        Ok((stage, backup))
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

    fn cleanup_handler_scratch(&self) -> Result<(), LifecycleError> {
        let scratch = self.lock()?.directory()?;
        let mut names = Vec::new();
        for entry in scratch.entries()? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_str().ok_or_else(|| {
                LifecycleError::Invalid(
                    "lifecycle handler scratch contains a non-UTF-8 entry".to_owned(),
                )
            })?;
            if !name.starts_with(".agent-source-activation-")
                && !name.starts_with(".config.toml.agent-skills-")
            {
                return invalid(format!(
                    "lifecycle handler scratch contains an unknown entry: {name}"
                ));
            }
            let metadata = scratch.symlink_metadata(name)?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return invalid(format!("lifecycle handler scratch entry is unsafe: {name}"));
            }
            names.push(name.to_owned());
        }
        names.sort();
        for name in names {
            scratch.remove_file(&name)?;
        }
        Ok(())
    }

    fn validate_staged_install_identity(
        &self,
        plan: &ValidatedInstallPlan,
    ) -> Result<(), LifecycleError> {
        let identity = self.staged_install_identity.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "lifecycle workspace has no staged Install Plan layout".to_owned(),
            )
        })?;
        if identity.install_plan_fingerprint != plan.fingerprint()
            || identity.package_lock_fingerprint != plan.package_lock_fingerprint()
        {
            return invalid("validated Install Plan differs from staged workspace identity");
        }
        Ok(())
    }

    fn require_external_state_not_staged(&self) -> Result<(), LifecycleError> {
        if self.staged_external_state.is_some() {
            return invalid("lifecycle workspace external state is already staged");
        }
        Ok(())
    }

    pub(super) fn target_directory_cap(&self) -> &Dir {
        &self.target_directory
    }

    pub(super) fn handler_scratch_directory(&self) -> Result<Dir, LifecycleError> {
        self.lock()?.directory()
    }

    pub(super) fn handler_scratch_path(&self) -> PathBuf {
        self.target.join(super::LIFECYCLE_LOCK_DIRECTORY)
    }

    pub(super) fn has_staged_rollback_point(&self) -> bool {
        self.staged_rollback_point.is_some()
    }

    pub(super) fn has_fresh_rollback_point(&self) -> bool {
        self.staged_rollback_point.is_some() && self.staged_rollback_point_is_fresh
    }

    pub(super) fn requires_persistent_rollback_restore(&self) -> bool {
        self.requires_persistent_rollback_restore
    }

    pub(super) fn verify_fresh_recovery(&self) -> Result<(), LifecycleError> {
        self.validate()?;
        if !self.has_fresh_rollback_point() {
            return invalid("fresh recovery requires a fresh rollback point");
        }
        require_fresh_managed_target(&self.target_directory)?;
        let rollback = self.staged_rollback_point.as_ref().ok_or_else(|| {
            LifecycleError::Invalid("lifecycle workspace has no frozen rollback point".to_owned())
        })?;
        rollback_stage::verify_staged_external_preimage(
            &self.target_directory,
            self.stage.directory()?,
            rollback,
            &self.rollback_external_paths,
        )?;
        self.validate()
    }

    pub(super) fn verify_recovery_backup(&self) -> Result<(), LifecycleError> {
        let rollback = self.staged_rollback_point.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "recovery backup verification requires a staged rollback point".to_owned(),
            )
        })?;
        rollback_stage::verify_backup(self.backup.directory()?, rollback)
    }

    pub(super) fn verify_restored_install(&self) -> Result<(), LifecycleError> {
        let rollback = self.staged_rollback_point.as_ref().ok_or_else(|| {
            LifecycleError::Invalid(
                "restored installation verification requires a staged rollback point".to_owned(),
            )
        })?;
        rollback_stage::verify_restored(&self.target_directory, rollback)
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
        make_owned_tree_removable(self.directory()?)?;
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

#[cfg(unix)]
fn make_owned_tree_removable(directory: &Dir) -> Result<(), LifecycleError> {
    use cap_fs_ext::DirExt as _;
    use cap_std::fs::{Permissions, PermissionsExt as _};

    directory.set_permissions(".", Permissions::from_mode(WORKSPACE_DIRECTORY_MODE))?;
    let names = directory
        .entries()?
        .map(|entry| entry.map(|entry| entry.file_name()))
        .collect::<Result<Vec<_>, _>>()?;
    for name in names {
        let before = directory.symlink_metadata(&name)?;
        if before.file_type().is_symlink() || !before.is_dir() {
            continue;
        }
        let child = directory.open_dir_nofollow(&name)?;
        let opened = child.dir_metadata()?;
        if !same_object_cap(&before, &opened) {
            return invalid("lifecycle workspace tree changed while preparing cleanup");
        }
        make_owned_tree_removable(&child)?;
        let current = directory.open_dir_nofollow(&name)?.dir_metadata()?;
        if !same_object_cap(&opened, &current) {
            return invalid("lifecycle workspace tree changed while preparing cleanup");
        }
    }
    Ok(())
}

#[cfg(windows)]
fn make_owned_tree_removable(directory: &Dir) -> Result<(), LifecycleError> {
    let mut root_permissions = directory.dir_metadata()?.permissions();
    root_permissions.set_readonly(false);
    directory.set_permissions(".", root_permissions)?;
    let names = directory
        .entries()?
        .map(|entry| entry.map(|entry| entry.file_name()))
        .collect::<Result<Vec<_>, _>>()?;
    for name in names {
        let before = directory.symlink_metadata(&name)?;
        if before.file_type().is_symlink() {
            continue;
        }
        if before.is_dir() {
            let child = external_stage::open_directory(
                directory,
                &name,
                None,
                "lifecycle workspace directory",
            )?;
            let opened = child.dir_metadata()?;
            if !same_object_cap(&before, &opened) {
                return invalid("lifecycle workspace tree changed while preparing cleanup");
            }
            make_owned_tree_removable(&child)?;
            let current = external_stage::open_directory(
                directory,
                &name,
                None,
                "lifecycle workspace directory",
            )?
            .dir_metadata()?;
            if !same_object_cap(&opened, &current) {
                return invalid("lifecycle workspace tree changed while preparing cleanup");
            }
        } else if before.is_file() {
            if before.permissions().readonly() {
                clear_windows_file_readonly(directory, &name, &before)?;
            }
        } else {
            return invalid("lifecycle workspace contains an unsupported filesystem object");
        }
    }
    Ok(())
}

#[cfg(windows)]
fn clear_windows_file_readonly(
    directory: &Dir,
    name: &std::ffi::OsStr,
    before: &cap_std::fs::Metadata,
) -> Result<(), LifecycleError> {
    use cap_fs_ext::MetadataExt as _;
    use cap_std::fs::OpenOptionsExt as _;

    const FILE_READ_ATTRIBUTES: u32 = 0x0080;
    const FILE_WRITE_ATTRIBUTES: u32 = 0x0100;
    let mut options = cap_std::fs::OpenOptions::new();
    options
        .read(true)
        .access_mode(FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES);
    super::configure_nofollow(&mut options);
    let file = directory.open_with(name, &options)?;
    let opened = file.metadata()?;
    if before.nlink() != 1
        || opened.file_type().is_symlink()
        || !opened.is_file()
        || opened.nlink() != 1
        || !same_object_cap(before, &opened)
    {
        return invalid("lifecycle workspace tree changed while preparing cleanup");
    }
    let mut permissions = opened.permissions();
    permissions.set_readonly(false);
    file.set_permissions(permissions)?;
    let current = directory.symlink_metadata(name)?;
    if current.file_type().is_symlink()
        || !current.is_file()
        || current.nlink() != 1
        || !same_object_cap(&opened, &current)
    {
        return invalid("lifecycle workspace tree changed while preparing cleanup");
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
#[allow(clippy::unnecessary_wraps)]
fn make_owned_tree_removable(_directory: &Dir) -> Result<(), LifecycleError> {
    Ok(())
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

fn read_persistent_agents(root: &Dir) -> Result<Vec<u8>, LifecycleError> {
    let mut agents = open_child_file(
        root,
        "AGENTS.md",
        super::MANAGED_FILE_MODE,
        "rollback point AGENTS.md",
    )?;
    let opened = agents.metadata()?;
    if opened.len() > MAX_CONTRACT_JSON_BYTES as u64 {
        return invalid("rollback point AGENTS.md exceeds the size limit");
    }
    let mut instructions = Vec::new();
    std::io::Read::by_ref(&mut agents)
        .take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
        .read_to_end(&mut instructions)?;
    if instructions.len() > MAX_CONTRACT_JSON_BYTES {
        return invalid("rollback point AGENTS.md exceeds the size limit");
    }
    let completed = agents.metadata()?;
    let current = open_child_file(
        root,
        "AGENTS.md",
        super::MANAGED_FILE_MODE,
        "rollback point AGENTS.md",
    )?
    .metadata()?;
    if !same_object_cap(&opened, &completed)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &completed)
        || !same_content_state_cap(&opened, &current)
    {
        return invalid("rollback point AGENTS.md changed while staging");
    }
    Ok(instructions)
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

fn require_fresh_managed_target(target: &Dir) -> Result<(), LifecycleError> {
    for name in ["AGENTS.md", "skills", ".agent-skills"] {
        match target.symlink_metadata(name) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
            Ok(_) => {
                return invalid(format!(
                    "fresh source activation requires an absent managed root: {name}"
                ));
            }
        }
    }
    Ok(())
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
    fn handler_scratch_is_removed_before_lifecycle_lock_release() {
        let root = temporary_path("handler-scratch-cleanup");
        std::fs::create_dir(&root).expect("create handler scratch target");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        let scratch = workspace
            .handler_scratch_directory()
            .expect("open handler scratch");
        external_stage::write_independent_file(
            &scratch,
            ".agent-source-activation-old-test",
            b"preimage\n",
            0o600,
            "test handler scratch",
        )
        .expect("write handler scratch");
        drop(scratch);
        workspace.cleanup().expect("cleanup lifecycle workspace");
        assert!(!root.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        std::fs::remove_dir(&root).expect("remove handler scratch target");
    }

    #[test]
    fn recovery_workspace_survives_handler_scratch_cleanup_failure() {
        let root = temporary_path("preserve-recovery-scratch-failure");
        std::fs::create_dir(&root).expect("create recovery target");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        let stage = workspace.stage_path();
        let backup = workspace.backup_path();
        workspace
            .stage_directory()
            .expect("borrow stage capability")
            .write("quarantined", b"stage evidence")
            .expect("write stage evidence");
        workspace
            .backup_directory()
            .expect("borrow backup capability")
            .write("legacy-link", b"backup evidence")
            .expect("write backup evidence");
        workspace
            .handler_scratch_directory()
            .expect("open handler scratch")
            .write("unexpected-entry", b"unsafe scratch residue")
            .expect("write unexpected scratch entry");

        let error = workspace
            .preserve_recovery_workspace()
            .expect_err("unknown scratch entry must fail preservation cleanup");
        assert!(error.to_string().contains("unknown entry"));
        assert_eq!(
            std::fs::read(stage.join("quarantined")).expect("read preserved stage evidence"),
            b"stage evidence"
        );
        assert_eq!(
            std::fs::read(backup.join("legacy-link")).expect("read preserved backup evidence"),
            b"backup evidence"
        );

        std::fs::remove_dir_all(&root).expect("remove preserved recovery target");
    }

    fn assert_recovery_peer_survives_workspace_namespace_drift(drift_stage: bool) {
        let label = if drift_stage {
            "preserve-recovery-stage-drift"
        } else {
            "preserve-recovery-backup-drift"
        };
        let root = temporary_path(label);
        std::fs::create_dir(&root).expect("create recovery target");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        let stage = workspace.stage_path();
        let backup = workspace.backup_path();
        workspace
            .stage_directory()
            .expect("borrow stage capability")
            .write("stage-evidence", b"stage evidence")
            .expect("write stage evidence");
        workspace
            .backup_directory()
            .expect("borrow backup capability")
            .write("backup-evidence", b"backup evidence")
            .expect("write backup evidence");
        let drifted = root.join("detached-workspace");
        std::fs::rename(if drift_stage { &stage } else { &backup }, &drifted)
            .expect("drift workspace namespace");

        workspace
            .preserve_recovery_workspace()
            .expect_err("workspace namespace drift must fail preservation validation");
        let (peer, peer_name, peer_bytes) = if drift_stage {
            (&backup, "backup-evidence", b"backup evidence".as_slice())
        } else {
            (&stage, "stage-evidence", b"stage evidence".as_slice())
        };
        assert_eq!(
            std::fs::read(peer.join(peer_name)).expect("read preserved peer evidence"),
            peer_bytes
        );
        let drifted_name = if drift_stage {
            "stage-evidence"
        } else {
            "backup-evidence"
        };
        assert!(drifted.join(drifted_name).is_file());

        std::fs::remove_dir_all(&root).expect("remove preserved recovery target");
    }

    #[test]
    fn recovery_backup_survives_stage_namespace_drift() {
        assert_recovery_peer_survives_workspace_namespace_drift(true);
    }

    #[test]
    fn recovery_stage_survives_backup_namespace_drift() {
        assert_recovery_peer_survives_workspace_namespace_drift(false);
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

    #[cfg(windows)]
    #[allow(clippy::permissions_set_readonly_false)]
    #[test]
    fn readonly_cleanup_rejects_external_hard_link_alias() {
        let root = temporary_path("readonly-hard-link");
        std::fs::create_dir(&root).expect("create workspace target");
        let victim = root.join("victim");
        std::fs::write(&victim, b"outside\n").expect("write outside victim");
        let mut permissions = std::fs::metadata(&victim)
            .expect("inspect outside victim")
            .permissions();
        permissions.set_readonly(true);
        std::fs::set_permissions(&victim, permissions).expect("make outside victim readonly");
        let workspace = LifecycleWorkspace::begin(&root).expect("begin lifecycle workspace");
        let stage = workspace.stage_path();
        std::fs::hard_link(&victim, stage.join("alias")).expect("create workspace hard-link alias");

        let error = workspace
            .cleanup()
            .expect_err("cleanup must reject shared readonly file");
        assert!(error.to_string().contains("preparing cleanup"), "{error}");
        assert!(
            std::fs::metadata(&victim)
                .expect("inspect outside victim after cleanup")
                .permissions()
                .readonly()
        );
        let mut permissions = std::fs::metadata(&victim)
            .expect("inspect outside victim for cleanup")
            .permissions();
        permissions.set_readonly(false);
        std::fs::set_permissions(&victim, permissions).expect("restore victim permissions");
        std::fs::remove_dir_all(stage).expect("remove rejected stage");
        std::fs::remove_file(victim).expect("remove outside victim");
        std::fs::remove_dir(root).expect("remove workspace target");
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
