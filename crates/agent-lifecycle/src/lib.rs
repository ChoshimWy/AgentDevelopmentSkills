//! Lifecycle diagnostics and transaction foundations for the native migration.
//!
//! Doctor inspection is non-mutating. The explicit [`LifecycleLock`] and
//! [`LifecycleWorkspace`] APIs create the target/lock and temporary stage/backup
//! foundations. [`ValidatedInstallPlan`] binds a complete Install Plan to its
//! persistent Lockfile; the workspace can then assemble and semantically verify
//! all three managed roots, preserve external `.system`/Activation state, and
//! verify the complete staged topology before a swap, including a rollback
//! point for an intact current installation. [`PublishedInstall`] then performs
//! identity-bound, no-replace managed-root publication and keeps the previous
//! roots recoverable until explicit commit or rollback. After managed-root
//! recovery, its internal mutation boundary restores validated external
//! rollback preimages through a private quarantine. The approved external scope
//! must remain quiescent without concurrently writable handles. The first
//! trusted handlers perform source deactivation plus replacement and
//! fresh-install source activation with exact rollback-scope and Activation
//! ownership checks. Fresh activation freezes package assets from the stage
//! and unmanaged preimages from the target before publication, then removes
//! the new managed roots and restores those preimages on failure.
//! [`PublishedUninstall`] adds rollback-backed full managed removal while
//! preserving local profiles, config semantics, and `skills/.system`.
//! The legacy-adoption transaction additionally preserves exact source
//! symlink objects and their external `.system` tree until activation commits.
//! Bootstrap routing of that transaction remains outside this slice.

mod codex_config;
mod doctor_report;
mod external_stage;
mod installed_smoke;
mod managed_swap;
mod packages;
mod post_install;
mod rollback;
mod rollback_stage;
mod source_activation;
mod source_bundle;
mod source_install;
mod source_lifecycle;
mod source_packages;
mod source_rollback;
mod staged_install;
mod staged_tree;
mod transaction_lock;
mod transaction_workspace;
mod upgrade_plan;
mod upgrade_scope;

pub use codex_config::render_codex_config;
pub use doctor_report::{inspect_doctor_report_v1, inspect_doctor_report_v2};
#[cfg(test)]
use doctor_report::{validate_doctor_report_v1, validate_doctor_report_v2};
pub use managed_swap::{PublishedInstall, PublishedUninstall, inspect_uninstall_plan};
pub use source_bundle::{SourceInstallBundle, compile_source_install_bundle};
pub use source_install::{
    SourceInstallSelection, inspect_source_platform_options, resolve_source_install_selection,
};
pub use source_lifecycle::{
    InstalledSourceSelection, compile_source_upgrade_bundle, compile_source_upgrade_bundle_bound,
    inspect_installed_source_selection, inspect_legacy_adoption, inspect_source_install,
    inspect_source_install_with_activation, inspect_source_upgrade, inspect_source_upgrade_bound,
    install_source_bundle, install_source_bundle_with_activation,
    install_source_bundle_with_legacy_adoption, upgrade_source_bundle,
};
pub use source_packages::{SourcePackageSet, snapshot_source_packages};
pub use source_rollback::rollback_source_install;
pub use staged_install::ValidatedInstallPlan;
pub use transaction_lock::{LifecycleLock, normalize_lifecycle_target};
pub use transaction_workspace::LifecycleWorkspace;
pub use upgrade_plan::compile_upgrade_plan;
pub use upgrade_scope::{UpgradePlanningSnapshot, inspect_upgrade_planning_snapshot};

use agent_contracts::{
    ContractError, MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256, parse_json,
};
use agent_engine::{
    install_plan_identity_hash, schema_inventory, validate_install_plan, validate_package_lock,
};
use cap_fs_ext::{DirExt as _, FollowSymlinks, OpenOptionsFollowExt as _};
use cap_std::ambient_authority;
use cap_std::fs::{Dir, OpenOptions};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::HashSet;
use std::io::Read as _;
use std::path::{Path, PathBuf};
use thiserror::Error;

const MANAGED_DIRECTORY_MODE: u32 = 0o755;
const MANAGED_FILE_MODE: u32 = 0o644;
const PERSISTENT_PACKAGE_LOCK: &str = "agent-skills.lock";
const EXTERNAL_ACTIVATION_LOCK: &str = "activation-lock.json";
const ROLLBACK_POINT_DIRECTORY: &str = "rollback-point";
const LIFECYCLE_LOCK_DIRECTORY: &str = ".agent-skills-lifecycle.lock";
const INSTALL_BACKUP_PREFIX: &str = ".agent-skills-backup-";
const INSTALL_STAGE_PREFIX: &str = ".agent-skills-stage-";
const UNINSTALL_BACKUP_PREFIX: &str = ".agent-skills-uninstall-backup-";
const RECOVERY_PREFIXES: [(&str, &str); 3] = [
    (INSTALL_BACKUP_PREFIX, "install-backup"),
    (INSTALL_STAGE_PREFIX, "install-stage"),
    (UNINSTALL_BACKUP_PREFIX, "uninstall-backup"),
];

/// Native lifecycle inspection failures.
#[derive(Debug, Error)]
pub enum LifecycleError {
    #[error(transparent)]
    Contract(#[from] ContractError),
    #[error(transparent)]
    Engine(#[from] agent_engine::EngineError),
    #[error(transparent)]
    Registry(#[from] agent_registry::RegistryError),
    #[error("lifecycle input cannot be read: {0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Invalid(String),
}

#[derive(Debug)]
struct BaselineState {
    checks: Vec<Value>,
    install_lock: Option<Value>,
    installed_semantics: Option<Value>,
    package_lock: Option<Value>,
    recovery_candidates: Vec<Value>,
    recovery_unknown: bool,
}

impl BaselineState {
    fn new() -> Self {
        Self {
            checks: Vec::new(),
            install_lock: None,
            installed_semantics: None,
            package_lock: None,
            recovery_candidates: Vec::new(),
            recovery_unknown: true,
        }
    }

    fn record(
        &mut self,
        check_id: &str,
        category: &str,
        status: &str,
        summary: &str,
        details: Value,
    ) {
        let mut check = json!({
            "category": category,
            "details": null,
            "id": check_id,
            "status": status,
            "summary": summary,
        });
        check["details"] = details;
        self.checks.push(check);
    }

    fn failed(
        &mut self,
        check_id: &str,
        category: &str,
        summary: &str,
        error: impl std::fmt::Display,
    ) {
        let message = error.to_string();
        self.record(
            check_id,
            category,
            "failed",
            summary,
            json!({
                "errors": [
                    if message.is_empty() {
                        "lifecycle inspection failed"
                    } else {
                        &message
                    }
                ]
            }),
        );
    }
}

/// Inspect the first fail-closed Doctor boundary without modifying the target.
///
/// The returned projection contains the existing Doctor check records for the
/// safe-target, recovery-residue, managed-layout, Install Lock, persistent
/// Lockfile, Core runtime identity, Schema inventory, and Activation integrity
/// checks, plus installed package/Manifest and Skill integrity, global AGENTS
/// composition, Capability Binding and Provider closure freezing, and
/// permission freezing against rebuilt package semantics. A persistent
/// rollback point, when present, is verified as a complete read-only snapshot.
/// This is intentionally a compatibility probe rather than a new public
/// artifact schema.
///
/// Target and managed directories are held as directory capabilities. Contract
/// files are opened without following symlinks and their identities are checked
/// before and after the open, so a concurrent replacement cannot redirect a
/// diagnostic read outside the inspected tree.
///
/// # Errors
/// Returns an error only when the canonical projection itself cannot be
/// constructed. Individual diagnostic failures are represented as check
/// records, matching the Python Doctor behavior.
#[allow(clippy::too_many_lines)]
pub fn inspect_doctor_baseline(
    target_root: impl AsRef<Path>,
    schema_root: impl AsRef<Path>,
) -> Result<Value, LifecycleError> {
    let schemas = absolute_path(schema_root.as_ref())?;
    inspect_doctor_baseline_with_schema_source(
        target_root.as_ref(),
        DoctorSchemaSource::Filesystem(&schemas),
    )
}

pub(crate) fn inspect_doctor_baseline_embedded(
    target_root: impl AsRef<Path>,
    schema_inventory: &Value,
) -> Result<Value, LifecycleError> {
    inspect_doctor_baseline_with_schema_source(
        target_root.as_ref(),
        DoctorSchemaSource::Embedded(schema_inventory),
    )
}

#[derive(Clone, Copy)]
enum DoctorSchemaSource<'a> {
    Filesystem(&'a Path),
    Embedded(&'a Value),
}

#[allow(clippy::too_many_lines)]
fn inspect_doctor_baseline_with_schema_source(
    target_root: &Path,
    schema_source: DoctorSchemaSource<'_>,
) -> Result<Value, LifecycleError> {
    let target = absolute_path(target_root)?;
    let mut state = BaselineState::new();

    let target_directory =
        match open_root_directory(&target, None, "install target").and_then(|directory| {
            directory.entries()?;
            Ok(directory)
        }) {
            Ok(directory) => {
                state.record(
                    "filesystem.target",
                    "filesystem",
                    "passed",
                    "Install target is a safe directory",
                    json!({"path": target}),
                );
                Some(directory)
            }
            Err(error) => {
                state.failed(
                    "filesystem.target",
                    "filesystem",
                    "Install target is a safe directory",
                    error,
                );
                None
            }
        };

    inspect_recovery(target_directory.as_ref(), &mut state);

    if let Some(target_directory) = target_directory.as_ref() {
        match check_layout(target_directory) {
            Ok(details) => state.record(
                "filesystem.layout",
                "filesystem",
                "passed",
                "Managed root layout and modes are canonical",
                details,
            ),
            Err(error) => state.failed(
                "filesystem.layout",
                "filesystem",
                "Managed root layout and modes are canonical",
                error,
            ),
        }
    } else {
        state.record(
            "filesystem.layout",
            "filesystem",
            "skipped",
            "Managed root layout requires a safe install target",
            json!({}),
        );
    }

    if let Some(target_directory) = target_directory.as_ref() {
        let result = open_child_directory(
            target_directory,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )
        .and_then(|managed| load_install_lock(&managed));
        match result {
            Ok(value) => {
                let details = json!({
                    "fingerprint": value.get("fingerprint").cloned().unwrap_or(Value::Null),
                    "lock_schema_version": value.get("lock_schema_version").cloned().unwrap_or(Value::Null),
                });
                state.install_lock = Some(value);
                state.record(
                    "install.lock",
                    "install",
                    "passed",
                    "Install Lock is valid and installed",
                    details,
                );
            }
            Err(error) => state.failed(
                "install.lock",
                "install",
                "Install Lock is valid and installed",
                error,
            ),
        }
    } else {
        state.record(
            "install.lock",
            "install",
            "skipped",
            "Install Lock check requires a safe install target",
            json!({}),
        );
    }

    if let (Some(target_directory), Some(install_lock)) =
        (target_directory.as_ref(), state.install_lock.clone())
    {
        let result = open_child_directory(
            target_directory,
            ".agent-skills",
            Some(MANAGED_DIRECTORY_MODE),
            "managed metadata directory",
        )
        .and_then(|managed| load_package_lock(&managed, &install_lock));
        match result {
            Ok(value) => {
                let details = json!({
                    "fingerprint": value.get("fingerprint").cloned().unwrap_or(Value::Null),
                    "previous": value.pointer("/lineage/previous_lock_hash").cloned().unwrap_or(Value::Null),
                });
                state.package_lock = Some(value);
                state.record(
                    "lock.persistent",
                    "lock",
                    "passed",
                    "Persistent Lockfile is valid and anchored",
                    details,
                );
            }
            Err(error) => state.failed(
                "lock.persistent",
                "lock",
                "Persistent Lockfile is valid and anchored",
                error,
            ),
        }
    } else {
        state.record(
            "lock.persistent",
            "lock",
            "skipped",
            "Persistent Lockfile check requires a valid Install Lock",
            json!({}),
        );
    }

    if let Some(target_directory) = target_directory.as_ref() {
        match rollback::check_rollback_point(target_directory) {
            Ok(details) => state.record(
                "recovery.rollback-point",
                "recovery",
                "passed",
                "Persistent rollback point is absent or valid",
                details,
            ),
            Err(error) => state.failed(
                "recovery.rollback-point",
                "recovery",
                "Persistent rollback point is absent or valid",
                error,
            ),
        }
    } else {
        state.record(
            "recovery.rollback-point",
            "recovery",
            "skipped",
            "Rollback point verification requires a safe install target",
            json!({}),
        );
    }

    if let (Some(install_lock), Some(package_lock)) =
        (state.install_lock.as_ref(), state.package_lock.as_ref())
    {
        match check_core_identity(install_lock, package_lock) {
            Ok(details) => state.record(
                "environment.core",
                "environment",
                "passed",
                "Core runtime identity matches both Lockfiles",
                details,
            ),
            Err(error) => state.failed(
                "environment.core",
                "environment",
                "Core runtime identity matches both Lockfiles",
                error,
            ),
        }
    } else {
        state.record(
            "environment.core",
            "environment",
            "skipped",
            "Core identity check requires both Lockfiles",
            json!({}),
        );
    }

    if let Some(package_lock) = state.package_lock.as_ref() {
        match check_schema_inventory(&schema_source, package_lock) {
            Ok(details) => state.record(
                "schema.inventory",
                "schema",
                "passed",
                "Runtime Schema inventory matches the package Lockfile",
                details,
            ),
            Err(error) => state.failed(
                "schema.inventory",
                "schema",
                "Runtime Schema inventory matches the package Lockfile",
                error,
            ),
        }
    } else {
        state.record(
            "schema.inventory",
            "schema",
            "skipped",
            "Schema inventory check requires a valid package Lockfile",
            json!({}),
        );
    }

    if let (Some(target_directory), Some(install_lock), Some(package_lock)) = (
        target_directory.as_ref(),
        state.install_lock.as_ref(),
        state.package_lock.as_ref(),
    ) {
        let mut retained_semantics = None;
        match packages::check_package_integrity(
            target_directory,
            install_lock,
            package_lock,
            &mut retained_semantics,
        ) {
            Ok(inspection) => {
                state.installed_semantics = Some(inspection.semantics);
                state.record(
                    "package.integrity",
                    "package",
                    "passed",
                    "Installed packages and Manifests match both Lockfiles",
                    inspection.details,
                );
            }
            Err(error) => {
                state.installed_semantics = retained_semantics;
                state.failed(
                    "package.integrity",
                    "package",
                    "Installed packages and Manifests match both Lockfiles",
                    error,
                );
            }
        }
    } else {
        state.record(
            "package.integrity",
            "package",
            "skipped",
            "Package verification requires both Lockfiles",
            json!({}),
        );
    }

    if let (Some(target_directory), Some(install_lock)) =
        (target_directory.as_ref(), state.install_lock.as_ref())
    {
        match post_install::check_skill_integrity(
            target_directory,
            install_lock,
            state.installed_semantics.as_ref(),
        ) {
            Ok(details) => state.record(
                "skill.integrity",
                "skill",
                "passed",
                "Installed Skills match the Install Lock",
                details,
            ),
            Err(error) => state.failed(
                "skill.integrity",
                "skill",
                "Installed Skills match the Install Lock",
                error,
            ),
        }
    } else {
        state.record(
            "skill.integrity",
            "skill",
            "skipped",
            "Skill verification requires a valid Install Lock",
            json!({}),
        );
    }

    if let (Some(target_directory), Some(install_lock), Some(package_lock)) = (
        target_directory.as_ref(),
        state.install_lock.as_ref(),
        state.package_lock.as_ref(),
    ) {
        match post_install::check_global_instructions(
            target_directory,
            install_lock,
            package_lock,
            state.installed_semantics.as_ref(),
        ) {
            Ok(details) => state.record(
                "instructions.global",
                "instructions",
                "passed",
                "Unique global AGENTS source, fragment order, rule trace and final hash are valid",
                details,
            ),
            Err(error) => state.failed(
                "instructions.global",
                "instructions",
                "Unique global AGENTS source, fragment order, rule trace and final hash are valid",
                error,
            ),
        }
    } else {
        state.record(
            "instructions.global",
            "instructions",
            "skipped",
            "AGENTS verification requires both Lockfiles",
            json!({}),
        );
    }

    if let (Some(install_lock), Some(package_lock)) =
        (state.install_lock.as_ref(), state.package_lock.as_ref())
    {
        match post_install::check_binding_freeze(
            install_lock,
            package_lock,
            state.installed_semantics.as_ref(),
        ) {
            Ok(details) => state.record(
                "binding.freeze",
                "binding",
                "passed",
                "Capability Bindings and Provider closure are frozen",
                details,
            ),
            Err(error) => state.failed(
                "binding.freeze",
                "binding",
                "Capability Bindings and Provider closure are frozen",
                error,
            ),
        }
    } else {
        state.record(
            "binding.freeze",
            "binding",
            "skipped",
            "Binding verification requires both Lockfiles",
            json!({}),
        );
    }

    if let (Some(install_lock), Some(package_lock)) =
        (state.install_lock.as_ref(), state.package_lock.as_ref())
    {
        match post_install::check_permission_freeze(
            install_lock,
            package_lock,
            state.installed_semantics.as_ref(),
        ) {
            Ok(details) => state.record(
                "permission.freeze",
                "permission",
                "passed",
                "Permission profiles and per-Capability grants are frozen",
                details,
            ),
            Err(error) => state.failed(
                "permission.freeze",
                "permission",
                "Permission profiles and per-Capability grants are frozen",
                error,
            ),
        }
    } else {
        state.record(
            "permission.freeze",
            "permission",
            "skipped",
            "Permission verification requires both Lockfiles",
            json!({}),
        );
    }

    if let Some(target_directory) = target_directory.as_ref() {
        match check_activation(target_directory) {
            Ok(details) => state.record(
                "activation.integrity",
                "activation",
                "passed",
                "Activation files are absent or match their Lock",
                details,
            ),
            Err(error) => state.failed(
                "activation.integrity",
                "activation",
                "Activation files are absent or match their Lock",
                error,
            ),
        }
    } else {
        state.record(
            "activation.integrity",
            "activation",
            "skipped",
            "Activation verification requires a safe install target",
            json!({}),
        );
    }

    let install = json!({
        "install_plan_fingerprint": state.install_lock.as_ref()
            .and_then(|value| value.get("fingerprint")).cloned().unwrap_or(Value::Null),
        "package_lock_hash": state.package_lock.as_ref()
            .and_then(|value| value.get("fingerprint")).cloned().unwrap_or(Value::Null),
        "selected_disciplines": state.install_lock.as_ref()
            .and_then(|value| value.get("selected_disciplines")).cloned().unwrap_or_else(|| json!([])),
        "selected_platforms": state.install_lock.as_ref()
            .and_then(|value| value.get("selected_platforms")).cloned().unwrap_or_else(|| json!([])),
        "selected_runtime_configs": state.install_lock.as_ref()
            .and_then(|value| value.get("selected_runtime_configs")).cloned().unwrap_or_else(|| json!([])),
    });
    let recovery_status = if state.recovery_unknown {
        "unknown"
    } else if state.recovery_candidates.is_empty() {
        "clean"
    } else {
        "attention"
    };
    let recovery = json!({
        "candidates": state.recovery_candidates,
        "status": recovery_status,
    });
    let mut projection = json!({
        "checks": state.checks,
        "install": install,
        "recovery": recovery,
        "target_root": target,
    });
    let fingerprint = canonical_sha256(&projection)?;
    projection
        .as_object_mut()
        .ok_or_else(|| LifecycleError::Invalid("Doctor baseline projection is invalid".to_owned()))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    Ok(projection)
}

fn inspect_recovery(target: Option<&Dir>, state: &mut BaselineState) {
    let Some(target) = target else {
        state.record(
            "recovery.residue",
            "recovery",
            "skipped",
            "Recovery residue check requires a safe install target",
            json!({}),
        );
        return;
    };

    match recovery_candidates(target) {
        Ok(candidates) => {
            state.recovery_unknown = false;
            state.recovery_candidates = candidates;
            if state.recovery_candidates.is_empty() {
                state.record(
                    "recovery.residue",
                    "recovery",
                    "passed",
                    "No interrupted lifecycle transaction residue was found",
                    json!({}),
                );
            } else {
                state.record(
                    "recovery.residue",
                    "recovery",
                    "failed",
                    "Interrupted lifecycle transaction residue requires attention",
                    json!({"candidates": state.recovery_candidates}),
                );
            }
        }
        Err(error) => {
            state.recovery_unknown = true;
            state.failed(
                "recovery.residue",
                "recovery",
                "Lifecycle recovery residue could not be inspected",
                error,
            );
        }
    }
}

fn recovery_candidates(target: &Dir) -> Result<Vec<Value>, LifecycleError> {
    let mut candidates = Vec::new();
    for entry in target.entries()? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name == LIFECYCLE_LOCK_DIRECTORY {
            candidates.push(json!({"kind": "lifecycle-lock", "path": name}));
            continue;
        }
        if let Some((_, kind)) = RECOVERY_PREFIXES
            .iter()
            .find(|(prefix, _)| name.starts_with(prefix))
        {
            candidates.push(json!({"kind": kind, "path": name}));
        }
    }
    candidates.sort_by(|left, right| {
        left.get("path")
            .and_then(Value::as_str)
            .cmp(&right.get("path").and_then(Value::as_str))
    });
    Ok(candidates)
}

fn check_layout(target: &Dir) -> Result<Value, LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    drop(open_child_file(
        target,
        "AGENTS.md",
        MANAGED_FILE_MODE,
        "global AGENTS.md",
    )?);
    drop(open_child_directory(
        target,
        "skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed skills directory",
    )?);

    let mut entries = Vec::new();
    for entry in managed.entries()? {
        let entry = entry?;
        if ignored_os_metadata(&managed, &entry.file_name())? {
            continue;
        }
        entries.push(entry.file_name().to_string_lossy().into_owned());
    }
    entries.sort();
    let mut expected = vec![
        PERSISTENT_PACKAGE_LOCK.to_owned(),
        "install-lock.json".to_owned(),
        "packages".to_owned(),
    ];
    if entries
        .iter()
        .any(|entry| entry == EXTERNAL_ACTIVATION_LOCK)
    {
        expected.push(EXTERNAL_ACTIVATION_LOCK.to_owned());
    }
    if entries
        .iter()
        .any(|entry| entry == ROLLBACK_POINT_DIRECTORY)
    {
        expected.push(ROLLBACK_POINT_DIRECTORY.to_owned());
    }
    expected.sort();
    if entries != expected {
        return Err(LifecycleError::Invalid(
            "managed metadata contains missing or unknown entries".to_owned(),
        ));
    }
    Ok(json!({"managed_entries": entries}))
}

fn load_install_lock(managed: &Dir) -> Result<Value, LifecycleError> {
    let value = load_json_file(
        managed,
        "install-lock.json",
        MANAGED_FILE_MODE,
        "Install Lock",
    )?;
    validate_install_plan(&value)?;
    if value.get("status").and_then(Value::as_str) != Some("installed") {
        return Err(LifecycleError::Invalid(
            "Install Lock status is not installed".to_owned(),
        ));
    }
    Ok(value)
}

fn load_package_lock(managed: &Dir, install_lock: &Value) -> Result<Value, LifecycleError> {
    let value = load_json_file(
        managed,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "persistent package Lockfile",
    )?;
    validate_package_lock(&value)?;
    if value.get("fingerprint") != install_lock.get("package_lock_hash") {
        return Err(LifecycleError::Invalid(
            "persistent package Lockfile fingerprint differs from Install Lock".to_owned(),
        ));
    }
    if value
        .get("install_plan_identity_hash")
        .and_then(Value::as_str)
        != Some(install_plan_identity_hash(install_lock)?.as_str())
    {
        return Err(LifecycleError::Invalid(
            "persistent package Lockfile Install Plan identity differs".to_owned(),
        ));
    }
    Ok(value)
}

fn check_core_identity(
    install_lock: &Value,
    package_lock: &Value,
) -> Result<Value, LifecycleError> {
    let versions = json!({
        "install_lock": install_lock.get("core_version").cloned().unwrap_or(Value::Null),
        "package_lock": package_lock.pointer("/core/runtime_version").cloned().unwrap_or(Value::Null),
        "runtime": env!("CARGO_PKG_VERSION"),
    });
    let install_version = versions.get("install_lock").and_then(Value::as_str);
    let package_version = versions.get("package_lock").and_then(Value::as_str);
    let runtime_version = versions.get("runtime").and_then(Value::as_str);
    if install_version != runtime_version || package_version != runtime_version {
        return Err(LifecycleError::Invalid(
            "Core runtime version differs from the installed Lockfiles".to_owned(),
        ));
    }
    Ok(versions)
}

fn check_schema_inventory(
    schemas: &DoctorSchemaSource<'_>,
    package_lock: &Value,
) -> Result<Value, LifecycleError> {
    let current = match schemas {
        DoctorSchemaSource::Filesystem(path) => schema_inventory(path)?,
        DoctorSchemaSource::Embedded(inventory) => (*inventory).clone(),
    };
    if package_lock.get("schema_inventory") != Some(&current) {
        return Err(LifecycleError::Invalid(
            "runtime Schema inventory differs from persistent package Lockfile".to_owned(),
        ));
    }
    Ok(json!({
        "content_sha256": current.get("content_sha256").cloned().unwrap_or(Value::Null),
        "file_count": current.get("files").and_then(Value::as_array).map_or(0, Vec::len),
    }))
}

fn check_activation(target: &Dir) -> Result<Value, LifecycleError> {
    let lock_path = Path::new(".agent-skills").join(EXTERNAL_ACTIVATION_LOCK);
    match target.symlink_metadata(&lock_path) {
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(json!({"managed": false}));
        }
        Err(error) => return Err(error.into()),
    }

    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let activation = load_json_file(
        &managed,
        EXTERNAL_ACTIVATION_LOCK,
        MANAGED_FILE_MODE,
        "activation Lock",
    )?;
    let (version, files) = validate_activation_lock_contract(&activation)?;
    for entry in files {
        let path = entry.get("path").and_then(Value::as_str).ok_or_else(|| {
            LifecycleError::Invalid("activation-lock file path is invalid".to_owned())
        })?;
        let mode = entry
            .get("mode")
            .and_then(Value::as_u64)
            .and_then(|value| u32::try_from(value).ok())
            .ok_or_else(|| {
                LifecycleError::Invalid("activation-lock file mode is invalid".to_owned())
            })?;
        let expected = entry.get("sha256").and_then(Value::as_str).ok_or_else(|| {
            LifecycleError::Invalid("activation-lock file hash is invalid".to_owned())
        })?;
        let actual = hash_relative_file(target, path, mode)?;
        if actual != expected {
            return Err(LifecycleError::Invalid(format!(
                "activated file differs: {path}"
            )));
        }
    }
    Ok(json!({
        "deprecation": if version == "1.0" { "blocked-new-use" } else { "current" },
        "file_count": files.len(),
        "managed": true,
        "schema_version": version,
    }))
}

fn validate_activation_lock_contract(value: &Value) -> Result<(&str, &[Value]), LifecycleError> {
    let object = value
        .as_object()
        .ok_or_else(|| LifecycleError::Invalid("activation-lock must be an object".to_owned()))?;
    let version_value = object.get("schema_version").unwrap_or(&Value::Null);
    let Some(version) = version_value.as_str() else {
        return Err(LifecycleError::Invalid(format!(
            "unsupported activation-lock schema_version: {}",
            canonical_json_inline(version_value)?
        )));
    };
    let fields = if version == "1.0" {
        &["files", "manager", "schema_version"][..]
    } else if version == "2.0" {
        &["files", "handler", "manager", "schema_version"][..]
    } else {
        return Err(LifecycleError::Invalid(format!(
            "unsupported activation-lock schema_version: {}",
            canonical_json_inline(version_value)?
        )));
    };
    if object.len() != fields.len() || fields.iter().any(|field| !object.contains_key(*field)) {
        return Err(LifecycleError::Invalid(
            "activation-lock fields differ from its versioned contract".to_owned(),
        ));
    }
    if object.get("manager").and_then(Value::as_str) != Some("agent-development-skills") {
        return Err(LifecycleError::Invalid(
            "activation-lock manager is invalid".to_owned(),
        ));
    }
    if version == "2.0"
        && object.get("handler").and_then(Value::as_str)
            != Some("core.source-activation.apple-codex-v1")
    {
        return Err(LifecycleError::Invalid(
            "activation-lock handler is invalid".to_owned(),
        ));
    }
    let files = object
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            LifecycleError::Invalid("activation-lock files must be an array".to_owned())
        })?;
    let mut paths = HashSet::with_capacity(files.len());
    for entry in files {
        let entry = entry.as_object().ok_or_else(|| {
            LifecycleError::Invalid("activation-lock file entry is invalid".to_owned())
        })?;
        if entry.len() != 3
            || ["mode", "path", "sha256"]
                .iter()
                .any(|field| !entry.contains_key(*field))
        {
            return Err(LifecycleError::Invalid(
                "activation-lock file entry is invalid".to_owned(),
            ));
        }
        let path = entry.get("path").and_then(Value::as_str).ok_or_else(|| {
            LifecycleError::Invalid("activation-lock file path is invalid".to_owned())
        })?;
        if path.is_empty() || path.starts_with('/') || path.split('/').any(|part| part == "..") {
            return Err(LifecycleError::Invalid(
                "activation-lock file path is invalid".to_owned(),
            ));
        }
        if entry
            .get("mode")
            .and_then(Value::as_u64)
            .is_none_or(|mode| mode > 0o777)
        {
            return Err(LifecycleError::Invalid(
                "activation-lock file mode is invalid".to_owned(),
            ));
        }
        if !entry
            .get("sha256")
            .and_then(Value::as_str)
            .is_some_and(valid_sha256)
        {
            return Err(LifecycleError::Invalid(
                "activation-lock file hash is invalid".to_owned(),
            ));
        }
        if !paths.insert(path) {
            return Err(LifecycleError::Invalid(
                "activation-lock file paths must be unique".to_owned(),
            ));
        }
    }
    Ok((version, files))
}

fn hash_relative_file(root: &Dir, relative: &str, mode: u32) -> Result<String, LifecycleError> {
    let mut file = open_relative_file(root, relative, mode)?;
    let opened = file.metadata()?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024].into_boxed_slice();
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let after = file.metadata()?;
    let current = open_relative_file(root, relative, mode)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
    {
        return Err(LifecycleError::Invalid(
            "activated file changed while reading".to_owned(),
        ));
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn open_relative_file(
    root: &Dir,
    relative: &str,
    mode: u32,
) -> Result<cap_std::fs::File, LifecycleError> {
    let path = Path::new(relative);
    if path.is_absolute() {
        return Err(LifecycleError::Invalid(
            "activated file must be a package-relative path".to_owned(),
        ));
    }
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            std::path::Component::Normal(part) => {
                parts.push(part.to_str().ok_or_else(|| {
                    LifecycleError::Invalid("activated file path is invalid".to_owned())
                })?);
            }
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir
            | std::path::Component::Prefix(_)
            | std::path::Component::RootDir => {
                return Err(LifecycleError::Invalid(
                    "activated file must be a package-relative path".to_owned(),
                ));
            }
        }
    }
    let (name, parents) = parts.split_last().ok_or_else(|| {
        LifecycleError::Invalid("activated file must be a package-relative path".to_owned())
    })?;
    let mut directory = root.try_clone()?;
    for parent in parents {
        match directory.symlink_metadata(parent) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(LifecycleError::Invalid(format!(
                    "activated file must not traverse a symlink: {relative}"
                )));
            }
            Ok(metadata) if metadata.is_dir() => {}
            _ => {
                return Err(LifecycleError::Invalid(format!(
                    "activated file differs: {relative}"
                )));
            }
        }
        directory = open_child_directory(&directory, parent, None, "activation directory")
            .map_err(|_| LifecycleError::Invalid(format!("activated file differs: {relative}")))?;
    }
    match directory.symlink_metadata(name) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(LifecycleError::Invalid(format!(
                "activated file must not traverse a symlink: {relative}"
            )));
        }
        Ok(metadata) if metadata.is_file() => {}
        _ => {
            return Err(LifecycleError::Invalid(format!(
                "activated file differs: {relative}"
            )));
        }
    }
    open_child_file(&directory, name, mode, "activated file")
        .map_err(|_| LifecycleError::Invalid(format!("activated file differs: {relative}")))
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn canonical_json_inline(value: &Value) -> Result<String, LifecycleError> {
    let mut bytes = canonical_json(value)?;
    if bytes.last() == Some(&b'\n') {
        bytes.pop();
    }
    String::from_utf8(bytes)
        .map_err(|_| LifecycleError::Invalid("canonical JSON must be UTF-8".to_owned()))
}

fn open_root_directory(path: &Path, mode: Option<u32>, label: &str) -> Result<Dir, LifecycleError> {
    let directory = open_absolute_directory_nofollow(path)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    let opened = directory.dir_metadata()?;
    if !opened.is_dir() {
        return Err(LifecycleError::Invalid(format!(
            "{label} is missing or unsafe"
        )));
    }
    if let Some(mode) = mode {
        require_cap_mode(&opened, mode, label)?;
    }
    let current = open_absolute_directory_nofollow(path)
        .and_then(|directory| Ok(directory.dir_metadata()?))
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    if !same_object_cap(&opened, &current) {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while opening"
        )));
    }
    if let Some(mode) = mode {
        require_cap_mode(&current, mode, label)?;
    }
    Ok(directory)
}

fn open_child_directory(
    parent: &Dir,
    name: &str,
    mode: Option<u32>,
    label: &str,
) -> Result<Dir, LifecycleError> {
    open_child_directory_with_hook(parent, name, mode, label, || Ok(()))
}

fn open_child_directory_with_hook(
    parent: &Dir,
    name: &str,
    mode: Option<u32>,
    label: &str,
    before_open: impl FnOnce() -> Result<(), LifecycleError>,
) -> Result<Dir, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_dir() {
        return Err(LifecycleError::Invalid(format!(
            "{label} is missing or unsafe"
        )));
    }
    before_open()?;
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
    if after.file_type().is_symlink() || !after.is_dir() || !same_object_cap(&opened, &current) {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while opening"
        )));
    }
    if let Some(mode) = mode {
        require_cap_mode(&opened, mode, label)?;
        require_cap_mode(&current, mode, label)?;
    }
    Ok(directory)
}

fn open_child_file(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<cap_std::fs::File, LifecycleError> {
    open_child_file_with_hook(parent, name, mode, label, || Ok(()))
}

fn open_child_file_with_hook(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
    before_open: impl FnOnce() -> Result<(), LifecycleError>,
) -> Result<cap_std::fs::File, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_file() {
        return Err(LifecycleError::Invalid(format!(
            "{label} is missing or unsafe"
        )));
    }
    before_open()?;
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
    let current = parent
        .open_with(name, &options)
        .and_then(|file| file.metadata())
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    if after.file_type().is_symlink() || !after.is_file() || !same_object_cap(&opened, &current) {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while opening"
        )));
    }
    require_cap_mode(&opened, mode, label)?;
    require_cap_mode(&current, mode, label)?;
    Ok(file)
}

fn load_json_file(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<Value, LifecycleError> {
    let mut file = open_child_file(parent, name, mode, label)?;
    let opened = file.metadata()?;
    let length = opened.len();
    if length > MAX_CONTRACT_JSON_BYTES as u64 {
        return Err(ContractError::InputTooLarge {
            maximum: MAX_CONTRACT_JSON_BYTES,
        }
        .into());
    }
    let mut bytes = Vec::with_capacity(
        usize::try_from(length)
            .unwrap_or(MAX_CONTRACT_JSON_BYTES)
            .min(MAX_CONTRACT_JSON_BYTES),
    );
    file.by_ref()
        .take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_CONTRACT_JSON_BYTES {
        return Err(ContractError::InputTooLarge {
            maximum: MAX_CONTRACT_JSON_BYTES,
        }
        .into());
    }
    let after_file = file.metadata()?;
    let after_path = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while reading")))?;
    let after_opened = open_child_file(parent, name, mode, label)?.metadata()?;
    if after_path.file_type().is_symlink()
        || !after_path.is_file()
        || !same_object_cap(&opened, &after_file)
        || !same_object_cap(&opened, &after_opened)
        || !same_content_state_cap(&opened, &after_file)
        || !same_content_state_cap(&opened, &after_opened)
    {
        return Err(LifecycleError::Invalid(format!(
            "{label} changed while reading"
        )));
    }
    Ok(parse_json(&bytes)?)
}

fn open_absolute_directory_nofollow(path: &Path) -> Result<Dir, LifecycleError> {
    let input = absolute_path(path)?;
    if !input.is_absolute() {
        return Err(LifecycleError::Invalid(
            "directory path must be absolute".to_owned(),
        ));
    }
    let mut absolute = PathBuf::new();
    for component in input.components() {
        match component {
            std::path::Component::Prefix(prefix) => absolute.push(prefix.as_os_str()),
            std::path::Component::RootDir => absolute.push(component.as_os_str()),
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                return Err(LifecycleError::Invalid(
                    "directory parent traversal is forbidden".to_owned(),
                ));
            }
            std::path::Component::Normal(part) => {
                absolute.push(part);
                let metadata = std::fs::symlink_metadata(&absolute)?;
                if metadata.file_type().is_symlink() && absolute.components().count() <= 2 {
                    absolute = std::fs::canonicalize(&absolute)?;
                } else if metadata.file_type().is_symlink() || !metadata.is_dir() {
                    return Err(LifecycleError::Invalid(
                        "directory path is missing or unsafe".to_owned(),
                    ));
                }
            }
        }
    }
    let mut root = PathBuf::new();
    let mut children = Vec::new();
    for component in absolute.components() {
        match component {
            std::path::Component::Prefix(prefix) => root.push(prefix.as_os_str()),
            std::path::Component::RootDir => root.push(component.as_os_str()),
            std::path::Component::CurDir => {}
            std::path::Component::Normal(part) => children.push(part.to_owned()),
            std::path::Component::ParentDir => {
                return Err(LifecycleError::Invalid(
                    "directory parent traversal is forbidden".to_owned(),
                ));
            }
        }
    }
    if root.as_os_str().is_empty() {
        return Err(LifecycleError::Invalid(
            "directory path must be absolute".to_owned(),
        ));
    }
    let mut directory = Dir::open_ambient_dir(&root, ambient_authority())?;
    for child in children {
        directory = directory.open_dir_nofollow(child)?;
    }
    Ok(directory)
}

fn ignored_os_metadata(parent: &Dir, name: &std::ffi::OsStr) -> Result<bool, LifecycleError> {
    if name != ".DS_Store" {
        return Ok(false);
    }
    let metadata = parent.symlink_metadata(name)?;
    Ok(!metadata.file_type().is_symlink() && metadata.is_file())
}

#[cfg(unix)]
pub(crate) fn configure_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
}

#[cfg(windows)]
pub(crate) fn configure_nofollow(options: &mut OpenOptions) {
    use cap_std::fs::OpenOptionsExt as _;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(unix, windows)))]
pub(crate) fn configure_nofollow(_options: &mut OpenOptions) {}

#[cfg(unix)]
fn require_cap_mode(
    metadata: &cap_std::fs::Metadata,
    expected: u32,
    label: &str,
) -> Result<(), LifecycleError> {
    use cap_std::fs::MetadataExt as _;
    if metadata.mode() & 0o777 != expected {
        return Err(LifecycleError::Invalid(format!(
            "{label} mode is not canonical"
        )));
    }
    Ok(())
}

#[cfg(not(unix))]
#[allow(clippy::unnecessary_wraps)]
fn require_cap_mode(
    _metadata: &cap_std::fs::Metadata,
    _expected: u32,
    _label: &str,
) -> Result<(), LifecycleError> {
    Ok(())
}

#[cfg(unix)]
fn same_object_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    use cap_std::fs::MetadataExt as _;
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(unix)]
fn same_content_state_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    use cap_std::fs::MetadataExt as _;
    left.size() == right.size()
        && left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime() == right.ctime()
        && left.ctime_nsec() == right.ctime_nsec()
}

#[cfg(windows)]
fn same_object_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    use cap_fs_ext::MetadataExt as _;
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(windows)]
fn same_content_state_cap(left: &cap_std::fs::Metadata, right: &cap_std::fs::Metadata) -> bool {
    left.len() == right.len()
        && left.modified().ok().is_some()
        && left.modified().ok() == right.modified().ok()
}

#[cfg(not(any(unix, windows)))]
fn same_object_cap(_left: &cap_std::fs::Metadata, _right: &cap_std::fs::Metadata) -> bool {
    false
}

#[cfg(not(any(unix, windows)))]
fn same_content_state_cap(_left: &cap_std::fs::Metadata, _right: &cap_std::fs::Metadata) -> bool {
    false
}

fn absolute_path(path: &Path) -> Result<PathBuf, LifecycleError> {
    if path.is_absolute() {
        Ok(path.to_path_buf())
    } else {
        Ok(std::env::current_dir()?.join(path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn temporary_root(label: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "agent-lifecycle-{label}-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).expect("create lifecycle test root");
        root
    }

    fn check<'a>(value: &'a Value, check_id: &str) -> &'a Value {
        value
            .get("checks")
            .and_then(Value::as_array)
            .and_then(|checks| {
                checks
                    .iter()
                    .find(|check| check.get("id").and_then(Value::as_str) == Some(check_id))
            })
            .expect("Doctor baseline check")
    }

    #[test]
    fn empty_target_is_read_only_and_reports_dependencies_as_skipped() {
        let root = temporary_root("empty");
        let before = std::fs::read_dir(&root)
            .expect("read lifecycle test root")
            .count();
        let value = inspect_doctor_baseline(&root, root.join("schemas"))
            .expect("inspect empty lifecycle target");
        assert_eq!(
            check(&value, "filesystem.target").get("status"),
            Some(&json!("passed"))
        );
        assert_eq!(
            check(&value, "filesystem.layout").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            check(&value, "lock.persistent").get("status"),
            Some(&json!("skipped"))
        );
        assert_eq!(
            check(&value, "environment.core").get("status"),
            Some(&json!("skipped"))
        );
        assert_eq!(
            check(&value, "schema.inventory").get("status"),
            Some(&json!("skipped"))
        );
        assert_eq!(
            check(&value, "activation.integrity").get("status"),
            Some(&json!("passed"))
        );
        assert_eq!(
            check(&value, "activation.integrity").get("details"),
            Some(&json!({"managed": false}))
        );
        assert_eq!(
            std::fs::read_dir(&root)
                .expect("re-read lifecycle test root")
                .count(),
            before
        );
        std::fs::remove_dir(&root).expect("remove lifecycle test root");
    }

    #[test]
    fn doctor_report_v1_requires_explicit_canonical_host_attestation() {
        let root = temporary_root("doctor-report");
        let report = inspect_doctor_report_v1(&root, root.join("schemas"), "3.11.0")
            .expect("emit complete Doctor Report v1");
        assert_eq!(report.get("schema_version"), Some(&json!("1.0")));
        assert_eq!(report.get("status"), Some(&json!("blocked")));
        assert_eq!(
            report.pointer("/checks/0"),
            Some(&json!({
                "category": "environment",
                "details": {"actual": "3.11.0", "required": ">=3.11"},
                "id": "environment.python",
                "status": "passed",
                "summary": "Python runtime satisfies the supported baseline",
            }))
        );
        validate_doctor_report_v1(&report).expect("validate emitted Doctor Report v1");

        let unsupported = inspect_doctor_report_v1(&root, root.join("schemas"), "3.10.9")
            .expect("emit unsupported Python Doctor Report");
        assert_eq!(
            unsupported.pointer("/checks/0/status"),
            Some(&json!("failed"))
        );

        for invalid_version in ["", "3.11", "03.11.0", "3.11.0.1", "3.11.x"] {
            assert!(
                inspect_doctor_report_v1(&root, root.join("schemas"), invalid_version).is_err()
            );
        }

        let mut tampered = report;
        tampered["summary"]["passed"] = json!(999);
        assert!(validate_doctor_report_v1(&tampered).is_err());
        std::fs::remove_dir(&root).expect("remove lifecycle test root");
    }

    #[test]
    fn doctor_report_v2_is_runtime_neutral_and_self_contained() {
        let root = temporary_root("doctor-report-v2");
        let report = inspect_doctor_report_v2(&root).expect("emit Doctor Report v2");
        assert_eq!(report.get("schema_version"), Some(&json!("2.0")));
        assert_eq!(report.get("status"), Some(&json!("blocked")));
        assert_eq!(
            report.pointer("/environment/implementation/name"),
            Some(&json!("agent-skills-rs"))
        );
        assert!(
            report
                .pointer("/environment/schema_inventory/file_count")
                .and_then(Value::as_u64)
                .is_some_and(|count| count > 0)
        );
        assert!(report.pointer("/environment/python_version").is_none());
        assert!(
            report
                .get("checks")
                .and_then(Value::as_array)
                .is_some_and(|checks| checks.iter().all(|check| {
                    check.get("id").and_then(Value::as_str) != Some("environment.python")
                }))
        );
        validate_doctor_report_v2(&report).expect("validate emitted Doctor Report v2");

        let mut tampered = report.clone();
        tampered["environment"]["schema_inventory"]["file_count"] = json!(0);
        assert!(validate_doctor_report_v2(&tampered).is_err());
        std::fs::remove_dir(&root).expect("remove lifecycle test root");
    }

    #[test]
    fn recovery_residue_is_sorted_and_fail_closed() {
        let root = temporary_root("recovery");
        for name in [
            ".agent-skills-uninstall-backup-z",
            ".agent-skills-lifecycle.lock",
            ".agent-skills-stage-a",
        ] {
            std::fs::create_dir(root.join(name)).expect("create recovery residue");
        }
        let value = inspect_doctor_baseline(&root, root.join("schemas"))
            .expect("inspect lifecycle residue");
        assert_eq!(
            check(&value, "recovery.residue").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            value.pointer("/recovery/candidates"),
            Some(&json!([
                {"kind": "lifecycle-lock", "path": ".agent-skills-lifecycle.lock"},
                {"kind": "install-stage", "path": ".agent-skills-stage-a"},
                {"kind": "uninstall-backup", "path": ".agent-skills-uninstall-backup-z"},
            ]))
        );
        std::fs::remove_dir_all(&root).expect("remove lifecycle test root");
    }

    #[test]
    fn core_identity_requires_runtime_and_both_locks_to_match() {
        let install_lock = json!({"core_version": env!("CARGO_PKG_VERSION")});
        let package_lock = json!({"core": {"runtime_version": env!("CARGO_PKG_VERSION")}});
        assert_eq!(
            check_core_identity(&install_lock, &package_lock).expect("matching Core identity"),
            json!({
                "install_lock": env!("CARGO_PKG_VERSION"),
                "package_lock": env!("CARGO_PKG_VERSION"),
                "runtime": env!("CARGO_PKG_VERSION"),
            })
        );

        let stale_install = json!({"core_version": "stale"});
        let error = check_core_identity(&stale_install, &package_lock)
            .expect_err("stale Install Lock Core version must fail");
        assert_eq!(
            error.to_string(),
            "Core runtime version differs from the installed Lockfiles"
        );
    }

    #[test]
    fn activation_lock_versions_and_paths_are_fail_closed() {
        let legacy = json!({
            "files": [{
                "mode": 0o644,
                "path": "agents/reviewer.toml",
                "sha256": "a".repeat(64),
            }],
            "manager": "agent-development-skills",
            "schema_version": "1.0",
        });
        let (version, files) =
            validate_activation_lock_contract(&legacy).expect("legacy activation Lock");
        assert_eq!(version, "1.0");
        assert_eq!(files.len(), 1);

        let current = json!({
            "files": [],
            "handler": "core.source-activation.apple-codex-v1",
            "manager": "agent-development-skills",
            "schema_version": "2.0",
        });
        assert_eq!(
            validate_activation_lock_contract(&current)
                .expect("current activation Lock")
                .0,
            "2.0"
        );

        let mut unsafe_path = legacy.clone();
        unsafe_path["files"][0]["path"] = json!("../outside");
        assert!(
            validate_activation_lock_contract(&unsafe_path)
                .expect_err("unsafe activation path must fail")
                .to_string()
                .contains("path is invalid")
        );

        let mut duplicate = legacy.clone();
        let duplicate_entry = duplicate["files"][0].clone();
        duplicate["files"]
            .as_array_mut()
            .expect("activation files")
            .push(duplicate_entry);
        assert_eq!(
            validate_activation_lock_contract(&duplicate)
                .expect_err("duplicate paths must fail")
                .to_string(),
            "activation-lock file paths must be unique"
        );

        let mut invalid_version = legacy;
        invalid_version["schema_version"] = json!(1);
        assert_eq!(
            validate_activation_lock_contract(&invalid_version)
                .expect_err("non-string version must fail")
                .to_string(),
            "unsupported activation-lock schema_version: 1"
        );

        for (invalid, expected) in [
            (
                json!("a'b"),
                r#"unsupported activation-lock schema_version: "a'b""#,
            ),
            (
                json!("a\u{2028}b"),
                "unsupported activation-lock schema_version: \"a\u{2028}b\"",
            ),
            (
                json!("a\u{034f}b"),
                "unsupported activation-lock schema_version: \"a\u{034f}b\"",
            ),
        ] {
            invalid_version["schema_version"] = invalid;
            assert_eq!(
                validate_activation_lock_contract(&invalid_version)
                    .expect_err("unsupported string version must fail")
                    .to_string(),
                expected
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn symlink_target_and_managed_root_are_rejected() {
        use std::os::unix::fs::symlink;

        let root = temporary_root("symlink");
        let real = root.join("real");
        std::fs::create_dir(&real).expect("create real target");
        let linked = root.join("linked");
        symlink(&real, &linked).expect("create target symlink");
        let linked_value =
            inspect_doctor_baseline(&linked, root.join("schemas")).expect("inspect linked target");
        assert_eq!(
            check(&linked_value, "filesystem.target").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            linked_value.pointer("/recovery/status"),
            Some(&json!("unknown"))
        );

        let target = root.join("target");
        std::fs::create_dir(&target).expect("create target");
        symlink(&real, target.join(".agent-skills")).expect("create managed-root symlink");
        let managed_value = inspect_doctor_baseline(&target, root.join("schemas"))
            .expect("inspect linked managed root");
        assert_eq!(
            check(&managed_value, "filesystem.layout").get("status"),
            Some(&json!("failed"))
        );
        assert_eq!(
            check(&managed_value, "install.lock").get("status"),
            Some(&json!("failed"))
        );

        let nested = real.join("nested");
        std::fs::create_dir(&nested).expect("create nested real target");
        let intermediate = root.join("intermediate");
        symlink(&real, &intermediate).expect("create intermediate target symlink");
        let intermediate_value =
            inspect_doctor_baseline(intermediate.join("nested"), root.join("schemas"))
                .expect("inspect intermediate linked target");
        assert_eq!(
            check(&intermediate_value, "filesystem.target").get("status"),
            Some(&json!("failed"))
        );
        std::fs::remove_dir_all(&root).expect("remove lifecycle test root");
    }

    #[cfg(unix)]
    #[test]
    fn canonical_modes_are_rechecked_after_replacement() {
        use std::os::unix::fs::PermissionsExt as _;

        let root = temporary_root("mode-swap");
        let directory = Dir::open_ambient_dir(&root, ambient_authority())
            .expect("open lifecycle mode-swap root");

        let managed = root.join(".agent-skills");
        let original_managed = root.join(".agent-skills-original");
        let replacement_managed = root.join(".agent-skills-replacement");
        std::fs::create_dir(&managed).expect("create canonical managed directory");
        std::fs::create_dir(&replacement_managed).expect("create replacement managed directory");
        std::fs::set_permissions(&managed, std::fs::Permissions::from_mode(0o755))
            .expect("set canonical directory mode");
        std::fs::set_permissions(&replacement_managed, std::fs::Permissions::from_mode(0o777))
            .expect("set noncanonical directory mode");
        let error = open_child_directory_with_hook(
            &directory,
            ".agent-skills",
            Some(0o755),
            "managed directory",
            || {
                std::fs::rename(&managed, &original_managed)?;
                std::fs::rename(&replacement_managed, &managed)?;
                Ok(())
            },
        )
        .expect_err("directory mode replacement must fail");
        assert!(error.to_string().contains("mode is not canonical"));

        for name in ["install-lock.json", PERSISTENT_PACKAGE_LOCK] {
            let path = root.join(name);
            let original = root.join(format!("{name}.original"));
            let replacement = root.join(format!("{name}.replacement"));
            std::fs::write(&path, b"{}\n").expect("write canonical contract");
            std::fs::write(&replacement, b"{}\n").expect("write replacement contract");
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644))
                .expect("set canonical contract mode");
            std::fs::set_permissions(&replacement, std::fs::Permissions::from_mode(0o666))
                .expect("set noncanonical contract mode");
            let error =
                open_child_file_with_hook(&directory, name, 0o644, "managed contract", || {
                    std::fs::rename(&path, &original)?;
                    std::fs::rename(&replacement, &path)?;
                    Ok(())
                })
                .expect_err("file mode replacement must fail");
            assert!(error.to_string().contains("mode is not canonical"));
        }

        drop(directory);
        std::fs::remove_dir_all(root).expect("remove lifecycle mode-swap root");
    }

    #[cfg(windows)]
    #[test]
    fn target_rejects_drive_relative_prefix() {
        let value = inspect_doctor_baseline(Path::new(r"C:relative"), Path::new(r"C:schemas"))
            .expect("Doctor reports unsafe target as a failed check");
        assert_eq!(
            check(&value, "filesystem.target").get("status"),
            Some(&json!("failed"))
        );
    }
}
