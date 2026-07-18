//! Persistent deterministic package Lockfile compatibility.

use super::{EngineError, invalid};
use agent_contracts::{canonical_sha256, load_json};
use agent_registry::satisfies;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read as _;
use std::path::{Component, Path, PathBuf};

const LOCK_SCHEMA_VERSION: &str = "1.0";
const LOCK_MANAGER: &str = "agent-development-skills";
const MAX_LOCK_PACKAGES: usize = 16_384;
const MAX_LOCK_DEPENDENCIES: usize = 65_536;
const MAX_LOCK_PROVIDERS: usize = 65_536;
const MAX_LOCK_SCHEMAS: usize = 65_536;
const MAX_PACKAGE_FILES: usize = 100_000;
const MAX_SCHEMA_DIRECTORY_ENTRIES: usize = 100_000;
const MAX_PATH_BYTES: usize = 4_096;
type SelectedPlanRecords<'a> = BTreeMap<String, (&'a Map<String, Value>, String, Vec<String>)>;

/// Return the non-circular Install Plan identity frozen by a package Lockfile.
///
/// # Errors
/// Returns an error when canonical hashing fails.
pub fn install_plan_identity_hash(install_plan: &Value) -> Result<String, EngineError> {
    let mut identity = object(install_plan, "Install Plan")?.clone();
    for field in ["fingerprint", "package_lock_hash", "status"] {
        identity.remove(field);
    }
    Ok(canonical_sha256(&Value::Object(identity))?)
}

/// Freeze the public schema surface without host-absolute paths.
///
/// # Errors
/// Returns a fail-closed error for unsafe roots, symlinks, malformed schemas,
/// unsupported drafts, excessive directory breadth, or I/O failures.
pub fn schema_inventory(schema_root: impl AsRef<Path>) -> Result<Value, EngineError> {
    let root = schema_root.as_ref();
    let metadata = std::fs::symlink_metadata(root).map_err(|error| {
        EngineError::Invalid(format!(
            "schema root is missing or unsafe: {}: {error}",
            root.display()
        ))
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return invalid(format!(
            "schema root is missing or unsafe: {}",
            root.display()
        ));
    }
    let root_is_schemas = root.file_name().is_some_and(|name| name == "schemas");
    let repository_root = if root_is_schemas {
        root.parent().ok_or_else(|| {
            EngineError::Invalid("schema root has no repository parent".to_owned())
        })?
    } else {
        root
    };
    let mut candidates = BTreeSet::new();
    let mut entry_count = 0_usize;
    if root_is_schemas {
        collect_schema_files(root, &mut candidates, &mut entry_count)?;
        for (container, children) in [
            ("disciplines", &["contracts"][..]),
            ("platforms", &["contracts", "config"][..]),
            ("stacks", &["contracts"][..]),
        ] {
            collect_nested_schema_files(
                &repository_root.join(container),
                children,
                &mut candidates,
                &mut entry_count,
            )?;
        }
    } else {
        collect_schema_files(root, &mut candidates, &mut entry_count)?;
    }
    if candidates.len() > MAX_LOCK_SCHEMAS {
        return invalid(format!(
            "schema inventory exceeds maximum of {MAX_LOCK_SCHEMAS} files"
        ));
    }
    let mut files = Vec::with_capacity(candidates.len());
    for path in candidates {
        let metadata = std::fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return invalid(format!(
                "schema file is unsafe: {}",
                path.file_name().unwrap_or_default().to_string_lossy()
            ));
        }
        let value = load_json(&path).map_err(|error| {
            EngineError::Invalid(format!(
                "schema file is invalid: {}: {error}",
                path.file_name().unwrap_or_default().to_string_lossy()
            ))
        })?;
        if value.get("$schema").and_then(Value::as_str)
            != Some("https://json-schema.org/draft/2020-12/schema")
        {
            return invalid(format!(
                "schema file has unsupported draft: {}",
                path.file_name().unwrap_or_default().to_string_lossy()
            ));
        }
        let relative = path
            .strip_prefix(repository_root)
            .map_err(|_| EngineError::Invalid("schema file escapes repository root".to_owned()))?;
        let relative = portable_relative_path(relative)?;
        files.push(json!({
            "path": relative,
            "sha256": file_sha256(&path)?,
        }));
    }
    if files.is_empty() {
        return invalid("schema inventory must not be empty");
    }
    Ok(json!({
        "algorithm": "sha256",
        "content_sha256": canonical_sha256(&Value::Array(files.clone()))?,
        "files": files,
    }))
}

/// Resolve an Install Plan v2 into a byte-stable package Lockfile.
///
/// `package_sources` maps package IDs to `{kind, uri}` objects and
/// `artifact_hashes` maps package IDs to trusted HTTPS artifact identities.
///
/// # Errors
/// Returns a fail-closed error for malformed plans, source escapes, unsafe
/// schema inputs, invalid predecessor locks, or inconsistent output.
#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
pub fn resolve_package_lock(
    install_plan: &Value,
    schema_root: impl AsRef<Path>,
    package_sources: Option<&Map<String, Value>>,
    artifact_hashes: Option<&Map<String, Value>>,
    source_base: impl AsRef<Path>,
    previous_lock: Option<&Value>,
) -> Result<Value, EngineError> {
    validate_install_plan_projection(install_plan)?;
    let plan = object(install_plan, "Install Plan")?;
    if plan.get("lock_schema_version").and_then(Value::as_str) != Some("2.0") {
        return invalid("persistent package lock requires Install Plan/Lock v2 metadata");
    }
    if let Some(previous_lock) = previous_lock {
        validate_package_lock(previous_lock)?;
    }
    let sources = package_sources.cloned().unwrap_or_default();
    let artifacts = artifact_hashes.cloned().unwrap_or_default();
    let selected_packages = array_field(plan, "selected_packages", "Install Plan")?;
    if selected_packages.len() > MAX_LOCK_PACKAGES {
        return invalid(format!(
            "package lock exceeds maximum of {MAX_LOCK_PACKAGES} packages"
        ));
    }
    let package_ids = selected_packages
        .iter()
        .map(|item| required_string(object(item, "selected package")?, "id"))
        .collect::<Result<BTreeSet<_>, _>>()?;
    reject_unknown_override_keys(&sources, &package_ids, "source overrides")?;
    reject_unknown_override_keys(&artifacts, &package_ids, "artifact hashes")?;

    let package_records = array_field(plan, "packages", "Install Plan")?
        .iter()
        .map(|item| {
            let item = object(item, "Install Plan package")?;
            Ok((required_string(item, "id")?, item))
        })
        .collect::<Result<BTreeMap<_, _>, EngineError>>()?;
    let mut packages = Vec::with_capacity(selected_packages.len());
    for selected in selected_packages {
        let selected = object(selected, "selected package")?;
        let package_id = required_string(selected, "id")?;
        let record = package_records.get(&package_id).ok_or_else(|| {
            EngineError::Invalid(format!(
                "Install Plan selected package has no package record: {package_id}"
            ))
        })?;
        let mut source = sources.get(&package_id).map_or_else(
            || {
                Ok::<Value, EngineError>(json!({
                    "kind": "local-registry",
                    "uri": format!("registry://{package_id}"),
                }))
            },
            |value| {
                object(value, "package source override")?;
                Ok::<Value, EngineError>(value.clone())
            },
        )?;
        let source = source.as_object_mut().ok_or_else(|| {
            EngineError::Invalid(format!("package lock source is invalid: {package_id}"))
        })?;
        source.insert(
            "sha256".to_owned(),
            Value::String(required_string(selected, "source_sha256")?),
        );
        source.insert(
            "artifact_sha256".to_owned(),
            artifacts.get(&package_id).cloned().unwrap_or(Value::Null),
        );
        let source = Value::Object(source.clone());
        validate_source(&source, &package_id)?;
        if source.get("kind").and_then(Value::as_str) == Some("relative-path") {
            validate_relative_source_snapshot(
                source_base.as_ref(),
                source.get("uri").and_then(Value::as_str).ok_or_else(|| {
                    EngineError::Invalid("package source URI is invalid".to_owned())
                })?,
                record,
                &package_id,
            )?;
        }
        packages.push(json!({
            "core_compatibility": selected.get("core_compatibility").cloned().ok_or_else(
                || EngineError::Invalid("selected package core compatibility is missing".to_owned())
            )?,
            "id": package_id,
            "kind": selected.get("kind").cloned().ok_or_else(
                || EngineError::Invalid("selected package kind is missing".to_owned())
            )?,
            "manifest_sha256": record.get("manifest_sha256").cloned().ok_or_else(
                || EngineError::Invalid("Install Plan package manifest digest is missing".to_owned())
            )?,
            "provider_compatibility": selected.get("provider_compatibility").cloned().unwrap_or(Value::Null),
            "provider_manifest_sha256": record.get("provider_manifest_sha256").cloned().unwrap_or(Value::Null),
            "provider_version": selected.get("provider_version").cloned().unwrap_or(Value::Null),
            "source": source,
            "version": selected.get("version").cloned().ok_or_else(
                || EngineError::Invalid("selected package version is missing".to_owned())
            )?,
        }));
    }
    let core = packages
        .iter()
        .find(|package| package.get("id").and_then(Value::as_str) == Some("core"))
        .ok_or_else(|| EngineError::Invalid("package lock core package is missing".to_owned()))?;
    let mut body = json!({
        "assets_sha256": nested_field(plan, "asset_summary", "content_sha256")?,
        "bindings_sha256": canonical_sha256(plan.get("bindings").ok_or_else(
            || EngineError::Invalid("Install Plan bindings are missing".to_owned())
        )?)?,
        "capability_providers": plan.get("capability_providers").cloned().ok_or_else(
            || EngineError::Invalid("Install Plan capability providers are missing".to_owned())
        )?,
        "core": {
            "package_version": core.get("version").cloned().unwrap_or(Value::Null),
            "runtime_version": plan.get("core_version").cloned().ok_or_else(
                || EngineError::Invalid("Install Plan core version is missing".to_owned())
            )?,
            "source_sha256": core.pointer("/source/sha256").cloned().unwrap_or(Value::Null),
        },
        "dependencies": plan.get("resolved_dependencies").cloned().ok_or_else(
            || EngineError::Invalid("Install Plan dependencies are missing".to_owned())
        )?,
        "install_plan_identity_hash": install_plan_identity_hash(install_plan)?,
        "instructions": {
            "rule_trace_sha256": canonical_sha256(
                plan.get("instructions").and_then(|value| value.get("rule_trace")).ok_or_else(
                    || EngineError::Invalid("Install Plan rule trace is missing".to_owned())
                )?
            )?,
            "sha256": plan.get("instructions").and_then(|value| value.get("sha256")).cloned().ok_or_else(
                || EngineError::Invalid("Install Plan instructions digest is missing".to_owned())
            )?,
        },
        "lineage": {
            "previous_lock_hash": previous_lock
                .and_then(|value| value.get("fingerprint"))
                .cloned()
                .unwrap_or(Value::Null),
        },
        "manager": LOCK_MANAGER,
        "packages": packages,
        "permission_profiles": plan.get("permission_profiles").cloned().ok_or_else(
            || EngineError::Invalid("Install Plan permission profiles are missing".to_owned())
        )?,
        "schema_inventory": schema_inventory(schema_root)?,
        "schema_version": LOCK_SCHEMA_VERSION,
        "selection": {
            "disciplines": plan.get("selected_disciplines").cloned().ok_or_else(
                || EngineError::Invalid("Install Plan selected disciplines are missing".to_owned())
            )?,
            "platforms": plan.get("selected_platforms").cloned().ok_or_else(
                || EngineError::Invalid("Install Plan selected platforms are missing".to_owned())
            )?,
            "runtime_configs": plan.get("selected_runtime_configs").cloned().ok_or_else(
                || EngineError::Invalid("Install Plan selected runtime configs are missing".to_owned())
            )?,
        },
        "side_effects": plan.get("side_effects").cloned().ok_or_else(
            || EngineError::Invalid("Install Plan side effects are missing".to_owned())
        )?,
    });
    let fingerprint = canonical_sha256(&body)?;
    body.as_object_mut()
        .ok_or_else(|| EngineError::Invalid("package lock body is invalid".to_owned()))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    validate_package_lock(&body)?;
    if plan
        .get("package_lock_hash")
        .is_some_and(|value| !value.is_null())
        && sources.is_empty()
        && artifacts.is_empty()
        && previous_lock.is_none()
        && plan.get("package_lock_hash") != body.get("fingerprint")
    {
        return invalid("Install Plan package lock anchor differs from resolved Lockfile");
    }
    Ok(body)
}

/// Validate the complete package Lockfile integrity and dependency closure.
///
/// # Errors
/// Returns a fail-closed error for malformed, stale, incompatible, excessive,
/// or internally inconsistent Lockfiles.
#[allow(clippy::too_many_lines)]
pub fn validate_package_lock(value: &Value) -> Result<(), EngineError> {
    let lock = exact_object(
        value,
        &[
            "assets_sha256",
            "bindings_sha256",
            "capability_providers",
            "core",
            "dependencies",
            "fingerprint",
            "install_plan_identity_hash",
            "instructions",
            "lineage",
            "manager",
            "packages",
            "permission_profiles",
            "schema_inventory",
            "schema_version",
            "selection",
            "side_effects",
        ],
        "agent-skills-lock fields are invalid",
    )?;
    if lock.get("schema_version").and_then(Value::as_str) != Some(LOCK_SCHEMA_VERSION)
        || lock.get("manager").and_then(Value::as_str) != Some(LOCK_MANAGER)
    {
        return invalid("agent-skills-lock identity is invalid");
    }
    for field in [
        "assets_sha256",
        "bindings_sha256",
        "install_plan_identity_hash",
        "fingerprint",
    ] {
        if !is_sha256(lock.get(field).unwrap_or(&Value::Null)) {
            return invalid(format!("agent-skills-lock {field} is invalid"));
        }
    }
    let core = exact_object(
        lock.get("core").unwrap_or(&Value::Null),
        &["package_version", "runtime_version", "source_sha256"],
        "agent-skills-lock core identity is invalid",
    )?;
    for field in ["package_version", "runtime_version"] {
        if !core
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(is_strict_semver)
        {
            return invalid("agent-skills-lock core version is invalid");
        }
    }
    if !is_sha256(core.get("source_sha256").unwrap_or(&Value::Null)) {
        return invalid("agent-skills-lock core source digest is invalid");
    }
    let packages = lock
        .get("packages")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            EngineError::Invalid("agent-skills-lock packages must not be empty".to_owned())
        })?;
    if packages.is_empty() {
        return invalid("agent-skills-lock packages must not be empty");
    }
    if packages.len() > MAX_LOCK_PACKAGES {
        return invalid(format!(
            "agent-skills-lock packages exceed maximum of {MAX_LOCK_PACKAGES}"
        ));
    }
    let mut package_ids = Vec::with_capacity(packages.len());
    let mut package_by_id = BTreeMap::new();
    for package in packages {
        let package = exact_object(
            package,
            &[
                "core_compatibility",
                "id",
                "kind",
                "manifest_sha256",
                "provider_compatibility",
                "provider_manifest_sha256",
                "provider_version",
                "source",
                "version",
            ],
            "agent-skills-lock package fields are invalid",
        )?;
        let package_id = package
            .get("id")
            .and_then(Value::as_str)
            .filter(|value| is_safe_id(value))
            .ok_or_else(|| {
                EngineError::Invalid("agent-skills-lock package id is invalid".to_owned())
            })?
            .to_owned();
        if !package
            .get("kind")
            .and_then(Value::as_str)
            .is_some_and(|kind| {
                matches!(
                    kind,
                    "core" | "platform" | "stack" | "discipline" | "adapter" | "runtime-config"
                )
            })
        {
            return invalid(format!(
                "agent-skills-lock package kind is invalid: {package_id}"
            ));
        }
        if !package
            .get("version")
            .and_then(Value::as_str)
            .is_some_and(is_strict_semver)
        {
            return invalid(format!(
                "agent-skills-lock package version is invalid: {package_id}"
            ));
        }
        if !is_sha256(package.get("manifest_sha256").unwrap_or(&Value::Null)) {
            return invalid(format!(
                "agent-skills-lock manifest digest is invalid: {package_id}"
            ));
        }
        let provider_digest = package
            .get("provider_manifest_sha256")
            .unwrap_or(&Value::Null);
        if !provider_digest.is_null() && !is_sha256(provider_digest) {
            return invalid(format!(
                "agent-skills-lock provider digest is invalid: {package_id}"
            ));
        }
        validate_source(package.get("source").unwrap_or(&Value::Null), &package_id)?;
        let core_compatibility = package
            .get("core_compatibility")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                EngineError::Invalid(format!(
                    "agent-skills-lock core compatibility is not satisfied: {package_id}"
                ))
            })?;
        let runtime_version = required_string(core, "runtime_version")?;
        if !satisfies(&runtime_version, core_compatibility)? {
            return invalid(format!(
                "agent-skills-lock core compatibility is not satisfied: {package_id}"
            ));
        }
        let provider_version = package.get("provider_version").unwrap_or(&Value::Null);
        let provider_compatibility = package
            .get("provider_compatibility")
            .unwrap_or(&Value::Null);
        if provider_digest.is_null() {
            if !provider_version.is_null() || !provider_compatibility.is_null() {
                return invalid(format!(
                    "agent-skills-lock provider compatibility is unexpected: {package_id}"
                ));
            }
        } else {
            let version = provider_version.as_str().ok_or_else(|| {
                EngineError::Invalid(format!(
                    "agent-skills-lock provider compatibility is not satisfied: {package_id}"
                ))
            })?;
            let compatibility = provider_compatibility.as_str().ok_or_else(|| {
                EngineError::Invalid(format!(
                    "agent-skills-lock provider compatibility is not satisfied: {package_id}"
                ))
            })?;
            if !satisfies(version, compatibility)? {
                return invalid(format!(
                    "agent-skills-lock provider compatibility is not satisfied: {package_id}"
                ));
            }
        }
        package_ids.push(package_id.clone());
        package_by_id.insert(package_id, package);
    }
    if package_ids.first().map(String::as_str) != Some("core")
        || package_ids.iter().collect::<BTreeSet<_>>().len() != package_ids.len()
    {
        return invalid("agent-skills-lock package order or identity is invalid");
    }
    let core_package = package_by_id.get("core").ok_or_else(|| {
        EngineError::Invalid("agent-skills-lock core package is missing".to_owned())
    })?;
    if core.get("package_version") != core_package.get("version")
        || core.get("source_sha256") != core_package.get("source").and_then(|v| v.get("sha256"))
        || core_package.get("kind").and_then(Value::as_str) != Some("core")
        || package_by_id.iter().any(|(package_id, package)| {
            package_id != "core" && package.get("kind").and_then(Value::as_str) == Some("core")
        })
    {
        return invalid("agent-skills-lock core identity differs from package closure");
    }
    validate_schema_inventory(lock.get("schema_inventory").unwrap_or(&Value::Null))?;

    let selection = exact_object(
        lock.get("selection").unwrap_or(&Value::Null),
        &["disciplines", "platforms", "runtime_configs"],
        "agent-skills-lock selection is invalid",
    )?;
    let mut selected = BTreeSet::new();
    for key in ["disciplines", "platforms", "runtime_configs"] {
        let values = sorted_unique_string_array(
            selection.get(key).unwrap_or(&Value::Null),
            &format!("agent-skills-lock selection {key} is invalid"),
            MAX_LOCK_PACKAGES,
        )?;
        selected.extend(values);
    }
    let package_id_set = package_ids.iter().cloned().collect::<BTreeSet<_>>();
    if !selected.is_subset(&package_id_set) {
        return invalid("agent-skills-lock selection references unknown packages");
    }
    for field in ["permission_profiles", "side_effects"] {
        sorted_unique_string_array(
            lock.get(field).unwrap_or(&Value::Null),
            &format!("agent-skills-lock {field} is invalid"),
            MAX_LOCK_PROVIDERS,
        )?;
    }
    let permission_profiles = lock
        .get("permission_profiles")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            EngineError::Invalid("agent-skills-lock permission_profiles is invalid".to_owned())
        })?;
    let providers = lock
        .get("capability_providers")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            EngineError::Invalid("agent-skills-lock capability providers are invalid".to_owned())
        })?;
    if providers.len() > MAX_LOCK_PROVIDERS {
        return invalid(format!(
            "agent-skills-lock capability providers exceed maximum of {MAX_LOCK_PROVIDERS}"
        ));
    }
    for (capability, provider) in providers {
        if capability.is_empty() {
            return invalid("agent-skills-lock capability provider is invalid");
        }
        let provider = exact_object(
            provider,
            &[
                "binding",
                "package",
                "package_version",
                "permission_profile",
                "source_sha256",
            ],
            "agent-skills-lock capability provider fields are invalid",
        )?;
        let package_id = provider
            .get("package")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                EngineError::Invalid(
                    "agent-skills-lock capability provider references unknown package".to_owned(),
                )
            })?;
        let package = package_by_id.get(package_id).ok_or_else(|| {
            EngineError::Invalid(
                "agent-skills-lock capability provider references unknown package".to_owned(),
            )
        })?;
        let permission = provider
            .get("permission_profile")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty());
        if provider.get("package_version") != package.get("version")
            || provider.get("source_sha256")
                != package.get("source").and_then(|value| value.get("sha256"))
            || permission.is_none()
            || !permission_profiles
                .iter()
                .any(|value| value.as_str() == permission)
        {
            return invalid("agent-skills-lock capability provider identity is stale");
        }
    }
    let expected_bindings = providers
        .iter()
        .map(|(capability, provider)| {
            Ok((
                capability.clone(),
                json!({
                    "binding": provider.get("binding").cloned().unwrap_or(Value::Null),
                    "package": provider.get("package").cloned().unwrap_or(Value::Null),
                }),
            ))
        })
        .collect::<Result<Map<_, _>, EngineError>>()?;
    if lock.get("bindings_sha256").and_then(Value::as_str)
        != Some(canonical_sha256(&Value::Object(expected_bindings))?.as_str())
    {
        return invalid("agent-skills-lock bindings digest is inconsistent");
    }

    validate_dependencies(lock, &package_by_id, &selected, &package_id_set)?;
    for (selection_key, expected_kind) in [
        ("disciplines", "discipline"),
        ("platforms", "platform"),
        ("runtime_configs", "runtime-config"),
    ] {
        let values = selection
            .get(selection_key)
            .and_then(Value::as_array)
            .ok_or_else(|| EngineError::Invalid("selection is invalid".to_owned()))?;
        for package_id in values.iter().filter_map(Value::as_str) {
            if package_by_id
                .get(package_id)
                .and_then(|package| package.get("kind"))
                .and_then(Value::as_str)
                != Some(expected_kind)
            {
                return invalid(format!(
                    "agent-skills-lock selection {selection_key} package kind is invalid"
                ));
            }
        }
    }
    let instructions = exact_object(
        lock.get("instructions").unwrap_or(&Value::Null),
        &["rule_trace_sha256", "sha256"],
        "agent-skills-lock instructions identity is invalid",
    )?;
    if instructions.values().any(|value| !is_sha256(value)) {
        return invalid("agent-skills-lock instructions identity is invalid");
    }
    let lineage = exact_object(
        lock.get("lineage").unwrap_or(&Value::Null),
        &["previous_lock_hash"],
        "agent-skills-lock lineage is invalid",
    )?;
    let previous = lineage.get("previous_lock_hash").unwrap_or(&Value::Null);
    if !previous.is_null() && !is_sha256(previous) {
        return invalid("agent-skills-lock previous lock hash is invalid");
    }
    let mut content = lock.clone();
    content.remove("fingerprint");
    if lock.get("fingerprint").and_then(Value::as_str)
        != Some(canonical_sha256(&Value::Object(content))?.as_str())
    {
        return invalid("agent-skills-lock fingerprint mismatch");
    }
    Ok(())
}

/// Return a deterministic field-oriented Lockfile diff.
///
/// # Errors
/// Returns an error when either Lockfile is invalid.
pub fn diff_package_locks(before: &Value, after: &Value) -> Result<Value, EngineError> {
    validate_package_lock(before)?;
    validate_package_lock(after)?;
    let before_packages = indexed_values(before, "/packages", "id")?;
    let after_packages = indexed_values(after, "/packages", "id")?;
    let before_caps = object(
        before.get("capability_providers").unwrap_or(&Value::Null),
        "capability providers",
    )?;
    let after_caps = object(
        after.get("capability_providers").unwrap_or(&Value::Null),
        "capability providers",
    )?;
    let before_caps_index = before_caps
        .iter()
        .map(|(key, value)| (key.clone(), value))
        .collect::<BTreeMap<_, _>>();
    let after_caps_index = after_caps
        .iter()
        .map(|(key, value)| (key.clone(), value))
        .collect::<BTreeMap<_, _>>();
    let before_schemas =
        indexed_scalar_values(before, "/schema_inventory/files", "path", "sha256")?;
    let after_schemas = indexed_scalar_values(after, "/schema_inventory/files", "path", "sha256")?;
    let before_permissions = value_string_set(
        before.get("permission_profiles").unwrap_or(&Value::Null),
        "permission profiles",
    )?;
    let after_permissions = value_string_set(
        after.get("permission_profiles").unwrap_or(&Value::Null),
        "permission profiles",
    )?;
    let changed_capabilities = before_caps
        .keys()
        .filter(|key| after_caps.contains_key(*key))
        .filter(|key| {
            before_caps[*key].get("permission_profile")
                != after_caps[*key].get("permission_profile")
        })
        .cloned()
        .collect::<Vec<_>>();
    let mut body = json!({
        "bindings": map_changes(&before_caps_index, &after_caps_index),
        "from_lock_hash": before.get("fingerprint").cloned().unwrap_or(Value::Null),
        "packages": map_changes(&before_packages, &after_packages),
        "permissions": {
            "added": after_permissions.difference(&before_permissions).cloned().collect::<Vec<_>>(),
            "changed_capabilities": changed_capabilities,
            "removed": before_permissions.difference(&after_permissions).cloned().collect::<Vec<_>>(),
        },
        "schema_version": LOCK_SCHEMA_VERSION,
        "schemas": map_changes(&before_schemas, &after_schemas),
        "selection_changed": before.get("selection") != after.get("selection"),
        "to_lock_hash": after.get("fingerprint").cloned().unwrap_or(Value::Null),
    });
    let unchanged = before.get("fingerprint") == after.get("fingerprint");
    body.as_object_mut()
        .ok_or_else(|| EngineError::Invalid("Lockfile diff body is invalid".to_owned()))?
        .insert(
            "status".to_owned(),
            Value::String(if unchanged { "unchanged" } else { "changed" }.to_owned()),
        );
    let fingerprint = canonical_sha256(&body)?;
    body.as_object_mut()
        .ok_or_else(|| EngineError::Invalid("Lockfile diff body is invalid".to_owned()))?
        .insert("fingerprint".to_owned(), Value::String(fingerprint));
    Ok(body)
}

/// Return a compact deterministic explanation of a valid package Lockfile.
///
/// # Errors
/// Returns an error when the Lockfile is invalid.
pub fn explain_package_lock(value: &Value) -> Result<Value, EngineError> {
    validate_package_lock(value)?;
    let packages = value
        .get("packages")
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid("package lock packages are invalid".to_owned()))?;
    Ok(json!({
        "binding_count": value.get("capability_providers").and_then(Value::as_object).map_or(0, Map::len),
        "core_version": value.pointer("/core/runtime_version").cloned().unwrap_or(Value::Null),
        "lock_hash": value.get("fingerprint").cloned().unwrap_or(Value::Null),
        "package_count": packages.len(),
        "packages": packages.iter().map(|item| json!({
            "id": item.get("id").cloned().unwrap_or(Value::Null),
            "kind": item.get("kind").cloned().unwrap_or(Value::Null),
            "source": item.pointer("/source/uri").cloned().unwrap_or(Value::Null),
            "version": item.get("version").cloned().unwrap_or(Value::Null),
        })).collect::<Vec<_>>(),
        "permission_profiles": value.get("permission_profiles").cloned().unwrap_or(Value::Null),
        "schema_count": value.pointer("/schema_inventory/files").and_then(Value::as_array).map_or(0, Vec::len),
        "schema_version": LOCK_SCHEMA_VERSION,
        "selection": value.get("selection").cloned().unwrap_or(Value::Null),
        "status": "locked",
    }))
}

/// Reject a valid but unrelated Lockfile before a Workflow Plan is used.
///
/// # Errors
/// Returns an error for a malformed plan, unrelated Lockfile, binding drift,
/// permission drift, or provider-manifest drift.
pub fn validate_plan_package_lock(plan: &Value, package_lock: &Value) -> Result<(), EngineError> {
    super::validate_compiled_plan(plan)?;
    validate_package_lock(package_lock)?;
    if plan.get("package_lock_hash") != package_lock.get("fingerprint") {
        return invalid("workflow plan package lock hash does not match Lockfile");
    }
    let providers = object(
        package_lock
            .get("capability_providers")
            .unwrap_or(&Value::Null),
        "package lock providers",
    )?;
    let packages = indexed_values(package_lock, "/packages", "id")?;
    let nodes = plan
        .get("nodes")
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid("workflow-plan nodes must be an array".to_owned()))?;
    for node in nodes {
        if node.get("provider").is_none_or(Value::is_null) {
            continue;
        }
        let capability = node
            .get("capability")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("workflow capability is invalid".to_owned()))?;
        let locked = providers.get(capability).ok_or_else(|| {
            EngineError::Invalid(format!(
                "workflow capability is not frozen by package lock: {capability}"
            ))
        })?;
        if node.get("binding") != locked.get("binding") {
            return invalid(format!(
                "workflow binding differs from package lock: {capability}"
            ));
        }
        if node.get("permission_profile") != locked.get("permission_profile") {
            return invalid(format!(
                "workflow permission differs from package lock: {capability}"
            ));
        }
        let package_id = locked
            .get("package")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("locked provider package is invalid".to_owned()))?;
        let package = packages
            .get(package_id)
            .ok_or_else(|| EngineError::Invalid("locked provider package is missing".to_owned()))?;
        let expected_manifest = package
            .get("provider_manifest_sha256")
            .filter(|value| !value.is_null())
            .or_else(|| package.get("manifest_sha256"));
        if node
            .get("provider_manifest_digest")
            .is_some_and(|value| !value.is_null())
            && node.get("provider_manifest_digest") != expected_manifest
        {
            return invalid(format!(
                "workflow provider manifest differs from package lock: {capability}"
            ));
        }
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn validate_install_plan_projection(value: &Value) -> Result<(), EngineError> {
    let plan = object(value, "Install Plan")?;
    for field in [
        "asset_summary",
        "assets",
        "bindings",
        "capability_providers",
        "core_version",
        "fingerprint",
        "instructions",
        "lock_schema_version",
        "managed_roots",
        "manager",
        "packages",
        "permission_profiles",
        "resolved_dependencies",
        "schema_version",
        "selected_disciplines",
        "selected_packages",
        "selected_platforms",
        "selected_runtime_configs",
        "side_effects",
        "skills",
        "status",
    ] {
        if !plan.contains_key(field) {
            return invalid(format!("Install Plan missing required field: {field}"));
        }
    }
    if plan.get("schema_version").and_then(Value::as_str) != Some("1.0")
        || plan.get("manager").and_then(Value::as_str) != Some(LOCK_MANAGER)
    {
        return invalid("Install Plan identity is invalid");
    }
    if plan.get("lock_schema_version").and_then(Value::as_str) != Some("2.0") {
        return invalid("Install Plan lock schema version is unsupported");
    }
    if plan.get("managed_roots") != Some(&json!(["AGENTS.md", "skills", ".agent-skills"]))
        || !plan
            .get("status")
            .and_then(Value::as_str)
            .is_some_and(|status| matches!(status, "planned" | "installed"))
    {
        return invalid("Install Plan lifecycle identity is invalid");
    }
    if plan
        .get("package_lock_hash")
        .is_some_and(|value| !value.is_null() && !is_sha256(value))
    {
        return invalid("Install Plan package lock hash is invalid");
    }
    if !plan
        .get("core_version")
        .and_then(Value::as_str)
        .is_some_and(is_strict_semver)
    {
        return invalid("Install Plan core version is invalid");
    }
    let mut identity = plan.clone();
    identity.remove("fingerprint");
    identity.remove("status");
    if plan.get("fingerprint").and_then(Value::as_str)
        != Some(canonical_sha256(&Value::Object(identity))?.as_str())
    {
        return invalid("install-plan fingerprint mismatch");
    }
    for field in [
        "selected_disciplines",
        "selected_platforms",
        "selected_runtime_configs",
        "permission_profiles",
        "side_effects",
    ] {
        unique_string_array(
            plan.get(field).unwrap_or(&Value::Null),
            &format!("Install Plan {field} is invalid"),
            MAX_LOCK_PROVIDERS,
            true,
        )?;
    }
    let selected = array_field(plan, "selected_packages", "Install Plan")?;
    let packages = array_field(plan, "packages", "Install Plan")?;
    if selected.is_empty()
        || selected.len() > MAX_LOCK_PACKAGES
        || packages.len() > MAX_LOCK_PACKAGES
    {
        return invalid("Install Plan package closure is invalid");
    }
    let package_ids = packages
        .iter()
        .map(|item| {
            let package = object(item, "Install Plan package")?;
            let package_id = required_string(package, "id")?;
            if !is_safe_id(&package_id) {
                return invalid("Install Plan package id is invalid");
            }
            Ok(package_id)
        })
        .collect::<Result<Vec<_>, _>>()?;
    if package_ids.iter().collect::<BTreeSet<_>>().len() != package_ids.len() {
        return invalid("Install Plan package records are invalid");
    }
    let package_set = package_ids.iter().cloned().collect::<BTreeSet<_>>();
    let package_positions = package_ids
        .iter()
        .enumerate()
        .map(|(index, package_id)| (package_id.clone(), index))
        .collect::<BTreeMap<_, _>>();
    let mut package_records = BTreeMap::new();
    let mut total_package_files = 0_usize;
    for package in packages {
        let package = object(package, "Install Plan package")?;
        for field in [
            "directories",
            "file_count",
            "files",
            "files_sha256",
            "id",
            "manifest_sha256",
            "provider_manifest_sha256",
            "root_mode",
        ] {
            require_field(package, field, "Install Plan package")?;
        }
        let file_count = validate_install_tree(package, "files_sha256", true)?;
        total_package_files = total_package_files
            .checked_add(file_count)
            .ok_or_else(|| EngineError::Invalid("Install Plan file counter overflow".to_owned()))?;
        if total_package_files > MAX_PACKAGE_FILES {
            return invalid(format!(
                "Install Plan assets exceed maximum of {MAX_PACKAGE_FILES} files"
            ));
        }
        if !is_sha256(package.get("manifest_sha256").unwrap_or(&Value::Null)) {
            return invalid("Install Plan package manifest digest is invalid");
        }
        let provider_digest = package
            .get("provider_manifest_sha256")
            .unwrap_or(&Value::Null);
        if !provider_digest.is_null() && !is_sha256(provider_digest) {
            return invalid("Install Plan package provider digest is invalid");
        }
        package_records.insert(required_string(package, "id")?, package);
    }

    let selected_ids = selected
        .iter()
        .map(|item| required_string(object(item, "selected package")?, "id"))
        .collect::<Result<Vec<_>, _>>()?;
    if selected_ids != package_ids || selected_ids.first().map(String::as_str) != Some("core") {
        return invalid("Install Plan selected packages must match package order");
    }
    let core_version = required_string(plan, "core_version")?;
    let mut selected_records = BTreeMap::new();
    for selected_package in selected {
        let selected_package = object(selected_package, "selected package")?;
        for field in [
            "core_compatibility",
            "id",
            "kind",
            "provider_compatibility",
            "provider_version",
            "selection_reasons",
            "source_sha256",
            "version",
        ] {
            require_field(selected_package, field, "selected package")?;
        }
        let package_id = required_string(selected_package, "id")?;
        let kind = selected_package
            .get("kind")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("selected package kind is invalid".to_owned()))?;
        let version = selected_package
            .get("version")
            .and_then(Value::as_str)
            .filter(|version| is_strict_semver(version))
            .ok_or_else(|| {
                EngineError::Invalid("Install Plan selected package version is invalid".to_owned())
            })?;
        if !matches!(
            kind,
            "core" | "platform" | "stack" | "discipline" | "adapter" | "runtime-config"
        ) {
            return invalid("Install Plan selected package kind is invalid");
        }
        let reasons = unique_string_array(
            selected_package
                .get("selection_reasons")
                .unwrap_or(&Value::Null),
            "Install Plan selected package reasons are invalid",
            MAX_LOCK_PACKAGES,
            false,
        )?;
        if reasons.is_empty() {
            return invalid("Install Plan selected package reasons are invalid");
        }
        let core_compatibility = selected_package
            .get("core_compatibility")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                EngineError::Invalid(
                    "Install Plan selected package core compatibility is invalid".to_owned(),
                )
            })?;
        if !satisfies(&core_version, core_compatibility)? {
            return invalid("Install Plan selected package core compatibility is invalid");
        }
        let provider_version = selected_package
            .get("provider_version")
            .unwrap_or(&Value::Null);
        let provider_compatibility = selected_package
            .get("provider_compatibility")
            .unwrap_or(&Value::Null);
        if provider_version.is_null() != provider_compatibility.is_null() {
            return invalid("Install Plan provider compatibility metadata is incomplete");
        }
        if let Some(provider_version) = provider_version.as_str() {
            let provider_compatibility = provider_compatibility.as_str().ok_or_else(|| {
                EngineError::Invalid("Install Plan provider compatibility is invalid".to_owned())
            })?;
            if !satisfies(provider_version, provider_compatibility)? {
                return invalid("Install Plan provider compatibility is not satisfied");
            }
        } else if !provider_version.is_null() {
            return invalid("Install Plan provider compatibility is invalid");
        }
        if !is_sha256(
            selected_package
                .get("source_sha256")
                .unwrap_or(&Value::Null),
        ) {
            return invalid("Install Plan selected package source digest is invalid");
        }
        let package = package_records.get(&package_id).ok_or_else(|| {
            EngineError::Invalid("Install Plan selected package record is missing".to_owned())
        })?;
        if selected_package.get("source_sha256") != package.get("files_sha256") {
            return invalid(
                "Install Plan selected package source digest differs from package files",
            );
        }
        if package_id == "core" && (kind != "core" || reasons != ["core"]) {
            return invalid("Install Plan core package metadata is invalid");
        }
        selected_records.insert(package_id, (selected_package, version.to_owned(), reasons));
    }
    validate_install_selections(plan, &selected_records)?;
    let required_edges =
        validate_install_dependencies(plan, &selected_records, &package_set, &package_positions)?;
    validate_selection_reasons(&selected_records, &required_edges)?;
    validate_install_skills(plan, &package_set)?;
    validate_install_instructions(plan, &package_set, &package_positions)?;
    validate_install_assets(plan, packages, total_package_files)?;
    validate_install_providers(plan, &selected_records, &package_records)?;
    Ok(())
}

fn validate_install_selections(
    plan: &Map<String, Value>,
    selected: &SelectedPlanRecords<'_>,
) -> Result<(), EngineError> {
    for (field, prefix, kind) in [
        ("selected_platforms", "platform", "platform"),
        ("selected_disciplines", "discipline", "discipline"),
        (
            "selected_runtime_configs",
            "runtime-config",
            "runtime-config",
        ),
    ] {
        let declared = value_string_set(plan.get(field).unwrap_or(&Value::Null), field)?;
        let explicit = selected
            .iter()
            .filter_map(|(package_id, (_, _, reasons))| {
                reasons
                    .contains(&format!("{prefix}:{package_id}"))
                    .then_some(package_id.clone())
            })
            .collect::<BTreeSet<_>>();
        if declared != explicit {
            return invalid(format!(
                "Install Plan {field} differ from package selection reasons"
            ));
        }
        for package_id in explicit {
            if selected[&package_id].0.get("kind").and_then(Value::as_str) != Some(kind) {
                return invalid(format!("Install Plan {field} package kind is invalid"));
            }
        }
    }
    Ok(())
}

fn validate_install_dependencies(
    plan: &Map<String, Value>,
    selected: &SelectedPlanRecords<'_>,
    package_set: &BTreeSet<String>,
    package_positions: &BTreeMap<String, usize>,
) -> Result<BTreeSet<(String, String)>, EngineError> {
    let dependencies = array_field(plan, "resolved_dependencies", "Install Plan")?;
    if dependencies.len() > MAX_LOCK_DEPENDENCIES {
        return invalid(format!(
            "Install Plan dependencies exceed maximum of {MAX_LOCK_DEPENDENCIES}"
        ));
    }
    let mut edges = BTreeSet::new();
    let mut required_edges = BTreeSet::new();
    for dependency in dependencies {
        let dependency = object(dependency, "Install Plan dependency")?;
        for field in [
            "from",
            "to",
            "requirement",
            "version",
            "required_capabilities",
        ] {
            require_field(dependency, field, "Install Plan dependency")?;
        }
        let from = required_string(dependency, "from")?;
        let to = required_string(dependency, "to")?;
        let requirement = dependency
            .get("requirement")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("dependency requirement is invalid".to_owned()))?;
        let version = dependency
            .get("version")
            .and_then(Value::as_str)
            .filter(|version| is_version_requirement(version))
            .ok_or_else(|| EngineError::Invalid("dependency version is invalid".to_owned()))?;
        let required_capabilities = unique_string_array(
            dependency
                .get("required_capabilities")
                .unwrap_or(&Value::Null),
            "Install Plan dependency capabilities are invalid",
            MAX_LOCK_PROVIDERS,
            false,
        )?;
        if !package_set.contains(&from)
            || !package_set.contains(&to)
            || from == to
            || !matches!(requirement, "required" | "optional")
            || required_capabilities.is_empty()
        {
            return invalid("Install Plan resolved dependency is invalid");
        }
        let edge = (from.clone(), to.clone());
        if !edges.insert(edge.clone()) {
            return invalid("Install Plan resolved dependency edges must be unique");
        }
        if package_positions[&to] >= package_positions[&from] {
            return invalid("Install Plan package order violates dependency topology");
        }
        if !satisfies(&selected[&to].1, version)? {
            return invalid("Install Plan dependency version is not satisfied");
        }
        if requirement == "required" {
            required_edges.insert(edge);
        }
    }
    Ok(required_edges)
}

fn validate_selection_reasons(
    selected: &SelectedPlanRecords<'_>,
    required_edges: &BTreeSet<(String, String)>,
) -> Result<(), EngineError> {
    for (package_id, (_, _, reasons)) in selected {
        if package_id == "core" {
            continue;
        }
        let mut allowed = BTreeSet::from([
            format!("platform:{package_id}"),
            format!("discipline:{package_id}"),
            format!("runtime-config:{package_id}"),
        ]);
        for (consumer, provider) in required_edges {
            if provider == package_id {
                allowed.insert(format!("dependency:{consumer}"));
            }
        }
        if reasons.iter().any(|reason| !allowed.contains(reason)) {
            return invalid("Install Plan package selection reason is invalid");
        }
    }
    for (consumer, provider) in required_edges {
        if !selected[provider]
            .2
            .contains(&format!("dependency:{consumer}"))
        {
            return invalid("Install Plan required dependency selection reason is missing");
        }
    }
    Ok(())
}

fn validate_install_skills(
    plan: &Map<String, Value>,
    package_set: &BTreeSet<String>,
) -> Result<(), EngineError> {
    let skills = array_field(plan, "skills", "Install Plan")?;
    if skills.len() > MAX_LOCK_PROVIDERS {
        return invalid("Install Plan skills exceed maximum");
    }
    let mut names = BTreeSet::new();
    for skill in skills {
        let skill = object(skill, "Install Plan skill")?;
        for field in [
            "directories",
            "file_count",
            "files",
            "name",
            "package",
            "root_mode",
            "sha256",
        ] {
            require_field(skill, field, "Install Plan skill")?;
        }
        let name = required_string(skill, "name")?;
        if !is_safe_id(&name) || !names.insert(name) {
            return invalid("Install Plan skill names are invalid");
        }
        validate_install_tree(skill, "sha256", false)?;
        if !package_set.contains(&required_string(skill, "package")?) {
            return invalid("Install Plan skill references an unknown package");
        }
    }
    Ok(())
}

fn validate_install_instructions(
    plan: &Map<String, Value>,
    package_set: &BTreeSet<String>,
    package_positions: &BTreeMap<String, usize>,
) -> Result<(), EngineError> {
    let instructions = object(
        plan.get("instructions").unwrap_or(&Value::Null),
        "Install Plan instructions",
    )?;
    for field in ["fragments", "path", "rule_trace", "sha256"] {
        require_field(instructions, field, "Install Plan instructions")?;
    }
    if instructions.get("path").and_then(Value::as_str) != Some("AGENTS.md")
        || !is_sha256(instructions.get("sha256").unwrap_or(&Value::Null))
    {
        return invalid("Install Plan instructions identity is invalid");
    }
    let fragments = array_field(instructions, "fragments", "Install Plan instructions")?;
    if fragments.len() > MAX_LOCK_PROVIDERS {
        return invalid("Install Plan instruction fragments exceed maximum");
    }
    let mut fragment_ids = BTreeSet::new();
    let mut last_key = None;
    for fragment in fragments {
        let fragment = object(fragment, "Install Plan instruction fragment")?;
        for field in [
            "id",
            "merge_strategy",
            "order",
            "package",
            "path",
            "scope",
            "sha256",
        ] {
            require_field(fragment, field, "Install Plan instruction fragment")?;
        }
        let id = required_string(fragment, "id")?;
        let package = required_string(fragment, "package")?;
        let scope = required_string(fragment, "scope")?;
        let path = required_string(fragment, "path")?;
        let order = fragment
            .get("order")
            .and_then(Value::as_i64)
            .ok_or_else(|| EngineError::Invalid("instruction order is invalid".to_owned()))?;
        if id.is_empty()
            || !fragment_ids.insert(id.clone())
            || !package_set.contains(&package)
            || scope.is_empty()
            || !is_safe_contract_path(&path)
            || !fragment
                .get("merge_strategy")
                .and_then(Value::as_str)
                .is_some_and(|value| matches!(value, "append" | "locked"))
            || !is_sha256(fragment.get("sha256").unwrap_or(&Value::Null))
        {
            return invalid("Install Plan instruction fragment identity is invalid");
        }
        let key = (package_positions[&package], order, id);
        if last_key.as_ref().is_some_and(|last| last > &key) {
            return invalid("Install Plan instruction fragment order is not canonical");
        }
        last_key = Some(key);
    }
    let rules = array_field(instructions, "rule_trace", "Install Plan instructions")?;
    if rules.len() > MAX_LOCK_PROVIDERS {
        return invalid("Install Plan instruction rules exceed maximum");
    }
    for rule in rules {
        let rule = object(rule, "Install Plan instruction rule")?;
        for field in [
            "id",
            "effect",
            "locked",
            "package",
            "scope",
            "content_sha256",
            "decision",
        ] {
            require_field(rule, field, "Install Plan instruction rule")?;
        }
        if !rule
            .get("effect")
            .and_then(Value::as_str)
            .is_some_and(|value| matches!(value, "allow" | "deny"))
            || rule.get("locked").and_then(Value::as_bool).is_none()
            || !rule
                .get("decision")
                .and_then(Value::as_str)
                .is_some_and(|value| matches!(value, "accepted" | "replaced" | "deny-wins"))
            || !is_sha256(rule.get("content_sha256").unwrap_or(&Value::Null))
        {
            return invalid("Install Plan instruction rule trace is invalid");
        }
    }
    Ok(())
}

fn validate_install_assets(
    plan: &Map<String, Value>,
    packages: &[Value],
    total_files: usize,
) -> Result<(), EngineError> {
    let assets = array_field(plan, "assets", "Install Plan")?;
    if assets.len() != total_files || assets.len() > MAX_PACKAGE_FILES {
        return invalid("Install Plan asset allowlist size is invalid");
    }
    let expected = packages
        .iter()
        .flat_map(|package| {
            let package_id = package.get("id").cloned().unwrap_or(Value::Null);
            package
                .get("files")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(move |entry| {
                    json!({
                        "mode": entry.get("mode").cloned().unwrap_or(Value::Null),
                        "package": package_id,
                        "path": entry.get("path").cloned().unwrap_or(Value::Null),
                        "sha256": entry.get("sha256").cloned().unwrap_or(Value::Null),
                    })
                })
        })
        .collect::<Vec<_>>();
    if assets != &expected {
        return invalid("Install Plan asset allowlist differs from selected package files");
    }
    let summary = object(
        plan.get("asset_summary").unwrap_or(&Value::Null),
        "Install Plan asset summary",
    )?;
    for field in [
        "content_sha256",
        "file_count",
        "package_count",
        "skill_count",
    ] {
        require_field(summary, field, "Install Plan asset summary")?;
    }
    if summary.get("content_sha256").and_then(Value::as_str)
        != Some(canonical_sha256(&Value::Array(assets.clone()))?.as_str())
        || summary.get("file_count").and_then(Value::as_u64) != Some(assets.len() as u64)
        || summary.get("package_count").and_then(Value::as_u64) != Some(packages.len() as u64)
        || summary.get("skill_count").and_then(Value::as_u64)
            != Some(
                plan.get("skills")
                    .and_then(Value::as_array)
                    .map_or(0, Vec::len) as u64,
            )
    {
        return invalid("Install Plan asset allowlist digest is invalid");
    }
    Ok(())
}

fn validate_install_providers(
    plan: &Map<String, Value>,
    selected: &SelectedPlanRecords<'_>,
    packages: &BTreeMap<String, &Map<String, Value>>,
) -> Result<(), EngineError> {
    let bindings = object(
        plan.get("bindings").unwrap_or(&Value::Null),
        "Install Plan bindings",
    )?;
    let providers = object(
        plan.get("capability_providers").unwrap_or(&Value::Null),
        "Install Plan capability providers",
    )?;
    if providers.len() > MAX_LOCK_PROVIDERS
        || providers.keys().collect::<BTreeSet<_>>() != bindings.keys().collect::<BTreeSet<_>>()
    {
        return invalid("Install Plan capability provider mapping differs from bindings");
    }
    let mut expected = Map::new();
    for (capability, binding) in bindings {
        let binding = object(binding, "Install Plan binding")?;
        require_field(binding, "binding", "Install Plan binding")?;
        let package_id = required_string(binding, "package")?;
        let selected_package = selected.get(&package_id).ok_or_else(|| {
            EngineError::Invalid("Install Plan binding references an unknown package".to_owned())
        })?;
        let package = packages.get(&package_id).ok_or_else(|| {
            EngineError::Invalid("Install Plan binding package record is missing".to_owned())
        })?;
        let provider = object(
            providers.get(capability).unwrap_or(&Value::Null),
            "Install Plan capability provider",
        )?;
        let permission = provider
            .get("permission_profile")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                EngineError::Invalid(
                    "Install Plan capability provider permission is required".to_owned(),
                )
            })?;
        expected.insert(
            capability.clone(),
            json!({
                "binding": binding.get("binding").cloned().unwrap_or(Value::Null),
                "package": package_id,
                "package_version": selected_package.1,
                "permission_profile": permission,
                "source_sha256": package.get("files_sha256").cloned().unwrap_or(Value::Null),
            }),
        );
    }
    if providers != &expected {
        return invalid("Install Plan capability provider mapping is inconsistent");
    }
    Ok(())
}

fn validate_install_tree(
    record: &Map<String, Value>,
    digest_field: &str,
    require_manifest: bool,
) -> Result<usize, EngineError> {
    let files = array_field(record, "files", "Install Plan tree")?;
    let directories = array_field(record, "directories", "Install Plan tree")?;
    if files.len() > MAX_PACKAGE_FILES || directories.len() > MAX_PACKAGE_FILES {
        return invalid("Install Plan tree exceeds maximum entries");
    }
    let mut file_paths = Vec::with_capacity(files.len());
    for file in files {
        let file = object(file, "Install Plan file")?;
        for field in ["mode", "path", "sha256"] {
            require_field(file, field, "Install Plan file")?;
        }
        validate_install_entry(file)?;
        if !is_sha256(file.get("sha256").unwrap_or(&Value::Null)) {
            return invalid("Install Plan file digest is invalid");
        }
        file_paths.push(required_string(file, "path")?);
    }
    let mut directory_paths = Vec::with_capacity(directories.len());
    for directory in directories {
        let directory = object(directory, "Install Plan directory")?;
        for field in ["mode", "path"] {
            require_field(directory, field, "Install Plan directory")?;
        }
        validate_install_entry(directory)?;
        directory_paths.push(required_string(directory, "path")?);
    }
    if !is_sorted_unique(&file_paths)
        || !is_sorted_unique(&directory_paths)
        || file_paths.iter().any(|path| directory_paths.contains(path))
    {
        return invalid("Install Plan tree paths are invalid");
    }
    if record.get("file_count").and_then(Value::as_u64) != Some(files.len() as u64)
        || record.get(digest_field).and_then(Value::as_str)
            != Some(canonical_sha256(&Value::Array(files.clone()))?.as_str())
        || !valid_mode(record.get("root_mode").unwrap_or(&Value::Null))
    {
        return invalid("Install Plan tree identity is invalid");
    }
    if require_manifest && !file_paths.iter().any(|path| path == "manifest.json") {
        return invalid("Install Plan package tree must contain manifest.json");
    }
    Ok(files.len())
}

fn validate_install_entry(entry: &Map<String, Value>) -> Result<(), EngineError> {
    let path = entry
        .get("path")
        .and_then(Value::as_str)
        .filter(|path| is_safe_contract_path(path))
        .ok_or_else(|| EngineError::Invalid("Install Plan entry path is unsafe".to_owned()))?;
    if path.is_empty() || !valid_mode(entry.get("mode").unwrap_or(&Value::Null)) {
        return invalid("Install Plan entry identity is invalid");
    }
    Ok(())
}

fn valid_mode(value: &Value) -> bool {
    value.as_u64().is_some_and(|mode| mode <= 0o777)
}

fn require_field(object: &Map<String, Value>, field: &str, label: &str) -> Result<(), EngineError> {
    if object.contains_key(field) {
        Ok(())
    } else {
        invalid(format!("{label} missing required field: {field}"))
    }
}

fn unique_string_array(
    value: &Value,
    message: &str,
    maximum: usize,
    allow_empty: bool,
) -> Result<Vec<String>, EngineError> {
    let values = value
        .as_array()
        .ok_or_else(|| EngineError::Invalid(message.to_owned()))?;
    if values.len() > maximum {
        return invalid(message);
    }
    let strings = values
        .iter()
        .map(|item| {
            item.as_str()
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| EngineError::Invalid(message.to_owned()))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if (!allow_empty && strings.is_empty())
        || strings.iter().collect::<BTreeSet<_>>().len() != strings.len()
    {
        return invalid(message);
    }
    Ok(strings)
}

fn is_sorted_unique(values: &[String]) -> bool {
    values.windows(2).all(|items| items[0] < items[1])
}

fn is_version_requirement(value: &str) -> bool {
    !value.is_empty()
        && value.split(' ').all(|item| {
            [">=", "<=", "==", ">", "<"]
                .into_iter()
                .find_map(|operator| item.strip_prefix(operator))
                .is_some_and(is_strict_semver)
        })
}

fn validate_source(source: &Value, package_id: &str) -> Result<(), EngineError> {
    let source = exact_object(
        source,
        &["artifact_sha256", "kind", "sha256", "uri"],
        &format!("package lock source is invalid: {package_id}"),
    )?;
    let kind = source.get("kind").and_then(Value::as_str).ok_or_else(|| {
        EngineError::Invalid(format!(
            "package lock source kind is unsupported: {package_id}"
        ))
    })?;
    if !matches!(kind, "local-registry" | "relative-path" | "https") {
        return invalid(format!(
            "package lock source kind is unsupported: {package_id}"
        ));
    }
    let uri = source
        .get("uri")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            EngineError::Invalid(format!(
                "package lock source identity is invalid: {package_id}"
            ))
        })?;
    if uri.len() > MAX_PATH_BYTES
        || uri.contains('\\')
        || uri.bytes().any(|byte| byte <= 0x20 || byte == 0x7f)
        || !is_sha256(source.get("sha256").unwrap_or(&Value::Null))
    {
        return invalid(format!(
            "package lock source identity is invalid: {package_id}"
        ));
    }
    let artifact = source.get("artifact_sha256").unwrap_or(&Value::Null);
    match kind {
        "local-registry" => {
            if !artifact.is_null() {
                return invalid(format!(
                    "package lock registry source must not declare an artifact: {package_id}"
                ));
            }
            if uri != format!("registry://{package_id}") {
                return invalid(format!(
                    "package lock registry source is invalid: {package_id}"
                ));
            }
        }
        "relative-path" => {
            if !artifact.is_null() {
                return invalid(format!(
                    "package lock relative source must not declare an artifact: {package_id}"
                ));
            }
            if !is_safe_relative_uri(uri) {
                return invalid(format!(
                    "package lock relative source is unsafe: {package_id}"
                ));
            }
        }
        "https" => {
            if !is_safe_https_uri(uri) {
                return invalid(format!("package lock HTTPS source is unsafe: {package_id}"));
            }
            if !is_sha256(artifact) {
                return invalid(format!(
                    "package lock HTTPS source requires an artifact SHA-256: {package_id}"
                ));
            }
        }
        _ => unreachable!(),
    }
    Ok(())
}

fn validate_relative_source_snapshot(
    source_base: &Path,
    uri: &str,
    package_record: &Map<String, Value>,
    package_id: &str,
) -> Result<(), EngineError> {
    let base_metadata = std::fs::symlink_metadata(source_base).map_err(|_| {
        EngineError::Invalid("package lock source base is missing or unsafe".to_owned())
    })?;
    if base_metadata.file_type().is_symlink() || !base_metadata.is_dir() {
        return invalid("package lock source base is missing or unsafe");
    }
    let mut lexical = source_base.to_path_buf();
    for part in uri.trim_start_matches("./").split('/') {
        lexical.push(part);
        let metadata = std::fs::symlink_metadata(&lexical).map_err(|_| {
            EngineError::Invalid(format!(
                "package lock relative source is missing or unsafe: {package_id}"
            ))
        })?;
        if metadata.file_type().is_symlink() {
            return invalid(format!(
                "package lock relative source traverses a symlink: {package_id}"
            ));
        }
    }
    let root = std::fs::canonicalize(&lexical).map_err(|_| {
        EngineError::Invalid(format!(
            "package lock relative source is missing or unsafe: {package_id}"
        ))
    })?;
    let base = std::fs::canonicalize(source_base)?;
    if !root.starts_with(&base) || !root.is_dir() {
        return invalid(format!(
            "package lock relative source escapes source base: {package_id}"
        ));
    }
    let expected_files = package_record
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid("Install Plan package files are invalid".to_owned()))?;
    if expected_files.len() > MAX_PACKAGE_FILES {
        return invalid(format!(
            "package source exceeds maximum of {MAX_PACKAGE_FILES} files"
        ));
    }
    let mut actual_files = Vec::with_capacity(expected_files.len());
    for expected in expected_files {
        let expected = object(expected, "Install Plan package file")?;
        let relative = required_string(expected, "path")?;
        if !is_safe_contract_path(&relative) {
            return invalid(format!(
                "package lock relative source file is missing or unsafe: {package_id}"
            ));
        }
        let (path, metadata) = safe_relative_source_file(&root, &relative, package_id)?;
        actual_files.push(json!({
            "mode": executable_mode(&metadata),
            "path": relative,
            "sha256": file_sha256(&path)?,
        }));
    }
    if package_record.get("files") != Some(&Value::Array(actual_files.clone()))
        || package_record.get("files_sha256").and_then(Value::as_str)
            != Some(canonical_sha256(&Value::Array(actual_files))?.as_str())
    {
        return invalid(format!(
            "package lock relative source content differs from Install Plan: {package_id}"
        ));
    }
    Ok(())
}

fn safe_relative_source_file(
    root: &Path,
    relative: &str,
    package_id: &str,
) -> Result<(PathBuf, std::fs::Metadata), EngineError> {
    let parts = relative
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect::<Vec<_>>();
    let mut path = root.to_path_buf();
    for (index, part) in parts.iter().enumerate() {
        path.push(part);
        let metadata = std::fs::symlink_metadata(&path).map_err(|_| {
            EngineError::Invalid(format!(
                "package lock relative source file is missing or unsafe: {package_id}"
            ))
        })?;
        if metadata.file_type().is_symlink()
            || (index + 1 < parts.len() && !metadata.is_dir())
            || (index + 1 == parts.len() && !metadata.is_file())
        {
            return invalid(format!(
                "package lock relative source file is missing or unsafe: {package_id}"
            ));
        }
        if index + 1 == parts.len() {
            return Ok((path, metadata));
        }
    }
    invalid(format!(
        "package lock relative source file is missing or unsafe: {package_id}"
    ))
}

fn validate_schema_inventory(value: &Value) -> Result<(), EngineError> {
    let inventory = exact_object(
        value,
        &["algorithm", "content_sha256", "files"],
        "agent-skills-lock schema inventory is invalid",
    )?;
    if inventory.get("algorithm").and_then(Value::as_str) != Some("sha256")
        || !is_sha256(inventory.get("content_sha256").unwrap_or(&Value::Null))
    {
        return invalid("agent-skills-lock schema inventory identity is invalid");
    }
    let files = inventory
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            EngineError::Invalid("agent-skills-lock schema files are invalid".to_owned())
        })?;
    if files.is_empty() || files.len() > MAX_LOCK_SCHEMAS {
        return invalid("agent-skills-lock schema inventory digest is invalid");
    }
    let mut paths = Vec::with_capacity(files.len());
    for item in files {
        let item = exact_object(
            item,
            &["path", "sha256"],
            "agent-skills-lock schema entry is invalid",
        )?;
        let path = item.get("path").and_then(Value::as_str).ok_or_else(|| {
            EngineError::Invalid("agent-skills-lock schema path is invalid".to_owned())
        })?;
        if !path.ends_with(".schema.json")
            || path.len() > MAX_PATH_BYTES
            || !is_safe_contract_path(path)
            || !is_sha256(item.get("sha256").unwrap_or(&Value::Null))
        {
            return invalid("agent-skills-lock schema path is invalid");
        }
        paths.push(path.to_owned());
    }
    let mut expected_paths = paths.clone();
    expected_paths.sort();
    expected_paths.dedup();
    if paths != expected_paths
        || inventory.get("content_sha256").and_then(Value::as_str)
            != Some(canonical_sha256(&Value::Array(files.clone()))?.as_str())
    {
        return invalid("agent-skills-lock schema inventory digest is invalid");
    }
    Ok(())
}

fn validate_dependencies(
    lock: &Map<String, Value>,
    package_by_id: &BTreeMap<String, &Map<String, Value>>,
    selected: &BTreeSet<String>,
    package_ids: &BTreeSet<String>,
) -> Result<(), EngineError> {
    let dependencies = lock
        .get("dependencies")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            EngineError::Invalid("agent-skills-lock dependencies are invalid".to_owned())
        })?;
    if dependencies.len() > MAX_LOCK_DEPENDENCIES {
        return invalid(format!(
            "agent-skills-lock dependencies exceed maximum of {MAX_LOCK_DEPENDENCIES}"
        ));
    }
    let mut edges = Vec::with_capacity(dependencies.len());
    for dependency in dependencies {
        let dependency = exact_object(
            dependency,
            &[
                "from",
                "to",
                "requirement",
                "version",
                "required_capabilities",
            ],
            "agent-skills-lock dependency fields are invalid",
        )?;
        let from = dependency.get("from").and_then(Value::as_str);
        let to = dependency.get("to").and_then(Value::as_str);
        let capabilities = dependency
            .get("required_capabilities")
            .and_then(Value::as_array);
        if from.is_none()
            || to.is_none()
            || from == to
            || !from.is_some_and(|value| package_by_id.contains_key(value))
            || !to.is_some_and(|value| package_by_id.contains_key(value))
            || !dependency
                .get("requirement")
                .and_then(Value::as_str)
                .is_some_and(|value| matches!(value, "required" | "optional"))
            || dependency.get("version").and_then(Value::as_str).is_none()
            || capabilities.is_none_or(Vec::is_empty)
        {
            return invalid("agent-skills-lock dependency references unknown package");
        }
        sorted_unique_string_array(
            dependency
                .get("required_capabilities")
                .unwrap_or(&Value::Null),
            "agent-skills-lock dependency references unknown package",
            MAX_LOCK_PROVIDERS,
        )?;
        let to = to.unwrap_or_default();
        let target_version = package_by_id[to]
            .get("version")
            .and_then(Value::as_str)
            .ok_or_else(|| EngineError::Invalid("package version is invalid".to_owned()))?;
        if !satisfies(
            target_version,
            dependency
                .get("version")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        )? {
            return invalid("agent-skills-lock dependency version is not satisfied");
        }
        edges.push((from.unwrap_or_default().to_owned(), to.to_owned()));
    }
    let mut expected_edges = edges.clone();
    expected_edges.sort();
    expected_edges.dedup();
    if edges != expected_edges {
        return invalid("agent-skills-lock dependency edges must be sorted and unique");
    }
    validate_dependency_acyclic(package_ids, &edges)?;
    let mut reachable = selected.clone();
    reachable.insert("core".to_owned());
    loop {
        let before = reachable.len();
        for dependency in dependencies {
            let from = dependency
                .get("from")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let to = dependency
                .get("to")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if reachable.contains(from) {
                reachable.insert(to.to_owned());
            }
        }
        if reachable.len() == before {
            break;
        }
    }
    if &reachable != package_ids {
        return invalid(
            "agent-skills-lock contains packages outside the selected dependency closure",
        );
    }
    Ok(())
}

fn validate_dependency_acyclic(
    package_ids: &BTreeSet<String>,
    edges: &[(String, String)],
) -> Result<(), EngineError> {
    let mut incoming = package_ids
        .iter()
        .map(|package_id| (package_id.clone(), 0_usize))
        .collect::<BTreeMap<_, _>>();
    let mut outgoing = package_ids
        .iter()
        .map(|package_id| (package_id.clone(), Vec::<String>::new()))
        .collect::<BTreeMap<_, _>>();
    for (source, target) in edges {
        *incoming
            .get_mut(target)
            .ok_or_else(|| EngineError::Invalid("dependency target is unknown".to_owned()))? += 1;
        outgoing
            .get_mut(source)
            .ok_or_else(|| EngineError::Invalid("dependency source is unknown".to_owned()))?
            .push(target.clone());
    }
    let mut queue = incoming
        .iter()
        .filter_map(|(package_id, count)| (*count == 0).then_some(package_id.clone()))
        .collect::<Vec<_>>();
    let mut visited = 0_usize;
    while let Some(source) = queue.pop() {
        visited += 1;
        for target in &outgoing[&source] {
            let count = incoming
                .get_mut(target)
                .ok_or_else(|| EngineError::Invalid("dependency target is unknown".to_owned()))?;
            *count -= 1;
            if *count == 0 {
                queue.push(target.clone());
            }
        }
    }
    if visited != package_ids.len() {
        return invalid("agent-skills-lock dependency graph contains a cycle");
    }
    Ok(())
}

fn collect_schema_files(
    directory: &Path,
    candidates: &mut BTreeSet<PathBuf>,
    entry_count: &mut usize,
) -> Result<(), EngineError> {
    if !directory.exists() {
        return Ok(());
    }
    let metadata = std::fs::symlink_metadata(directory)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return invalid(format!(
            "schema directory is unsafe: {}",
            directory.display()
        ));
    }
    for entry in std::fs::read_dir(directory)? {
        let entry = entry?;
        *entry_count = entry_count.checked_add(1).ok_or_else(|| {
            EngineError::Invalid("schema directory entry counter overflow".to_owned())
        })?;
        if *entry_count > MAX_SCHEMA_DIRECTORY_ENTRIES {
            return invalid(format!(
                "schema inventory exceeds maximum of {MAX_SCHEMA_DIRECTORY_ENTRIES} directory entries"
            ));
        }
        let file_type = entry.file_type()?;
        let path = entry.path();
        if path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(".schema.json"))
        {
            if file_type.is_symlink() || !file_type.is_file() {
                return invalid(format!(
                    "schema file is unsafe: {}",
                    path.file_name().unwrap_or_default().to_string_lossy()
                ));
            }
            candidates.insert(path);
        }
    }
    Ok(())
}

fn collect_nested_schema_files(
    container: &Path,
    children: &[&str],
    candidates: &mut BTreeSet<PathBuf>,
    entry_count: &mut usize,
) -> Result<(), EngineError> {
    if !container.exists() {
        return Ok(());
    }
    let metadata = std::fs::symlink_metadata(container)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return invalid(format!(
            "schema directory is unsafe: {}",
            container.display()
        ));
    }
    for entry in std::fs::read_dir(container)? {
        let entry = entry?;
        *entry_count = entry_count.checked_add(1).ok_or_else(|| {
            EngineError::Invalid("schema directory entry counter overflow".to_owned())
        })?;
        if *entry_count > MAX_SCHEMA_DIRECTORY_ENTRIES {
            return invalid(format!(
                "schema inventory exceeds maximum of {MAX_SCHEMA_DIRECTORY_ENTRIES} directory entries"
            ));
        }
        let file_type = entry.file_type()?;
        if file_type.is_symlink() || !file_type.is_dir() {
            continue;
        }
        for child in children {
            let nested = entry.path().join(child);
            if nested.exists() {
                let metadata = std::fs::symlink_metadata(&nested)?;
                if metadata.file_type().is_symlink() || !metadata.is_dir() {
                    continue;
                }
                collect_schema_files(&nested, candidates, entry_count)?;
            }
        }
    }
    Ok(())
}

fn reject_unknown_override_keys(
    values: &Map<String, Value>,
    package_ids: &BTreeSet<String>,
    label: &str,
) -> Result<(), EngineError> {
    let unknown = values
        .keys()
        .filter(|key| !package_ids.contains(*key))
        .cloned()
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        return invalid(format!(
            "package lock {label} reference unknown packages: {}",
            unknown.join(", ")
        ));
    }
    Ok(())
}

fn nested_field(
    object: &Map<String, Value>,
    parent: &str,
    field: &str,
) -> Result<Value, EngineError> {
    object
        .get(parent)
        .and_then(|value| value.get(field))
        .cloned()
        .ok_or_else(|| EngineError::Invalid(format!("Install Plan {parent}.{field} is missing")))
}

fn indexed_values<'a>(
    value: &'a Value,
    pointer: &str,
    key: &str,
) -> Result<BTreeMap<String, &'a Value>, EngineError> {
    value
        .pointer(pointer)
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid(format!("{pointer} must be an array")))?
        .iter()
        .map(|item| {
            let identity = item.get(key).and_then(Value::as_str).ok_or_else(|| {
                EngineError::Invalid(format!("{pointer} item identity is invalid"))
            })?;
            Ok((identity.to_owned(), item))
        })
        .collect()
}

fn indexed_scalar_values(
    value: &Value,
    pointer: &str,
    key: &str,
    scalar: &str,
) -> Result<BTreeMap<String, Value>, EngineError> {
    value
        .pointer(pointer)
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid(format!("{pointer} must be an array")))?
        .iter()
        .map(|item| {
            let identity = item.get(key).and_then(Value::as_str).ok_or_else(|| {
                EngineError::Invalid(format!("{pointer} item identity is invalid"))
            })?;
            Ok((
                identity.to_owned(),
                item.get(scalar).cloned().unwrap_or(Value::Null),
            ))
        })
        .collect()
}

fn map_changes<T: PartialEq>(left: &BTreeMap<String, T>, right: &BTreeMap<String, T>) -> Value {
    let left_keys = left.keys().cloned().collect::<BTreeSet<_>>();
    let right_keys = right.keys().cloned().collect::<BTreeSet<_>>();
    json!({
        "added": right_keys.difference(&left_keys).cloned().collect::<Vec<_>>(),
        "changed": left_keys.intersection(&right_keys).filter(
            |key| left.get(*key) != right.get(*key)
        ).cloned().collect::<Vec<_>>(),
        "removed": left_keys.difference(&right_keys).cloned().collect::<Vec<_>>(),
    })
}

fn value_string_set(value: &Value, label: &str) -> Result<BTreeSet<String>, EngineError> {
    value
        .as_array()
        .ok_or_else(|| EngineError::Invalid(format!("{label} must be an array")))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| EngineError::Invalid(format!("{label} must contain strings")))
        })
        .collect::<Result<_, _>>()
}

fn sorted_unique_string_array(
    value: &Value,
    message: &str,
    maximum: usize,
) -> Result<Vec<String>, EngineError> {
    let values = value
        .as_array()
        .ok_or_else(|| EngineError::Invalid(message.to_owned()))?;
    if values.len() > maximum {
        return invalid(message);
    }
    let strings = values
        .iter()
        .map(|item| {
            item.as_str()
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| EngineError::Invalid(message.to_owned()))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut expected = strings.clone();
    expected.sort();
    expected.dedup();
    if strings != expected {
        return invalid(message);
    }
    Ok(strings)
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

fn object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, EngineError> {
    value
        .as_object()
        .ok_or_else(|| EngineError::Invalid(format!("{label} must be an object")))
}

fn array_field<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a Vec<Value>, EngineError> {
    object
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| EngineError::Invalid(format!("{label} {field} must be an array")))
}

fn required_string(object: &Map<String, Value>, field: &str) -> Result<String, EngineError> {
    object
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| EngineError::Invalid(format!("{field} must be a string")))
}

fn is_sha256(value: &Value) -> bool {
    value.as_str().is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    })
}

fn is_safe_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn is_strict_semver(value: &str) -> bool {
    let parts = value.split('.').collect::<Vec<_>>();
    parts.len() == 3
        && parts.iter().all(|part| {
            !part.is_empty()
                && part.bytes().all(|byte| byte.is_ascii_digit())
                && (part == &"0" || !part.starts_with('0'))
        })
}

fn is_safe_relative_uri(value: &str) -> bool {
    value.starts_with("./")
        && !value.contains('\\')
        && is_safe_contract_path(value.trim_start_matches("./"))
}

fn is_safe_contract_path(value: &str) -> bool {
    if value.is_empty()
        || value.starts_with('/')
        || value.contains('\\')
        || value.len() > MAX_PATH_BYTES
    {
        return false;
    }
    if value
        .as_bytes()
        .get(0..2)
        .is_some_and(|prefix| prefix[0].is_ascii_alphabetic() && prefix[1] == b':')
    {
        return false;
    }
    let parts = value
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect::<Vec<_>>();
    !parts.is_empty() && parts.iter().all(|part| *part != "..")
}

fn is_safe_https_uri(value: &str) -> bool {
    let Some(scheme) = value.get(..8) else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case("https://") {
        return false;
    }
    let remainder = &value[8..];
    let (without_suffix, suffix) = remainder.find(['?', '#']).map_or((remainder, ""), |index| {
        (&remainder[..index], &remainder[index..])
    });
    if !matches!(suffix, "" | "?" | "#" | "?#") {
        return false;
    }
    let authority = without_suffix.split('/').next().unwrap_or_default();
    if authority.is_empty() || authority.contains('@') {
        return false;
    }
    if let Some(address) = authority.strip_prefix('[') {
        let Some(closing) = address.find(']') else {
            return false;
        };
        if !is_valid_bracket_host(&address[..closing]) {
            return false;
        }
        let tail = &address[closing + 1..];
        return tail.is_empty() || tail.starts_with(':');
    }
    !authority.contains(['[', ']'])
}

fn is_valid_bracket_host(value: &str) -> bool {
    if value.parse::<std::net::Ipv6Addr>().is_ok() {
        return true;
    }
    if let Some((address, zone)) = value.split_once('%') {
        return !zone.is_empty() && address.parse::<std::net::Ipv6Addr>().is_ok();
    }
    let Some(future) = value.strip_prefix('v').or_else(|| value.strip_prefix('V')) else {
        return false;
    };
    let Some((version, address)) = future.split_once('.') else {
        return false;
    };
    !version.is_empty()
        && version.bytes().all(|byte| byte.is_ascii_hexdigit())
        && !address.is_empty()
}

fn portable_relative_path(path: &Path) -> Result<String, EngineError> {
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(value) => parts.push(value.to_string_lossy().into_owned()),
            _ => return invalid("schema inventory contains an unsafe relative path"),
        }
    }
    let value = parts.join("/");
    if !is_safe_contract_path(&value) {
        return invalid("schema inventory contains an unsafe relative path");
    }
    Ok(value)
}

fn file_sha256(path: &Path) -> Result<String, EngineError> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut block = vec![0_u8; 1024 * 1024];
    loop {
        let count = file.read(&mut block)?;
        if count == 0 {
            break;
        }
        digest.update(&block[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(unix)]
fn executable_mode(metadata: &std::fs::Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt as _;
    if metadata.permissions().mode() & 0o111 == 0 {
        0o644
    } else {
        0o755
    }
}

#[cfg(not(unix))]
const fn executable_mode(_metadata: &std::fs::Metadata) -> u32 {
    0o644
}

#[cfg(test)]
mod tests {
    use super::{is_safe_contract_path, is_safe_https_uri, is_safe_relative_uri};

    #[test]
    fn source_boundaries_are_fail_closed() {
        assert!(is_safe_relative_uri("./platforms/apple"));
        assert!(!is_safe_relative_uri("./../apple"));
        assert!(!is_safe_relative_uri("platforms/apple"));
        assert!(is_safe_https_uri("https://example.test/apple.zip"));
        assert!(is_safe_https_uri("https://[::1]:/apple.zip"));
        assert!(is_safe_https_uri("https://[v1.test]/apple.zip"));
        assert!(is_safe_https_uri("https://[fe80::1%25eth0]/apple.zip"));
        assert!(!is_safe_https_uri("https://user@example.test/apple.zip"));
        assert!(!is_safe_https_uri(
            "https://example.test/apple.zip?latest=1"
        ));
        assert!(is_safe_contract_path("schemas/example.schema.json"));
        assert!(is_safe_contract_path("灯:example.schema.json"));
        assert!(!is_safe_contract_path("../schemas/example.schema.json"));
        assert!(!is_safe_contract_path("C:example.schema.json"));
    }
}
