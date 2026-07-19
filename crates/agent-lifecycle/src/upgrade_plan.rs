//! Deterministic native Upgrade Plan v1 compiler.

use super::upgrade_scope::UpgradePlanningSnapshot;
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256};
use agent_engine::{
    EngineError, diff_package_locks, install_plan_identity_hash, validate_install_plan,
    validate_package_lock, validate_upgrade_conformance_evidence, validate_upgrade_plan,
};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

const LOCK_IDENTITY_OMISSIONS: [&str; 3] = ["fingerprint", "install_plan_identity_hash", "lineage"];
const MAX_UPGRADE_MIGRATIONS: usize = 128;

/// Frozen inputs required to compile an approval-bound Upgrade Plan.
///
/// This boundary is intentionally filesystem-neutral. Callers must first
/// freeze and validate the current installation, candidate bundle, rollback
/// point, Conformance evidence, and any external lifecycle scope.
struct UpgradePlanRequest<'a> {
    pub action: &'a str,
    pub target_root: &'a str,
    pub current_install_plan: &'a Value,
    pub current_package_lock: &'a Value,
    pub candidate_install_plan: &'a Value,
    pub candidate_package_lock: &'a Value,
    pub conformance_evidence: &'a Value,
    pub scope_receipt: &'a Value,
    pub trusted_activation_owned: bool,
    pub trusted_handler_sha256: &'a str,
    pub removed_platforms: &'a [String],
    pub removed_runtime_configs: &'a [String],
}

/// Compile one byte-stable Upgrade Plan v1 from frozen lifecycle artifacts.
///
/// # Errors
/// Returns a fail-closed error when an input contract is malformed, identities
/// are not mutually anchored, removal semantics are impure, evidence is stale,
/// or the resulting approval contract is invalid.
#[allow(clippy::too_many_lines)]
fn compile_upgrade_plan_from_request(
    request: &UpgradePlanRequest<'_>,
) -> Result<Value, EngineError> {
    if !matches!(request.action, "upgrade" | "partial-uninstall") {
        return invalid("upgrade action is invalid");
    }
    if request.target_root.is_empty() {
        return invalid("upgrade target root is invalid");
    }
    validate_install_plan(request.current_install_plan)?;
    validate_package_lock(request.current_package_lock)?;
    validate_install_plan(request.candidate_install_plan)?;
    validate_package_lock(request.candidate_package_lock)?;
    validate_upgrade_conformance_evidence(request.conformance_evidence)?;

    validate_install_lock_anchor(
        request.current_install_plan,
        request.current_package_lock,
        Some("installed"),
        "current",
    )?;
    validate_install_lock_anchor(
        request.candidate_install_plan,
        request.candidate_package_lock,
        Some("planned"),
        "candidate",
    )?;

    let current_selection = selection_from_install_plan(request.current_install_plan)?;
    let candidate_selection = selection_from_install_plan(request.candidate_install_plan)?;
    validate_lock_selection(request.current_package_lock, &current_selection, "current")?;
    validate_lock_selection(
        request.candidate_package_lock,
        &candidate_selection,
        "candidate",
    )?;

    let candidate_lock_hash = required_string(
        object(request.candidate_package_lock, "candidate package Lockfile")?,
        "fingerprint",
    )?;
    let schema_inventory_hash = required_string(
        object(
            request
                .candidate_package_lock
                .get("schema_inventory")
                .ok_or_else(|| {
                    EngineError::Invalid(
                        "candidate package Lockfile schema inventory is missing".to_owned(),
                    )
                })?,
            "candidate package Lockfile schema inventory",
        )?,
        "content_sha256",
    )?;
    if request
        .conformance_evidence
        .get("candidate_package_lock_hash")
        .and_then(Value::as_str)
        != Some(candidate_lock_hash)
        || request
            .conformance_evidence
            .get("schema_inventory_hash")
            .and_then(Value::as_str)
            != Some(schema_inventory_hash)
    {
        return invalid("upgrade Conformance evidence is stale for the candidate Lockfile");
    }

    let current_lock_hash = required_string(
        object(request.current_package_lock, "current package Lockfile")?,
        "fingerprint",
    )?;
    let scope = validate_scope_receipt(request)?;
    validate_rollback_point(scope.rollback_point)?;
    validate_rollback_anchor(
        scope.rollback_point,
        request.current_install_plan,
        current_lock_hash,
        scope.external_state_sha256,
    )?;

    let removed_platforms = normalized_unique(
        request.removed_platforms,
        "upgrade removed platform request must be unique",
    )?;
    let removed_runtime_configs = normalized_unique(
        request.removed_runtime_configs,
        "upgrade removed runtime config request must be unique",
    )?;
    if request.action == "upgrade"
        && (!removed_platforms.is_empty() || !removed_runtime_configs.is_empty())
    {
        return invalid("upgrade action must not contain a removal request");
    }
    if request.action == "partial-uninstall" {
        validate_partial_uninstall_purity(
            request.current_package_lock,
            request.candidate_package_lock,
        )?;
    }

    let current_semantic_identity = semantic_lock_identity(request.current_package_lock)?;
    let candidate_semantic_identity = semantic_lock_identity(request.candidate_package_lock)?;
    validate_candidate_lineage(
        request.candidate_package_lock,
        current_lock_hash,
        current_semantic_identity != candidate_semantic_identity,
    )?;
    let semantic_change =
        current_semantic_identity != candidate_semantic_identity || !scope.migrations.is_empty();
    let mut changes =
        diff_package_locks(request.current_package_lock, request.candidate_package_lock)?;
    let change_status = changes.get("status").and_then(Value::as_str);
    if (!scope.migrations.is_empty() && change_status == Some("unchanged"))
        || (!semantic_change && change_status != Some("unchanged"))
    {
        let changes = changes.as_object_mut().ok_or_else(|| {
            EngineError::Invalid("package Lockfile diff must be an object".to_owned())
        })?;
        changes.insert(
            "status".to_owned(),
            Value::String(
                if semantic_change {
                    "changed"
                } else {
                    "unchanged"
                }
                .to_owned(),
            ),
        );
        changes.remove("fingerprint");
        let fingerprint = canonical_sha256(&Value::Object(changes.clone()))?;
        changes.insert("fingerprint".to_owned(), Value::String(fingerprint));
    }

    let current_install_fingerprint = required_string(
        object(request.current_install_plan, "current Install Plan")?,
        "fingerprint",
    )?;
    let candidate_install_fingerprint = required_string(
        object(request.candidate_install_plan, "candidate Install Plan")?,
        "fingerprint",
    )?;
    let rollback = object(scope.rollback_point, "rollback point")?;
    let mut plan = json!({
        "action": request.action,
        "approvals_required": permission_approvals(
            request.current_package_lock,
            request.candidate_package_lock,
        )?,
        "candidate": {
            "install_plan_fingerprint": candidate_install_fingerprint,
            "package_lock_hash": candidate_lock_hash,
        },
        "compatibility": {
            "agent_skills_lock": "identity",
            "install_plan_lock": "identity",
            "mode": "identity-only",
        },
        "changes": changes,
        "conformance_attestation_key": request
            .conformance_evidence
            .get("attestation_key")
            .cloned()
            .ok_or_else(|| EngineError::Invalid(
                "upgrade Conformance attestation is missing".to_owned()
            ))?,
        "current": {
            "install_plan_fingerprint": current_install_fingerprint,
            "package_lock_hash": current_lock_hash,
        },
        "current_selection": current_selection,
        "external": {
            "handler": scope.handler,
            "handler_sha256": scope.handler_sha256,
            "path_count": scope.path_count,
            "paths_sha256": scope.paths_sha256,
        },
        "migrations": scope.migrations,
        "removed_platforms": removed_platforms,
        "removed_runtime_configs": removed_runtime_configs,
        "rollback": {
            "point_fingerprint": rollback.get("fingerprint").cloned().ok_or_else(
                || EngineError::Invalid("rollback point fingerprint is missing".to_owned())
            )?,
            "point_id": rollback.get("point_id").cloned().ok_or_else(
                || EngineError::Invalid("rollback point id is missing".to_owned())
            )?,
            "previous_lock_hash": current_lock_hash,
        },
        "schema_version": "1.0",
        "selection": candidate_selection,
        "status": if semantic_change { "planned" } else { "no-change" },
        "target_root": request.target_root,
        "upgrade_steps": upgrade_steps(
            request.current_package_lock,
            request.candidate_package_lock,
        )?,
    });
    let encoded = canonical_json(&plan)?;
    validate_upgrade_plan_size(encoded.len())?;
    let fingerprint = canonical_sha256(&plan)?;
    plan.as_object_mut()
        .ok_or_else(|| EngineError::Invalid("Upgrade Plan must be an object".to_owned()))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    validate_upgrade_plan_size(canonical_json(&plan)?.len())?;
    validate_upgrade_plan(&plan)?;
    Ok(plan)
}

/// Compile an approval-bound Upgrade Plan while the opaque lifecycle snapshot
/// keeps its target transaction lock held.
///
/// # Errors
/// Returns a fail-closed error when the snapshot lock changed, any candidate
/// or evidence contract is malformed, the trusted receipt no longer binds the
/// supplied request, or the resulting Upgrade Plan is invalid.
pub fn compile_upgrade_plan(
    snapshot: &UpgradePlanningSnapshot,
    action: &str,
    candidate_install_plan: &Value,
    candidate_package_lock: &Value,
    conformance_evidence: &Value,
    removed_platforms: &[String],
    removed_runtime_configs: &[String],
) -> Result<Value, crate::LifecycleError> {
    snapshot.validate_lock()?;
    let plan = compile_upgrade_plan_from_request(&UpgradePlanRequest {
        action,
        target_root: &snapshot.target_root,
        current_install_plan: &snapshot.current_install_plan,
        current_package_lock: &snapshot.current_package_lock,
        candidate_install_plan,
        candidate_package_lock,
        conformance_evidence,
        scope_receipt: &snapshot.receipt,
        trusted_activation_owned: snapshot.activation_owned,
        trusted_handler_sha256: &snapshot.handler_sha256,
        removed_platforms,
        removed_runtime_configs,
    })?;
    snapshot.validate_lock()?;
    Ok(plan)
}

struct ValidatedScope<'a> {
    external_state_sha256: &'a str,
    handler: &'a str,
    handler_sha256: &'a str,
    migrations: &'a [Value],
    path_count: u64,
    paths_sha256: &'a str,
    rollback_point: &'a Value,
}

#[allow(clippy::too_many_lines)]
fn validate_scope_receipt<'a>(
    request: &UpgradePlanRequest<'a>,
) -> Result<ValidatedScope<'a>, EngineError> {
    let receipt = exact_object(
        request.scope_receipt,
        &[
            "action",
            "activation_owned",
            "candidate",
            "current",
            "external",
            "fingerprint",
            "manager",
            "migrations",
            "removed_platforms",
            "removed_runtime_configs",
            "rollback_point",
            "schema_version",
            "target_root",
        ],
        "upgrade scope receipt fields are invalid",
    )?;
    if receipt.get("schema_version").and_then(Value::as_str) != Some("1.0")
        || receipt.get("manager").and_then(Value::as_str) != Some("agent-development-skills")
        || receipt.get("action").and_then(Value::as_str) != Some(request.action)
        || receipt.get("target_root").and_then(Value::as_str) != Some(request.target_root)
    {
        return invalid("upgrade scope receipt identity differs from the request");
    }
    verify_fingerprint(receipt, "upgrade scope receipt fingerprint mismatch")?;

    validate_receipt_artifact_identity(
        required(receipt, "current")?,
        request.current_install_plan,
        request.current_package_lock,
        "current",
    )?;
    validate_receipt_artifact_identity(
        required(receipt, "candidate")?,
        request.candidate_install_plan,
        request.candidate_package_lock,
        "candidate",
    )?;

    let receipt_removed_platforms = sorted_strings(
        required(receipt, "removed_platforms")?,
        "receipt removed platforms",
    )?;
    let receipt_removed_runtime_configs = sorted_strings(
        required(receipt, "removed_runtime_configs")?,
        "receipt removed runtime configs",
    )?;
    if receipt_removed_platforms
        != normalized_unique(
            request.removed_platforms,
            "upgrade removed platform request must be unique",
        )?
        || receipt_removed_runtime_configs
            != normalized_unique(
                request.removed_runtime_configs,
                "upgrade removed runtime config request must be unique",
            )?
    {
        return invalid("upgrade scope receipt removal request differs from the request");
    }

    let activation_owned = receipt
        .get("activation_owned")
        .and_then(Value::as_bool)
        .ok_or_else(|| {
            EngineError::Invalid("receipt activation ownership is invalid".to_owned())
        })?;
    if activation_owned != request.trusted_activation_owned {
        return invalid("upgrade scope receipt Activation ownership is not lifecycle-issued");
    }
    let current_selection = selection_from_install_plan(request.current_install_plan)?;
    let candidate_selection = selection_from_install_plan(request.candidate_install_plan)?;
    if activation_owned
        && !(selection_contains(&current_selection, "platforms", "apple")
            && selection_contains(&current_selection, "runtime_configs", "codex"))
    {
        return invalid("receipt Activation ownership differs from the current selection");
    }

    let external = exact_object(
        required(receipt, "external")?,
        &[
            "handler",
            "handler_sha256",
            "path_count",
            "paths",
            "paths_sha256",
            "state_sha256",
        ],
        "upgrade scope receipt external fields are invalid",
    )?;
    let handler = required_string(external, "handler")?;
    let handler_sha256 = required_string(external, "handler_sha256")?;
    let paths = sorted_strings(required(external, "paths")?, "receipt external paths")?;
    let path_count = external
        .get("path_count")
        .and_then(Value::as_u64)
        .ok_or_else(|| EngineError::Invalid("receipt external path count is invalid".to_owned()))?;
    let paths_sha256 = required_string(external, "paths_sha256")?;
    let external_state_sha256 = required_string(external, "state_sha256")?;
    if !is_sha256(handler_sha256)
        || !is_sha256(paths_sha256)
        || !is_sha256(external_state_sha256)
        || path_count != paths.len() as u64
        || paths_sha256 != canonical_sha256(&json!(paths))?
        || (handler == "none") != paths.is_empty()
    {
        return invalid("upgrade scope receipt external binding is invalid");
    }
    if handler_sha256 != request.trusted_handler_sha256 {
        return invalid("upgrade scope receipt handler hash is not lifecycle-issued");
    }
    if handler == "none" {
        if handler_sha256 != canonical_sha256(&Value::String("none".to_owned()))? {
            return invalid("upgrade scope receipt no-handler hash is invalid");
        }
    } else if !matches!(
        handler,
        "core.source-activation.apple-codex-v1"
            | "core.source-deactivation.apple-codex-v1"
            | "core.source-preserve.apple-codex-v1"
    ) {
        return invalid("upgrade scope receipt handler is not a trusted Core handler");
    }

    let expected_handler = if request.action == "partial-uninstall" {
        if activation_owned
            && receipt_removed_platforms
                .iter()
                .any(|value| value == "apple")
        {
            if receipt_removed_runtime_configs != ["codex"] {
                return invalid(
                    "partial uninstall may remove only activation-owned codex with Apple",
                );
            }
            "core.source-deactivation.apple-codex-v1"
        } else if activation_owned {
            if !receipt_removed_runtime_configs.is_empty() {
                return invalid("partial uninstall runtime removal violates Activation ownership");
            }
            "core.source-preserve.apple-codex-v1"
        } else {
            if !receipt_removed_runtime_configs.is_empty() {
                return invalid("partial uninstall cannot remove unowned runtime configuration");
            }
            "none"
        }
    } else if activation_owned {
        if selection_contains(&candidate_selection, "platforms", "apple")
            && selection_contains(&candidate_selection, "runtime_configs", "codex")
        {
            "core.source-activation.apple-codex-v1"
        } else {
            "core.source-deactivation.apple-codex-v1"
        }
    } else {
        "none"
    };
    if handler != expected_handler {
        return invalid("upgrade scope receipt handler differs from Activation ownership policy");
    }

    let migrations = receipt
        .get("migrations")
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid("receipt migrations are invalid".to_owned()))?;
    if migrations.len() > MAX_UPGRADE_MIGRATIONS {
        return invalid(format!(
            "upgrade migration count exceeds {MAX_UPGRADE_MIGRATIONS}"
        ));
    }
    let mut migration_bytes = 0_usize;
    for migration in migrations {
        migration_bytes = migration_bytes
            .checked_add(canonical_json(migration)?.len())
            .ok_or_else(|| EngineError::Invalid("upgrade migration size overflow".to_owned()))?;
        if migration_bytes > MAX_CONTRACT_JSON_BYTES {
            return invalid(format!(
                "upgrade migration inputs have more than {MAX_CONTRACT_JSON_BYTES} bytes"
            ));
        }
    }
    if !migrations.is_empty() && handler != "core.source-activation.apple-codex-v1" {
        return invalid("upgrade migrations require the trusted source Activation handler");
    }

    let rollback_point = required(receipt, "rollback_point")?;
    if rollback_point
        .get("external_state_sha256")
        .and_then(Value::as_str)
        != Some(external_state_sha256)
    {
        return invalid("upgrade scope receipt rollback external state differs");
    }
    Ok(ValidatedScope {
        external_state_sha256,
        handler,
        handler_sha256,
        migrations,
        path_count,
        paths_sha256,
        rollback_point,
    })
}

fn validate_receipt_artifact_identity(
    receipt_identity: &Value,
    install_plan: &Value,
    package_lock: &Value,
    label: &str,
) -> Result<(), EngineError> {
    let identity = exact_object(
        receipt_identity,
        &[
            "install_plan_fingerprint",
            "install_plan_identity_hash",
            "package_lock_hash",
        ],
        "upgrade scope receipt artifact identity is invalid",
    )?;
    if identity.get("install_plan_fingerprint") != install_plan.get("fingerprint")
        || identity.get("package_lock_hash") != package_lock.get("fingerprint")
        || identity
            .get("install_plan_identity_hash")
            .and_then(Value::as_str)
            != Some(install_plan_identity_hash(install_plan)?.as_str())
    {
        return invalid(format!(
            "upgrade scope receipt {label} artifact identity differs"
        ));
    }
    Ok(())
}

fn selection_contains(selection: &Value, field: &str, expected: &str) -> bool {
    selection
        .get(field)
        .and_then(Value::as_array)
        .is_some_and(|values| values.iter().any(|value| value.as_str() == Some(expected)))
}

fn validate_upgrade_plan_size(length: usize) -> Result<(), EngineError> {
    if length > MAX_CONTRACT_JSON_BYTES {
        return invalid(format!(
            "Upgrade Plan exceeds maximum of {MAX_CONTRACT_JSON_BYTES} bytes"
        ));
    }
    Ok(())
}

fn validate_install_lock_anchor(
    install_plan: &Value,
    package_lock: &Value,
    expected_status: Option<&str>,
    label: &str,
) -> Result<(), EngineError> {
    let plan = object(install_plan, &format!("{label} Install Plan"))?;
    let lock = object(package_lock, &format!("{label} package Lockfile"))?;
    if expected_status
        .is_some_and(|status| plan.get("status").and_then(Value::as_str) != Some(status))
        || plan.get("package_lock_hash") != lock.get("fingerprint")
        || lock
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

fn selection_from_install_plan(plan: &Value) -> Result<Value, EngineError> {
    let plan = object(plan, "Install Plan")?;
    let platforms = sorted_strings(required(plan, "selected_platforms")?, "selected platforms")?;
    let disciplines = sorted_strings(
        required(plan, "selected_disciplines")?,
        "selected disciplines",
    )?;
    let runtime_configs = sorted_strings(
        required(plan, "selected_runtime_configs")?,
        "selected runtime configs",
    )?;
    Ok(json!({
        "core_only": platforms.is_empty() && disciplines.is_empty() && runtime_configs.is_empty(),
        "disciplines": disciplines,
        "platforms": platforms,
        "runtime_configs": runtime_configs,
    }))
}

fn validate_lock_selection(
    package_lock: &Value,
    expected: &Value,
    label: &str,
) -> Result<(), EngineError> {
    let mut expected = object(expected, "selection")?.clone();
    expected.remove("core_only");
    if package_lock.get("selection") != Some(&Value::Object(expected)) {
        return invalid(format!(
            "upgrade {label} Install Plan selection differs from package Lockfile"
        ));
    }
    Ok(())
}

pub(super) fn semantic_lock_identity(value: &Value) -> Result<String, EngineError> {
    let mut identity = object(value, "package Lockfile")?.clone();
    for field in LOCK_IDENTITY_OMISSIONS {
        identity.remove(field);
    }
    Ok(canonical_sha256(&Value::Object(identity))?)
}

fn validate_partial_uninstall_purity(before: &Value, after: &Value) -> Result<(), EngineError> {
    let before = object(before, "current package Lockfile")?;
    let after = object(after, "candidate package Lockfile")?;
    let before_packages = indexed_packages(required(before, "packages")?)?;
    let after_packages = indexed_packages(required(after, "packages")?)?;
    let before_ids = before_packages.keys().collect::<BTreeSet<_>>();
    let after_ids = after_packages.keys().collect::<BTreeSet<_>>();
    if !after_ids.is_subset(&before_ids) || after_ids.len() >= before_ids.len() {
        return invalid(
            "partial uninstall must remove at least one package without adding packages",
        );
    }
    let changed = after_packages
        .iter()
        .filter_map(|(id, record)| {
            (before_packages.get(id).copied() != Some(*record)).then_some(id.as_str())
        })
        .collect::<Vec<_>>();
    if !changed.is_empty() {
        return invalid(format!(
            "partial uninstall would upgrade or modify remaining packages: {}",
            changed.join(", ")
        ));
    }
    if after.get("core") != before.get("core")
        || after.get("schema_inventory") != before.get("schema_inventory")
    {
        return invalid("partial uninstall would change Core or Schema identity");
    }
    Ok(())
}

fn permission_approvals(before: &Value, after: &Value) -> Result<Vec<String>, EngineError> {
    let old = object(
        before
            .get("capability_providers")
            .ok_or_else(|| EngineError::Invalid("current providers are missing".to_owned()))?,
        "current providers",
    )?;
    let new = object(
        after
            .get("capability_providers")
            .ok_or_else(|| EngineError::Invalid("candidate providers are missing".to_owned()))?,
        "candidate providers",
    )?;
    let mut approvals = Vec::new();
    for (capability, provider) in new {
        let current = required_string(
            object(provider, "candidate provider")?,
            "permission_profile",
        )?;
        let previous = old
            .get(capability)
            .and_then(Value::as_object)
            .and_then(|provider| provider.get("permission_profile"))
            .and_then(Value::as_str)
            .unwrap_or("none");
        if previous != current {
            approvals.push(format!("permission:{capability}:{previous}->{current}"));
        }
    }
    approvals.sort();
    Ok(approvals)
}

fn upgrade_steps(before: &Value, after: &Value) -> Result<Vec<Value>, EngineError> {
    let before = object(before, "current package Lockfile")?;
    let after = object(after, "candidate package Lockfile")?;
    let mut steps = Vec::<(u8, String, String, &'static str)>::new();
    let before_runtime = pointer_string(before, "/core/runtime_version")?;
    let after_runtime = pointer_string(after, "/core/runtime_version")?;
    if before_runtime != after_runtime {
        steps.push((0, before_runtime, after_runtime, "core"));
    }
    let before_schema = pointer_string(before, "/schema_inventory/content_sha256")?;
    let after_schema = pointer_string(after, "/schema_inventory/content_sha256")?;
    if before_schema != after_schema {
        steps.push((1, before_schema, after_schema, "schema"));
    }
    let before_packages = indexed_packages(required(before, "packages")?)?;
    let after_packages = indexed_packages(required(after, "packages")?)?;
    for package_id in before_packages
        .keys()
        .chain(after_packages.keys())
        .collect::<BTreeSet<_>>()
    {
        let old_identity = package_identity(before_packages.get(package_id).copied())?;
        let new_identity = package_identity(after_packages.get(package_id).copied())?;
        if old_identity != new_identity {
            steps.push((
                2,
                format!("{package_id}:{old_identity}"),
                format!("{package_id}:{new_identity}"),
                "package",
            ));
        }
    }
    if semantic_lock_identity(&Value::Object(before.clone()))?
        != semantic_lock_identity(&Value::Object(after.clone()))?
    {
        steps.push((
            3,
            required_string(before, "fingerprint")?.to_owned(),
            required_string(after, "fingerprint")?.to_owned(),
            "lock",
        ));
    }
    steps.sort_by(|left, right| (&left.0, &left.1, &left.2).cmp(&(&right.0, &right.1, &right.2)));
    Ok(steps
        .into_iter()
        .map(|(_, from, to, kind)| json!({"kind": kind, "from": from, "to": to}))
        .collect())
}

fn validate_rollback_point(value: &Value) -> Result<(), EngineError> {
    let point = exact_object(
        value,
        &[
            "external_state_sha256",
            "fingerprint",
            "install_plan_fingerprint",
            "manager",
            "package_lock_hash",
            "point_id",
            "schema_version",
            "snapshot_sha256",
        ],
        "rollback-point fields are invalid",
    )?;
    if point.get("schema_version").and_then(Value::as_str) != Some("1.0")
        || point.get("manager").and_then(Value::as_str) != Some("agent-development-skills")
    {
        return invalid("rollback-point identity is invalid");
    }
    for field in [
        "external_state_sha256",
        "fingerprint",
        "install_plan_fingerprint",
        "package_lock_hash",
        "snapshot_sha256",
    ] {
        if point
            .get(field)
            .is_none_or(|value| value.as_str().is_none_or(|value| !is_sha256(value)))
        {
            return invalid(format!("rollback-point {field} is invalid"));
        }
    }
    let lock_hash = required_string(point, "package_lock_hash")?;
    if point.get("point_id").and_then(Value::as_str)
        != Some(format!("rollback-{}", &lock_hash[..12]).as_str())
    {
        return invalid("rollback-point id is invalid");
    }
    verify_fingerprint(point, "rollback-point fingerprint mismatch")
}

fn validate_rollback_anchor(
    rollback: &Value,
    install_plan: &Value,
    lock_hash: &str,
    expected_external_state_hash: &str,
) -> Result<(), EngineError> {
    if rollback.get("install_plan_fingerprint") != install_plan.get("fingerprint")
        || rollback.get("package_lock_hash").and_then(Value::as_str) != Some(lock_hash)
        || rollback
            .get("external_state_sha256")
            .and_then(Value::as_str)
            != Some(expected_external_state_hash)
    {
        return invalid("rollback point differs from the current installed state");
    }
    Ok(())
}

fn validate_candidate_lineage(
    candidate: &Value,
    current_lock_hash: &str,
    lock_semantic_change: bool,
) -> Result<(), EngineError> {
    let previous = candidate.pointer("/lineage/previous_lock_hash");
    let expected = if lock_semantic_change {
        Some(&Value::String(current_lock_hash.to_owned()))
    } else {
        Some(&Value::Null)
    };
    if previous != expected {
        return invalid("upgrade candidate lineage differs from the current Lockfile");
    }
    Ok(())
}

fn package_identity(package: Option<&Value>) -> Result<String, EngineError> {
    let Some(package) = package else {
        return Ok("absent".to_owned());
    };
    let package = object(package, "package Lockfile package")?;
    let version = required_string(package, "version")?;
    let source = object(required(package, "source")?, "package source")?;
    Ok(format!("{version}@{}", required_string(source, "sha256")?))
}

fn indexed_packages(value: &Value) -> Result<BTreeMap<String, &Value>, EngineError> {
    let packages = value.as_array().ok_or_else(|| {
        EngineError::Invalid("package Lockfile packages must be an array".to_owned())
    })?;
    packages
        .iter()
        .map(|package| {
            let id = required_string(object(package, "package Lockfile package")?, "id")?;
            Ok((id.to_owned(), package))
        })
        .collect()
}

fn pointer_string(object: &Map<String, Value>, pointer: &str) -> Result<String, EngineError> {
    Value::Object(object.clone())
        .pointer(pointer)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| EngineError::Invalid(format!("{pointer} must be a string")))
}

fn normalized_unique(values: &[String], message: &str) -> Result<Vec<String>, EngineError> {
    if values.iter().any(String::is_empty)
        || values.iter().collect::<BTreeSet<_>>().len() != values.len()
    {
        return invalid(message);
    }
    let mut values = values.to_vec();
    values.sort();
    Ok(values)
}

fn sorted_strings(value: &Value, label: &str) -> Result<Vec<String>, EngineError> {
    let values = value
        .as_array()
        .ok_or_else(|| EngineError::Invalid(format!("{label} must be an array")))?
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| EngineError::Invalid(format!("{label} must contain strings")))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if values.windows(2).any(|pair| pair[0] >= pair[1]) {
        return invalid(format!("{label} must be sorted and unique"));
    }
    Ok(values)
}

fn exact_object<'a>(
    value: &'a Value,
    fields: &[&str],
    message: &str,
) -> Result<&'a Map<String, Value>, EngineError> {
    let object = object(value, message)?;
    let expected = fields.iter().copied().collect::<BTreeSet<_>>();
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    if actual != expected {
        return invalid(message);
    }
    Ok(object)
}

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, EngineError> {
    value
        .as_object()
        .ok_or_else(|| EngineError::Invalid(format!("{label} must be an object")))
}

fn required<'a>(object: &'a Map<String, Value>, field: &str) -> Result<&'a Value, EngineError> {
    object
        .get(field)
        .ok_or_else(|| EngineError::Invalid(format!("{field} is required")))
}

fn required_string<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<&'a str, EngineError> {
    required(object, field)?
        .as_str()
        .ok_or_else(|| EngineError::Invalid(format!("{field} must be a string")))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn verify_fingerprint(object: &Map<String, Value>, message: &str) -> Result<(), EngineError> {
    let mut identity = object.clone();
    let expected = identity
        .remove("fingerprint")
        .and_then(|value| value.as_str().map(str::to_owned));
    if expected.as_deref() != Some(canonical_sha256(&Value::Object(identity))?.as_str()) {
        return invalid(message);
    }
    Ok(())
}

fn invalid<T>(message: impl Into<String>) -> Result<T, EngineError> {
    Err(EngineError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::{
        UpgradePlanRequest, validate_candidate_lineage, validate_scope_receipt,
        validate_upgrade_plan_size,
    };
    use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_sha256};
    use serde_json::{Value, json};

    #[test]
    fn candidate_lineage_is_bound_to_semantic_change() {
        let current = "a".repeat(64);
        let unchanged = json!({"lineage": {"previous_lock_hash": null}});
        validate_candidate_lineage(&unchanged, &current, false).expect("unchanged lineage");
        assert!(validate_candidate_lineage(&unchanged, &current, true).is_err());

        let changed = json!({"lineage": {"previous_lock_hash": current}});
        validate_candidate_lineage(&changed, &current, true).expect("changed lineage");
        assert!(validate_candidate_lineage(&changed, "b".repeat(64).as_str(), true).is_err());
        assert!(validate_candidate_lineage(&changed, &current, false).is_err());
    }

    #[test]
    fn final_upgrade_plan_size_boundary_is_inclusive() {
        validate_upgrade_plan_size(MAX_CONTRACT_JSON_BYTES).expect("exact size boundary");
        assert!(validate_upgrade_plan_size(MAX_CONTRACT_JSON_BYTES + 1).is_err());
    }

    #[test]
    fn scope_receipt_is_identity_bound_and_rejects_self_consistent_handler_tampering() {
        let current_plan = json!({
            "fingerprint": "a".repeat(64),
            "selected_disciplines": [],
            "selected_platforms": ["apple"],
            "selected_runtime_configs": ["codex"],
        });
        let candidate_plan = json!({
            "fingerprint": "b".repeat(64),
            "selected_disciplines": [],
            "selected_platforms": ["apple"],
            "selected_runtime_configs": ["codex"],
        });
        let current_lock = json!({"fingerprint": "c".repeat(64)});
        let candidate_lock = json!({"fingerprint": "d".repeat(64)});
        let external_state = canonical_sha256(&json!({
            "directories": [],
            "entries": [],
            "schema_version": "1.0",
        }))
        .expect("external state hash");
        let no_handler_hash =
            canonical_sha256(&Value::String("none".to_owned())).expect("none hash");
        let trusted_handler_hash = "1".repeat(64);
        let mut receipt = json!({
            "action": "upgrade",
            "activation_owned": true,
            "candidate": {
                "install_plan_fingerprint": candidate_plan["fingerprint"],
                "install_plan_identity_hash":
                    super::install_plan_identity_hash(&candidate_plan).expect("candidate identity"),
                "package_lock_hash": candidate_lock["fingerprint"],
            },
            "current": {
                "install_plan_fingerprint": current_plan["fingerprint"],
                "install_plan_identity_hash":
                    super::install_plan_identity_hash(&current_plan).expect("current identity"),
                "package_lock_hash": current_lock["fingerprint"],
            },
            "external": {
                "handler": "core.source-activation.apple-codex-v1",
                "handler_sha256": trusted_handler_hash,
                "path_count": 1,
                "paths": ["config.toml"],
                "paths_sha256":
                    canonical_sha256(&json!(["config.toml"])).expect("path hash"),
                "state_sha256": external_state,
            },
            "manager": "agent-development-skills",
            "migrations": [],
            "removed_platforms": [],
            "removed_runtime_configs": [],
            "rollback_point": {
                "external_state_sha256": external_state,
            },
            "schema_version": "1.0",
            "target_root": "/safe/target",
        });
        set_receipt_fingerprint(&mut receipt);
        let evidence = Value::Null;
        let empty = Vec::new();
        let request_for = |scope_receipt| UpgradePlanRequest {
            action: "upgrade",
            target_root: "/safe/target",
            current_install_plan: &current_plan,
            current_package_lock: &current_lock,
            candidate_install_plan: &candidate_plan,
            candidate_package_lock: &candidate_lock,
            conformance_evidence: &evidence,
            scope_receipt,
            trusted_activation_owned: true,
            trusted_handler_sha256: &trusted_handler_hash,
            removed_platforms: &empty,
            removed_runtime_configs: &empty,
        };
        validate_scope_receipt(&request_for(&receipt)).expect("valid receipt");

        let mut fingerprint_tamper = receipt.clone();
        fingerprint_tamper["external"]["state_sha256"] = Value::String("e".repeat(64));
        assert!(validate_scope_receipt(&request_for(&fingerprint_tamper)).is_err());

        let mut handler_tamper = receipt.clone();
        handler_tamper["external"]["handler_sha256"] = Value::String("f".repeat(64));
        set_receipt_fingerprint(&mut handler_tamper);
        assert!(validate_scope_receipt(&request_for(&handler_tamper)).is_err());

        let mut ownership_tamper = receipt.clone();
        ownership_tamper["activation_owned"] = Value::Bool(false);
        ownership_tamper["external"]["handler"] = Value::String("none".to_owned());
        ownership_tamper["external"]["handler_sha256"] = Value::String(no_handler_hash);
        ownership_tamper["external"]["path_count"] = json!(0);
        ownership_tamper["external"]["paths"] = json!([]);
        ownership_tamper["external"]["paths_sha256"] =
            Value::String(canonical_sha256(&json!([])).expect("empty path hash"));
        set_receipt_fingerprint(&mut ownership_tamper);
        assert!(validate_scope_receipt(&request_for(&ownership_tamper)).is_err());

        let mut identity_tamper = receipt.clone();
        identity_tamper["current"]["package_lock_hash"] = Value::String("e".repeat(64));
        set_receipt_fingerprint(&mut identity_tamper);
        assert!(validate_scope_receipt(&request_for(&identity_tamper)).is_err());
    }

    fn set_receipt_fingerprint(receipt: &mut Value) {
        let object = receipt.as_object_mut().expect("receipt object");
        object.remove("fingerprint");
        let fingerprint =
            canonical_sha256(&Value::Object(object.clone())).expect("receipt fingerprint");
        object.insert("fingerprint".to_owned(), Value::String(fingerprint));
    }
}
