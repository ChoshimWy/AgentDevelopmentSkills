use super::{
    LifecycleError, absolute_path, inspect_doctor_baseline, inspect_doctor_baseline_embedded,
    valid_sha256,
};
use agent_contracts::{canonical_sha256, parse_json};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::path::Path;

const DOCTOR_SCHEMA_VERSION_V1: &str = "1.0";
const DOCTOR_SCHEMA_VERSION_V2: &str = "2.0";
const NATIVE_IMPLEMENTATION: &str = "agent-skills-rs";
const PYTHON_REQUIREMENT: &str = ">=3.11";
const EMBEDDED_SCHEMA_INVENTORY: &[u8] =
    include_bytes!(concat!(env!("OUT_DIR"), "/embedded-schema-inventory.json"));

/// Emit a complete Doctor Report v1 from the native read-only inspection.
///
/// Doctor Report v1 freezes the Python runtime version because the production
/// Doctor is still hosted by Python. A pure Rust process cannot prove that
/// value without executing an external interpreter, so the compatibility host
/// must attest it explicitly. The value is syntax-checked and determines the
/// `environment.python` check, but this function does not claim to discover it.
///
/// # Errors
/// Returns an error when the host attestation is not a canonical semantic
/// version, the baseline projection cannot be constructed, or the assembled
/// report violates the Doctor Report v1 contract.
pub fn inspect_doctor_report_v1(
    target_root: impl AsRef<Path>,
    schema_root: impl AsRef<Path>,
    python_version: &str,
) -> Result<Value, LifecycleError> {
    let python = parse_semantic_version(python_version, "Python runtime version")?;
    let schemas = absolute_path(schema_root.as_ref())?;
    let mut projection = inspect_doctor_baseline(target_root, &schemas)?;
    let projection = projection
        .as_object_mut()
        .ok_or_else(|| invalid_error("Doctor baseline projection is invalid"))?;
    projection.remove("fingerprint");
    let mut checks = projection
        .remove("checks")
        .and_then(|value| value.as_array().cloned())
        .ok_or_else(|| invalid_error("Doctor baseline checks are invalid"))?;
    let python_supported = match compare_decimal_component(python[0], "3") {
        std::cmp::Ordering::Greater => true,
        std::cmp::Ordering::Equal => {
            compare_decimal_component(python[1], "11") != std::cmp::Ordering::Less
        }
        std::cmp::Ordering::Less => false,
    };
    checks.insert(
        0,
        json!({
            "category": "environment",
            "details": {
                "actual": python_version,
                "required": PYTHON_REQUIREMENT,
            },
            "id": "environment.python",
            "status": if python_supported { "passed" } else { "failed" },
            "summary": if python_supported {
                "Python runtime satisfies the supported baseline"
            } else {
                "Python runtime does not satisfy the supported baseline"
            },
        }),
    );

    let mut counts = serde_json::Map::new();
    for status in ["passed", "failed", "skipped", "warning"] {
        let count = checks
            .iter()
            .filter(|check| check.get("status").and_then(Value::as_str) == Some(status))
            .count();
        counts.insert(status.to_owned(), json!(count));
    }
    let blocked = counts
        .get("failed")
        .and_then(Value::as_u64)
        .is_some_and(|count| count > 0);
    let mut report = json!({
        "checks": checks,
        "environment": {
            "core_version": env!("CARGO_PKG_VERSION"),
            "python_required": PYTHON_REQUIREMENT,
            "python_version": python_version,
            "schema_root": schemas,
        },
        "install": projection.remove("install").unwrap_or(Value::Null),
        "recovery": projection.remove("recovery").unwrap_or(Value::Null),
        "schema_version": DOCTOR_SCHEMA_VERSION_V1,
        "status": if blocked { "blocked" } else { "passed" },
        "summary": counts,
        "target_root": projection.remove("target_root").unwrap_or(Value::Null),
    });
    if !projection.is_empty() {
        return invalid("Doctor baseline projection contains unknown fields");
    }
    let fingerprint = canonical_sha256(&report)?;
    report
        .as_object_mut()
        .ok_or_else(|| invalid_error("Doctor Report v1 is invalid"))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    validate_doctor_report_v1(&report)?;
    Ok(report)
}

/// Emit the runtime-neutral Doctor Report v2 from the native read-only engine.
///
/// Report v2 removes the Python-host attestation from the public contract. The
/// native executable carries the exact release Schema inventory produced at
/// build time, so an installed CLI can diagnose its target without a source
/// checkout, an external interpreter, network access, or a caller-supplied
/// Schema directory.
///
/// # Errors
/// Returns an error when the embedded Schema inventory is invalid, the native
/// baseline cannot be assembled, or the result violates Doctor Report v2.
pub fn inspect_doctor_report_v2(target_root: impl AsRef<Path>) -> Result<Value, LifecycleError> {
    let schema_inventory = parse_json(EMBEDDED_SCHEMA_INVENTORY)?;
    let mut projection = inspect_doctor_baseline_embedded(target_root, &schema_inventory)?;
    let projection = projection
        .as_object_mut()
        .ok_or_else(|| invalid_error("Doctor baseline projection is invalid"))?;
    projection.remove("fingerprint");
    let checks = projection
        .remove("checks")
        .and_then(|value| value.as_array().cloned())
        .ok_or_else(|| invalid_error("Doctor baseline checks are invalid"))?;
    let counts = count_check_statuses(&checks);
    let blocked = counts
        .get("failed")
        .and_then(Value::as_u64)
        .is_some_and(|count| count > 0);
    let file_count = schema_inventory
        .get("files")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    let mut report = json!({
        "checks": checks,
        "environment": {
            "core_version": env!("CARGO_PKG_VERSION"),
            "implementation": {
                "name": NATIVE_IMPLEMENTATION,
                "version": env!("CARGO_PKG_VERSION"),
            },
            "schema_inventory": {
                "algorithm": schema_inventory.get("algorithm").cloned().unwrap_or(Value::Null),
                "content_sha256": schema_inventory.get("content_sha256").cloned().unwrap_or(Value::Null),
                "file_count": file_count,
            },
        },
        "install": projection.remove("install").unwrap_or(Value::Null),
        "recovery": projection.remove("recovery").unwrap_or(Value::Null),
        "schema_version": DOCTOR_SCHEMA_VERSION_V2,
        "status": if blocked { "blocked" } else { "passed" },
        "summary": counts,
        "target_root": projection.remove("target_root").unwrap_or(Value::Null),
    });
    if !projection.is_empty() {
        return invalid("Doctor baseline projection contains unknown fields");
    }
    let fingerprint = canonical_sha256(&report)?;
    report
        .as_object_mut()
        .ok_or_else(|| invalid_error("Doctor Report v2 is invalid"))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    validate_doctor_report_v2(&report)?;
    Ok(report)
}

#[allow(clippy::too_many_lines)]
pub(super) fn validate_doctor_report_v1(value: &Value) -> Result<(), LifecycleError> {
    let report = value
        .as_object()
        .filter(|object| {
            exact_object_fields(
                object,
                &[
                    "checks",
                    "environment",
                    "fingerprint",
                    "install",
                    "recovery",
                    "schema_version",
                    "status",
                    "summary",
                    "target_root",
                ],
            )
        })
        .ok_or_else(|| invalid_error("doctor-report must contain exactly the required fields"))?;
    if report.get("schema_version").and_then(Value::as_str) != Some(DOCTOR_SCHEMA_VERSION_V1) {
        return invalid("unsupported schema_version");
    }
    if !matches!(
        report.get("status").and_then(Value::as_str),
        Some("passed" | "blocked")
    ) || report
        .get("target_root")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        return invalid("doctor-report root fields are invalid");
    }

    let environment = exact_object(
        report.get("environment"),
        &[
            "core_version",
            "python_required",
            "python_version",
            "schema_root",
        ],
        "doctor-report environment",
    )?;
    parse_semantic_version(
        environment
            .get("python_version")
            .and_then(Value::as_str)
            .unwrap_or_default(),
        "doctor-report Python version",
    )?;
    parse_semantic_version(
        environment
            .get("core_version")
            .and_then(Value::as_str)
            .unwrap_or_default(),
        "doctor-report Core version",
    )?;
    if environment.get("python_required").and_then(Value::as_str) != Some(PYTHON_REQUIREMENT)
        || environment
            .get("schema_root")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        return invalid("doctor-report environment is invalid");
    }

    let install = exact_object(
        report.get("install"),
        &[
            "install_plan_fingerprint",
            "package_lock_hash",
            "selected_disciplines",
            "selected_platforms",
            "selected_runtime_configs",
        ],
        "doctor-report install",
    )?;
    for field in ["install_plan_fingerprint", "package_lock_hash"] {
        if !matches!(install.get(field), Some(Value::Null))
            && !install
                .get(field)
                .and_then(Value::as_str)
                .is_some_and(valid_sha256)
        {
            return invalid(format!("doctor-report {field} is invalid"));
        }
    }
    for field in [
        "selected_disciplines",
        "selected_platforms",
        "selected_runtime_configs",
    ] {
        validate_unique_strings(install.get(field), field)?;
    }

    let recovery = exact_object(
        report.get("recovery"),
        &["candidates", "status"],
        "doctor-report recovery",
    )?;
    let recovery_status = recovery
        .get("status")
        .and_then(Value::as_str)
        .filter(|status| matches!(*status, "clean" | "attention" | "unknown"))
        .ok_or_else(|| invalid_error("doctor-report recovery is invalid"))?;
    let candidates = recovery
        .get("candidates")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_error("doctor-report recovery is invalid"))?;
    let mut recovery_paths = Vec::with_capacity(candidates.len());
    for candidate in candidates {
        let candidate = exact_object(
            Some(candidate),
            &["kind", "path"],
            "doctor-report recovery candidate",
        )?;
        let kind = candidate
            .get("kind")
            .and_then(Value::as_str)
            .filter(|kind| {
                matches!(
                    *kind,
                    "install-backup" | "install-stage" | "lifecycle-lock" | "uninstall-backup"
                )
            });
        let path = candidate
            .get("path")
            .and_then(Value::as_str)
            .filter(|path| !path.is_empty());
        if kind.is_none() || path.is_none() {
            return invalid("doctor-report recovery candidate is invalid");
        }
        recovery_paths.push(path.unwrap_or_default());
    }
    if !recovery_paths.windows(2).all(|pair| pair[0] < pair[1])
        || (recovery_status == "clean" && !recovery_paths.is_empty())
        || (recovery_status == "attention" && recovery_paths.is_empty())
        || (recovery_status == "unknown" && !recovery_paths.is_empty())
    {
        return invalid("doctor-report recovery status or candidates are invalid");
    }

    let checks = report
        .get("checks")
        .and_then(Value::as_array)
        .filter(|checks| !checks.is_empty())
        .ok_or_else(|| invalid_error("doctor-report checks must not be empty"))?;
    let mut check_ids = HashSet::with_capacity(checks.len());
    let mut status_counts = std::collections::BTreeMap::from([
        ("failed", 0_u64),
        ("passed", 0_u64),
        ("skipped", 0_u64),
        ("warning", 0_u64),
    ]);
    let mut recovery_check_status = None;
    for check in checks {
        let check = exact_object(
            Some(check),
            &["category", "details", "id", "status", "summary"],
            "doctor-report check",
        )?;
        let check_id = check
            .get("id")
            .and_then(Value::as_str)
            .filter(|value| valid_check_id(value))
            .ok_or_else(|| invalid_error("doctor-report check is invalid"))?;
        let category = check
            .get("category")
            .and_then(Value::as_str)
            .filter(|value| {
                matches!(
                    *value,
                    "environment"
                        | "filesystem"
                        | "install"
                        | "lock"
                        | "schema"
                        | "package"
                        | "skill"
                        | "instructions"
                        | "binding"
                        | "permission"
                        | "activation"
                        | "recovery"
                )
            });
        let status = check
            .get("status")
            .and_then(Value::as_str)
            .filter(|value| status_counts.contains_key(*value));
        if category.is_none()
            || status.is_none()
            || check.get("details").and_then(Value::as_object).is_none()
            || check
                .get("summary")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
            || !check_ids.insert(check_id)
        {
            return invalid("doctor-report check is invalid");
        }
        let status = status.unwrap_or_default();
        *status_counts
            .get_mut(status)
            .unwrap_or_else(|| unreachable!()) += 1;
        if check_id == "recovery.residue" {
            recovery_check_status = Some(status);
        }
    }
    let recovery_matches = match recovery_status {
        "clean" => recovery_check_status == Some("passed"),
        "attention" => recovery_check_status == Some("failed"),
        "unknown" => matches!(recovery_check_status, Some("failed" | "skipped")),
        _ => false,
    };
    if !recovery_matches {
        return invalid("doctor-report recovery state differs from its check");
    }

    let summary = exact_object(
        report.get("summary"),
        &["failed", "passed", "skipped", "warning"],
        "doctor-report summary",
    )?;
    for (status, count) in &status_counts {
        if summary.get(*status).and_then(Value::as_u64) != Some(*count) {
            return invalid("doctor-report summary differs from checks");
        }
    }
    let failed = status_counts.get("failed").copied().unwrap_or_default();
    if report.get("status").and_then(Value::as_str)
        != Some(if failed > 0 { "blocked" } else { "passed" })
    {
        return invalid("doctor-report status differs from checks");
    }
    let fingerprint = report
        .get("fingerprint")
        .and_then(Value::as_str)
        .filter(|value| valid_sha256(value))
        .ok_or_else(|| invalid_error("doctor-report fingerprint is invalid"))?;
    let mut identity = report.clone();
    identity.remove("fingerprint");
    if canonical_sha256(&Value::Object(identity))? != fingerprint {
        return invalid("doctor-report fingerprint mismatch");
    }
    Ok(())
}

pub(super) fn validate_doctor_report_v2(value: &Value) -> Result<(), LifecycleError> {
    let report = value
        .as_object()
        .filter(|object| {
            exact_object_fields(
                object,
                &[
                    "checks",
                    "environment",
                    "fingerprint",
                    "install",
                    "recovery",
                    "schema_version",
                    "status",
                    "summary",
                    "target_root",
                ],
            )
        })
        .ok_or_else(|| invalid_error("doctor-report must contain exactly the required fields"))?;
    if report.get("schema_version").and_then(Value::as_str) != Some(DOCTOR_SCHEMA_VERSION_V2) {
        return invalid("unsupported schema_version");
    }
    let environment = exact_object(
        report.get("environment"),
        &["core_version", "implementation", "schema_inventory"],
        "doctor-report environment",
    )?;
    parse_semantic_version(
        environment
            .get("core_version")
            .and_then(Value::as_str)
            .unwrap_or_default(),
        "doctor-report Core version",
    )?;
    let implementation = exact_object(
        environment.get("implementation"),
        &["name", "version"],
        "doctor-report implementation",
    )?;
    if implementation
        .get("name")
        .and_then(Value::as_str)
        .is_none_or(|name| !valid_check_id(name))
    {
        return invalid("doctor-report implementation is invalid");
    }
    parse_semantic_version(
        implementation
            .get("version")
            .and_then(Value::as_str)
            .unwrap_or_default(),
        "doctor-report implementation version",
    )?;
    let inventory = exact_object(
        environment.get("schema_inventory"),
        &["algorithm", "content_sha256", "file_count"],
        "doctor-report Schema inventory",
    )?;
    if inventory.get("algorithm").and_then(Value::as_str) != Some("sha256")
        || !inventory
            .get("content_sha256")
            .and_then(Value::as_str)
            .is_some_and(valid_sha256)
        || inventory
            .get("file_count")
            .and_then(Value::as_u64)
            .is_none_or(|count| count == 0)
    {
        return invalid("doctor-report Schema inventory is invalid");
    }
    validate_v2_schema_check(report, inventory)?;

    let fingerprint = report
        .get("fingerprint")
        .and_then(Value::as_str)
        .filter(|value| valid_sha256(value))
        .ok_or_else(|| invalid_error("doctor-report fingerprint is invalid"))?;
    let mut identity = report.clone();
    identity.remove("fingerprint");
    if canonical_sha256(&Value::Object(identity))? != fingerprint {
        return invalid("doctor-report fingerprint mismatch");
    }

    validate_v2_compatibility(value, environment)
}

fn validate_v2_schema_check(
    report: &serde_json::Map<String, Value>,
    inventory: &serde_json::Map<String, Value>,
) -> Result<(), LifecycleError> {
    let check = report
        .get("checks")
        .and_then(Value::as_array)
        .and_then(|checks| {
            checks
                .iter()
                .find(|check| check.get("id").and_then(Value::as_str) == Some("schema.inventory"))
        })
        .ok_or_else(|| invalid_error("doctor-report Schema inventory check is missing"))?;
    if check.get("status").and_then(Value::as_str) == Some("passed") {
        let details = check
            .get("details")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid_error("doctor-report Schema inventory check is invalid"))?;
        if details.get("content_sha256") != inventory.get("content_sha256")
            || details.get("file_count") != inventory.get("file_count")
        {
            return invalid("doctor-report Schema inventory differs from its check");
        }
    }
    Ok(())
}

fn validate_v2_compatibility(
    value: &Value,
    environment: &serde_json::Map<String, Value>,
) -> Result<(), LifecycleError> {
    let mut compatibility = value.clone();
    compatibility["schema_version"] = Value::String(DOCTOR_SCHEMA_VERSION_V1.to_owned());
    compatibility["environment"] = json!({
        "core_version": environment.get("core_version").cloned().unwrap_or(Value::Null),
        "python_required": PYTHON_REQUIREMENT,
        "python_version": "3.11.0",
        "schema_root": "embedded://agent-skills/schema-inventory",
    });
    compatibility
        .as_object_mut()
        .ok_or_else(|| invalid_error("doctor-report compatibility projection is invalid"))?
        .remove("fingerprint");
    let compatibility_fingerprint = canonical_sha256(&compatibility)?;
    compatibility
        .as_object_mut()
        .ok_or_else(|| invalid_error("doctor-report compatibility projection is invalid"))?
        .insert(
            "fingerprint".to_owned(),
            Value::String(compatibility_fingerprint),
        );
    validate_doctor_report_v1(&compatibility)
}

fn count_check_statuses(checks: &[Value]) -> Value {
    let mut counts = serde_json::Map::new();
    for status in ["passed", "failed", "skipped", "warning"] {
        let count = checks
            .iter()
            .filter(|check| check.get("status").and_then(Value::as_str) == Some(status))
            .count();
        counts.insert(status.to_owned(), json!(count));
    }
    Value::Object(counts)
}

fn exact_object<'a>(
    value: Option<&'a Value>,
    fields: &[&str],
    label: &str,
) -> Result<&'a serde_json::Map<String, Value>, LifecycleError> {
    value
        .and_then(Value::as_object)
        .filter(|object| exact_object_fields(object, fields))
        .ok_or_else(|| invalid_error(format!("{label} fields are invalid")))
}

fn exact_object_fields(object: &serde_json::Map<String, Value>, fields: &[&str]) -> bool {
    object.len() == fields.len() && fields.iter().all(|field| object.contains_key(*field))
}

fn validate_unique_strings(value: Option<&Value>, label: &str) -> Result<(), LifecycleError> {
    let values = value
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_error(format!("doctor-report {label} is invalid")))?;
    let mut unique = HashSet::with_capacity(values.len());
    if values.iter().any(|value| {
        value
            .as_str()
            .filter(|value| !value.is_empty())
            .is_none_or(|value| !unique.insert(value))
    }) {
        return invalid(format!("doctor-report {label} is invalid"));
    }
    Ok(())
}

fn parse_semantic_version<'a>(value: &'a str, label: &str) -> Result<[&'a str; 3], LifecycleError> {
    let mut parts = value.split('.');
    let version = [
        parts.next().unwrap_or_default(),
        parts.next().unwrap_or_default(),
        parts.next().unwrap_or_default(),
    ];
    if parts.next().is_some() {
        return invalid(format!("{label} is invalid"));
    }
    for part in version {
        if part.is_empty()
            || !part.bytes().all(|byte| byte.is_ascii_digit())
            || (part != "0" && part.starts_with('0'))
        {
            return invalid(format!("{label} is invalid"));
        }
    }
    Ok(version)
}

fn compare_decimal_component(left: &str, right: &str) -> std::cmp::Ordering {
    left.len().cmp(&right.len()).then_with(|| left.cmp(right))
}

fn valid_check_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
        && bytes
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b".-".contains(&byte))
}

fn invalid_error(message: impl Into<String>) -> LifecycleError {
    LifecycleError::Invalid(message.into())
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(invalid_error(message))
}
