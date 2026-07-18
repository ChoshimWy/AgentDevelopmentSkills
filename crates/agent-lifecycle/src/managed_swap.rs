use super::{
    LifecycleError, LifecycleWorkspace, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    ValidatedInstallPlan, external_stage, open_child_directory, open_child_file, same_object_cap,
    source_activation,
};
use cap_std::fs::{Dir, Metadata};
use serde_json::Value;
use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::fmt;
use std::path::{Path, PathBuf};

const MANAGED_ROOTS: [ManagedRoot; 3] = [
    ManagedRoot::file("AGENTS.md", MANAGED_FILE_MODE),
    ManagedRoot::directory("skills", MANAGED_DIRECTORY_MODE),
    ManagedRoot::directory(".agent-skills", MANAGED_DIRECTORY_MODE),
];

#[derive(Clone, Copy)]
struct ManagedRoot {
    kind: RootKind,
    mode: u32,
    name: &'static str,
}

impl ManagedRoot {
    const fn file(name: &'static str, mode: u32) -> Self {
        Self {
            kind: RootKind::File,
            mode,
            name,
        }
    }

    const fn directory(name: &'static str, mode: u32) -> Self {
        Self {
            kind: RootKind::Directory,
            mode,
            name,
        }
    }
}

#[derive(Clone, Copy)]
enum RootKind {
    Directory,
    File,
}

struct RootMove {
    backed_up: bool,
    previous_identity: Option<Metadata>,
    published: bool,
    root: ManagedRoot,
    staged_identity: Metadata,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExternalMutationState {
    Preserved,
    InProgress,
    ActivationApplied,
    ActivationRemoved,
}

/// A published managed installation whose previous roots remain recoverable.
///
/// The guard keeps the lifecycle lock and recovery backup alive across final
/// verification. Call [`Self::commit`] only after all transaction-local checks
/// pass, or call [`Self::rollback`] to restore the previous installation. If
/// dropped without either operation, it attempts the same identity-bound
/// rollback and preserves the backup when recovery cannot complete.
#[must_use = "a published install must be committed or rolled back"]
pub struct PublishedInstall {
    external_mutation_started: bool,
    external_mutation_state: ExternalMutationState,
    plan: ValidatedInstallPlan,
    roots: Vec<RootMove>,
    workspace: Option<LifecycleWorkspace>,
}

struct RemovalRootMove {
    backed_up: bool,
    identity: Metadata,
    root: ManagedRoot,
}

struct PreservedSystemSkills {
    identity: Metadata,
    phase: SystemSkillsPhase,
}

enum SystemSkillsPhase {
    BackupOnly,
    TargetRootCreated { identity: Metadata },
    Published { target_root_identity: Metadata },
}

/// A fully published managed uninstall whose complete source installation
/// remains recoverable until explicit commit.
#[must_use = "a published uninstall must be committed or rolled back"]
pub struct PublishedUninstall {
    deactivation: Option<source_activation::SourceDeactivation>,
    external_mutation_started: bool,
    report: Value,
    roots: Vec<RemovalRootMove>,
    system_skills: Option<PreservedSystemSkills>,
    workspace: Option<LifecycleWorkspace>,
}

impl fmt::Debug for PublishedUninstall {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PublishedUninstall")
            .field(
                "target",
                &self.workspace.as_ref().map(LifecycleWorkspace::target),
            )
            .field("managed_root_count", &self.roots.len())
            .field("external_mutation_started", &self.external_mutation_started)
            .finish_non_exhaustive()
    }
}

impl fmt::Debug for PublishedInstall {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PublishedInstall")
            .field(
                "target",
                &self.workspace.as_ref().map(LifecycleWorkspace::target),
            )
            .field("managed_root_count", &self.roots.len())
            .finish_non_exhaustive()
    }
}

impl LifecycleWorkspace {
    /// Atomically publish all three staged managed roots under the held lock.
    ///
    /// Existing managed roots are first moved to the private backup. A target
    /// is accepted only when all managed roots are absent, or when all are
    /// present and the staged transaction contains a verified rollback point.
    /// Every move uses no-replace semantics and is checked against the exact
    /// source object identity before and after the rename.
    ///
    /// On any failure, completed moves are reversed. If that recovery is
    /// incomplete, the backup is preserved and its path is included in the
    /// returned error.
    ///
    /// # Errors
    /// Fails closed for an incomplete stage, partial target layout, unexpected
    /// backup content, identity drift, namespace collision, or incomplete
    /// recovery.
    pub fn publish_staged_install(
        self,
        plan: &ValidatedInstallPlan,
    ) -> Result<PublishedInstall, LifecycleError> {
        self.publish_staged_install_with(plan, |_, _| Ok(()))
    }

    #[cfg(test)]
    pub(crate) fn publish_staged_install_with_hook(
        self,
        plan: &ValidatedInstallPlan,
        hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<PublishedInstall, LifecycleError> {
        self.publish_staged_install_with(plan, hook)
    }

    fn publish_staged_install_with(
        self,
        plan: &ValidatedInstallPlan,
        mut move_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<PublishedInstall, LifecycleError> {
        let mut roots = Vec::new();
        let result = (|| {
            self.verify_staged_install(plan)?;
            require_empty(self.backup_directory()?, "lifecycle recovery backup")?;
            let (captured, present) = capture_root_moves(&self)?;
            roots = captured;
            if present != 0 && present != MANAGED_ROOTS.len() {
                return invalid("target managed roots are incomplete");
            }
            if present != 0 && !self.has_staged_rollback_point() {
                return invalid(
                    "replacing an existing installation requires a verified rollback point",
                );
            }

            backup_current_roots(&self, &mut roots, &mut move_hook)?;
            if present != 0 {
                self.verify_recovery_backup()?;
            }
            publish_new_roots(&self, &mut roots, &mut move_hook)?;
            self.verify_published_install(plan)?;
            verify_published_roots(&self, &roots)
        })();

        match result {
            Ok(()) => Ok(PublishedInstall {
                external_mutation_started: false,
                external_mutation_state: ExternalMutationState::Preserved,
                plan: plan.clone(),
                roots,
                workspace: Some(self),
            }),
            Err(error) => Err(abort_workspace(self, &mut roots, plan, false, error)),
        }
    }

    /// Publish a full managed uninstall under the held lifecycle lock.
    ///
    /// The complete managed installation and exact external deactivation scope
    /// are frozen into a private rollback point before any rename. All three
    /// managed roots then move into the private backup, activation-owned files
    /// are removed through the trusted source-deactivation handler, and an
    /// existing `skills/.system` tree is returned to the target unchanged.
    ///
    /// # Errors
    /// Fails closed for an incomplete or modified installation, unsafe
    /// Activation ownership, unsupported activation paths, external drift,
    /// namespace collisions, or incomplete recovery.
    pub fn publish_uninstall(self) -> Result<PublishedUninstall, LifecycleError> {
        self.publish_uninstall_with(|_, _| Ok(()), |_, _| Ok(()), |_, _| Ok(()))
    }

    #[cfg(test)]
    pub(crate) fn publish_uninstall_with_test_hooks(
        self,
        root_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
        handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
        system_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<PublishedUninstall, LifecycleError> {
        self.publish_uninstall_with(root_hook, handler_hook, system_hook)
    }

    fn publish_uninstall_with(
        mut self,
        mut root_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
        mut handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
        mut system_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<PublishedUninstall, LifecycleError> {
        let target = self.target_directory()?;
        let deactivation =
            source_activation::SourceDeactivation::prepare_for_uninstall(&target, self.target())?;
        let external_paths = deactivation
            .as_ref()
            .map_or(&[][..], |prepared| prepared.scope());
        let rollback_fingerprint = self.stage_uninstall_rollback(external_paths)?;
        if let Some(prepared) = deactivation.as_ref() {
            prepared.revalidate(&target)?;
        }
        require_empty(self.backup_directory()?, "uninstall recovery backup")?;
        let preserved_profiles = inspect_preserved_profiles(&target)?;
        let config_action = if deactivation.is_some() {
            None
        } else {
            Some(inspect_unmanaged_config_action(&target)?)
        };
        let mut roots = capture_removal_roots(&self)?;
        let mut system_skills = None;
        let mut external_mutation_started = false;
        let mut deactivation_result = None;
        let result = (|| {
            backup_removal_roots(&self, &mut roots, &mut root_hook)?;
            self.verify_recovery_backup()?;
            if let Some(prepared) = deactivation.as_ref() {
                external_mutation_started = true;
                deactivation_result = Some(prepared.apply_after_managed_backup(
                    &target,
                    self.backup_directory()?,
                    &self.handler_scratch_directory()?,
                    &mut handler_hook,
                )?);
            }
            preserve_system_skills(&self, &mut system_skills, &mut system_hook)?;
            verify_published_uninstall(&self, &roots, system_skills.as_ref(), deactivation.as_ref())
        })();
        if let Err(error) = result {
            return Err(abort_uninstall(
                self,
                &mut roots,
                &mut system_skills,
                external_mutation_started,
                error,
            ));
        }
        let deactivation_result = deactivation_result.unwrap_or_else(|| {
            serde_json::json!({
                "config_action": config_action.unwrap_or("missing"),
                "handler": Value::Null,
                "removed_files": [],
            })
        });
        let report = serde_json::json!({
            "activated_files": deactivation_result
                .get("removed_files")
                .cloned()
                .unwrap_or_else(|| serde_json::json!([])),
            "config_action": deactivation_result
                .get("config_action")
                .cloned()
                .unwrap_or_else(|| serde_json::json!("missing")),
            "handler": deactivation_result.get("handler").cloned().unwrap_or(Value::Null),
            "legacy_links_restored": false,
            "managed_roots": MANAGED_ROOTS.iter().map(|root| root.name).collect::<Vec<_>>(),
            "preserved_profiles": preserved_profiles,
            "preserved_system_skills": system_skills.is_some(),
            "rollback_point": rollback_fingerprint,
            "status": "published",
        });
        Ok(PublishedUninstall {
            deactivation,
            external_mutation_started,
            report,
            roots,
            system_skills,
            workspace: Some(self),
        })
    }
}

fn capture_removal_roots(
    workspace: &LifecycleWorkspace,
) -> Result<Vec<RemovalRootMove>, LifecycleError> {
    let mut roots = Vec::with_capacity(MANAGED_ROOTS.len());
    for root in MANAGED_ROOTS {
        let identity = capture_required(
            workspace.target_directory_cap(),
            root,
            "uninstall managed root",
        )?;
        verify_cleanup_ready(workspace.target_directory_cap(), root)?;
        roots.push(RemovalRootMove {
            backed_up: false,
            identity,
            root,
        });
    }
    Ok(roots)
}

fn backup_removal_roots(
    workspace: &LifecycleWorkspace,
    roots: &mut [RemovalRootMove],
    hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    for moved in roots {
        hook(moved.root.name, "backup")?;
        prepare_bound_move(
            workspace.target_directory_cap(),
            moved.root,
            &moved.identity,
            workspace.backup_directory()?,
            "uninstall managed root",
            "uninstall recovery root",
        )?;
        hook(moved.root.name, "backup-before-rename")?;
        rename_bound_root(
            workspace.target_directory_cap(),
            workspace.target(),
            moved.root,
            workspace.backup_directory()?,
            &workspace.backup_path(),
            "uninstall managed root",
            "uninstall recovery root",
        )?;
        moved.backed_up = true;
        hook(moved.root.name, "backup-after-rename")?;
        verify_bound_move(
            workspace.target_directory_cap(),
            moved.root,
            &moved.identity,
            workspace.backup_directory()?,
            "uninstall managed root",
            "uninstall recovery root",
        )?;
    }
    Ok(())
}

fn restore_removal_roots(
    workspace: &LifecycleWorkspace,
    roots: &mut [RemovalRootMove],
) -> Vec<String> {
    let mut errors = Vec::new();
    for moved in roots.iter_mut().rev() {
        if !moved.backed_up {
            continue;
        }
        let backup = match workspace.backup_directory() {
            Ok(backup) => backup,
            Err(error) => {
                errors.push(format!("restore {}: {error}", moved.root.name));
                continue;
            }
        };
        let restored = prepare_bound_move(
            backup,
            moved.root,
            &moved.identity,
            workspace.target_directory_cap(),
            "uninstall recovery root",
            "restored uninstall root",
        )
        .and_then(|()| {
            rename_bound_root(
                backup,
                &workspace.backup_path(),
                moved.root,
                workspace.target_directory_cap(),
                workspace.target(),
                "uninstall recovery root",
                "restored uninstall root",
            )
        })
        .and_then(|()| {
            verify_bound_move(
                backup,
                moved.root,
                &moved.identity,
                workspace.target_directory_cap(),
                "uninstall recovery root",
                "restored uninstall root",
            )
        });
        match restored {
            Ok(()) => moved.backed_up = false,
            Err(error) => errors.push(format!("restore {}: {error}", moved.root.name)),
        }
    }
    errors
}

fn preserve_system_skills(
    workspace: &LifecycleWorkspace,
    state: &mut Option<PreservedSystemSkills>,
    hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    let backup_skills = open_child_directory(
        workspace.backup_directory()?,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "uninstall recovery Skills",
    )?;
    let identity = match backup_skills.symlink_metadata(".system") {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return invalid("uninstall recovery skills/.system is unsafe");
        }
        Ok(_) => open_child_directory(
            &backup_skills,
            ".system",
            Some(MANAGED_DIRECTORY_MODE),
            "uninstall recovery skills/.system",
        )?
        .dir_metadata()?,
    };
    *state = Some(PreservedSystemSkills {
        identity: identity.clone(),
        phase: SystemSkillsPhase::BackupOnly,
    });
    require_absent(
        workspace.target_directory_cap(),
        "skills",
        "uninstall preserved system Skills root",
    )?;
    let target_skills = external_stage::create_directory(
        workspace.target_directory_cap(),
        OsStr::new("skills"),
        Some(MANAGED_DIRECTORY_MODE),
        "uninstall preserved system Skills root",
    )?;
    let target_skills_identity = target_skills.dir_metadata()?;
    state
        .as_mut()
        .ok_or_else(|| LifecycleError::Invalid("system Skills state is missing".to_owned()))?
        .phase = SystemSkillsPhase::TargetRootCreated {
        identity: target_skills_identity.clone(),
    };
    hook("skills/.system", "target-root-created")?;
    rename_no_replace(
        &backup_skills,
        &workspace.backup_path().join("skills"),
        ".system",
        &target_skills,
        &workspace.target().join("skills"),
        ".system",
    )?;
    state
        .as_mut()
        .ok_or_else(|| LifecycleError::Invalid("system Skills state is missing".to_owned()))?
        .phase = SystemSkillsPhase::Published {
        target_root_identity: target_skills_identity,
    };
    hook("skills/.system", "published-after-rename")?;
    require_absent(
        &backup_skills,
        ".system",
        "uninstall recovery system Skills source",
    )?;
    let published = open_child_directory(
        &target_skills,
        ".system",
        Some(MANAGED_DIRECTORY_MODE),
        "preserved system Skills",
    )?
    .dir_metadata()?;
    if !same_object_cap(&identity, &published) {
        return invalid("preserved system Skills identity changed after rename");
    }
    require_only_system_skills(&target_skills)?;
    Ok(())
}

fn recover_system_skills(
    workspace: &LifecycleWorkspace,
    system: &mut PreservedSystemSkills,
) -> Result<(), LifecycleError> {
    match &system.phase {
        SystemSkillsPhase::BackupOnly => return Ok(()),
        SystemSkillsPhase::TargetRootCreated { identity } => {
            let target_skills = open_child_directory(
                workspace.target_directory_cap(),
                "skills",
                Some(MANAGED_DIRECTORY_MODE),
                "empty preserved system Skills root",
            )?;
            if !same_object_cap(identity, &target_skills.dir_metadata()?) {
                return invalid("empty preserved system Skills root identity changed");
            }
            if target_skills.entries()?.next().transpose()?.is_some() {
                return invalid("empty preserved system Skills root is not empty");
            }
            workspace.target_directory_cap().remove_dir("skills")?;
            require_absent(
                workspace.target_directory_cap(),
                "skills",
                "restored system Skills root",
            )?;
            system.phase = SystemSkillsPhase::BackupOnly;
            return Ok(());
        }
        SystemSkillsPhase::Published { .. } => {}
    }
    let target_skills = open_child_directory(
        workspace.target_directory_cap(),
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "preserved system Skills root",
    )?;
    let current_root = target_skills.dir_metadata()?;
    let target_root_identity = match &system.phase {
        SystemSkillsPhase::Published {
            target_root_identity,
        } => target_root_identity,
        SystemSkillsPhase::BackupOnly | SystemSkillsPhase::TargetRootCreated { .. } => {
            return invalid("preserved system Skills recovery phase is invalid");
        }
    };
    if !same_object_cap(target_root_identity, &current_root) {
        return invalid("preserved system Skills root identity changed");
    }
    require_only_system_skills(&target_skills)?;
    let current = open_child_directory(
        &target_skills,
        ".system",
        Some(MANAGED_DIRECTORY_MODE),
        "preserved system Skills",
    )?
    .dir_metadata()?;
    if !same_object_cap(&system.identity, &current) {
        return invalid("preserved system Skills identity changed before recovery");
    }
    let backup_skills = open_child_directory(
        workspace.backup_directory()?,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "uninstall recovery Skills",
    )?;
    require_absent(
        &backup_skills,
        ".system",
        "uninstall recovery system Skills destination",
    )?;
    rename_no_replace(
        &target_skills,
        &workspace.target().join("skills"),
        ".system",
        &backup_skills,
        &workspace.backup_path().join("skills"),
        ".system",
    )?;
    system.phase = SystemSkillsPhase::TargetRootCreated {
        identity: current_root,
    };
    let restored = open_child_directory(
        &backup_skills,
        ".system",
        Some(MANAGED_DIRECTORY_MODE),
        "restored recovery system Skills",
    )?
    .dir_metadata()?;
    if !same_object_cap(&system.identity, &restored) {
        return invalid("restored recovery system Skills identity changed");
    }
    workspace.target_directory_cap().remove_dir("skills")?;
    require_absent(
        workspace.target_directory_cap(),
        "skills",
        "restored system Skills root",
    )?;
    system.phase = SystemSkillsPhase::BackupOnly;
    Ok(())
}

fn require_only_system_skills(skills: &Dir) -> Result<(), LifecycleError> {
    let mut entries = BTreeSet::new();
    for entry in skills.entries()? {
        let entry = entry?;
        entries.insert(
            entry
                .file_name()
                .to_str()
                .ok_or_else(|| {
                    LifecycleError::Invalid(
                        "preserved system Skills root contains a non-UTF-8 entry".to_owned(),
                    )
                })?
                .to_owned(),
        );
    }
    if entries != BTreeSet::from([".system".to_owned()]) {
        return invalid("preserved system Skills root contains unmanaged entries");
    }
    Ok(())
}

fn verify_published_uninstall(
    workspace: &LifecycleWorkspace,
    roots: &[RemovalRootMove],
    system_skills: Option<&PreservedSystemSkills>,
    deactivation: Option<&source_activation::SourceDeactivation>,
) -> Result<(), LifecycleError> {
    workspace.validate()?;
    workspace.verify_staged_rollback_point()?;
    for moved in roots {
        if !moved.backed_up {
            return invalid("uninstall recovery roots are incomplete");
        }
        require_identity(
            workspace.backup_directory()?,
            moved.root,
            &moved.identity,
            "uninstall recovery root",
        )?;
    }
    workspace.verify_recovery_backup()?;
    require_absent(
        workspace.target_directory_cap(),
        "AGENTS.md",
        "published uninstall",
    )?;
    require_absent(
        workspace.target_directory_cap(),
        ".agent-skills",
        "published uninstall",
    )?;
    if let Some(system) = system_skills {
        let target_skills_identity = match &system.phase {
            SystemSkillsPhase::Published {
                target_root_identity,
            } => target_root_identity,
            SystemSkillsPhase::BackupOnly | SystemSkillsPhase::TargetRootCreated { .. } => {
                return invalid("preserved system Skills were not published");
            }
        };
        let skills = open_child_directory(
            workspace.target_directory_cap(),
            "skills",
            Some(MANAGED_DIRECTORY_MODE),
            "preserved system Skills root",
        )?;
        if !same_object_cap(target_skills_identity, &skills.dir_metadata()?) {
            return invalid("preserved system Skills root identity changed");
        }
        require_only_system_skills(&skills)?;
        let current = open_child_directory(
            &skills,
            ".system",
            Some(MANAGED_DIRECTORY_MODE),
            "preserved system Skills",
        )?
        .dir_metadata()?;
        if !same_object_cap(&system.identity, &current) {
            return invalid("preserved system Skills identity changed");
        }
    } else {
        require_absent(
            workspace.target_directory_cap(),
            "skills",
            "published uninstall",
        )?;
    }
    if let Some(prepared) = deactivation {
        prepared.verify_uninstall_output(workspace.target_directory_cap())?;
    }
    workspace.validate()
}

fn inspect_preserved_profiles(target: &Dir) -> Result<Vec<String>, LifecycleError> {
    let mut profiles = Vec::new();
    for name in source_activation::PROFILE_NAMES {
        match target.symlink_metadata(name) {
            Ok(_) => profiles.push((*name).to_owned()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(profiles)
}

fn inspect_unmanaged_config_action(target: &Dir) -> Result<&'static str, LifecycleError> {
    match target.symlink_metadata("config.toml") {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok("missing"),
        Err(error) => Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            invalid("config.toml must be a regular file")
        }
        Ok(_) => Ok("preserved"),
    }
}

fn capture_root_moves(
    workspace: &LifecycleWorkspace,
) -> Result<(Vec<RootMove>, usize), LifecycleError> {
    let stage = workspace.stage_directory()?;
    let target = workspace.target_directory_cap();
    let mut present = 0_usize;
    let mut roots = Vec::with_capacity(MANAGED_ROOTS.len());
    for root in MANAGED_ROOTS {
        let staged_identity = capture_required(stage, root, "staged managed root")?;
        let previous_identity = capture_optional(target, root, "target managed root")?;
        if previous_identity.is_some() {
            verify_cleanup_ready(target, root)?;
        }
        present += usize::from(previous_identity.is_some());
        roots.push(RootMove {
            backed_up: false,
            previous_identity,
            published: false,
            root,
            staged_identity,
        });
    }
    Ok((roots, present))
}

fn backup_current_roots(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    let target = workspace.target_directory_cap();
    for moved in roots {
        let Some(previous) = moved.previous_identity.as_ref() else {
            continue;
        };
        hook(moved.root.name, "backup")?;
        prepare_bound_move(
            target,
            moved.root,
            previous,
            workspace.backup_directory()?,
            "target managed root",
            "recovery backup root",
        )?;
        hook(moved.root.name, "backup-before-rename")?;
        rename_bound_root(
            target,
            workspace.target(),
            moved.root,
            workspace.backup_directory()?,
            &workspace.backup_path(),
            "target managed root",
            "recovery backup root",
        )?;
        moved.backed_up = true;
        hook(moved.root.name, "backup-after-rename")?;
        verify_bound_move(
            target,
            moved.root,
            previous,
            workspace.backup_directory()?,
            "target managed root",
            "recovery backup root",
        )?;
    }
    Ok(())
}

fn publish_new_roots(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Result<(), LifecycleError> {
    let target = workspace.target_directory_cap();
    for moved in roots {
        hook(moved.root.name, "publish")?;
        prepare_bound_move(
            workspace.stage_directory()?,
            moved.root,
            &moved.staged_identity,
            target,
            "staged managed root",
            "published managed root",
        )?;
        hook(moved.root.name, "publish-before-rename")?;
        rename_bound_root(
            workspace.stage_directory()?,
            &workspace.stage_path(),
            moved.root,
            target,
            workspace.target(),
            "staged managed root",
            "published managed root",
        )?;
        moved.published = true;
        hook(moved.root.name, "publish-after-rename")?;
        verify_bound_move(
            workspace.stage_directory()?,
            moved.root,
            &moved.staged_identity,
            target,
            "staged managed root",
            "published managed root",
        )?;
    }
    Ok(())
}

impl PublishedInstall {
    /// Return the locked installation target.
    ///
    /// # Errors
    /// Fails only if the guard has already been consumed internally.
    pub fn target(&self) -> Result<&Path, LifecycleError> {
        Ok(self.workspace()?.target())
    }

    /// Return the recovery backup path for diagnostics.
    ///
    /// # Errors
    /// Fails only if the guard has already been consumed internally.
    pub fn backup_path(&self) -> Result<PathBuf, LifecycleError> {
        Ok(self.workspace()?.backup_path())
    }

    /// Revalidate the published roots, complete managed semantics, preserved
    /// external state, rollback point, and recovery backup identities.
    ///
    /// # Errors
    /// Fails if any published or backed-up root changed, or if installed
    /// content differs from the validated transaction.
    pub fn verify(&self, plan: &ValidatedInstallPlan) -> Result<(), LifecycleError> {
        let workspace = self.workspace()?;
        verify_published_roots(workspace, &self.roots)?;
        self.verify_published_state(workspace, plan)?;
        verify_published_roots(workspace, &self.roots)
    }

    /// Run the trusted source-deactivation handler inside this transaction.
    ///
    /// The handler derives its exact scope from the validated Activation Lock
    /// and requires that scope to match the frozen rollback point before the
    /// first external write. It validates every owned file and `config.toml`
    /// preimage, removes only Activation-owned files, removes only the managed
    /// root-level `model_instructions_file` assignment, and finally removes
    /// the Activation Lock. Any error leaves this guard recoverable; dropping
    /// or rolling it back restores the frozen external and managed preimages.
    ///
    /// # Errors
    /// Fails when the handler is repeated, rollback scope is incomplete,
    /// Activation ownership or config semantics drift, or a write cannot be
    /// completed safely.
    pub fn apply_source_deactivation(&mut self) -> Result<Value, LifecycleError> {
        self.apply_source_deactivation_with(|_, _| Ok(()))
    }

    /// Run the trusted source-activation handler inside this transaction.
    ///
    /// Static assets and the shared Codex config are read only from the
    /// validated newly published package snapshot. `session_launcher` is the
    /// caller-supplied compatibility launcher payload; production routing must
    /// bind it to a released native executable before using this method.
    ///
    /// The handler requires an exact frozen rollback scope, validates all old
    /// owned and unmanaged preimages before writing, creates profiles only
    /// when missing, publishes every replacement through private scratch with
    /// no-replace renames, and writes the new Activation Lock last.
    ///
    /// # Errors
    /// Fails when rollback evidence is missing, source assets or preimages
    /// drift, an unmanaged destination differs, or publication cannot complete
    /// safely.
    pub fn apply_source_activation(
        &mut self,
        session_launcher: &[u8],
    ) -> Result<Value, LifecycleError> {
        self.apply_source_activation_with(session_launcher, |_, _| Ok(()))
    }

    fn apply_source_activation_with(
        &mut self,
        session_launcher: &[u8],
        handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        if self.external_mutation_state != ExternalMutationState::Preserved {
            return invalid("external mutation has already started");
        }
        let (prepared, target, target_path, scratch, scratch_path) = {
            let workspace = self.workspace()?;
            workspace.verify_published_install(&self.plan)?;
            let target = workspace.target_directory()?;
            let target_path = workspace.target().to_path_buf();
            let scratch = workspace.handler_scratch_directory()?;
            let scratch_path = workspace.handler_scratch_path();
            let prepared = source_activation::SourceActivation::prepare(
                &target,
                workspace.target(),
                session_launcher,
            )?;
            workspace.require_rollback_external_paths(prepared.scope())?;
            prepared.revalidate(&target)?;
            (prepared, target, target_path, scratch, scratch_path)
        };
        self.external_mutation_started = true;
        self.external_mutation_state = ExternalMutationState::InProgress;
        let result = prepared.apply_with_hook(
            &target,
            &target_path,
            &scratch,
            &scratch_path,
            handler_hook,
        )?;
        self.external_mutation_state = ExternalMutationState::ActivationApplied;
        Ok(result)
    }

    fn apply_source_deactivation_with(
        &mut self,
        handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        if self.external_mutation_state != ExternalMutationState::Preserved {
            return invalid("external mutation has already started");
        }
        let (prepared, target, scratch) = {
            let workspace = self.workspace()?;
            workspace.verify_published_install(&self.plan)?;
            let target = workspace.target_directory()?;
            let scratch = workspace.handler_scratch_directory()?;
            let prepared =
                source_activation::SourceDeactivation::prepare(&target, workspace.target())?;
            workspace.require_rollback_external_paths(prepared.scope())?;
            prepared.revalidate(&target)?;
            (prepared, target, scratch)
        };
        self.external_mutation_started = true;
        self.external_mutation_state = ExternalMutationState::InProgress;
        let result = prepared.apply_with_hook(&target, &scratch, handler_hook)?;
        self.external_mutation_state = ExternalMutationState::ActivationRemoved;
        Ok(result)
    }

    #[cfg(test)]
    pub(crate) fn apply_source_deactivation_with_test_hook(
        &mut self,
        handler_hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<Value, LifecycleError> {
        self.apply_source_deactivation_with(handler_hook)
    }

    /// Finalize the published installation and discard the recovery backup.
    ///
    /// Final verification runs before cleanup. A verification failure triggers
    /// automatic rollback; incomplete rollback preserves the backup.
    ///
    /// # Errors
    /// Fails when verification, rollback, cleanup, or lock release is
    /// incomplete.
    pub fn commit(mut self, plan: &ValidatedInstallPlan) -> Result<(), LifecycleError> {
        let workspace = self.take_workspace()?;
        if let Err(error) = verify_published_roots(&workspace, &self.roots)
            .and_then(|()| self.verify_published_state(&workspace, plan))
            .and_then(|()| verify_published_roots(&workspace, &self.roots))
        {
            return Err(abort_workspace(
                workspace,
                &mut self.roots,
                &self.plan,
                self.external_mutation_started,
                error,
            ));
        }
        workspace.cleanup()
    }

    /// Restore the previous managed roots, or remove newly published roots for
    /// a fresh install, then clean the transaction workspace.
    ///
    /// # Errors
    /// Fails closed when a published root changed or recovery/cleanup cannot
    /// complete. Recovery failures preserve the backup.
    pub fn rollback(mut self) -> Result<(), LifecycleError> {
        let workspace = self.take_workspace()?;
        let recovery_errors = recover_transaction(
            &workspace,
            &mut self.roots,
            &self.plan,
            self.external_mutation_started,
        );
        if recovery_errors.is_empty() {
            workspace.cleanup()
        } else {
            Err(preserve_incomplete_recovery(
                workspace,
                "published install rollback",
                &recovery_errors,
            ))
        }
    }

    #[cfg(test)]
    pub(crate) fn rollback_with_hook(
        mut self,
        hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
    ) -> Result<(), LifecycleError> {
        let workspace = self.take_workspace()?;
        let recovery_errors = recover_transaction_with_hook(
            &workspace,
            &mut self.roots,
            &self.plan,
            self.external_mutation_started,
            hook,
        );
        if recovery_errors.is_empty() {
            workspace.cleanup()
        } else {
            Err(preserve_incomplete_recovery(
                workspace,
                "published install rollback",
                &recovery_errors,
            ))
        }
    }

    fn workspace(&self) -> Result<&LifecycleWorkspace, LifecycleError> {
        self.workspace
            .as_ref()
            .ok_or_else(|| LifecycleError::Invalid("published install is inactive".to_owned()))
    }

    fn verify_published_state(
        &self,
        workspace: &LifecycleWorkspace,
        plan: &ValidatedInstallPlan,
    ) -> Result<(), LifecycleError> {
        match self.external_mutation_state {
            ExternalMutationState::Preserved => workspace.verify_published_install(plan),
            ExternalMutationState::ActivationApplied => workspace
                .verify_published_after_handler(plan, external_stage::PublishedActivation::Managed),
            ExternalMutationState::ActivationRemoved => workspace
                .verify_published_after_handler(plan, external_stage::PublishedActivation::Absent),
            ExternalMutationState::InProgress => {
                invalid("external handler did not complete successfully")
            }
        }
    }

    #[cfg(test)]
    pub(crate) fn run_external_mutation_with<T>(
        &mut self,
        mutation: impl FnOnce(&Path) -> Result<T, LifecycleError>,
    ) -> Result<T, LifecycleError> {
        let target = {
            let workspace = self.workspace()?;
            workspace.verify_published_install(&self.plan)?;
            if !workspace.has_staged_rollback_point() {
                return invalid("external mutation requires a verified rollback point");
            }
            workspace.target().to_path_buf()
        };
        if self.external_mutation_started {
            return invalid("external mutation has already started");
        }
        self.external_mutation_started = true;
        self.external_mutation_state = ExternalMutationState::InProgress;
        let result = mutation(&target)?;
        self.external_mutation_state = ExternalMutationState::Preserved;
        Ok(result)
    }

    fn take_workspace(&mut self) -> Result<LifecycleWorkspace, LifecycleError> {
        self.workspace
            .take()
            .ok_or_else(|| LifecycleError::Invalid("published install is inactive".to_owned()))
    }
}

impl Drop for PublishedInstall {
    fn drop(&mut self) {
        let Some(workspace) = self.workspace.take() else {
            return;
        };
        let recovery_errors = recover_transaction(
            &workspace,
            &mut self.roots,
            &self.plan,
            self.external_mutation_started,
        );
        if recovery_errors.is_empty() {
            let _ = workspace.cleanup();
        } else {
            let _ = workspace.preserve_backup();
        }
    }
}

impl PublishedUninstall {
    /// Return the immutable publication report.
    #[must_use]
    pub fn report(&self) -> &Value {
        &self.report
    }

    /// Revalidate the published uninstall and its complete recovery evidence.
    ///
    /// # Errors
    /// Fails when any final namespace, external result, preserved system Skill,
    /// managed backup, or rollback point differs from the frozen transaction.
    pub fn verify(&self) -> Result<(), LifecycleError> {
        verify_published_uninstall(
            self.workspace()?,
            &self.roots,
            self.system_skills.as_ref(),
            self.deactivation.as_ref(),
        )
    }

    /// Commit the uninstall and discard the private managed/rollback backups.
    ///
    /// # Errors
    /// A final verification failure triggers the same identity-bound rollback.
    /// Incomplete recovery preserves the workspace and returns its path.
    pub fn commit(mut self) -> Result<Value, LifecycleError> {
        let workspace = self.take_workspace()?;
        if let Err(error) = verify_published_uninstall(
            &workspace,
            &self.roots,
            self.system_skills.as_ref(),
            self.deactivation.as_ref(),
        ) {
            return Err(abort_uninstall(
                workspace,
                &mut self.roots,
                &mut self.system_skills,
                self.external_mutation_started,
                error,
            ));
        }
        let mut report = self.report.clone();
        report["status"] = Value::String("uninstalled".to_owned());
        workspace.cleanup()?;
        Ok(report)
    }

    /// Restore the complete pre-uninstall managed and external state.
    ///
    /// # Errors
    /// Fails closed when a namespace or object identity changed. Incomplete
    /// recovery preserves the private workspace for manual recovery.
    pub fn rollback(mut self) -> Result<(), LifecycleError> {
        let workspace = self.take_workspace()?;
        let errors = recover_uninstall(
            &workspace,
            &mut self.roots,
            &mut self.system_skills,
            self.external_mutation_started,
        );
        if errors.is_empty() {
            workspace.cleanup()
        } else {
            Err(preserve_incomplete_recovery(
                workspace,
                "published uninstall rollback",
                &errors,
            ))
        }
    }

    fn workspace(&self) -> Result<&LifecycleWorkspace, LifecycleError> {
        self.workspace
            .as_ref()
            .ok_or_else(|| LifecycleError::Invalid("published uninstall is inactive".to_owned()))
    }

    fn take_workspace(&mut self) -> Result<LifecycleWorkspace, LifecycleError> {
        self.workspace
            .take()
            .ok_or_else(|| LifecycleError::Invalid("published uninstall is inactive".to_owned()))
    }
}

impl Drop for PublishedUninstall {
    fn drop(&mut self) {
        let Some(workspace) = self.workspace.take() else {
            return;
        };
        let errors = recover_uninstall(
            &workspace,
            &mut self.roots,
            &mut self.system_skills,
            self.external_mutation_started,
        );
        if errors.is_empty() {
            let _ = workspace.cleanup();
        } else {
            let _ = workspace.preserve_recovery_workspace();
        }
    }
}

fn abort_uninstall(
    workspace: LifecycleWorkspace,
    roots: &mut [RemovalRootMove],
    system_skills: &mut Option<PreservedSystemSkills>,
    external_mutation_started: bool,
    primary: LifecycleError,
) -> LifecycleError {
    let errors = recover_uninstall(&workspace, roots, system_skills, external_mutation_started);
    if errors.is_empty() {
        return match workspace.cleanup() {
            Ok(()) => primary,
            Err(cleanup) => LifecycleError::Invalid(format!(
                "managed uninstall failed ({primary}); workspace cleanup is incomplete: {cleanup}"
            )),
        };
    }
    preserve_incomplete_recovery(
        workspace,
        &format!("managed uninstall failed ({primary})"),
        &errors,
    )
}

fn recover_uninstall(
    workspace: &LifecycleWorkspace,
    roots: &mut [RemovalRootMove],
    system_skills: &mut Option<PreservedSystemSkills>,
    restore_external: bool,
) -> Vec<String> {
    let mut errors = Vec::new();
    if let Some(system) = system_skills.as_mut()
        && let Err(error) = recover_system_skills(workspace, system)
    {
        errors.push(format!("restore skills/.system to managed backup: {error}"));
        return errors;
    }
    if roots.iter().all(|root| root.backed_up)
        && let Err(error) = workspace.verify_recovery_backup()
    {
        errors.push(format!("verify uninstall recovery backup: {error}"));
        return errors;
    }
    errors.extend(restore_removal_roots(workspace, roots));
    if !errors.is_empty() {
        return errors;
    }
    if restore_external && let Err(error) = workspace.restore_uninstall_external_state() {
        errors.push(format!("restore uninstall external state: {error}"));
        return errors;
    }
    if let Err(error) = workspace.verify_restored_install() {
        errors.push(format!("verify restored uninstall source: {error}"));
    }
    errors
}

fn abort_workspace(
    workspace: LifecycleWorkspace,
    roots: &mut [RootMove],
    plan: &ValidatedInstallPlan,
    external_mutation_started: bool,
    primary: LifecycleError,
) -> LifecycleError {
    let recovery_errors = recover_transaction(&workspace, roots, plan, external_mutation_started);
    if recovery_errors.is_empty() {
        return match workspace.cleanup() {
            Ok(()) => primary,
            Err(cleanup) => LifecycleError::Invalid(format!(
                "managed-root publication failed ({primary}); workspace cleanup is incomplete: {cleanup}"
            )),
        };
    }
    preserve_incomplete_recovery(
        workspace,
        &format!("managed-root publication failed ({primary})"),
        &recovery_errors,
    )
}

fn preserve_incomplete_recovery(
    workspace: LifecycleWorkspace,
    operation: &str,
    recovery_errors: &[String],
) -> LifecycleError {
    let stage = workspace.stage_path();
    let backup = workspace.backup_path();
    match workspace.preserve_recovery_workspace() {
        Ok((stage, backup)) => LifecycleError::Invalid(format!(
            "{operation}; recovery incomplete; backup preserved at {}; stage preserved at {}: {}",
            backup.display(),
            stage.display(),
            recovery_errors.join("; ")
        )),
        Err(error) => LifecycleError::Invalid(format!(
            "{operation}; recovery incomplete; workspace preservation at stage {} and backup {} is also incomplete: {}; {}",
            stage.display(),
            backup.display(),
            recovery_errors.join("; "),
            error
        )),
    }
}

fn recover_transaction(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    plan: &ValidatedInstallPlan,
    external_mutation_started: bool,
) -> Vec<String> {
    recover_transaction_with_hook(workspace, roots, plan, external_mutation_started, |_, _| {
        Ok(())
    })
}

fn recover_transaction_with_hook(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    plan: &ValidatedInstallPlan,
    external_mutation_started: bool,
    hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Vec<String> {
    if external_mutation_started
        && let Err(error) = verify_published_roots(workspace, roots)
            .and_then(|()| workspace.verify_published_during_handler(plan))
            .and_then(|()| verify_published_roots(workspace, roots))
    {
        return vec![format!(
            "verify transaction before external lifecycle recovery: {error}"
        )];
    }
    recover_roots_with_hook(workspace, roots, plan, external_mutation_started, hook)
}

fn restore_staged_external_state(workspace: &LifecycleWorkspace) -> Result<(), LifecycleError> {
    workspace.restore_uninstall_external_state()
}

fn recover_roots_with_hook(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    plan: &ValidatedInstallPlan,
    restore_external: bool,
    mut hook: impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Vec<String> {
    let mut errors = Vec::new();
    let has_previous = roots.iter().any(|moved| moved.previous_identity.is_some());
    let fully_published = !roots.is_empty() && roots.iter().all(|moved| moved.published);
    let complete_backup = has_previous
        && roots
            .iter()
            .filter(|moved| moved.previous_identity.is_some())
            .all(|moved| moved.backed_up);
    if complete_backup && let Err(error) = workspace.verify_recovery_backup() {
        errors.push(format!("verify recovery backup: {error}"));
        return errors;
    }

    errors.extend(unpublish_new_roots(workspace, roots, &mut hook));
    if !errors.is_empty() || !has_previous {
        return errors;
    }
    if complete_backup && let Err(error) = workspace.verify_recovery_backup() {
        errors.push(format!("verify recovery backup before restore: {error}"));
        if fully_published {
            errors.extend(reinstate_after_failed_recovery(
                workspace,
                roots,
                plan,
                restore_external,
            ));
        }
        return errors;
    }
    errors.extend(restore_previous_roots(workspace, roots, &mut hook));
    let mut external_recovery_started = false;
    if errors.is_empty() && restore_external {
        external_recovery_started = true;
        if let Err(error) = restore_staged_external_state(workspace) {
            errors.push(format!("restore external lifecycle state: {error}"));
        }
    }
    if errors.is_empty()
        && let Err(error) = workspace.verify_restored_install()
    {
        errors.push(format!("verify restored managed installation: {error}"));
    }
    if errors.is_empty() || !fully_published || !complete_backup || external_recovery_started {
        return errors;
    }

    let primary = errors.join("; ");
    let reinstate_errors = reinstate_published_roots(workspace, roots, plan, restore_external);
    if reinstate_errors.is_empty() {
        vec![format!(
            "{primary}; original publication was reinstated after failed recovery"
        )]
    } else {
        errors.extend(reinstate_errors);
        errors
    }
}

fn unpublish_new_roots(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Vec<String> {
    let mut errors = Vec::new();
    for moved in roots.iter_mut().rev() {
        if moved.published {
            let stage = match workspace.stage_directory() {
                Ok(stage) => stage,
                Err(error) => {
                    errors.push(format!("restore staged {}: {error}", moved.root.name));
                    continue;
                }
            };
            if let Err(error) = prepare_bound_move(
                workspace.target_directory_cap(),
                moved.root,
                &moved.staged_identity,
                stage,
                "published managed root",
                "restored staged root",
            )
            .and_then(|()| {
                rename_bound_root(
                    workspace.target_directory_cap(),
                    workspace.target(),
                    moved.root,
                    stage,
                    &workspace.stage_path(),
                    "published managed root",
                    "restored staged root",
                )
            }) {
                errors.push(format!("remove published {}: {error}", moved.root.name));
                continue;
            }
            moved.published = false;
            if let Err(error) = hook(moved.root.name, "unpublish-after-rename").and_then(|()| {
                verify_bound_move(
                    workspace.target_directory_cap(),
                    moved.root,
                    &moved.staged_identity,
                    stage,
                    "published managed root",
                    "restored staged root",
                )
            }) {
                errors.push(format!("remove published {}: {error}", moved.root.name));
            }
        }
    }
    errors
}

fn restore_previous_roots(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    hook: &mut impl FnMut(&str, &str) -> Result<(), LifecycleError>,
) -> Vec<String> {
    let mut errors = Vec::new();
    for moved in roots.iter_mut().rev() {
        if moved.backed_up {
            let Some(previous) = moved.previous_identity.as_ref() else {
                errors.push(format!(
                    "restore {}: recovery identity is missing",
                    moved.root.name
                ));
                continue;
            };
            let backup = match workspace.backup_directory() {
                Ok(backup) => backup,
                Err(error) => {
                    errors.push(format!("restore {}: {error}", moved.root.name));
                    continue;
                }
            };
            if let Err(error) = hook(moved.root.name, "restore")
                .and_then(|()| {
                    prepare_bound_move(
                        backup,
                        moved.root,
                        previous,
                        workspace.target_directory_cap(),
                        "recovery backup root",
                        "restored managed root",
                    )
                })
                .and_then(|()| {
                    rename_bound_root(
                        backup,
                        &workspace.backup_path(),
                        moved.root,
                        workspace.target_directory_cap(),
                        workspace.target(),
                        "recovery backup root",
                        "restored managed root",
                    )
                })
            {
                errors.push(format!("restore {}: {error}", moved.root.name));
                break;
            }
            moved.backed_up = false;
            if let Err(error) = hook(moved.root.name, "restore-after-rename").and_then(|()| {
                verify_bound_move(
                    backup,
                    moved.root,
                    previous,
                    workspace.target_directory_cap(),
                    "recovery backup root",
                    "restored managed root",
                )
            }) {
                errors.push(format!("restore {}: {error}", moved.root.name));
                break;
            }
        }
    }
    errors
}

fn reinstate_after_failed_recovery(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    plan: &ValidatedInstallPlan,
    external_mutation_started: bool,
) -> Vec<String> {
    let mut errors = Vec::new();
    let reinstate_errors =
        reinstate_published_roots(workspace, roots, plan, external_mutation_started);
    if reinstate_errors.is_empty() {
        errors.push("original publication was reinstated after failed recovery".to_owned());
    } else {
        errors.extend(reinstate_errors);
    }
    errors
}

fn reinstate_published_roots(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
    plan: &ValidatedInstallPlan,
    external_mutation_started: bool,
) -> Vec<String> {
    let verified = if external_mutation_started {
        workspace.verify_reinstatement_stage_during_handler(plan)
    } else {
        workspace.verify_reinstatement_stage(plan)
    };
    if let Err(error) = verified {
        return vec![format!(
            "verify staged publication before failed-recovery reinstatement: {error}"
        )];
    }
    let mut errors = return_restored_roots_to_backup(workspace, roots);
    if errors.is_empty() {
        errors.extend(republish_staged_roots(workspace, roots));
    }
    if errors.is_empty()
        && let Err(error) =
            verify_reinstated_publication(workspace, roots, plan, external_mutation_started)
    {
        errors.push(format!(
            "verify reinstated publication after failed recovery: {error}"
        ));
    }
    errors
}

fn return_restored_roots_to_backup(
    workspace: &LifecycleWorkspace,
    roots: &mut [RootMove],
) -> Vec<String> {
    let mut errors = Vec::new();
    let backup = match workspace.backup_directory() {
        Ok(backup) => backup,
        Err(error) => return vec![format!("reopen recovery backup for reinstatement: {error}")],
    };

    for moved in roots.iter_mut() {
        if let Some(previous) = moved.previous_identity.as_ref()
            && !moved.backed_up
        {
            if let Err(error) = prepare_bound_move(
                workspace.target_directory_cap(),
                moved.root,
                previous,
                backup,
                "restored managed root",
                "reinstated recovery backup root",
            )
            .and_then(|()| {
                rename_bound_root(
                    workspace.target_directory_cap(),
                    workspace.target(),
                    moved.root,
                    backup,
                    &workspace.backup_path(),
                    "restored managed root",
                    "reinstated recovery backup root",
                )
            }) {
                errors.push(format!(
                    "return restored {} to recovery backup: {error}",
                    moved.root.name
                ));
                break;
            }
            moved.backed_up = true;
            if let Err(error) = verify_bound_move(
                workspace.target_directory_cap(),
                moved.root,
                previous,
                backup,
                "restored managed root",
                "reinstated recovery backup root",
            ) {
                errors.push(format!(
                    "verify returned {} recovery backup: {error}",
                    moved.root.name
                ));
                break;
            }
        }
    }
    errors
}

fn republish_staged_roots(workspace: &LifecycleWorkspace, roots: &mut [RootMove]) -> Vec<String> {
    let mut errors = Vec::new();
    let stage = match workspace.stage_directory() {
        Ok(stage) => stage,
        Err(error) => return vec![format!("reopen stage for reinstatement: {error}")],
    };
    for moved in roots.iter_mut() {
        if !moved.published {
            if let Err(error) = prepare_bound_move(
                stage,
                moved.root,
                &moved.staged_identity,
                workspace.target_directory_cap(),
                "restored staged root",
                "reinstated published root",
            )
            .and_then(|()| {
                rename_bound_root(
                    stage,
                    &workspace.stage_path(),
                    moved.root,
                    workspace.target_directory_cap(),
                    workspace.target(),
                    "restored staged root",
                    "reinstated published root",
                )
            }) {
                errors.push(format!(
                    "republish {} after failed recovery: {error}",
                    moved.root.name
                ));
                break;
            }
            moved.published = true;
            if let Err(error) = verify_bound_move(
                stage,
                moved.root,
                &moved.staged_identity,
                workspace.target_directory_cap(),
                "restored staged root",
                "reinstated published root",
            ) {
                errors.push(format!(
                    "verify republished {} after failed recovery: {error}",
                    moved.root.name
                ));
                break;
            }
        }
    }
    errors
}

fn verify_reinstated_publication(
    workspace: &LifecycleWorkspace,
    roots: &[RootMove],
    plan: &ValidatedInstallPlan,
    external_mutation_started: bool,
) -> Result<(), LifecycleError> {
    workspace.validate()?;
    for moved in roots {
        if !moved.published || (moved.previous_identity.is_some() && !moved.backed_up) {
            return invalid("reinstated managed-root state is incomplete");
        }
        require_identity(
            workspace.target_directory_cap(),
            moved.root,
            &moved.staged_identity,
            "reinstated published root",
        )?;
        require_absent(
            workspace.stage_directory()?,
            moved.root.name,
            "reinstated stage source",
        )?;
        if let Some(previous) = moved.previous_identity.as_ref() {
            require_identity(
                workspace.backup_directory()?,
                moved.root,
                previous,
                "reinstated recovery backup root",
            )?;
        }
    }
    if external_mutation_started {
        workspace.verify_published_during_handler(plan)?;
    } else {
        workspace.verify_published_install(plan)?;
    }
    workspace.validate()
}

fn verify_published_roots(
    workspace: &LifecycleWorkspace,
    roots: &[RootMove],
) -> Result<(), LifecycleError> {
    workspace.validate()?;
    for moved in roots {
        if !moved.published {
            return invalid("published managed-root state is incomplete");
        }
        require_identity(
            workspace.target_directory_cap(),
            moved.root,
            &moved.staged_identity,
            "published managed root",
        )?;
        require_absent(
            workspace.stage_directory()?,
            moved.root.name,
            "published stage source",
        )?;
        if let Some(previous) = moved.previous_identity.as_ref() {
            if !moved.backed_up {
                return invalid("published recovery-backup state is incomplete");
            }
            require_identity(
                workspace.backup_directory()?,
                moved.root,
                previous,
                "recovery backup root",
            )?;
        } else {
            require_absent(
                workspace.backup_directory()?,
                moved.root.name,
                "fresh-install recovery backup",
            )?;
        }
    }
    if roots.iter().any(|moved| moved.previous_identity.is_some()) {
        workspace.verify_recovery_backup()?;
    }
    workspace.validate()
}

fn prepare_bound_move(
    source: &Dir,
    root: ManagedRoot,
    identity: &Metadata,
    destination: &Dir,
    source_label: &str,
    destination_label: &str,
) -> Result<(), LifecycleError> {
    require_identity(source, root, identity, source_label)?;
    require_absent(destination, root.name, destination_label)
}

fn rename_bound_root(
    source: &Dir,
    source_path: &Path,
    root: ManagedRoot,
    destination: &Dir,
    destination_path: &Path,
    source_label: &str,
    destination_label: &str,
) -> Result<(), LifecycleError> {
    rename_no_replace(
        source,
        source_path,
        root.name,
        destination,
        destination_path,
        root.name,
    )
    .map_err(|error| {
        LifecycleError::Invalid(format!(
            "could not move {} from {source_label} to {destination_label}: {error}",
            root.name
        ))
    })
}

fn verify_bound_move(
    source: &Dir,
    root: ManagedRoot,
    identity: &Metadata,
    destination: &Dir,
    source_label: &str,
    destination_label: &str,
) -> Result<(), LifecycleError> {
    require_absent(source, root.name, source_label)?;
    require_identity(destination, root, identity, destination_label)
}

fn capture_required(
    parent: &Dir,
    root: ManagedRoot,
    label: &str,
) -> Result<Metadata, LifecycleError> {
    capture_optional(parent, root, label)?.ok_or_else(|| {
        LifecycleError::Invalid(format!("{label} is missing or unsafe: {}", root.name))
    })
}

fn capture_optional(
    parent: &Dir,
    root: ManagedRoot,
    label: &str,
) -> Result<Option<Metadata>, LifecycleError> {
    match parent.symlink_metadata(root.name) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
        Ok(metadata) if metadata.file_type().is_symlink() => {
            invalid(format!("{label} is missing or unsafe: {}", root.name))
        }
        Ok(metadata)
            if matches!(root.kind, RootKind::Directory) && !metadata.is_dir()
                || matches!(root.kind, RootKind::File) && !metadata.is_file() =>
        {
            invalid(format!("{label} has the wrong type: {}", root.name))
        }
        Ok(_) => {
            let opened = match root.kind {
                RootKind::Directory => {
                    open_child_directory(parent, root.name, Some(root.mode), label)?
                        .dir_metadata()?
                }
                RootKind::File => {
                    open_child_file(parent, root.name, root.mode, label)?.metadata()?
                }
            };
            Ok(Some(opened))
        }
    }
}

fn require_identity(
    parent: &Dir,
    root: ManagedRoot,
    expected: &Metadata,
    label: &str,
) -> Result<(), LifecycleError> {
    let current = capture_required(parent, root, label)?;
    if !same_object_cap(expected, &current) {
        return invalid(format!("{label} identity changed: {}", root.name));
    }
    Ok(())
}

fn require_absent(parent: &Dir, name: &str, label: &str) -> Result<(), LifecycleError> {
    match parent.symlink_metadata(name) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
        Ok(_) => invalid(format!("{label} is unexpectedly occupied: {name}")),
    }
}

fn require_empty(directory: &Dir, label: &str) -> Result<(), LifecycleError> {
    if directory.entries()?.next().transpose()?.is_some() {
        return invalid(format!("{label} is not empty"));
    }
    Ok(())
}

#[cfg(windows)]
fn verify_cleanup_ready(parent: &Dir, root: ManagedRoot) -> Result<(), LifecycleError> {
    use cap_fs_ext::DirExt as _;

    match root.kind {
        RootKind::Directory => {
            let directory = parent.open_dir_nofollow(root.name)?;
            verify_windows_cleanup_tree(&directory)
        }
        RootKind::File => verify_windows_cleanup_file(&parent.symlink_metadata(root.name)?),
    }
}

#[cfg(windows)]
fn verify_windows_cleanup_tree(directory: &Dir) -> Result<(), LifecycleError> {
    use cap_fs_ext::DirExt as _;

    for entry in directory.entries()? {
        let entry = entry?;
        let name = entry.file_name();
        let metadata = directory.symlink_metadata(&name)?;
        if metadata.file_type().is_symlink() {
            return invalid("managed root contains a symlink before Windows cleanup");
        }
        if metadata.is_dir() {
            let child = directory.open_dir_nofollow(&name)?;
            let opened = child.dir_metadata()?;
            if !same_object_cap(&metadata, &opened) {
                return invalid("managed root changed before Windows cleanup");
            }
            verify_windows_cleanup_tree(&child)?;
        } else if metadata.is_file() {
            verify_windows_cleanup_file(&metadata)?;
        } else {
            return invalid("managed root contains an unsupported Windows filesystem object");
        }
    }
    Ok(())
}

#[cfg(windows)]
fn verify_windows_cleanup_file(metadata: &Metadata) -> Result<(), LifecycleError> {
    use cap_fs_ext::MetadataExt as _;

    if metadata.permissions().readonly() && metadata.nlink() != 1 {
        return invalid("readonly managed file has aliases that make Windows cleanup unsafe");
    }
    Ok(())
}

#[cfg(not(windows))]
#[allow(clippy::unnecessary_wraps)]
fn verify_cleanup_ready(_parent: &Dir, _root: ManagedRoot) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(any(target_vendor = "apple", target_os = "linux"))]
pub(super) fn rename_no_replace(
    source: &Dir,
    _source_path: &Path,
    source_name: &str,
    destination: &Dir,
    _destination_path: &Path,
    destination_name: &str,
) -> std::io::Result<()> {
    use std::os::fd::AsFd as _;

    rustix::fs::renameat_with(
        source.as_fd(),
        source_name,
        destination.as_fd(),
        destination_name,
        rustix::fs::RenameFlags::NOREPLACE,
    )
    .map_err(Into::into)
}

#[cfg(windows)]
pub(super) fn rename_no_replace(
    source: &Dir,
    _source_path: &Path,
    source_name: &str,
    destination: &Dir,
    _destination_path: &Path,
    destination_name: &str,
) -> std::io::Result<()> {
    renamore::rename_exclusive(
        windows_directory_handle_path(source)?.join(source_name),
        windows_directory_handle_path(destination)?.join(destination_name),
    )
}

#[cfg(windows)]
fn windows_directory_handle_path(directory: &Dir) -> std::io::Result<PathBuf> {
    // Resolve from a cloned view of the held directory handle. `cap-std`
    // opens directories without FILE_SHARE_DELETE, so junction ancestors
    // cannot be replaced before the subsequent no-replace rename.
    let file = directory.try_clone()?.into_std_file();
    winx::file::get_file_path(&file)
}

#[cfg(not(any(target_vendor = "apple", target_os = "linux", windows)))]
pub(super) fn rename_no_replace(
    _source: &Dir,
    _source_path: &Path,
    _source_name: &str,
    _destination: &Dir,
    _destination_path: &Path,
    _destination_name: &str,
) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "atomic no-replace rename is unsupported on this platform",
    ))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}
