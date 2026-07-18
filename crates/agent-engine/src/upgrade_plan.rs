//! Deterministic native Upgrade Plan v1 compiler.

use super::{
    EngineError, diff_package_locks, install_plan_identity_hash, invalid, validate_install_plan,
    validate_package_lock, validate_upgrade_conformance_evidence, validate_upgrade_plan,
};
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json, canonical_sha256};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

const LOCK_IDENTITY_OMISSIONS: [&str; 3] = ["fingerprint", "install_plan_identity_hash", "lineage"];

/// Frozen inputs required to compile an approval-bound Upgrade Plan.
///
/// This boundary is intentionally filesystem-neutral. Callers must first
/// freeze and validate the current installation, candidate bundle, rollback
/// point, Conformance evidence, and any external lifecycle scope.
pub struct UpgradePlanRequest<'a> {
    pub action: &'a str,
    pub target_root: &'a str,
    pub current_install_plan: &'a Value,
    pub current_package_lock: &'a Value,
    pub candidate_install_plan: &'a Value,
    pub candidate_package_lock: &'a Value,
    pub conformance_evidence: &'a Value,
    pub rollback_point: &'a Value,
    pub migrations: &'a [Value],
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
pub fn compile_upgrade_plan(request: &UpgradePlanRequest<'_>) -> Result<Value, EngineError> {
    if !matches!(request.action, "upgrade" | "partial-uninstall") {
        return invalid("upgrade action is invalid");
    }
    if request.target_root.is_empty() {
        return invalid("upgrade target root is invalid");
    }
    if !request.migrations.is_empty() {
        return invalid("native Upgrade Plan compilation does not yet support schema migrations");
    }
    validate_install_plan(request.current_install_plan)?;
    validate_package_lock(request.current_package_lock)?;
    validate_install_plan(request.candidate_install_plan)?;
    validate_package_lock(request.candidate_package_lock)?;
    validate_upgrade_conformance_evidence(request.conformance_evidence)?;
    validate_rollback_point(request.rollback_point)?;

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
    let empty_external_state_hash = canonical_sha256(&json!({
        "directories": [],
        "entries": [],
        "schema_version": "1.0",
    }))?;
    validate_rollback_anchor(
        request.rollback_point,
        request.current_install_plan,
        current_lock_hash,
        &empty_external_state_hash,
    )?;
    let no_handler_hash = canonical_sha256(&Value::String("none".to_owned()))?;

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
        current_semantic_identity != candidate_semantic_identity || !request.migrations.is_empty();
    if semantic_change
        && current_selection
            .get("platforms")
            .and_then(Value::as_array)
            .is_some_and(|platforms| platforms.iter().any(|value| value == "apple"))
    {
        return invalid(
            "native Upgrade Plan compilation requires trusted external-scope evidence for Apple",
        );
    }
    let mut changes =
        diff_package_locks(request.current_package_lock, request.candidate_package_lock)?;
    let change_status = changes.get("status").and_then(Value::as_str);
    if (!request.migrations.is_empty() && change_status == Some("unchanged"))
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
    let rollback = object(request.rollback_point, "rollback point")?;
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
            "handler": "none",
            "handler_sha256": no_handler_hash,
            "path_count": 0,
            "paths_sha256": canonical_sha256(&json!([]))?,
        },
        "migrations": request.migrations,
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

fn semantic_lock_identity(value: &Value) -> Result<String, EngineError> {
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

#[cfg(test)]
mod tests {
    use super::{validate_candidate_lineage, validate_upgrade_plan_size};
    use agent_contracts::MAX_CONTRACT_JSON_BYTES;
    use serde_json::json;

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
}
