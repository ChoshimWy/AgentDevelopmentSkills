use crate::{
    LifecycleError, LifecycleLock, LifecycleWorkspace, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    PERSISTENT_PACKAGE_LOCK, ValidatedInstallPlan, load_json_file, open_child_directory, rollback,
    rollback_stage,
};
use serde_json::{Value, json};
use std::path::Path;

/// Restore the exact persistent rollback point under one guarded transaction.
///
/// Both approvals are compared before any stage or backup workspace is
/// created. The current installation, persistent rollback point, desired
/// managed projection, external scope, and generated reverse rollback point
/// remain bound to the same lifecycle lock. Publication uses the regular
/// `PublishedInstall` recovery window, so any managed or external failure
/// restores the exact pre-rollback installation.
///
/// # Errors
/// Returns a fail-closed error for an unsafe or incomplete installation,
/// invalid/tampered rollback evidence, stale approvals, namespace drift,
/// external restoration failure, or incomplete recovery.
pub fn rollback_source_install(
    target_root: impl AsRef<Path>,
    approve_current_lock: &str,
    approve_rollback_point: &str,
) -> Result<Value, LifecycleError> {
    let lock = LifecycleLock::acquire_existing(target_root)?;
    let target = lock.target_directory()?;
    let managed = open_child_directory(
        &target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let current_install = load_json_file(
        &managed,
        "install-lock.json",
        MANAGED_FILE_MODE,
        "current Install Lock",
    )?;
    let current_package = load_json_file(
        &managed,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "current package Lockfile",
    )?;
    if current_install.get("status").and_then(Value::as_str) != Some("installed") {
        return invalid("rollback requires an installed Install Lock");
    }
    ValidatedInstallPlan::new(current_install, current_package.clone())?;
    let current_hash = current_package
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("current package Lockfile fingerprint is invalid".to_owned())
        })?
        .to_owned();
    let persistent = rollback::open_persistent_rollback_point(&target)?;
    rollback_stage::verify_source_install(&target, persistent.external_paths())?;
    lock.validate()?;
    if current_hash != approve_current_lock {
        return invalid("rollback requires approval of the exact current Lockfile");
    }
    let point_fingerprint = persistent
        .point()
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid("rollback point fingerprint is invalid".to_owned()))?
        .to_owned();
    if point_fingerprint != approve_rollback_point {
        return invalid("rollback requires approval of the exact rollback point");
    }
    let restored_lock_hash = persistent
        .point()
        .get("package_lock_hash")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("rollback point package Lock identity is invalid".to_owned())
        })?
        .to_owned();

    let mut workspace = LifecycleWorkspace::from_lock(lock)?;
    let (plan, reverse_point) = workspace.stage_persistent_rollback_install(&persistent)?;
    let mut published = workspace.publish_staged_install(&plan)?;
    published.apply_persistent_rollback(&point_fingerprint)?;
    published.verify(&plan)?;
    published.commit(&plan)?;
    Ok(json!({
        "from_lock_hash": current_hash,
        "restored_lock_hash": restored_lock_hash,
        "rollback_point": reverse_point,
        "status": "rolled-back",
    }))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}
