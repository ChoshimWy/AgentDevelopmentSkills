use super::{
    LifecycleError, LifecycleWorkspace, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    ValidatedInstallPlan, open_child_directory, open_child_file, same_object_cap,
};
use cap_std::fs::{Dir, Metadata};
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
    plan: ValidatedInstallPlan,
    roots: Vec<RootMove>,
    workspace: Option<LifecycleWorkspace>,
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
                plan: plan.clone(),
                roots,
                workspace: Some(self),
            }),
            Err(error) => Err(abort_workspace(self, &mut roots, plan, false, error)),
        }
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
        workspace.verify_published_install(plan)?;
        verify_published_roots(workspace, &self.roots)
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
            .and_then(|()| workspace.verify_published_install(plan))
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
        mutation(&target)
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
            .and_then(|()| workspace.verify_published_install(plan))
            .and_then(|()| verify_published_roots(workspace, roots))
    {
        return vec![format!(
            "verify transaction before external lifecycle recovery: {error}"
        )];
    }
    recover_roots_with_hook(workspace, roots, plan, external_mutation_started, hook)
}

fn restore_staged_external_state(workspace: &LifecycleWorkspace) -> Result<(), LifecycleError> {
    use std::ffi::OsStr;

    workspace.verify_staged_rollback_point()?;
    let managed = open_child_directory(
        workspace.stage_directory()?,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "restored staged managed metadata",
    )?;
    let rollback = open_child_directory(
        &managed,
        super::ROLLBACK_POINT_DIRECTORY,
        Some(MANAGED_DIRECTORY_MODE),
        "restored staged rollback point",
    )?;
    let quarantine = super::external_stage::create_directory(
        workspace.stage_directory()?,
        OsStr::new("external-recovery"),
        Some(0o700),
        "external recovery quarantine",
    )?;
    super::rollback::restore_external_state(
        &rollback,
        workspace.target_directory_cap(),
        workspace.target(),
        &quarantine,
        &workspace.stage_path().join("external-recovery"),
    )
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
            errors.extend(reinstate_after_failed_recovery(workspace, roots, plan));
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
    let reinstate_errors = reinstate_published_roots(workspace, roots, plan);
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
) -> Vec<String> {
    let mut errors = Vec::new();
    let reinstate_errors = reinstate_published_roots(workspace, roots, plan);
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
) -> Vec<String> {
    if let Err(error) = workspace.verify_reinstatement_stage(plan) {
        return vec![format!(
            "verify staged publication before failed-recovery reinstatement: {error}"
        )];
    }
    let mut errors = return_restored_roots_to_backup(workspace, roots);
    if errors.is_empty() {
        errors.extend(republish_staged_roots(workspace, roots));
    }
    if errors.is_empty()
        && let Err(error) = verify_reinstated_publication(workspace, roots, plan)
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
    workspace.verify_published_install(plan)?;
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
