use super::{
    LifecycleError, LifecycleLock, MANAGED_DIRECTORY_MODE, MANAGED_FILE_MODE,
    PERSISTENT_PACKAGE_LOCK, load_json_file, open_child_directory, rollback_stage,
    source_activation::{
        ACTIVATION_HANDLER_ID, DEACTIVATION_HANDLER_ID, PRESERVE_HANDLER_ID, SourceActivation,
        SourceDeactivation,
    },
};
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256};
use agent_engine::{install_plan_identity_hash, validate_install_plan, validate_package_lock};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::path::Path;

const RECEIPT_SCHEMA_VERSION: &str = "1.0";
const RECEIPT_MANAGER: &str = "agent-development-skills";

/// Filesystem-frozen inputs for native Upgrade Plan compilation.
///
/// The receipt is intentionally created only by the lifecycle crate. It binds
/// the current installation, candidate identities, exact external scope,
/// trusted handler implementation, rollback snapshot and any source Activation
/// migration discovered from the same capability-held target.
pub struct UpgradePlanningSnapshot {
    pub(super) activation_owned: bool,
    pub(super) current_install_plan: Value,
    pub(super) current_package_lock: Value,
    pub(super) external_paths: Vec<String>,
    pub(super) handler: String,
    pub(super) handler_sha256: String,
    pub(super) lock: LifecycleLock,
    pub(super) receipt: Value,
    pub(super) target_root: String,
}

impl UpgradePlanningSnapshot {
    pub(super) fn validate_lock(&self) -> Result<(), LifecycleError> {
        self.lock.validate()
    }

    pub(super) fn into_workspace(self) -> Result<crate::LifecycleWorkspace, LifecycleError> {
        crate::LifecycleWorkspace::from_lock(self.lock)
    }
}

/// Inspect an installed target and issue a trusted external-scope receipt.
///
/// # Errors
/// Returns a fail-closed error when target state is unsafe, current or
/// candidate identities are invalid, Activation ownership is inconsistent,
/// the requested partial removal violates ownership policy, or the rollback
/// scope cannot be frozen and revalidated.
#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
pub fn inspect_upgrade_planning_snapshot(
    target_root: impl AsRef<Path>,
    candidate_install_plan: &Value,
    candidate_package_lock: &Value,
    action: &str,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
    session_launcher: Option<&[u8]>,
) -> Result<UpgradePlanningSnapshot, LifecycleError> {
    inspect_upgrade_planning_snapshot_with_migration_policy(
        target_root,
        candidate_install_plan,
        candidate_package_lock,
        action,
        removed_platforms,
        removed_runtime_configs,
        session_launcher,
        true,
    )
}

/// Inspect an installed target using the legacy Python Upgrade Plan migration
/// projection.
///
/// The compatibility command intentionally reports only schema migrations,
/// matching the Python control plane. Native lifecycle commands use
/// [`inspect_upgrade_planning_snapshot`] and additionally bind activation-state
/// changes such as replacing the native launcher.
///
/// # Errors
/// Returns the same fail-closed errors as
/// [`inspect_upgrade_planning_snapshot`].
pub fn inspect_upgrade_planning_snapshot_compatibility(
    target_root: impl AsRef<Path>,
    candidate_install_plan: &Value,
    candidate_package_lock: &Value,
    action: &str,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
    session_launcher: Option<&[u8]>,
) -> Result<UpgradePlanningSnapshot, LifecycleError> {
    inspect_upgrade_planning_snapshot_with_migration_policy(
        target_root,
        candidate_install_plan,
        candidate_package_lock,
        action,
        removed_platforms,
        removed_runtime_configs,
        session_launcher,
        false,
    )
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
fn inspect_upgrade_planning_snapshot_with_migration_policy(
    target_root: impl AsRef<Path>,
    candidate_install_plan: &Value,
    candidate_package_lock: &Value,
    action: &str,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
    session_launcher: Option<&[u8]>,
    report_activation_state_migrations: bool,
) -> Result<UpgradePlanningSnapshot, LifecycleError> {
    if !matches!(action, "upgrade" | "partial-uninstall") {
        return invalid("upgrade action is invalid");
    }
    let removed_platforms = normalized_unique(removed_platforms, "removed platforms")?;
    let removed_runtime_configs =
        normalized_unique(removed_runtime_configs, "removed runtime configs")?;
    if action == "upgrade" && (!removed_platforms.is_empty() || !removed_runtime_configs.is_empty())
    {
        return invalid("upgrade action must not contain a removal request");
    }

    validate_install_plan(candidate_install_plan)?;
    validate_package_lock(candidate_package_lock)?;
    require_install_lock_anchor(
        candidate_install_plan,
        candidate_package_lock,
        "planned",
        "candidate",
    )?;

    let lock = LifecycleLock::acquire_existing(target_root)?;
    let target_path = lock.target().to_path_buf();
    let target_root = target_path
        .to_str()
        .ok_or_else(|| LifecycleError::Invalid("upgrade target root is not UTF-8".to_owned()))?
        .to_owned();
    let target = lock.target_directory()?;
    let managed = open_child_directory(
        &target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let current_install_plan = load_json_file(
        &managed,
        "install-lock.json",
        MANAGED_FILE_MODE,
        "current Install Lock",
    )?;
    let current_package_lock = load_json_file(
        &managed,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "current package Lockfile",
    )?;
    validate_install_plan(&current_install_plan)?;
    validate_package_lock(&current_package_lock)?;
    require_install_lock_anchor(
        &current_install_plan,
        &current_package_lock,
        "installed",
        "current",
    )?;

    let current_selection = selection(&current_install_plan)?;
    let candidate_selection = selection(candidate_install_plan)?;
    let deactivation = SourceDeactivation::prepare_for_uninstall(&target, &target_path)?;
    let activation_owned = deactivation.is_some();
    if activation_owned
        && !(selection_contains(&current_selection, "platforms", "apple")
            && selection_contains(&current_selection, "runtime_configs", "codex"))
    {
        return invalid(
            "source Activation ownership requires the installed Apple and codex selection",
        );
    }

    let (handler, paths, migrations, activation) = if action == "partial-uninstall" {
        let expected_removed_runtime =
            if activation_owned && removed_platforms.iter().any(|value| value == "apple") {
                vec!["codex".to_owned()]
            } else {
                Vec::new()
            };
        if removed_runtime_configs != expected_removed_runtime {
            return invalid(
                "partial uninstall may remove only activation-owned codex with an activated Apple platform",
            );
        }
        match deactivation.as_ref() {
            None => ("none", Vec::new(), Vec::new(), None),
            Some(prepared) if removed_platforms.iter().any(|value| value == "apple") => (
                DEACTIVATION_HANDLER_ID,
                prepared.scope().to_vec(),
                Vec::new(),
                None,
            ),
            Some(prepared) => (
                PRESERVE_HANDLER_ID,
                prepared.scope().to_vec(),
                Vec::new(),
                None,
            ),
        }
    } else {
        match deactivation.as_ref() {
            None => ("none", Vec::new(), Vec::new(), None),
            Some(_prepared)
                if selection_contains(&candidate_selection, "platforms", "apple")
                    && selection_contains(&candidate_selection, "runtime_configs", "codex") =>
            {
                let launcher = session_launcher.ok_or_else(|| {
                    LifecycleError::Invalid(
                        "native Apple source upgrade requires --session-launcher".to_owned(),
                    )
                })?;
                let activation = SourceActivation::prepare(&target, &target_path, launcher)?;
                let migration = activation
                    .preview()
                    .get("migration")
                    .filter(|value| !value.is_null())
                    .map(planned_migration)
                    .transpose()?
                    .filter(|value| {
                        report_activation_state_migrations
                            || value.get("artifact").and_then(Value::as_str)
                                != Some("source-activation-state")
                    });
                (
                    ACTIVATION_HANDLER_ID,
                    activation.scope().to_vec(),
                    migration.into_iter().collect(),
                    Some(activation),
                )
            }
            Some(prepared) => (
                DEACTIVATION_HANDLER_ID,
                prepared.scope().to_vec(),
                Vec::new(),
                None,
            ),
        }
    };
    require_sorted_unique_paths(&paths)?;
    let (rollback_point, external_state) = rollback_stage::preview(&target, &paths)?;

    if let Some(prepared) = activation.as_ref() {
        prepared.revalidate(&target)?;
    } else if let Some(prepared) = deactivation.as_ref() {
        prepared.revalidate(&target)?;
    }
    let (revalidated_rollback, revalidated_external_state) =
        rollback_stage::preview(&target, &paths)?;
    if revalidated_rollback != rollback_point || revalidated_external_state != external_state {
        return invalid("rollback source changed while issuing upgrade scope receipt");
    }
    let current_install_plan_after = load_json_file(
        &managed,
        "install-lock.json",
        MANAGED_FILE_MODE,
        "current Install Lock",
    )?;
    let current_package_lock_after = load_json_file(
        &managed,
        PERSISTENT_PACKAGE_LOCK,
        MANAGED_FILE_MODE,
        "current package Lockfile",
    )?;
    if current_install_plan_after != current_install_plan
        || current_package_lock_after != current_package_lock
    {
        return invalid("current installation changed while issuing upgrade scope receipt");
    }
    lock.validate()?;

    let handler_sha256 = if handler == "none" {
        canonical_sha256(&Value::String("none".to_owned()))?
    } else {
        env!("AGENT_LIFECYCLE_SOURCE_SHA256").to_owned()
    };
    let external_state_sha256 = external_state
        .get("fingerprint")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            LifecycleError::Invalid("rollback external state fingerprint is invalid".to_owned())
        })?;
    if rollback_point
        .get("external_state_sha256")
        .and_then(Value::as_str)
        != Some(external_state_sha256)
    {
        return invalid("rollback point does not bind the frozen external state");
    }

    let paths_sha256 = canonical_sha256(&json!(paths))?;
    let mut receipt = json!({
        "action": action,
        "activation_owned": activation_owned,
        "candidate": identity(candidate_install_plan, candidate_package_lock)?,
        "current": identity(&current_install_plan, &current_package_lock)?,
        "external": {
            "handler": handler,
            "handler_sha256": handler_sha256,
            "path_count": paths.len(),
            "paths": paths,
            "paths_sha256": paths_sha256,
            "state_sha256": external_state_sha256,
        },
        "fingerprint": "",
        "manager": RECEIPT_MANAGER,
        "migrations": migrations,
        "removed_platforms": removed_platforms,
        "removed_runtime_configs": removed_runtime_configs,
        "rollback_point": rollback_point,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "target_root": target_root,
    });
    let identity = receipt
        .as_object_mut()
        .ok_or_else(|| LifecycleError::Invalid("upgrade scope receipt is invalid".to_owned()))?;
    identity.remove("fingerprint");
    let fingerprint = canonical_sha256(&Value::Object(identity.clone()))?;
    identity.insert("fingerprint".to_owned(), Value::String(fingerprint));
    if canonical_json(&receipt)?.len() > MAX_CONTRACT_JSON_BYTES {
        return invalid("upgrade scope receipt exceeds the contract size limit");
    }

    Ok(UpgradePlanningSnapshot {
        activation_owned,
        current_install_plan,
        current_package_lock,
        external_paths: paths,
        handler: handler.to_owned(),
        handler_sha256,
        lock,
        receipt,
        target_root,
    })
}

fn identity(install_plan: &Value, package_lock: &Value) -> Result<Value, LifecycleError> {
    Ok(json!({
        "install_plan_fingerprint": required_string(install_plan, "fingerprint")?,
        "install_plan_identity_hash": install_plan_identity_hash(install_plan)?,
        "package_lock_hash": required_string(package_lock, "fingerprint")?,
    }))
}

fn planned_migration(value: &Value) -> Result<Value, LifecycleError> {
    let mut migration = value.as_object().cloned().ok_or_else(|| {
        LifecycleError::Invalid("source Activation migration report is invalid".to_owned())
    })?;
    migration.insert("status".to_owned(), Value::String("planned".to_owned()));
    migration.remove("fingerprint");
    let fingerprint = canonical_sha256(&Value::Object(migration.clone()))?;
    migration.insert("fingerprint".to_owned(), Value::String(fingerprint));
    Ok(Value::Object(migration))
}

fn require_install_lock_anchor(
    install_plan: &Value,
    package_lock: &Value,
    expected_status: &str,
    label: &str,
) -> Result<(), LifecycleError> {
    if install_plan.get("status").and_then(Value::as_str) != Some(expected_status)
        || install_plan.get("package_lock_hash") != package_lock.get("fingerprint")
        || package_lock
            .get("install_plan_identity_hash")
            .and_then(Value::as_str)
            != Some(install_plan_identity_hash(install_plan)?.as_str())
    {
        return invalid(format!(
            "upgrade {label} Install Plan and package Lockfile are not anchored"
        ));
    }
    Ok(())
}

fn selection(install_plan: &Value) -> Result<Value, LifecycleError> {
    Ok(json!({
        "platforms": sorted_strings(install_plan, "selected_platforms")?,
        "runtime_configs": sorted_strings(install_plan, "selected_runtime_configs")?,
    }))
}

fn selection_contains(selection: &Value, field: &str, expected: &str) -> bool {
    selection
        .get(field)
        .and_then(Value::as_array)
        .is_some_and(|values| values.iter().any(|value| value.as_str() == Some(expected)))
}

fn sorted_strings(value: &Value, field: &str) -> Result<Vec<String>, LifecycleError> {
    let values = value
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

fn normalized_unique(values: &[String], label: &str) -> Result<Vec<String>, LifecycleError> {
    if values.iter().any(String::is_empty)
        || values.iter().collect::<BTreeSet<_>>().len() != values.len()
    {
        return invalid(format!("{label} must be non-empty and unique"));
    }
    let mut values = values.to_vec();
    values.sort();
    Ok(values)
}

fn require_sorted_unique_paths(paths: &[String]) -> Result<(), LifecycleError> {
    if paths.windows(2).any(|pair| pair[0] >= pair[1]) {
        return invalid("upgrade external lifecycle paths must be sorted and unique");
    }
    Ok(())
}

fn required_string<'a>(value: &'a Value, field: &str) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("upgrade artifact {field} is invalid")))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::inspect_upgrade_planning_snapshot;
    use crate::{
        LifecycleLock, compile_source_install_bundle, install_source_bundle,
        resolve_source_install_selection, snapshot_source_packages,
    };
    use std::path::PathBuf;

    #[test]
    fn planning_snapshot_holds_the_target_lifecycle_lock_until_compilation_finishes() {
        let workspace = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("workspace root")
            .to_path_buf();
        let selection = resolve_source_install_selection(
            workspace.join("platforms"),
            &["desktop".to_owned()],
            &[],
            &[],
            false,
        )
        .expect("desktop selection");
        let packages = snapshot_source_packages(&selection).expect("source package snapshot");
        let bundle =
            compile_source_install_bundle(&selection, &packages, workspace.join("schemas"), None)
                .expect("source install bundle");
        let temporary = tempfile::tempdir().expect("temporary root");
        let target = temporary.path().join("target");
        install_source_bundle(&bundle, &packages, &target).expect("install fixture");

        let snapshot = inspect_upgrade_planning_snapshot(
            &target,
            bundle.plan(),
            bundle.package_lock(),
            "upgrade",
            &[],
            &[],
            None,
        )
        .expect("planning snapshot");
        assert!(
            LifecycleLock::acquire_existing(&target).is_err(),
            "snapshot must retain the transaction lock"
        );
        drop(snapshot);
        let _released =
            LifecycleLock::acquire_existing(&target).expect("lock released after snapshot");
    }
}
