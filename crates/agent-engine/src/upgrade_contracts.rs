//! Strict native validators for the approval-bound upgrade control plane.

use super::{EngineError, invalid};
use agent_contracts::{canonical_sha256, json_integer, require_schema_version};
use serde_json::{Map, Value};
use std::collections::BTreeSet;

const EVIDENCE_FIELDS: &[&str] = &[
    "attestation_key",
    "candidate_package_lock_hash",
    "command_results",
    "environment",
    "fingerprint",
    "manifest_count",
    "negative_contract_count",
    "runner_sha256",
    "schema_inventory_hash",
    "schema_version",
    "status",
    "suite",
    "suite_definition_hash",
    "test_count",
];
const SOURCE_QUALIFICATION_FIELDS: &[&str] = &[
    "attestation_key",
    "command_results",
    "environment",
    "fingerprint",
    "manifest_count",
    "negative_contract_count",
    "runner_sha256",
    "schema_inventory_hash",
    "schema_version",
    "source",
    "source_materials_sha256",
    "status",
    "suite",
    "suite_definition_hash",
    "test_count",
];
const PLAN_FIELDS: &[&str] = &[
    "action",
    "approvals_required",
    "candidate",
    "changes",
    "compatibility",
    "conformance_attestation_key",
    "current",
    "current_selection",
    "external",
    "fingerprint",
    "migrations",
    "removed_platforms",
    "removed_runtime_configs",
    "rollback",
    "schema_version",
    "selection",
    "status",
    "target_root",
    "upgrade_steps",
];

/// Validate one Upgrade Conformance Evidence v1 artifact.
///
/// The stable attestation intentionally excludes output digests while the
/// complete fingerprint includes them, matching the Python compatibility
/// contract. This lets equivalent successful executions authorize the same
/// plan without discarding their audit identities.
///
/// # Errors
/// Returns a fail-closed error for unknown fields, malformed values, unstable
/// ordering, or either identity mismatch.
pub fn validate_upgrade_conformance_evidence(value: &Value) -> Result<(), EngineError> {
    require_schema_version(value, "1.0")?;
    let evidence = exact_object(
        value,
        EVIDENCE_FIELDS,
        "upgrade-conformance-evidence fields are invalid",
    )?;
    let stable_results = validate_suite_execution(evidence, "upgrade-conformance-evidence")?;
    for field in [
        "candidate_package_lock_hash",
        "schema_inventory_hash",
        "suite_definition_hash",
        "runner_sha256",
        "attestation_key",
        "fingerprint",
    ] {
        require_hash(
            evidence.get(field),
            &format!("upgrade-conformance-evidence {field} is invalid"),
        )?;
    }
    let mut stable = evidence.clone();
    stable.remove("attestation_key");
    stable.remove("fingerprint");
    stable.insert("command_results".to_owned(), Value::Array(stable_results));
    if evidence.get("attestation_key").and_then(Value::as_str)
        != Some(canonical_sha256(&Value::Object(stable))?.as_str())
    {
        return invalid("upgrade-conformance-evidence attestation key mismatch");
    }
    verify_fingerprint(
        evidence,
        "upgrade-conformance-evidence fingerprint mismatch",
    )
}

/// Validate one release-bound Upgrade Source Qualification v1 artifact.
///
/// Unlike candidate-bound Conformance Evidence v1, this artifact binds the
/// repository-owned suite to one immutable source archive and its complete
/// SBOM material identity. A later hosted upgrade must still authenticate and
/// extract that exact archive before compiling a lineage-specific candidate.
///
/// # Errors
/// Returns a fail-closed error for unknown fields, malformed source identity,
/// invalid suite results, unstable ordering, or either identity mismatch.
pub fn validate_upgrade_source_qualification(value: &Value) -> Result<(), EngineError> {
    require_schema_version(value, "1.0")?;
    let qualification = exact_object(
        value,
        SOURCE_QUALIFICATION_FIELDS,
        "upgrade-source-qualification fields are invalid",
    )?;
    let stable_results = validate_suite_execution(qualification, "upgrade-source-qualification")?;
    for field in [
        "schema_inventory_hash",
        "source_materials_sha256",
        "suite_definition_hash",
        "runner_sha256",
        "attestation_key",
        "fingerprint",
    ] {
        require_hash(
            qualification.get(field),
            &format!("upgrade-source-qualification {field} is invalid"),
        )?;
    }
    let source = exact_object(
        required(qualification, "source")?,
        &["artifact_sha256", "artifact_size", "revision", "root"],
        "upgrade-source-qualification.source is invalid",
    )?;
    require_hash(
        source.get("artifact_sha256"),
        "upgrade-source-qualification source artifact hash is invalid",
    )?;
    if source
        .get("artifact_size")
        .is_none_or(|size| !is_bounded_source_size(size))
        || !is_source_revision(string(source, "revision")?)
        || !is_safe_source_root(string(source, "root")?)
    {
        return invalid("upgrade-source-qualification source identity is invalid");
    }
    let mut stable = qualification.clone();
    stable.remove("attestation_key");
    stable.remove("fingerprint");
    stable.insert("command_results".to_owned(), Value::Array(stable_results));
    if qualification.get("attestation_key").and_then(Value::as_str)
        != Some(canonical_sha256(&Value::Object(stable))?.as_str())
    {
        return invalid("upgrade-source-qualification attestation key mismatch");
    }
    verify_fingerprint(
        qualification,
        "upgrade-source-qualification fingerprint mismatch",
    )
}

/// Validate one Upgrade Plan v1 artifact.
///
/// # Errors
/// Returns a fail-closed error for unknown fields, invalid selection/removal
/// semantics, malformed approval or rollback identities, non-canonical order,
/// or a fingerprint mismatch.
#[allow(clippy::too_many_lines)]
pub fn validate_upgrade_plan(value: &Value) -> Result<(), EngineError> {
    require_schema_version(value, "1.0")?;
    let plan = exact_object(value, PLAN_FIELDS, "upgrade-plan fields are invalid")?;
    let action = string(plan, "action")?;
    if !matches!(action, "upgrade" | "partial-uninstall") {
        return invalid("upgrade-plan action is invalid");
    }
    if string(plan, "target_root")?.is_empty() {
        return invalid("upgrade-plan target_root is invalid");
    }
    let status = string(plan, "status")?;
    if !matches!(status, "planned" | "no-change") {
        return invalid("upgrade-plan status is invalid");
    }
    for label in ["current", "candidate"] {
        let identity = exact_object(
            required(plan, label)?,
            &["install_plan_fingerprint", "package_lock_hash"],
            &format!("upgrade-plan.{label} is invalid"),
        )?;
        for field in ["install_plan_fingerprint", "package_lock_hash"] {
            require_hash(
                identity.get(field),
                &format!("upgrade-plan {label} identity is invalid"),
            )?;
        }
    }
    let current_selection = validate_selection(required(plan, "current_selection")?, "current")?;
    let selection = validate_selection(required(plan, "selection")?, "candidate")?;
    let removed_platforms = sorted_unique_strings(
        required(plan, "removed_platforms")?,
        "upgrade-plan removed_platforms is invalid",
    )?;
    let removed_runtime = sorted_unique_strings(
        required(plan, "removed_runtime_configs")?,
        "upgrade-plan removed_runtime_configs is invalid",
    )?;
    if action == "upgrade" {
        if !removed_platforms.is_empty() || !removed_runtime.is_empty() {
            return invalid("upgrade action must not contain a removal request");
        }
    } else if removed_platforms.is_empty()
        || !is_exact_removal(
            &current_selection.platforms,
            &selection.platforms,
            &removed_platforms,
        )
        || !is_exact_removal(
            &current_selection.runtime_configs,
            &selection.runtime_configs,
            &removed_runtime,
        )
        || selection.disciplines != current_selection.disciplines
    {
        return invalid("partial-uninstall candidate differs from its frozen removal request");
    }

    let external = exact_object(
        required(plan, "external")?,
        &["handler", "handler_sha256", "path_count", "paths_sha256"],
        "upgrade-plan.external is invalid",
    )?;
    let handler = string(external, "handler")?;
    let path_count = required(external, "path_count")?;
    if !is_handler(handler)
        || !is_hash(required(external, "handler_sha256")?)
        || !is_hash(required(external, "paths_sha256")?)
        || !is_nonnegative_integer(path_count)
        || (handler == "none") != is_zero_integer(path_count)
    {
        return invalid("upgrade-plan external lifecycle identity is invalid");
    }
    let changes = required(plan, "changes")?
        .as_object()
        .ok_or_else(|| EngineError::Invalid("upgrade-plan changes are invalid".to_owned()))?;
    let change_status = changes.get("status").and_then(Value::as_str);
    if !matches!(change_status, Some("changed" | "unchanged"))
        || (status == "no-change") != (change_status == Some("unchanged"))
    {
        return invalid("upgrade-plan status differs from changes");
    }
    let compatibility = exact_object(
        required(plan, "compatibility")?,
        &["agent_skills_lock", "install_plan_lock", "mode"],
        "upgrade-plan compatibility is invalid",
    )?;
    if compatibility.get("mode").and_then(Value::as_str) != Some("identity-only")
        || compatibility
            .get("agent_skills_lock")
            .and_then(Value::as_str)
            != Some("identity")
        || compatibility
            .get("install_plan_lock")
            .and_then(Value::as_str)
            != Some("identity")
    {
        return invalid("upgrade-plan only supports identity schema compatibility");
    }
    validate_migrations(required(plan, "migrations")?)?;
    validate_upgrade_steps(required(plan, "upgrade_steps")?)?;
    let approvals = sorted_unique_strings(
        required(plan, "approvals_required")?,
        "upgrade-plan approvals_required is invalid",
    )?;
    if approvals
        .iter()
        .any(|approval| !is_permission_approval(approval))
    {
        return invalid("upgrade-plan approvals_required is invalid");
    }
    require_hash(
        plan.get("conformance_attestation_key"),
        "upgrade-plan conformance attestation key is invalid",
    )?;
    let rollback = exact_object(
        required(plan, "rollback")?,
        &["point_fingerprint", "point_id", "previous_lock_hash"],
        "upgrade-plan rollback identity is invalid",
    )?;
    let current = required(plan, "current")?
        .as_object()
        .ok_or_else(|| EngineError::Invalid("upgrade-plan current is invalid".to_owned()))?;
    let current_lock = string(current, "package_lock_hash")?;
    let point_id = string(rollback, "point_id")?;
    if !is_hash(required(rollback, "point_fingerprint")?)
        || rollback.get("previous_lock_hash").and_then(Value::as_str) != Some(current_lock)
        || point_id != format!("rollback-{}", &current_lock[..12])
    {
        return invalid("upgrade-plan rollback identity is invalid");
    }
    verify_fingerprint(plan, "upgrade-plan fingerprint mismatch")
}

#[derive(Debug)]
struct Selection {
    disciplines: Vec<String>,
    platforms: Vec<String>,
    runtime_configs: Vec<String>,
}

fn validate_selection(value: &Value, label: &str) -> Result<Selection, EngineError> {
    let selection = exact_object(
        value,
        &["core_only", "disciplines", "platforms", "runtime_configs"],
        &format!("upgrade-plan {label} selection is invalid"),
    )?;
    let core_only = selection
        .get("core_only")
        .and_then(Value::as_bool)
        .ok_or_else(|| {
            EngineError::Invalid(format!(
                "upgrade-plan {label} selection core_only is invalid"
            ))
        })?;
    let platforms = sorted_unique_strings(
        required(selection, "platforms")?,
        &format!("upgrade-plan {label} selection platforms is invalid"),
    )?;
    let disciplines = sorted_unique_strings(
        required(selection, "disciplines")?,
        &format!("upgrade-plan {label} selection disciplines is invalid"),
    )?;
    let runtime_configs = sorted_unique_strings(
        required(selection, "runtime_configs")?,
        &format!("upgrade-plan {label} selection runtime_configs is invalid"),
    )?;
    if core_only != (platforms.is_empty() && disciplines.is_empty() && runtime_configs.is_empty()) {
        return invalid(format!(
            "upgrade-plan {label} selection core_only differs from selection"
        ));
    }
    Ok(Selection {
        disciplines,
        platforms,
        runtime_configs,
    })
}

fn validate_migrations(value: &Value) -> Result<(), EngineError> {
    let migrations = value.as_array().ok_or_else(|| {
        EngineError::Invalid("upgrade-plan migrations must be an array".to_owned())
    })?;
    let mut identities = Vec::with_capacity(migrations.len());
    for migration in migrations {
        let report = exact_object(
            migration,
            &[
                "after_sha256",
                "artifact",
                "before_sha256",
                "fingerprint",
                "from_version",
                "lossless",
                "schema_version",
                "status",
                "steps",
                "to_version",
            ],
            "migration-report fields are invalid",
        )?;
        require_schema_version(migration, "1.0")?;
        let artifact = nonempty_string(report, "artifact", "migration-report artifact is invalid")?;
        let from = nonempty_string(
            report,
            "from_version",
            "migration-report from_version is invalid",
        )?;
        let to = nonempty_string(
            report,
            "to_version",
            "migration-report to_version is invalid",
        )?;
        if report.get("status").and_then(Value::as_str) != Some("planned") {
            return invalid("upgrade-plan migration reports must be planned");
        }
        for field in ["before_sha256", "after_sha256", "fingerprint"] {
            require_hash(
                report.get(field),
                &format!("migration-report {field} is invalid"),
            )?;
        }
        let lossless = report
            .get("lossless")
            .and_then(Value::as_bool)
            .ok_or_else(|| {
                EngineError::Invalid(
                    "migration-report status or lossless flag is invalid".to_owned(),
                )
            })?;
        let steps = required(report, "steps")?.as_array().ok_or_else(|| {
            EngineError::Invalid("migration-report steps must be an array".to_owned())
        })?;
        if from == to && !steps.is_empty() {
            return invalid("identity migration must not contain steps");
        }
        let mut expected_from = from;
        let mut all_lossless = true;
        for step in steps {
            let step = exact_object(
                step,
                &["changes", "from_version", "lossless", "to_version"],
                "migration-report.step is invalid",
            )?;
            let step_from = string(step, "from_version")?;
            let step_to = string(step, "to_version")?;
            if step_from != expected_from || step_from == step_to {
                return invalid("migration-report step chain is invalid");
            }
            let step_lossless = step
                .get("lossless")
                .and_then(Value::as_bool)
                .ok_or_else(|| {
                    EngineError::Invalid(
                        "migration-report step lossless flag is invalid".to_owned(),
                    )
                })?;
            sorted_unique_strings(
                required(step, "changes")?,
                "migration-report step changes are invalid",
            )?;
            expected_from = step_to;
            all_lossless &= step_lossless;
        }
        if expected_from != to || all_lossless != lossless {
            return invalid("migration-report path summary is invalid");
        }
        verify_fingerprint(report, "migration-report fingerprint mismatch")?;
        identities.push((artifact.to_owned(), from.to_owned(), to.to_owned()));
    }
    let mut expected = identities.clone();
    expected.sort();
    expected.dedup();
    if identities != expected {
        return invalid("upgrade-plan migrations must be sorted and unique");
    }
    Ok(())
}

fn validate_upgrade_steps(value: &Value) -> Result<(), EngineError> {
    let steps = value.as_array().ok_or_else(|| {
        EngineError::Invalid("upgrade-plan upgrade_steps must be an array".to_owned())
    })?;
    let mut keys = Vec::with_capacity(steps.len());
    for step in steps {
        let step = exact_object(
            step,
            &["from", "kind", "to"],
            "upgrade-plan upgrade-step is invalid",
        )?;
        let kind = string(step, "kind")?;
        let from = string(step, "from")?;
        let to = string(step, "to")?;
        let rank = match kind {
            "core" => 0_u8,
            "schema" => 1,
            "package" => 2,
            "lock" => 3,
            _ => return invalid("upgrade-plan upgrade step is invalid"),
        };
        if from.is_empty() || to.is_empty() || from == to {
            return invalid("upgrade-plan upgrade step is invalid");
        }
        keys.push((rank, from.to_owned(), to.to_owned()));
    }
    let mut expected = keys.clone();
    expected.sort();
    expected.dedup();
    if keys != expected {
        return invalid("upgrade-plan upgrade steps must be sorted and unique");
    }
    Ok(())
}

fn validate_suite_execution(
    document: &Map<String, Value>,
    label: &str,
) -> Result<Vec<Value>, EngineError> {
    if string(document, "suite")? != "agent-skills-release-conformance-v1"
        || string(document, "status")? != "passed"
    {
        return invalid(format!("{label} suite or status is invalid"));
    }
    for field in ["manifest_count", "negative_contract_count", "test_count"] {
        if document
            .get(field)
            .is_none_or(|count| !is_positive_integer(count))
        {
            return invalid(format!("{label} {field} is invalid"));
        }
    }
    let environment = exact_object(
        required(document, "environment")?,
        &["platform", "python"],
        &format!("{label}.environment is invalid"),
    )?;
    if string(environment, "platform")?.is_empty()
        || !is_supported_python(string(environment, "python")?)
    {
        return invalid(format!("{label} environment is invalid"));
    }
    let results = required(document, "command_results")?
        .as_array()
        .ok_or_else(|| EngineError::Invalid(format!("{label} command results are invalid")))?;
    if results.is_empty() {
        return invalid(format!("{label} command results are invalid"));
    }
    let mut commands = Vec::with_capacity(results.len());
    let mut stable_results = Vec::with_capacity(results.len());
    for result in results {
        let result = exact_object(
            result,
            &["command", "exit_code", "stderr_sha256", "stdout_sha256"],
            &format!("{label}.command-result is invalid"),
        )?;
        let command = string(result, "command")?;
        if command.is_empty()
            || result
                .get("exit_code")
                .is_none_or(|value| !is_zero_integer(value))
            || !is_hash(required(result, "stdout_sha256")?)
            || !is_hash(required(result, "stderr_sha256")?)
        {
            return invalid(format!("{label} command result is invalid"));
        }
        commands.push(command.to_owned());
        stable_results.push(serde_json::json!({"command": command, "exit_code": 0}));
    }
    if !is_sorted_unique(&commands) {
        return invalid(format!("{label} command results must be sorted and unique"));
    }
    Ok(stable_results)
}

fn exact_object<'a>(
    value: &'a Value,
    fields: &[&str],
    message: &str,
) -> Result<&'a Map<String, Value>, EngineError> {
    let object = value
        .as_object()
        .ok_or_else(|| EngineError::Invalid(message.to_owned()))?;
    let expected = fields.iter().copied().collect::<BTreeSet<_>>();
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    if actual != expected {
        return invalid(message);
    }
    Ok(object)
}

fn required<'a>(object: &'a Map<String, Value>, field: &str) -> Result<&'a Value, EngineError> {
    object
        .get(field)
        .ok_or_else(|| EngineError::Invalid(format!("{field} is required")))
}

fn string<'a>(object: &'a Map<String, Value>, field: &str) -> Result<&'a str, EngineError> {
    required(object, field)?
        .as_str()
        .ok_or_else(|| EngineError::Invalid(format!("{field} must be a string")))
}

fn nonempty_string<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    message: &str,
) -> Result<&'a str, EngineError> {
    let value = string(object, field)?;
    if value.is_empty() {
        return invalid(message);
    }
    Ok(value)
}

fn require_hash(value: Option<&Value>, message: &str) -> Result<(), EngineError> {
    if value.is_none_or(|value| !is_hash(value)) {
        return invalid(message);
    }
    Ok(())
}

fn is_hash(value: &Value) -> bool {
    value.as_str().is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    })
}

fn is_zero_integer(value: &Value) -> bool {
    json_integer(value) == json_integer(&Value::from(0))
}

fn is_positive_integer(value: &Value) -> bool {
    let Some(value) = json_integer(value) else {
        return false;
    };
    value > json_integer(&Value::from(0)).expect("zero is a JSON integer")
}

fn is_bounded_source_size(value: &Value) -> bool {
    value
        .as_u64()
        .is_some_and(|size| (1..=134_217_728).contains(&size))
}

fn is_source_revision(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn is_safe_source_root(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        || !value
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        || value.as_bytes().last() == Some(&b'.')
    {
        return false;
    }
    let basename = value
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    !matches!(basename.as_str(), "aux" | "con" | "nul" | "prn")
        && !is_numbered_windows_device(&basename, "com")
        && !is_numbered_windows_device(&basename, "lpt")
}

fn is_numbered_windows_device(value: &str, prefix: &str) -> bool {
    value
        .strip_prefix(prefix)
        .is_some_and(|suffix| matches!(suffix.as_bytes(), [b'1'..=b'9']))
}

fn is_nonnegative_integer(value: &Value) -> bool {
    let Some(value) = json_integer(value) else {
        return false;
    };
    value >= json_integer(&Value::from(0)).expect("zero is a JSON integer")
}

fn is_supported_python(value: &str) -> bool {
    let mut parts = value.split('.');
    if parts.next() != Some("3") || !matches!(parts.next(), Some("11" | "12" | "13" | "14")) {
        return false;
    }
    match (parts.next(), parts.next()) {
        (None, None) => true,
        (Some(patch), None) => !patch.is_empty() && patch.bytes().all(|byte| byte.is_ascii_digit()),
        _ => false,
    }
}

fn sorted_unique_strings(value: &Value, message: &str) -> Result<Vec<String>, EngineError> {
    let values = value
        .as_array()
        .ok_or_else(|| EngineError::Invalid(message.to_owned()))?;
    let strings = values
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| EngineError::Invalid(message.to_owned()))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if !is_sorted_unique(&strings) {
        return invalid(message);
    }
    Ok(strings)
}

fn is_sorted_unique(values: &[impl Ord]) -> bool {
    values.windows(2).all(|pair| pair[0] < pair[1])
}

fn is_exact_removal(current: &[String], candidate: &[String], removed: &[String]) -> bool {
    let current = current.iter().map(String::as_str).collect::<BTreeSet<_>>();
    let candidate = candidate
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    let removed = removed.iter().map(String::as_str).collect::<BTreeSet<_>>();
    removed.is_subset(&current) && candidate == current.difference(&removed).copied().collect()
}

fn is_handler(value: &str) -> bool {
    value == "none"
        || (!value.is_empty()
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')))
}

fn is_permission_approval(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("permission:") else {
        return false;
    };
    let Some((capability, transition)) = rest.split_once(':') else {
        return false;
    };
    let Some((before, after)) = transition.split_once("->") else {
        return false;
    };
    [capability, before, after]
        .iter()
        .all(|item| is_identifier(item))
}

fn is_identifier(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
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
    use super::{
        validate_upgrade_conformance_evidence, validate_upgrade_plan,
        validate_upgrade_source_qualification,
    };
    use agent_contracts::canonical_sha256;
    use serde_json::{Value, json};

    fn evidence() -> Value {
        let mut value = json!({
            "candidate_package_lock_hash": "1".repeat(64),
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
            "schema_inventory_hash": "5".repeat(64),
            "schema_version": "1.0",
            "status": "passed",
            "suite": "agent-skills-release-conformance-v1",
            "suite_definition_hash": "6".repeat(64),
            "test_count": 531,
        });
        let mut stable = value.as_object().unwrap().clone();
        stable.insert(
            "command_results".to_owned(),
            json!([{"command": "compatibility-suite", "exit_code": 0}]),
        );
        value["attestation_key"] = Value::String(canonical_sha256(&Value::Object(stable)).unwrap());
        let fingerprint = canonical_sha256(&value).unwrap();
        value["fingerprint"] = Value::String(fingerprint);
        value
    }

    fn plan(evidence: &Value) -> Value {
        let current_lock = "7".repeat(64);
        let current_plan = "8".repeat(64);
        let mut value = json!({
            "action": "upgrade",
            "approvals_required": [],
            "candidate": {
                "install_plan_fingerprint": current_plan,
                "package_lock_hash": current_lock,
            },
            "changes": {"status": "unchanged"},
            "compatibility": {
                "agent_skills_lock": "identity",
                "install_plan_lock": "identity",
                "mode": "identity-only",
            },
            "conformance_attestation_key": evidence["attestation_key"],
            "current": {
                "install_plan_fingerprint": current_plan,
                "package_lock_hash": current_lock,
            },
            "current_selection": {
                "core_only": false,
                "disciplines": [],
                "platforms": ["apple"],
                "runtime_configs": ["codex"],
            },
            "external": {
                "handler": "none",
                "handler_sha256": canonical_sha256(&json!("none")).unwrap(),
                "path_count": 0,
                "paths_sha256": canonical_sha256(&json!([])).unwrap(),
            },
            "migrations": [],
            "removed_platforms": [],
            "removed_runtime_configs": [],
            "rollback": {
                "point_fingerprint": "9".repeat(64),
                "point_id": format!("rollback-{}", &current_lock[..12]),
                "previous_lock_hash": current_lock,
            },
            "schema_version": "1.0",
            "selection": {
                "core_only": false,
                "disciplines": [],
                "platforms": ["apple"],
                "runtime_configs": ["codex"],
            },
            "status": "no-change",
            "target_root": "/tmp/codex",
            "upgrade_steps": [],
        });
        value["fingerprint"] = Value::String(canonical_sha256(&value).unwrap());
        value
    }

    fn source_qualification(evidence: &Value) -> Value {
        let mut value = evidence.as_object().unwrap().clone();
        value.remove("attestation_key");
        value.remove("candidate_package_lock_hash");
        value.remove("fingerprint");
        value.insert(
            "source".to_owned(),
            json!({
                "artifact_sha256": "a".repeat(64),
                "artifact_size": 1024,
                "revision": "b".repeat(40),
                "root": "agent-development-skills-1.0.0",
            }),
        );
        value.insert(
            "source_materials_sha256".to_owned(),
            Value::String("c".repeat(64)),
        );
        let mut stable = value.clone();
        stable.insert(
            "command_results".to_owned(),
            json!([{"command": "compatibility-suite", "exit_code": 0}]),
        );
        let attestation = canonical_sha256(&Value::Object(stable)).unwrap();
        value.insert("attestation_key".to_owned(), Value::String(attestation));
        let fingerprint = canonical_sha256(&Value::Object(value.clone())).unwrap();
        value.insert("fingerprint".to_owned(), Value::String(fingerprint));
        Value::Object(value)
    }

    fn refresh_fingerprint(value: &mut Value) {
        value.as_object_mut().unwrap().remove("fingerprint");
        value["fingerprint"] = Value::String(canonical_sha256(value).unwrap());
    }

    #[test]
    fn valid_upgrade_control_plane_contracts_are_accepted() {
        let evidence = evidence();
        validate_upgrade_conformance_evidence(&evidence).unwrap();
        validate_upgrade_source_qualification(&source_qualification(&evidence)).unwrap();
        validate_upgrade_plan(&plan(&evidence)).unwrap();
    }

    #[test]
    fn self_consistent_semantic_tampering_is_rejected() {
        let valid_evidence = evidence();
        let mut invalid_plan = plan(&valid_evidence);
        invalid_plan["current_selection"]["core_only"] = Value::Bool(true);
        refresh_fingerprint(&mut invalid_plan);
        assert!(validate_upgrade_plan(&invalid_plan).is_err());

        let mut invalid_evidence = valid_evidence;
        let duplicate = invalid_evidence["command_results"][0].clone();
        invalid_evidence["command_results"]
            .as_array_mut()
            .unwrap()
            .push(duplicate);
        invalid_evidence
            .as_object_mut()
            .unwrap()
            .remove("attestation_key");
        invalid_evidence
            .as_object_mut()
            .unwrap()
            .remove("fingerprint");
        let mut stable = invalid_evidence.as_object().unwrap().clone();
        stable.insert(
            "command_results".to_owned(),
            json!([
                {"command": "compatibility-suite", "exit_code": 0},
                {"command": "compatibility-suite", "exit_code": 0},
            ]),
        );
        invalid_evidence["attestation_key"] =
            Value::String(canonical_sha256(&Value::Object(stable)).unwrap());
        refresh_fingerprint(&mut invalid_evidence);
        assert!(validate_upgrade_conformance_evidence(&invalid_evidence).is_err());

        for unsafe_root in [
            "../source".to_owned(),
            "source.".to_owned(),
            "CON".to_owned(),
            "aux.txt".to_owned(),
            "a".repeat(129),
        ] {
            let mut invalid_qualification = source_qualification(&evidence());
            invalid_qualification["source"]["root"] = Value::String(unsafe_root);
            invalid_qualification
                .as_object_mut()
                .unwrap()
                .remove("attestation_key");
            invalid_qualification
                .as_object_mut()
                .unwrap()
                .remove("fingerprint");
            let mut stable = invalid_qualification.as_object().unwrap().clone();
            stable.insert(
                "command_results".to_owned(),
                json!([{"command": "compatibility-suite", "exit_code": 0}]),
            );
            invalid_qualification["attestation_key"] =
                Value::String(canonical_sha256(&Value::Object(stable)).unwrap());
            refresh_fingerprint(&mut invalid_qualification);
            assert!(validate_upgrade_source_qualification(&invalid_qualification).is_err());
        }
    }
}
