use crate::{
    INSTALL_BACKUP_PREFIX, INSTALL_STAGE_PREFIX, LIFECYCLE_LOCK_DIRECTORY, LifecycleError,
    LifecycleLock, LifecycleWorkspace, UNINSTALL_BACKUP_PREFIX, ValidatedInstallPlan,
    compile_source_install_bundle, compile_upgrade_plan, ignored_os_metadata,
    inspect_upgrade_planning_snapshot, load_json_file, open_child_directory, open_root_directory,
    source_activation, source_bundle::SourceInstallBundle, source_install::SourceInstallSelection,
    source_packages::SourcePackageSet, staged_install, transaction_lock,
    upgrade_plan::semantic_lock_identity,
};
use agent_engine::validate_install_plan;
use cap_std::fs::Dir;
use serde_json::Value;
use std::path::Path;

const MANAGED_ROOTS: [&str; 3] = ["AGENTS.md", "skills", ".agent-skills"];

/// Exact package selection persisted by one valid installed Lock pair.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstalledSourceSelection {
    platforms: Vec<String>,
    disciplines: Vec<String>,
    runtime_configs: Vec<String>,
    install_plan_fingerprint: String,
    package_lock_hash: String,
}

impl InstalledSourceSelection {
    /// Installed platform package IDs.
    #[must_use]
    pub fn platforms(&self) -> &[String] {
        &self.platforms
    }

    /// Installed discipline package IDs.
    #[must_use]
    pub fn disciplines(&self) -> &[String] {
        &self.disciplines
    }

    /// Installed runtime configuration package IDs.
    #[must_use]
    pub fn runtime_configs(&self) -> &[String] {
        &self.runtime_configs
    }

    /// Whether the persisted selection contains only Core.
    #[must_use]
    pub const fn core_only(&self) -> bool {
        self.platforms.is_empty() && self.disciplines.is_empty() && self.runtime_configs.is_empty()
    }

    fn validate_lock_pair(
        &self,
        install_plan: &Value,
        package_lock: &Value,
    ) -> Result<(), LifecycleError> {
        let current = installed_source_selection(install_plan, package_lock)?;
        if &current != self {
            return invalid("installed Lock pair changed after source selection was inferred");
        }
        Ok(())
    }
}

/// Read the exact package selection from one valid installed Lock pair.
///
/// The target lifecycle lock is held while both Lockfiles are loaded,
/// cross-validated, and projected. No source checkout or candidate Schema is
/// consulted, so the result remains valid when an upgrade changes Schemas.
///
/// # Errors
/// Returns a fail-closed error for an unsafe target, recovery/lock conflict,
/// malformed or unanchored Lock pair, invalid selection, or target drift.
pub fn inspect_installed_source_selection(
    target_root: impl AsRef<Path>,
) -> Result<InstalledSourceSelection, LifecycleError> {
    let lock = LifecycleLock::acquire_existing(target_root)?;
    let target = lock.target_directory()?;
    let managed = open_child_directory(
        &target,
        ".agent-skills",
        Some(crate::MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let install_plan = load_json_file(
        &managed,
        "install-lock.json",
        crate::MANAGED_FILE_MODE,
        "current Install Lock",
    )?;
    let package_lock = load_json_file(
        &managed,
        crate::PERSISTENT_PACKAGE_LOCK,
        crate::MANAGED_FILE_MODE,
        "current package Lockfile",
    )?;
    if install_plan.get("status").and_then(Value::as_str) != Some("installed") {
        return invalid("source selection requires an installed Install Lock");
    }
    ValidatedInstallPlan::new(install_plan.clone(), package_lock.clone())?;
    let selection = installed_source_selection(&install_plan, &package_lock)?;
    lock.validate()?;
    Ok(selection)
}

/// Validate a fresh native source installation without writing the target.
///
/// The returned value is the existing planned Install Plan projection, matching
/// the Python source install dry-run contract. This compatibility slice
/// intentionally rejects replacement of an existing managed installation;
/// replacement must use the approval-bound upgrade lifecycle.
///
/// # Errors
/// Returns a fail-closed error for an unrelated bundle/snapshot pair, invalid
/// contracts, unsafe or occupied targets, or visible lifecycle recovery state.
pub fn inspect_source_install(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
) -> Result<Value, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    inspect_fresh_target(target_root.as_ref())?;
    Ok(bundle.plan().clone())
}

/// Validate a fresh native source installation and its Apple activation without target writes.
///
/// The managed candidate is assembled in an ephemeral private directory so the
/// same package/Skill and activation readers used by the mutating transaction
/// validate the preview. The requested target remains untouched. Non-Apple
/// selections return the same Install Plan with no activation projection.
///
/// # Errors
/// Returns a fail-closed error for the same invalid source, target, or
/// activation conflicts as the fresh installation path.
pub fn inspect_source_install_with_activation(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
    session_launcher: &[u8],
) -> Result<Value, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    let target_root = target_root.as_ref();
    let target_snapshot = transaction_lock::LifecycleTargetSnapshot::capture(target_root)?;
    let target_contract = target_snapshot
        .contract_target()
        .to_str()
        .ok_or_else(|| LifecycleError::Invalid("lifecycle target is not UTF-8".to_owned()))?
        .to_owned();
    let selected = selected_platforms(bundle.plan())?;
    if !selected.contains("apple") {
        if let Some(target) = target_snapshot.directory() {
            inspect_fresh_target_directory(target, false)?;
        }
        target_snapshot.validate()?;
        return Ok(serde_json::json!({
            "activation": Value::Null,
            "install_plan": bundle.plan(),
            "target_root": target_contract,
        }));
    }
    if session_launcher.is_empty() {
        return invalid("native Apple source install requires a frozen session launcher");
    }

    if let Some(target) = target_snapshot.directory() {
        inspect_fresh_target_directory(target, false)?;
    }
    let empty_destination = if target_snapshot.directory().is_none() {
        Some(tempfile::tempdir()?)
    } else {
        None
    };
    let temporary_destination = empty_destination
        .as_ref()
        .map(|directory| {
            open_root_directory(directory.path(), None, "native install preview destination")
        })
        .transpose()?;
    let destination = target_snapshot
        .directory()
        .or(temporary_destination.as_ref())
        .ok_or_else(|| {
            LifecycleError::Invalid("native install preview destination is unavailable".to_owned())
        })?;

    let temporary_stage = tempfile::tempdir()?;
    let stage = open_root_directory(temporary_stage.path(), None, "native install preview stage")?;
    let plan = ValidatedInstallPlan::new(bundle.plan().clone(), bundle.package_lock().clone())?;
    staged_install::stage_layout(&stage, &plan, bundle.instructions().as_bytes())?;
    for ((package_id, root), package) in bundle.package_roots().iter().zip(&package_set.packages) {
        if package_id != &package.id {
            return invalid("source package order differs from Install Bundle");
        }
        let package_source =
            open_root_directory(root, None, &format!("source package {package_id}"))?;
        staged_install::stage_package(&stage, &plan, package_id, &package_source)?;
        for skill in &package.skills {
            let skill_source = open_root_directory(
                &root.join(&skill.relative_root),
                None,
                &format!("source Skill {}", skill.name),
            )?;
            staged_install::stage_skill(&stage, &plan, &skill.name, &skill_source)?;
        }
    }
    staged_install::verify(&stage, &plan, staged_install::ExternalLayout::default())?;
    let target_path = Path::new(&target_contract);
    let activation = source_activation::SourceActivation::prepare_fresh(
        &stage,
        destination,
        target_path,
        session_launcher,
    )?;
    activation.revalidate_from(&stage, destination)?;
    if let Some(target) = target_snapshot.directory() {
        inspect_fresh_target_directory(target, false)?;
    }
    target_snapshot.validate()?;
    Ok(serde_json::json!({
        "activation": activation.preview(),
        "install_plan": bundle.plan(),
        "target_root": target_contract,
    }))
}

/// Stage, verify, atomically publish, and commit a fresh native source install.
///
/// Package and Skill sources are reopened through directory capabilities and
/// copied only when every file matches the Install Plan. All managed roots,
/// Lockfiles, preserved external state, and rebuilt semantics are verified
/// before and after publication. A failed or dropped publication removes the
/// new roots and retains recovery evidence if cleanup cannot complete.
///
/// This first production-shaped compatibility slice does not run source
/// activation and does not replace an existing install. Those operations
/// remain separate approval-bound lifecycle steps.
///
/// # Errors
/// Returns a fail-closed error for invalid contracts, source drift, unsafe
/// targets, occupied managed roots, staging disagreement, publication races,
/// or incomplete rollback/cleanup.
pub fn install_source_bundle(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
) -> Result<Value, LifecycleError> {
    Ok(install_source_bundle_with_options(
        bundle,
        package_set,
        target_root.as_ref(),
        false,
        None,
        |_| Ok(()),
    )?
    .install_plan)
}

/// Install and activate a fresh source bundle with one frozen native session launcher.
///
/// Apple selections stage the launcher and every external activation preimage
/// under the same rollback contract as the managed roots. Non-Apple selections
/// complete the managed installation without external activation. This entry
/// point remains fresh-only; replacement and legacy adoption must use the
/// approval-bound compatibility path.
///
/// # Errors
/// Returns a fail-closed error for missing Apple launcher bytes, invalid source
/// or target state, activation conflicts, publication drift, or incomplete
/// rollback/cleanup.
pub fn install_source_bundle_with_activation(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
    session_launcher: &[u8],
) -> Result<Value, LifecycleError> {
    let outcome = install_source_bundle_with_options(
        bundle,
        package_set,
        target_root.as_ref(),
        true,
        Some(session_launcher),
        |_| Ok(()),
    )?;
    Ok(serde_json::json!({
        "activation": outcome.activation,
        "install_plan": outcome.install_plan,
        "target_root": outcome.target_root,
    }))
}

/// Compile a source candidate with lineage anchored to the installed Lockfile.
///
/// A lineage-free candidate is compiled first. The current Install Lock and
/// persistent package Lockfile are then loaded and cross-validated under the
/// target lifecycle lock. Only a semantic change causes a second compilation
/// with the current Lockfile as `previous_lock`; no-change candidates retain
/// the required null lineage. The eventual Plan/apply path reacquires the lock
/// and rejects any target drift after this preparation snapshot.
///
/// # Errors
/// Returns a fail-closed error for invalid source snapshots, unsafe current
/// installation state, broken Lock anchors, target drift, or candidate
/// compilation failure.
pub fn compile_source_upgrade_bundle(
    selection: &SourceInstallSelection,
    package_set: &SourcePackageSet,
    schema_root: impl AsRef<Path>,
    target_root: impl AsRef<Path>,
) -> Result<SourceInstallBundle, LifecycleError> {
    compile_source_upgrade_bundle_with_selection_identity(
        selection,
        package_set,
        schema_root.as_ref(),
        target_root.as_ref(),
        None,
    )
}

/// Compile a source candidate only while the installed Lock pair still matches
/// the identity that supplied its package selection.
///
/// # Errors
/// Returns the same fail-closed errors as [`compile_source_upgrade_bundle`],
/// and additionally rejects any selection or Lock identity drift since
/// [`inspect_installed_source_selection`] returned.
pub fn compile_source_upgrade_bundle_bound(
    selection: &SourceInstallSelection,
    package_set: &SourcePackageSet,
    schema_root: impl AsRef<Path>,
    target_root: impl AsRef<Path>,
    expected_installed: &InstalledSourceSelection,
) -> Result<SourceInstallBundle, LifecycleError> {
    compile_source_upgrade_bundle_with_selection_identity(
        selection,
        package_set,
        schema_root.as_ref(),
        target_root.as_ref(),
        Some(expected_installed),
    )
}

fn compile_source_upgrade_bundle_with_selection_identity(
    selection: &SourceInstallSelection,
    package_set: &SourcePackageSet,
    schema_root: &Path,
    target_root: &Path,
    expected_installed: Option<&InstalledSourceSelection>,
) -> Result<SourceInstallBundle, LifecycleError> {
    let base = compile_source_install_bundle(selection, package_set, schema_root, None)?;
    let lock = LifecycleLock::acquire_existing(target_root)?;
    let target = lock.target_directory()?;
    let managed = open_child_directory(
        &target,
        ".agent-skills",
        Some(crate::MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let current_install_plan = load_json_file(
        &managed,
        "install-lock.json",
        crate::MANAGED_FILE_MODE,
        "current Install Lock",
    )?;
    let current_package_lock = load_json_file(
        &managed,
        crate::PERSISTENT_PACKAGE_LOCK,
        crate::MANAGED_FILE_MODE,
        "current package Lockfile",
    )?;
    if current_install_plan.get("status").and_then(Value::as_str) != Some("installed") {
        return invalid("source upgrade requires an installed Install Lock");
    }
    ValidatedInstallPlan::new(current_install_plan.clone(), current_package_lock.clone())?;
    if let Some(expected) = expected_installed {
        expected.validate_lock_pair(&current_install_plan, &current_package_lock)?;
    }
    lock.validate()?;
    if semantic_lock_identity(base.package_lock())?
        == semantic_lock_identity(&current_package_lock)?
    {
        return Ok(base);
    }
    let candidate = compile_source_install_bundle(
        selection,
        package_set,
        schema_root,
        Some(&current_package_lock),
    )?;
    let reloaded_install_plan = load_json_file(
        &managed,
        "install-lock.json",
        crate::MANAGED_FILE_MODE,
        "current Install Lock",
    )?;
    let reloaded_package_lock = load_json_file(
        &managed,
        crate::PERSISTENT_PACKAGE_LOCK,
        crate::MANAGED_FILE_MODE,
        "current package Lockfile",
    )?;
    if reloaded_install_plan != current_install_plan
        || reloaded_package_lock != current_package_lock
    {
        return invalid("installed Lock pair changed while compiling source upgrade");
    }
    lock.validate()?;
    Ok(candidate)
}

/// Compile a source Upgrade Plan only if the current Lock pair still matches
/// the identity that supplied the candidate selection.
///
/// # Errors
/// Returns the same fail-closed errors as [`inspect_source_upgrade`], and
/// additionally rejects installed selection or Lock identity drift.
#[allow(clippy::too_many_arguments)]
pub fn inspect_source_upgrade_bound(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
    conformance_evidence: &Value,
    action: &str,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
    session_launcher: Option<&[u8]>,
    expected_installed: &InstalledSourceSelection,
) -> Result<Value, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    let snapshot = inspect_upgrade_planning_snapshot(
        target_root,
        bundle.plan(),
        bundle.package_lock(),
        action,
        removed_platforms,
        removed_runtime_configs,
        session_launcher,
    )?;
    expected_installed.validate_lock_pair(
        &snapshot.current_install_plan,
        &snapshot.current_package_lock,
    )?;
    compile_upgrade_plan(
        &snapshot,
        action,
        bundle.plan(),
        bundle.package_lock(),
        conformance_evidence,
        removed_platforms,
        removed_runtime_configs,
    )
}

/// Compile a native source Upgrade Plan without mutating the installed target.
///
/// The target lifecycle lock is held from current-state inspection through
/// receipt validation and Plan compilation, then released before returning.
///
/// # Errors
/// Returns a fail-closed error for source drift, unsafe current state, stale
/// evidence, invalid ownership/removal policy, or an unbound external scope.
#[allow(clippy::too_many_arguments)]
pub fn inspect_source_upgrade(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
    conformance_evidence: &Value,
    action: &str,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
    session_launcher: Option<&[u8]>,
) -> Result<Value, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    let snapshot = inspect_upgrade_planning_snapshot(
        target_root,
        bundle.plan(),
        bundle.package_lock(),
        action,
        removed_platforms,
        removed_runtime_configs,
        session_launcher,
    )?;
    compile_upgrade_plan(
        &snapshot,
        action,
        bundle.plan(),
        bundle.package_lock(),
        conformance_evidence,
        removed_platforms,
        removed_runtime_configs,
    )
}

/// Apply one exact approval-bound native source upgrade transaction.
///
/// The current target, candidate source snapshot, Conformance evidence,
/// external lifecycle scope, rollback point and supplied approvals are rebuilt
/// and compared with `approved_plan` while the target lock is held. A changed
/// candidate is staged and published through [`LifecycleWorkspace`], with the
/// approved trusted source handler applied before final verification and
/// commit. A no-change Plan returns without staging or target writes.
///
/// # Errors
/// Returns a fail-closed error for stale approval, source/target drift,
/// incomplete staging, handler mismatch, publication failure, or rollback
/// cleanup failure.
#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
pub fn upgrade_source_bundle(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
    conformance_evidence: &Value,
    approved_plan: &Value,
    approvals: &[String],
    action: &str,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
    session_launcher: Option<&[u8]>,
) -> Result<Value, LifecycleError> {
    upgrade_source_bundle_with_smoke(
        bundle,
        package_set,
        target_root,
        conformance_evidence,
        approved_plan,
        approvals,
        action,
        removed_platforms,
        removed_runtime_configs,
        session_launcher,
        |published| {
            crate::installed_smoke::run_installed_workflow_smoke(
                published.target()?,
                bundle.package_lock(),
            )
            .map(|_| ())
        },
        |published, launcher| published.apply_source_activation(launcher).map(|_| ()),
        |published| published.apply_source_deactivation().map(|_| ()),
    )
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
fn upgrade_source_bundle_with_smoke(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: impl AsRef<Path>,
    conformance_evidence: &Value,
    approved_plan: &Value,
    approvals: &[String],
    action: &str,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
    session_launcher: Option<&[u8]>,
    mut before_source_activation: impl FnMut(&crate::PublishedInstall) -> Result<(), LifecycleError>,
    mut apply_source_activation: impl FnMut(
        &mut crate::PublishedInstall,
        &[u8],
    ) -> Result<(), LifecycleError>,
    mut apply_source_deactivation: impl FnMut(
        &mut crate::PublishedInstall,
    ) -> Result<(), LifecycleError>,
) -> Result<Value, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    let snapshot = inspect_upgrade_planning_snapshot(
        target_root,
        bundle.plan(),
        bundle.package_lock(),
        action,
        removed_platforms,
        removed_runtime_configs,
        session_launcher,
    )?;
    let compiled = compile_upgrade_plan(
        &snapshot,
        action,
        bundle.plan(),
        bundle.package_lock(),
        conformance_evidence,
        removed_platforms,
        removed_runtime_configs,
    )?;
    if &compiled != approved_plan {
        return invalid("upgrade apply requires the exact approved Plan");
    }
    require_exact_upgrade_approvals(&compiled, approvals)?;
    let evidence_fingerprint = required_string(
        conformance_evidence,
        "fingerprint",
        "upgrade Conformance evidence",
    )?;
    let plan_fingerprint = required_string(&compiled, "fingerprint", "Upgrade Plan")?;
    if compiled.get("status").and_then(Value::as_str) == Some("no-change") {
        return Ok(serde_json::json!({
            "conformance_evidence_fingerprint": evidence_fingerprint,
            "plan_fingerprint": plan_fingerprint,
            "status": "no-change",
        }));
    }

    let handler = snapshot.handler.clone();
    let external_paths = snapshot.external_paths.clone();
    let mut workspace = snapshot.into_workspace()?;
    let plan = ValidatedInstallPlan::new(bundle.plan().clone(), bundle.package_lock().clone())?;
    stage_source_bundle(&mut workspace, &plan, bundle, package_set)?;
    workspace.stage_external_state(&plan)?;
    let rollback_fingerprint = workspace.stage_rollback_point(&plan, &external_paths)?;
    if compiled
        .pointer("/rollback/point_fingerprint")
        .and_then(Value::as_str)
        != Some(rollback_fingerprint.as_str())
    {
        return invalid("staged rollback point differs from the approved Upgrade Plan");
    }
    workspace.verify_staged_install(&plan)?;
    let mut published = workspace.publish_staged_install(&plan)?;
    match handler.as_str() {
        "none" | "core.source-preserve.apple-codex-v1" => {}
        "core.source-activation.apple-codex-v1" => {
            before_source_activation(&published)?;
            apply_source_activation(
                &mut published,
                session_launcher.ok_or_else(|| {
                    LifecycleError::Invalid(
                        "native Apple source upgrade requires a frozen session launcher".to_owned(),
                    )
                })?,
            )?;
        }
        "core.source-deactivation.apple-codex-v1" => {
            apply_source_deactivation(&mut published)?;
        }
        _ => return invalid("approved Upgrade Plan contains an unknown source handler"),
    }
    published.verify(&plan)?;
    published.commit(&plan)?;
    Ok(serde_json::json!({
        "conformance_evidence_fingerprint": evidence_fingerprint,
        "install_plan_fingerprint": plan.fingerprint(),
        "package_lock_hash": plan.package_lock_fingerprint(),
        "plan_fingerprint": plan_fingerprint,
        "rollback_point": compiled.get("rollback").cloned().ok_or_else(|| {
            LifecycleError::Invalid("approved Upgrade Plan rollback identity is missing".to_owned())
        })?,
        "status": "upgraded",
    }))
}

#[derive(Debug)]
struct SourceInstallOutcome {
    activation: Option<Value>,
    install_plan: Value,
    target_root: String,
}

fn stage_source_bundle(
    workspace: &mut LifecycleWorkspace,
    plan: &ValidatedInstallPlan,
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
) -> Result<(), LifecycleError> {
    workspace.stage_install_layout(plan, bundle.instructions().as_bytes())?;
    for ((package_id, root), package) in bundle.package_roots().iter().zip(&package_set.packages) {
        if package_id != &package.id {
            return invalid("source package order differs from Install Bundle");
        }
        let package_source =
            open_root_directory(root, None, &format!("source package {package_id}"))?;
        workspace.stage_plan_package(plan, package_id, &package_source)?;
        for skill in &package.skills {
            let skill_source = open_root_directory(
                &root.join(&skill.relative_root),
                None,
                &format!("source Skill {}", skill.name),
            )?;
            workspace.stage_plan_skill(plan, &skill.name, &skill_source)?;
        }
    }
    Ok(())
}

fn require_exact_upgrade_approvals(
    plan: &Value,
    approvals: &[String],
) -> Result<(), LifecycleError> {
    let mut supplied = approvals.to_vec();
    supplied.sort();
    supplied.dedup();
    let required = plan
        .get("approvals_required")
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid("Upgrade Plan approvals are invalid".to_owned()))?
        .iter()
        .map(|value| {
            value.as_str().map(str::to_owned).ok_or_else(|| {
                LifecycleError::Invalid("Upgrade Plan approvals are invalid".to_owned())
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    if supplied != required {
        return invalid("upgrade permission approvals differ from the approved Plan");
    }
    Ok(())
}

fn required_string<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} {field} is invalid")))
}

fn install_source_bundle_with_options(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
    target_root: &Path,
    activate_source: bool,
    session_launcher: Option<&[u8]>,
    mut before_lock: impl FnMut(&Path) -> Result<(), LifecycleError>,
) -> Result<SourceInstallOutcome, LifecycleError> {
    validate_source_install_inputs(bundle, package_set)?;
    let activate_apple = activate_source && selected_platforms(bundle.plan())?.contains("apple");
    if activate_apple && session_launcher.is_none() {
        return invalid("native Apple source install requires a frozen session launcher");
    }
    let expected_contract_target = transaction_lock::normalize_lifecycle_target(target_root)?;
    let expected_contract_target = expected_contract_target
        .to_str()
        .ok_or_else(|| LifecycleError::Invalid("lifecycle target is not UTF-8".to_owned()))?
        .to_owned();
    before_lock(target_root)?;
    let lock = LifecycleLock::acquire(target_root)?;
    if lock.contract_target().to_str() != Some(expected_contract_target.as_str()) {
        return invalid("lifecycle target changed while acquiring install transaction");
    }
    let locked_target = lock.target_directory()?;
    inspect_fresh_target_directory(&locked_target, true)?;
    let plan = ValidatedInstallPlan::new(bundle.plan().clone(), bundle.package_lock().clone())?;
    let mut installed = bundle.plan().clone();
    installed
        .as_object_mut()
        .ok_or_else(|| LifecycleError::Invalid("Install Plan must be an object".to_owned()))?
        .insert("status".to_owned(), Value::String("installed".to_owned()));
    validate_install_plan(&installed)?;

    let mut workspace = LifecycleWorkspace::from_lock(lock)?;
    let contract_target = expected_contract_target;
    workspace.stage_install_layout(&plan, bundle.instructions().as_bytes())?;
    for ((package_id, root), package) in bundle.package_roots().iter().zip(&package_set.packages) {
        if package_id != &package.id {
            return invalid("source package order differs from Install Bundle");
        }
        let package_source =
            open_root_directory(root, None, &format!("source package {package_id}"))?;
        workspace.stage_plan_package(&plan, package_id, &package_source)?;
        for skill in &package.skills {
            let skill_source = open_root_directory(
                &root.join(&skill.relative_root),
                None,
                &format!("source Skill {}", skill.name),
            )?;
            workspace.stage_plan_skill(&plan, &skill.name, &skill_source)?;
        }
    }
    workspace.stage_external_state(&plan)?;
    if activate_apple {
        workspace.stage_fresh_source_activation(
            &plan,
            session_launcher.ok_or_else(|| {
                LifecycleError::Invalid(
                    "native Apple source install requires a frozen session launcher".to_owned(),
                )
            })?,
        )?;
    }
    workspace.verify_staged_install(&plan)?;
    let mut published = workspace.publish_staged_install(&plan)?;
    if !activate_apple {
        published.verify(&plan)?;
    }
    let activation = if activate_apple {
        Some(
            published.apply_source_activation(session_launcher.ok_or_else(|| {
                LifecycleError::Invalid(
                    "native Apple source install requires a frozen session launcher".to_owned(),
                )
            })?)?,
        )
    } else {
        None
    };
    published.verify(&plan)?;
    published.commit(&plan)?;
    Ok(SourceInstallOutcome {
        activation,
        install_plan: installed,
        target_root: contract_target,
    })
}

fn selected_platforms(plan: &Value) -> Result<std::collections::BTreeSet<&str>, LifecycleError> {
    plan.get("selected_platforms")
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid("Install Plan platforms are invalid".to_owned()))?
        .iter()
        .map(|value| {
            value.as_str().ok_or_else(|| {
                LifecycleError::Invalid("Install Plan platform is invalid".to_owned())
            })
        })
        .collect()
}

fn validate_source_install_inputs(
    bundle: &SourceInstallBundle,
    package_set: &SourcePackageSet,
) -> Result<(), LifecycleError> {
    ValidatedInstallPlan::new(bundle.plan().clone(), bundle.package_lock().clone())?;
    if bundle.package_roots() != package_set.package_roots {
        return invalid("source package snapshots differ from compiled Install Bundle roots");
    }
    let bundle_ids = bundle
        .package_roots()
        .iter()
        .map(|(identifier, _)| identifier.as_str())
        .collect::<Vec<_>>();
    let package_ids = package_set
        .packages
        .iter()
        .map(|package| package.id.as_str())
        .collect::<Vec<_>>();
    if bundle_ids != package_ids {
        return invalid("source package snapshots differ from compiled Install Bundle closure");
    }
    Ok(())
}

fn installed_selection_strings(plan: &Value, field: &str) -> Result<Vec<String>, LifecycleError> {
    let values = plan
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid(format!("{field} is invalid")))?
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| LifecycleError::Invalid(format!("{field} is invalid")))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if values.windows(2).any(|pair| pair[0] >= pair[1]) {
        return invalid(format!("{field} must be sorted and unique"));
    }
    Ok(values)
}

fn installed_source_selection(
    install_plan: &Value,
    package_lock: &Value,
) -> Result<InstalledSourceSelection, LifecycleError> {
    let install_plan_fingerprint = install_plan
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid("Install Lock fingerprint is invalid".to_owned()))?
        .to_owned();
    let package_lock_hash = package_lock
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("current package Lock fingerprint is invalid".to_owned())
        })?
        .to_owned();
    Ok(InstalledSourceSelection {
        platforms: installed_selection_strings(install_plan, "selected_platforms")?,
        disciplines: installed_selection_strings(install_plan, "selected_disciplines")?,
        runtime_configs: installed_selection_strings(install_plan, "selected_runtime_configs")?,
        install_plan_fingerprint,
        package_lock_hash,
    })
}

fn inspect_fresh_target(target_root: &Path) -> Result<(), LifecycleError> {
    let Some(target) = transaction_lock::inspect_optional_target(target_root)? else {
        return Ok(());
    };
    inspect_fresh_target_directory(&target, false)
}

fn inspect_fresh_target_directory(
    target: &Dir,
    allow_owned_lock: bool,
) -> Result<(), LifecycleError> {
    let mut occupied = Vec::new();
    for name in MANAGED_ROOTS {
        match target.symlink_metadata(name) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
            Ok(_) => occupied.push(name),
        }
    }
    if !occupied.is_empty() {
        return invalid(format!(
            "refusing to overwrite unmanaged or modified install roots: {}",
            occupied.join(", ")
        ));
    }
    reject_recovery_state(target, allow_owned_lock)
}

fn reject_recovery_state(target: &Dir, allow_owned_lock: bool) -> Result<(), LifecycleError> {
    let mut recovery = Vec::new();
    for entry in target.entries()? {
        let entry = entry?;
        if ignored_os_metadata(target, &entry.file_name())? {
            continue;
        }
        let name = entry.file_name();
        let name = name.to_str().ok_or_else(|| {
            LifecycleError::Invalid("install target contains a non-UTF-8 entry".to_owned())
        })?;
        if (name == LIFECYCLE_LOCK_DIRECTORY && !allow_owned_lock)
            || name.starts_with(INSTALL_STAGE_PREFIX)
            || name.starts_with(INSTALL_BACKUP_PREFIX)
            || name.starts_with(UNINSTALL_BACKUP_PREFIX)
        {
            recovery.push(name.to_owned());
        }
    }
    recovery.sort();
    if !recovery.is_empty() {
        return invalid(format!(
            "lifecycle recovery state requires attention: {}",
            recovery.join(", ")
        ));
    }
    Ok(())
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        compile_source_install_bundle, resolve_source_install_selection, rollback_source_install,
        snapshot_source_packages,
    };
    use agent_contracts::{canonical_json, canonical_sha256, load_json};
    use serde_json::json;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct Fixture {
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "agent-native-install-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&root).expect("create install fixture");
            Self { root }
        }

        fn target(&self) -> PathBuf {
            self.root.join("target")
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    fn repository_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("workspace root")
            .to_path_buf()
    }

    fn core_bundle() -> (SourceInstallBundle, SourcePackageSet) {
        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve core");
        let packages = snapshot_source_packages(&selection).expect("snapshot core");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile core bundle");
        (bundle, packages)
    }

    fn upgrade_evidence(package_lock: &Value) -> Value {
        let mut evidence = json!({
            "candidate_package_lock_hash": package_lock["fingerprint"],
            "command_results": [{
                "command": "compatibility-suite",
                "exit_code": 0,
                "stderr_sha256": "2".repeat(64),
                "stdout_sha256": "3".repeat(64),
            }],
            "environment": {"platform": "unit-test", "python": "3.11.0"},
            "manifest_count": 19,
            "negative_contract_count": 16,
            "runner_sha256": "4".repeat(64),
            "schema_inventory_hash": package_lock["schema_inventory"]["content_sha256"],
            "schema_version": "1.0",
            "status": "passed",
            "suite": "agent-skills-release-conformance-v1",
            "suite_definition_hash": "6".repeat(64),
            "test_count": 531,
        });
        let mut stable = evidence.as_object().expect("evidence object").clone();
        stable.insert(
            "command_results".to_owned(),
            json!([{"command": "compatibility-suite", "exit_code": 0}]),
        );
        evidence["attestation_key"] =
            Value::String(canonical_sha256(&Value::Object(stable)).expect("attestation"));
        evidence["fingerprint"] =
            Value::String(canonical_sha256(&evidence).expect("evidence fingerprint"));
        evidence
    }

    fn desktop_bundle(previous: Option<&Value>) -> (SourceInstallBundle, SourcePackageSet) {
        let root = repository_root();
        let selection = resolve_source_install_selection(
            root.join("platforms"),
            &["desktop".to_owned()],
            &[],
            &[],
            false,
        )
        .expect("resolve desktop");
        let packages = snapshot_source_packages(&selection).expect("snapshot desktop");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), previous)
                .expect("compile desktop bundle");
        (bundle, packages)
    }

    fn apple_bundle() -> (SourceInstallBundle, SourcePackageSet) {
        let root = repository_root();
        let selection = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned()],
            &[],
            &["codex".to_owned()],
            false,
        )
        .expect("resolve Apple");
        let packages = snapshot_source_packages(&selection).expect("snapshot Apple");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile Apple bundle");
        (bundle, packages)
    }

    fn downgrade_activation_lock_to_v1(target: &Path) -> Vec<u8> {
        let path = target.join(".agent-skills/activation-lock.json");
        let mut lock = load_json(&path).expect("load Activation Lock");
        let object = lock.as_object_mut().expect("Activation Lock object");
        object.insert("schema_version".to_owned(), Value::String("1.0".to_owned()));
        object.remove("handler");
        let bytes = canonical_json(&lock).expect("encode legacy Activation Lock");
        std::fs::write(&path, &bytes).expect("write legacy Activation Lock");
        bytes
    }

    fn selected_strings(plan: &Value, field: &str) -> Vec<String> {
        plan.get(field)
            .and_then(Value::as_array)
            .expect("selection array")
            .iter()
            .map(|value| value.as_str().expect("selection string").to_owned())
            .collect()
    }

    fn remove_optional_rollback_point(target: &Path) {
        let rollback = target.join(".agent-skills/rollback-point");
        if rollback.exists() {
            std::fs::remove_dir_all(rollback).expect("remove prior optional rollback point");
        }
    }

    fn assert_no_lifecycle_recovery_residue(target: &Path) {
        assert!(
            std::fs::read_dir(target)
                .expect("read lifecycle target")
                .all(|entry| {
                    let name = entry.expect("target entry").file_name();
                    let name = name.to_string_lossy();
                    name != LIFECYCLE_LOCK_DIRECTORY
                        && !name.starts_with(INSTALL_STAGE_PREFIX)
                        && !name.starts_with(INSTALL_BACKUP_PREFIX)
                        && !name.starts_with(UNINSTALL_BACKUP_PREFIX)
                }),
            "lifecycle recovery residue must be removed"
        );
    }

    fn assert_native_rollback_round_trip(
        target: &Path,
        current: &SourceInstallBundle,
        candidate: &SourceInstallBundle,
    ) {
        let point = load_json(target.join(".agent-skills/rollback-point/rollback-point.json"))
            .expect("load persistent rollback point");
        rollback_source_install(target, "0", point["fingerprint"].as_str().expect("point"))
            .expect_err("stale current Lock approval must fail");
        rollback_source_install(
            target,
            candidate.package_lock()["fingerprint"]
                .as_str()
                .expect("candidate Lock"),
            "0",
        )
        .expect_err("stale rollback point approval must fail");
        let snapshot_agents = target.join(".agent-skills/rollback-point/AGENTS.md");
        let snapshot_agents_bytes =
            std::fs::read(&snapshot_agents).expect("read rollback AGENTS snapshot");
        let mut tampered_agents = snapshot_agents_bytes.clone();
        tampered_agents.extend_from_slice(b"tampered\n");
        std::fs::write(&snapshot_agents, tampered_agents).expect("tamper rollback AGENTS snapshot");
        rollback_source_install(
            target,
            candidate.package_lock()["fingerprint"]
                .as_str()
                .expect("candidate Lock"),
            point["fingerprint"].as_str().expect("point"),
        )
        .expect_err("tampered rollback point must fail before staging");
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("candidate Lock survives tamper"),
            *candidate.package_lock()
        );
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        std::fs::write(&snapshot_agents, snapshot_agents_bytes)
            .expect("restore rollback AGENTS snapshot");
        let lock = LifecycleLock::acquire_existing(target).expect("lock rollback target");
        let target_directory = lock.target_directory().expect("open rollback target");
        let persistent = crate::rollback::open_persistent_rollback_point(&target_directory)
            .expect("open rollback point");
        let mut workspace = LifecycleWorkspace::from_lock(lock).expect("begin rollback workspace");
        let (plan, _) = workspace
            .stage_persistent_rollback_install(&persistent)
            .expect("stage rollback projection");
        let published = workspace
            .publish_staged_install(&plan)
            .expect("publish rollback projection");
        let error = published
            .commit(&plan)
            .expect_err("rollback cannot commit before external restore");
        assert!(
            error
                .to_string()
                .contains("external restore must complete before commit")
        );
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("candidate Lock survives incomplete rollback"),
            *candidate.package_lock()
        );
        let rolled_back = rollback_source_install(
            target,
            candidate.package_lock()["fingerprint"]
                .as_str()
                .expect("candidate Lock"),
            point["fingerprint"].as_str().expect("point"),
        )
        .expect("rollback partial upgrade");
        assert_eq!(rolled_back["status"], "rolled-back");
        assert_eq!(
            rolled_back["from_lock_hash"],
            candidate.package_lock()["fingerprint"]
        );
        assert_eq!(
            rolled_back["restored_lock_hash"],
            current.package_lock()["fingerprint"]
        );
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock")).expect("load restored Lock"),
            *current.package_lock()
        );
        assert_eq!(
            rolled_back["rollback_point"]["package_lock_hash"],
            candidate.package_lock()["fingerprint"]
        );
        let reversed = rollback_source_install(
            target,
            current.package_lock()["fingerprint"]
                .as_str()
                .expect("restored Lock"),
            rolled_back["rollback_point"]["fingerprint"]
                .as_str()
                .expect("reverse rollback point"),
        )
        .expect("apply reverse rollback point");
        assert_eq!(reversed["status"], "rolled-back");
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("load reverse-restored Lock"),
            *candidate.package_lock()
        );
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
    }

    fn publish_persistent_rollback(target: &Path) -> (crate::PublishedInstall, String) {
        let lock = LifecycleLock::acquire_existing(target).expect("lock rollback target");
        let target_directory = lock.target_directory().expect("open rollback target");
        let persistent = crate::rollback::open_persistent_rollback_point(&target_directory)
            .expect("open persistent rollback point");
        let fingerprint = persistent
            .point()
            .get("fingerprint")
            .and_then(Value::as_str)
            .expect("rollback point fingerprint")
            .to_owned();
        let mut workspace = LifecycleWorkspace::from_lock(lock).expect("begin rollback workspace");
        let (plan, _) = workspace
            .stage_persistent_rollback_install(&persistent)
            .expect("stage persistent rollback");
        let published = workspace
            .publish_staged_install(&plan)
            .expect("publish rollback managed projection");
        (published, fingerprint)
    }

    #[test]
    fn dry_run_is_read_only_and_fresh_install_commits_exact_plan() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (bundle, packages) = core_bundle();

        let preview =
            inspect_source_install(&bundle, &packages, &target).expect("inspect fresh install");
        assert_eq!(preview, *bundle.plan());
        assert!(!target.exists());

        let installed =
            install_source_bundle(&bundle, &packages, &target).expect("install fresh bundle");
        assert_eq!(installed["status"], "installed");
        assert_eq!(
            load_json(target.join(".agent-skills/install-lock.json")).expect("load Install Lock"),
            installed
        );
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock")).expect("load package Lock"),
            *bundle.package_lock()
        );
        assert_eq!(
            std::fs::read_to_string(target.join("AGENTS.md")).expect("read AGENTS"),
            bundle.instructions()
        );
        let doctor = crate::inspect_doctor_report_v2(&target)
            .expect("diagnose native install without external runtime");
        assert_eq!(doctor["schema_version"], "2.0");
        assert_eq!(doctor["status"], "passed");
        let mut inconsistent_doctor = doctor;
        inconsistent_doctor["environment"]["schema_inventory"]["content_sha256"] =
            Value::String("0".repeat(64));
        inconsistent_doctor
            .as_object_mut()
            .expect("Doctor report object")
            .remove("fingerprint");
        inconsistent_doctor["fingerprint"] = Value::String(
            canonical_sha256(&inconsistent_doctor).expect("hash inconsistent Doctor report"),
        );
        assert!(crate::doctor_report::validate_doctor_report_v2(&inconsistent_doctor).is_err());
        assert!(
            std::fs::read_dir(&target)
                .expect("read target")
                .all(|entry| {
                    let name = entry.expect("target entry").file_name();
                    let name = name.to_string_lossy();
                    !name.starts_with(INSTALL_STAGE_PREFIX)
                        && !name.starts_with(INSTALL_BACKUP_PREFIX)
                        && name != LIFECYCLE_LOCK_DIRECTORY
                })
        );
    }

    #[test]
    fn installed_selection_is_read_from_the_validated_lock_pair() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (bundle, packages) = core_bundle();
        install_source_bundle(&bundle, &packages, &target).expect("install core fixture");

        let selection =
            inspect_installed_source_selection(&target).expect("inspect installed selection");
        assert!(selection.core_only());
        assert!(selection.platforms().is_empty());
        assert!(selection.disciplines().is_empty());
        assert!(selection.runtime_configs().is_empty());

        let mut lock =
            load_json(target.join(".agent-skills/install-lock.json")).expect("load installed Lock");
        lock["selected_platforms"] = json!(["apple", "apple"]);
        std::fs::write(
            target.join(".agent-skills/install-lock.json"),
            canonical_json(&lock).expect("encode tampered Lock"),
        )
        .expect("tamper installed selection");
        assert!(inspect_installed_source_selection(&target).is_err());
    }

    #[test]
    fn selection_bound_candidate_rejects_a_valid_cross_lock_replacement() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (core, core_packages) = core_bundle();
        install_source_bundle(&core, &core_packages, &target).expect("install core fixture");
        let expected =
            inspect_installed_source_selection(&target).expect("freeze installed selection");

        let replacement = fixture.root.join("replacement");
        let (desktop, desktop_packages) = desktop_bundle(None);
        install_source_bundle(&desktop, &desktop_packages, &replacement)
            .expect("install replacement fixture");
        let retired = fixture.root.join("retired");
        std::fs::rename(&target, &retired).expect("retire original target");
        std::fs::rename(&replacement, &target).expect("publish replacement target");

        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve stale Core selection");
        let packages = snapshot_source_packages(&selection).expect("snapshot stale Core");
        let error = compile_source_upgrade_bundle_bound(
            &selection,
            &packages,
            root.join("schemas"),
            &target,
            &expected,
        )
        .expect_err("cross-lock selection drift must fail");
        assert!(
            error
                .to_string()
                .contains("changed after source selection was inferred")
        );
    }

    #[test]
    fn native_no_change_upgrade_returns_without_staging_or_target_writes() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (installed, installed_packages) = core_bundle();
        install_source_bundle(&installed, &installed_packages, &target)
            .expect("install core fixture");
        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve core candidate");
        let packages = snapshot_source_packages(&selection).expect("snapshot core candidate");
        let bundle =
            compile_source_upgrade_bundle(&selection, &packages, root.join("schemas"), &target)
                .expect("compile no-change candidate");
        assert_eq!(
            bundle.package_lock().pointer("/lineage/previous_lock_hash"),
            Some(&Value::Null)
        );
        let evidence = upgrade_evidence(bundle.package_lock());
        let plan = inspect_source_upgrade(
            &bundle,
            &packages,
            &target,
            &evidence,
            "upgrade",
            &[],
            &[],
            None,
        )
        .expect("inspect no-change upgrade");
        assert_eq!(plan["status"], "no-change");
        let before = std::fs::read(target.join(".agent-skills/install-lock.json"))
            .expect("read Install Lock");

        let result = upgrade_source_bundle(
            &bundle,
            &packages,
            &target,
            &evidence,
            &plan,
            &[],
            "upgrade",
            &[],
            &[],
            None,
        )
        .expect("apply no-change upgrade");

        assert_eq!(result["status"], "no-change");
        assert_eq!(
            std::fs::read(target.join(".agent-skills/install-lock.json"))
                .expect("reread Install Lock"),
            before
        );
        assert!(!target.join(".agent-skills/rollback-point").exists());
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
    }

    #[test]
    fn source_upgrade_candidate_requires_an_installed_lock_projection() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (installed, installed_packages) = core_bundle();
        install_source_bundle(&installed, &installed_packages, &target)
            .expect("install core fixture");
        let install_lock_path = target.join(".agent-skills/install-lock.json");
        let mut install_lock = load_json(&install_lock_path).expect("load Install Lock");
        install_lock["status"] = Value::String("planned".to_owned());
        std::fs::write(
            install_lock_path,
            canonical_json(&install_lock).expect("encode planned Install Lock"),
        )
        .expect("write planned Install Lock");

        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve core candidate");
        let packages = snapshot_source_packages(&selection).expect("snapshot core candidate");
        let error =
            compile_source_upgrade_bundle(&selection, &packages, root.join("schemas"), &target)
                .expect_err("planned projection must not be accepted as installed");

        assert!(
            error
                .to_string()
                .contains("requires an installed Install Lock")
        );
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
    }

    #[test]
    fn native_partial_upgrade_requires_exact_plan_and_persists_rollback_point() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (current, current_packages) = desktop_bundle(None);
        install_source_bundle(&current, &current_packages, &target)
            .expect("install desktop fixture");
        let root = repository_root();
        let selection =
            resolve_source_install_selection(root.join("platforms"), &[], &[], &[], true)
                .expect("resolve core candidate");
        let candidate_packages =
            snapshot_source_packages(&selection).expect("snapshot core candidate");
        let candidate = compile_source_upgrade_bundle(
            &selection,
            &candidate_packages,
            root.join("schemas"),
            &target,
        )
        .expect("compile partial candidate");
        assert_eq!(
            candidate
                .package_lock()
                .pointer("/lineage/previous_lock_hash"),
            Some(&current.package_lock()["fingerprint"])
        );
        let evidence = upgrade_evidence(candidate.package_lock());
        let removed = vec!["desktop".to_owned()];
        let plan = inspect_source_upgrade(
            &candidate,
            &candidate_packages,
            &target,
            &evidence,
            "partial-uninstall",
            &removed,
            &[],
            None,
        )
        .expect("inspect partial upgrade");
        assert_eq!(plan["status"], "planned");

        upgrade_source_bundle(
            &candidate,
            &candidate_packages,
            &target,
            &evidence,
            &plan,
            &["permission:unexpected:none->project-write".to_owned()],
            "partial-uninstall",
            &removed,
            &[],
            None,
        )
        .expect_err("unexpected permission approval must fail");
        let mut tampered = plan.clone();
        tampered["fingerprint"] = Value::String("f".repeat(64));
        upgrade_source_bundle(
            &candidate,
            &candidate_packages,
            &target,
            &evidence,
            &tampered,
            &[],
            "partial-uninstall",
            &removed,
            &[],
            None,
        )
        .expect_err("tampered approval must fail");
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("current Lock survives rejection"),
            *current.package_lock()
        );

        let result = upgrade_source_bundle(
            &candidate,
            &candidate_packages,
            &target,
            &evidence,
            &plan,
            &[],
            "partial-uninstall",
            &removed,
            &[],
            None,
        )
        .expect("apply partial upgrade");

        assert_eq!(result["status"], "upgraded");
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock")).expect("load upgraded Lock"),
            *candidate.package_lock()
        );
        assert!(
            target
                .join(".agent-skills/rollback-point/rollback-point.json")
                .is_file()
        );
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());

        assert_native_rollback_round_trip(&target, &current, &candidate);
    }

    #[test]
    fn approval_comparison_requires_the_exact_semantic_set() {
        let plan = json!({
            "approvals_required": [
                "permission:implementation.apple:none->project-write",
            ],
        });
        require_exact_upgrade_approvals(&plan, &[])
            .expect_err("missing permission approval must fail");
        require_exact_upgrade_approvals(
            &plan,
            &[
                "permission:implementation.apple:none->project-write".to_owned(),
                "permission:implementation.apple:none->project-write".to_owned(),
            ],
        )
        .expect("duplicate presentation of the exact approval remains compatible");
        require_exact_upgrade_approvals(
            &plan,
            &[
                "permission:implementation.apple:none->project-write".to_owned(),
                "permission:unexpected:none->project-write".to_owned(),
            ],
        )
        .expect_err("unexpected permission approval must fail");
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn activation_upgrade_smoke_failure_restores_managed_and_external_preimages() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (bundle, packages) = apple_bundle();
        let launcher = b"native upgrade smoke launcher\n";
        install_source_bundle_with_activation(&bundle, &packages, &target, launcher)
            .expect("install activated Apple fixture");
        remove_optional_rollback_point(&target);
        let legacy_activation = downgrade_activation_lock_to_v1(&target);
        let old_install = std::fs::read(target.join(".agent-skills/install-lock.json"))
            .expect("old Install Lock");
        let old_package = std::fs::read(target.join(".agent-skills/agent-skills.lock"))
            .expect("old package Lock");
        let evidence = upgrade_evidence(bundle.package_lock());
        let plan = inspect_source_upgrade(
            &bundle,
            &packages,
            &target,
            &evidence,
            "upgrade",
            &[],
            &[],
            Some(launcher),
        )
        .expect("inspect Activation migration");
        assert_eq!(plan["status"], "planned");
        assert_eq!(
            plan["external"]["handler"],
            "core.source-activation.apple-codex-v1"
        );

        let error = upgrade_source_bundle_with_smoke(
            &bundle,
            &packages,
            &target,
            &evidence,
            &plan,
            &[],
            "upgrade",
            &[],
            &[],
            Some(launcher),
            |_| invalid("injected installed workflow smoke failure"),
            |published, launcher| published.apply_source_activation(launcher).map(|_| ()),
            |published| published.apply_source_deactivation().map(|_| ()),
        )
        .expect_err("smoke failure must roll back");
        assert!(
            error
                .to_string()
                .contains("injected installed workflow smoke failure")
        );
        assert_eq!(
            std::fs::read(target.join(".agent-skills/install-lock.json"))
                .expect("restored Install Lock"),
            old_install
        );
        assert_eq!(
            std::fs::read(target.join(".agent-skills/agent-skills.lock"))
                .expect("restored package Lock"),
            old_package
        );
        assert_eq!(
            std::fs::read(target.join(".agent-skills/activation-lock.json"))
                .expect("restored Activation Lock"),
            legacy_activation
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-session")).expect("restored session launcher"),
            launcher
        );
        assert!(!target.join(".agent-skills/rollback-point").exists());
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());

        let error = upgrade_source_bundle_with_smoke(
            &bundle,
            &packages,
            &target,
            &evidence,
            &plan,
            &[],
            "upgrade",
            &[],
            &[],
            Some(launcher),
            |_| Ok(()),
            |published, launcher| {
                published
                    .apply_source_activation_with_test_hook(launcher, |_, phase| {
                        if phase == "activation-lock-published" {
                            invalid("injected partial source Activation failure")
                        } else {
                            Ok(())
                        }
                    })
                    .map(|_| ())
            },
            |published| published.apply_source_deactivation().map(|_| ()),
        )
        .expect_err("partial Activation failure must roll back");
        assert!(
            error
                .to_string()
                .contains("injected partial source Activation failure")
        );
        assert_eq!(
            std::fs::read(target.join(".agent-skills/install-lock.json"))
                .expect("restored Install Lock after partial handler"),
            old_install
        );
        assert_eq!(
            std::fs::read(target.join(".agent-skills/agent-skills.lock"))
                .expect("restored package Lock after partial handler"),
            old_package
        );
        assert_eq!(
            std::fs::read(target.join(".agent-skills/activation-lock.json"))
                .expect("restored Activation Lock after partial handler"),
            legacy_activation
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-session"))
                .expect("restored launcher after partial handler"),
            launcher
        );
        assert!(!target.join(".agent-skills/rollback-point").exists());
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());

        let result = upgrade_source_bundle(
            &bundle,
            &packages,
            &target,
            &evidence,
            &plan,
            &[],
            "upgrade",
            &[],
            &[],
            Some(launcher),
        )
        .expect("apply Activation migration after passing smoke");
        assert_eq!(result["status"], "upgraded");
        assert_eq!(
            load_json(target.join(".agent-skills/activation-lock.json"))
                .expect("load migrated Activation Lock")["schema_version"],
            "2.0"
        );
        assert!(
            target
                .join(".agent-skills/rollback-point/rollback-point.json")
                .is_file()
        );
        let migrated_activation = std::fs::read(target.join(".agent-skills/activation-lock.json"))
            .expect("read migrated Activation Lock");
        let migrated_config =
            std::fs::read(target.join("config.toml")).expect("read migrated config");
        let (mut interrupted, interrupted_point) = publish_persistent_rollback(&target);
        let mut restored_entry = false;
        let error = interrupted
            .apply_persistent_rollback_with_test_hook(&interrupted_point, |_, phase| {
                if phase == "entry-restored" && !restored_entry {
                    restored_entry = true;
                    invalid("injected Activation migration rollback failure")
                } else {
                    Ok(())
                }
            })
            .expect_err("partial Activation migration rollback must fail");
        assert!(restored_entry);
        assert!(
            error
                .to_string()
                .contains("injected Activation migration rollback failure")
        );
        interrupted
            .rollback()
            .expect("recover interrupted Activation migration rollback");
        assert_eq!(
            std::fs::read(target.join(".agent-skills/activation-lock.json"))
                .expect("read recovered migrated Activation Lock"),
            migrated_activation
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-session"))
                .expect("read recovered migrated launcher"),
            launcher
        );
        assert_eq!(
            std::fs::read(target.join("config.toml")).expect("read recovered migrated config"),
            migrated_config
        );
        assert_no_lifecycle_recovery_residue(&target);
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn deactivation_upgrade_removes_only_activation_owned_state_and_persists_rollback() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (current, current_packages) = apple_bundle();
        let launcher = b"native deactivation launcher\n";
        install_source_bundle_with_activation(&current, &current_packages, &target, launcher)
            .expect("install activated Apple fixture");
        remove_optional_rollback_point(&target);
        std::fs::create_dir(target.join("skills/.system")).expect("create local system Skills");
        std::fs::write(
            target.join("skills/.system/local.txt"),
            b"local system skill\n",
        )
        .expect("write local system Skill");
        let installed = load_json(target.join(".agent-skills/install-lock.json"))
            .expect("load installed selection");
        let disciplines = selected_strings(&installed, "selected_disciplines");
        let root = repository_root();
        let core_only = disciplines.is_empty();
        let selection = resolve_source_install_selection(
            root.join("platforms"),
            &[],
            &disciplines,
            &[],
            core_only,
        )
        .expect("resolve deactivated candidate");
        let packages =
            snapshot_source_packages(&selection).expect("snapshot deactivated candidate");
        let candidate = compile_source_install_bundle(
            &selection,
            &packages,
            root.join("schemas"),
            Some(current.package_lock()),
        )
        .expect("compile deactivated candidate");
        let evidence = upgrade_evidence(candidate.package_lock());
        let removed_platforms = vec!["apple".to_owned()];
        let removed_runtime_configs = vec!["codex".to_owned()];
        let plan = inspect_source_upgrade(
            &candidate,
            &packages,
            &target,
            &evidence,
            "partial-uninstall",
            &removed_platforms,
            &removed_runtime_configs,
            None,
        )
        .expect("inspect source deactivation");
        assert_eq!(
            plan["external"]["handler"],
            "core.source-deactivation.apple-codex-v1"
        );

        let result = upgrade_source_bundle(
            &candidate,
            &packages,
            &target,
            &evidence,
            &plan,
            &[],
            "partial-uninstall",
            &removed_platforms,
            &removed_runtime_configs,
            None,
        )
        .expect("apply source deactivation");

        assert_eq!(result["status"], "upgraded");
        assert!(!target.join(".agent-skills/activation-lock.json").exists());
        assert!(!target.join("bin/agent-session").exists());
        assert!(!target.join("bin/agent-skills").exists());
        assert!(
            target
                .join(".agent-skills/rollback-point/rollback-point.json")
                .is_file()
        );
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("load deactivated package Lock"),
            *candidate.package_lock()
        );

        let deactivated_config =
            std::fs::read(target.join("config.toml")).expect("read deactivated config");
        let system_skill =
            std::fs::read(target.join("skills/.system/local.txt")).expect("read system Skill");
        let (mut interrupted, interrupted_point) = publish_persistent_rollback(&target);
        let mut restored_entry = false;
        let error = interrupted
            .apply_persistent_rollback_with_test_hook(&interrupted_point, |_, phase| {
                if phase == "entry-restored" && !restored_entry {
                    restored_entry = true;
                    invalid("injected forward external restore failure")
                } else {
                    Ok(())
                }
            })
            .expect_err("partial external restore must fail");
        assert!(restored_entry);
        assert!(
            error
                .to_string()
                .contains("injected forward external restore failure")
        );
        interrupted
            .rollback()
            .expect("recover interrupted persistent rollback");
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("load recovered deactivated Lock"),
            *candidate.package_lock()
        );
        assert!(!target.join(".agent-skills/activation-lock.json").exists());
        assert!(!target.join("bin/agent-session").exists());
        assert!(!target.join("bin/agent-skills").exists());
        assert_eq!(
            std::fs::read(target.join("config.toml")).expect("read recovered config"),
            deactivated_config
        );
        assert_eq!(
            std::fs::read(target.join("skills/.system/local.txt"))
                .expect("read recovered system Skill"),
            system_skill
        );
        assert_no_lifecycle_recovery_residue(&target);

        let point = load_json(target.join(".agent-skills/rollback-point/rollback-point.json"))
            .expect("load deactivation rollback point");
        let rolled_back = rollback_source_install(
            &target,
            candidate.package_lock()["fingerprint"]
                .as_str()
                .expect("candidate Lock"),
            point["fingerprint"].as_str().expect("rollback point"),
        )
        .expect("restore activated Apple projection");
        assert_eq!(rolled_back["status"], "rolled-back");
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("load restored Apple Lock"),
            *current.package_lock()
        );
        assert!(target.join(".agent-skills/activation-lock.json").is_file());
        assert_eq!(
            std::fs::read(target.join("bin/agent-session")).expect("restored session launcher"),
            launcher
        );
        assert_eq!(
            rolled_back["rollback_point"]["package_lock_hash"],
            candidate.package_lock()["fingerprint"]
        );
    }

    #[test]
    fn preserve_upgrade_keeps_activation_state_byte_exact_and_persists_rollback() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let root = repository_root();
        let current_selection = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned(), "desktop".to_owned()],
            &[],
            &["codex".to_owned()],
            false,
        )
        .expect("resolve Apple and Desktop");
        let current_packages =
            snapshot_source_packages(&current_selection).expect("snapshot Apple and Desktop");
        let current = compile_source_install_bundle(
            &current_selection,
            &current_packages,
            root.join("schemas"),
            None,
        )
        .expect("compile Apple and Desktop bundle");
        let launcher = b"native preserve launcher\n";
        install_source_bundle_with_activation(&current, &current_packages, &target, launcher)
            .expect("install activated multi-platform fixture");
        remove_optional_rollback_point(&target);
        let installed = load_json(target.join(".agent-skills/install-lock.json"))
            .expect("load multi-platform selection");
        let disciplines = selected_strings(&installed, "selected_disciplines");
        let candidate_selection = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned()],
            &disciplines,
            &["codex".to_owned()],
            false,
        )
        .expect("resolve preserved Apple candidate");
        let packages =
            snapshot_source_packages(&candidate_selection).expect("snapshot preserved candidate");
        let candidate = compile_source_install_bundle(
            &candidate_selection,
            &packages,
            root.join("schemas"),
            Some(current.package_lock()),
        )
        .expect("compile preserved candidate");
        let evidence = upgrade_evidence(candidate.package_lock());
        let removed = vec!["desktop".to_owned()];
        let plan = inspect_source_upgrade(
            &candidate,
            &packages,
            &target,
            &evidence,
            "partial-uninstall",
            &removed,
            &[],
            None,
        )
        .expect("inspect preserve upgrade");
        assert_eq!(
            plan["external"]["handler"],
            "core.source-preserve.apple-codex-v1"
        );
        let activation =
            std::fs::read(target.join(".agent-skills/activation-lock.json")).expect("Activation");
        let session = std::fs::read(target.join("bin/agent-session")).expect("session launcher");

        let result = upgrade_source_bundle(
            &candidate,
            &packages,
            &target,
            &evidence,
            &plan,
            &[],
            "partial-uninstall",
            &removed,
            &[],
            None,
        )
        .expect("apply preserve upgrade");

        assert_eq!(result["status"], "upgraded");
        assert_eq!(
            std::fs::read(target.join(".agent-skills/activation-lock.json"))
                .expect("preserved Activation"),
            activation
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-session")).expect("preserved session launcher"),
            session
        );
        assert!(
            target
                .join(".agent-skills/rollback-point/rollback-point.json")
                .is_file()
        );
        assert_eq!(
            load_json(target.join(".agent-skills/agent-skills.lock"))
                .expect("load preserved package Lock"),
            *candidate.package_lock()
        );
    }

    #[test]
    fn apple_dry_run_validates_native_activation_without_target_writes() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let root = repository_root();
        let selection = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned()],
            &[],
            &["codex".to_owned()],
            false,
        )
        .expect("resolve Apple");
        let packages = snapshot_source_packages(&selection).expect("snapshot Apple");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile Apple bundle");

        let preview = inspect_source_install_with_activation(
            &bundle,
            &packages,
            &target,
            b"frozen native preview launcher\n",
        )
        .expect("preview Apple activation");

        assert_eq!(preview["install_plan"], *bundle.plan());
        assert_eq!(preview["activation"]["config_changed"], true);
        assert!(
            preview["activation"]["updated_files"]
                .as_array()
                .expect("updated activation files")
                .iter()
                .any(|value| value == "bin/agent-session")
        );
        assert!(
            preview["activation"]["updated_files"]
                .as_array()
                .expect("updated activation files")
                .iter()
                .any(|value| value == "bin/agent-skills")
        );
        assert!(!target.exists(), "dry-run must not create the target");
    }

    #[test]
    fn apple_dry_run_rejects_unmanaged_native_cli_without_mutation() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let root = repository_root();
        std::fs::create_dir_all(target.join("bin")).expect("create target bin");
        std::fs::write(target.join("bin/agent-skills"), b"unmanaged\n")
            .expect("write unmanaged CLI");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            std::fs::set_permissions(
                target.join("bin/agent-skills"),
                std::fs::Permissions::from_mode(0o755),
            )
            .expect("set unmanaged CLI mode");
        }
        let selection = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned()],
            &[],
            &["codex".to_owned()],
            false,
        )
        .expect("resolve Apple");
        let packages = snapshot_source_packages(&selection).expect("snapshot Apple");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile Apple bundle");

        let error = inspect_source_install_with_activation(
            &bundle,
            &packages,
            &target,
            b"frozen native preview launcher\n",
        )
        .expect_err("unmanaged native CLI must block preview");

        assert!(
            error
                .to_string()
                .contains("refusing to overwrite unmanaged activation destination"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-skills")).expect("read unmanaged CLI"),
            b"unmanaged\n"
        );
        assert_eq!(
            std::fs::read_dir(target.join("bin"))
                .expect("read target bin")
                .count(),
            1
        );
    }

    #[test]
    fn occupied_managed_root_is_rejected_without_mutation() {
        let fixture = Fixture::new();
        let target = fixture.target();
        std::fs::create_dir(&target).expect("create target");
        std::fs::write(target.join("AGENTS.md"), b"unmanaged\n").expect("write unmanaged root");
        let (bundle, packages) = core_bundle();

        let error = inspect_source_install(&bundle, &packages, &target)
            .expect_err("occupied preview fails");
        assert!(error.to_string().contains("refusing to overwrite"));
        let error =
            install_source_bundle(&bundle, &packages, &target).expect_err("occupied install fails");
        assert!(error.to_string().contains("refusing to overwrite"));
        assert_eq!(
            std::fs::read(target.join("AGENTS.md")).expect("read unmanaged root"),
            b"unmanaged\n"
        );
        assert_eq!(std::fs::read_dir(&target).expect("read target").count(), 1);
    }

    #[test]
    fn recovery_residue_injected_before_lock_is_rejected_under_lock() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let (bundle, packages) = core_bundle();
        let residue = format!("{INSTALL_STAGE_PREFIX}interrupted");

        let error = install_source_bundle_with_options(
            &bundle,
            &packages,
            &target,
            false,
            None,
            |target| {
                std::fs::create_dir(target)?;
                std::fs::create_dir(target.join(&residue))?;
                Ok(())
            },
        )
        .expect_err("locked preflight rejects recovery residue");
        assert!(
            error
                .to_string()
                .contains("lifecycle recovery state requires attention")
        );
        assert!(target.join(&residue).is_dir());
        assert!(!target.join(LIFECYCLE_LOCK_DIRECTORY).exists());
        for root in MANAGED_ROOTS {
            assert!(!target.join(root).exists());
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn non_utf8_target_is_rejected_before_the_install_transaction() {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt as _;

        let fixture = Fixture::new();
        let target = fixture.root.join(OsString::from_vec(vec![
            b't', b'a', b'r', b'g', b'e', b't', 0xff,
        ]));
        std::fs::create_dir(&target).expect("create non-UTF-8 target");
        let (bundle, packages) = core_bundle();

        let error = install_source_bundle(&bundle, &packages, &target)
            .expect_err("non-UTF-8 report target must fail before mutation");

        assert!(error.to_string().contains("target is not UTF-8"));
        assert_eq!(
            std::fs::read_dir(&target)
                .expect("read rejected target")
                .count(),
            0
        );
    }

    #[test]
    fn fresh_apple_install_activates_the_same_frozen_native_launcher() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let root = repository_root();
        let selection = resolve_source_install_selection(
            root.join("platforms"),
            &["apple".to_owned()],
            &[],
            &["codex".to_owned()],
            false,
        )
        .expect("resolve Apple source selection");
        let packages = snapshot_source_packages(&selection).expect("snapshot Apple packages");
        let bundle =
            compile_source_install_bundle(&selection, &packages, root.join("schemas"), None)
                .expect("compile Apple bundle");
        let launcher = b"frozen native session launcher\n";

        let outcome = install_source_bundle_with_activation(&bundle, &packages, &target, launcher)
            .expect("install activated Apple bundle");
        assert_eq!(outcome["install_plan"]["status"], "installed");
        assert_eq!(
            outcome["activation"]["handler"],
            "core.source-activation.apple-codex-v1"
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-session")).expect("read installed launcher"),
            launcher
        );
        assert_eq!(
            std::fs::read(target.join("bin/agent-skills")).expect("read installed native CLI"),
            launcher
        );
        assert!(target.join(".agent-skills/activation-lock.json").is_file());
    }
}
