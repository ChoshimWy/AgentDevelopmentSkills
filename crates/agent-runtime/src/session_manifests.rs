//! Manifest-driven Worktree Session platform and capability closure compiler.

use crate::RuntimeError;
use agent_contracts::load_json;
use agent_registry::{CORE_VERSION, ManifestRegistry};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::path::Path;

/// Compile the deterministic platform/provider selection embedded in a
/// Worktree Session Context.
///
/// This is a read-only operation. It validates the explicitly trusted
/// Manifest root and never invokes Provider or package code.
///
/// # Errors
/// Returns a fail-closed error for duplicate selections, unsafe roots,
/// bootstrap-only platforms, invalid Provider identities, empty bindings, or
/// unavailable shared review/Git verification capabilities.
pub fn compile_session_manifest_selection(
    manifest_root: Option<&Path>,
    selected_platforms: &[String],
) -> Result<Value, RuntimeError> {
    let mut selected = selected_platforms.to_vec();
    if selected.iter().any(|platform| !valid_platform(platform)) {
        return contract("selected platforms contain an invalid platform id");
    }
    let original_count = selected.len();
    selected.sort();
    selected.dedup();
    if selected.len() != original_count {
        return contract("selected platforms must be unique");
    }

    if !selected.is_empty() {
        let root = manifest_root.ok_or_else(|| {
            RuntimeError::Contract(
                "platform selection requires an explicit trusted Manifest root".to_owned(),
            )
        })?;
        validate_manifest_root(root)?;
    }

    let Some(root) = manifest_root else {
        return Ok(json!({
            "capability_closure": {},
            "platform_contexts": {},
            "selected_platforms": selected,
        }));
    };
    validate_manifest_root(root)?;
    let registry = ManifestRegistry::from_directory(root, &BTreeSet::new(), CORE_VERSION)
        .map_err(|error| RuntimeError::Contract(error.to_string()))?;
    let mut contexts = Map::new();
    for platform_id in &selected {
        contexts.insert(
            platform_id.clone(),
            load_platform_context(root, &registry, platform_id)?,
        );
    }
    let closure = build_capability_closure(&registry, &contexts)?;
    Ok(json!({
        "capability_closure": closure,
        "platform_contexts": contexts,
        "selected_platforms": selected,
    }))
}

fn load_platform_context(
    root: &Path,
    registry: &ManifestRegistry,
    platform_id: &str,
) -> Result<Value, RuntimeError> {
    let manifest = load_trusted_platform_manifest(root, registry, platform_id)?;
    let installation = manifest.get("installation").and_then(Value::as_object);
    if manifest
        .get("implementation_status")
        .and_then(Value::as_str)
        != Some("implemented")
        || installation.is_none()
    {
        return contract(format!(
            "bootstrap_required: platform Provider is not implemented: {platform_id}"
        ));
    }
    let provider_contract = manifest.get("provider_contract").and_then(Value::as_object);
    if !installation
        .and_then(|value| value.get("provider_manifest"))
        .is_some_and(Value::is_string)
        || provider_contract.is_none()
    {
        return contract(format!(
            "bootstrap_required: platform Provider contract is unavailable: {platform_id}"
        ));
    }
    let package_id = provider_contract
        .and_then(|contract| contract.get("package_id"))
        .and_then(Value::as_str)
        .ok_or_else(|| {
            RuntimeError::Contract(format!(
                "bootstrap_required: platform Provider Manifest is unavailable: {platform_id}"
            ))
        })?;
    let provider = &registry
        .by_id(package_id)
        .ok_or_else(|| {
            RuntimeError::Contract(format!(
                "bootstrap_required: platform Provider Manifest is unavailable: {platform_id}"
            ))
        })?
        .value;
    validated_provider_bindings(provider, package_id, platform_id).map(|bindings| {
        json!({
            "bindings": bindings,
            "context": {},
            "provider_id": package_id,
        })
    })
}

fn load_trusted_platform_manifest(
    root: &Path,
    registry: &ManifestRegistry,
    platform_id: &str,
) -> Result<Value, RuntimeError> {
    let platform_directory = root.join(platform_id);
    let platform_metadata = platform_directory.symlink_metadata().map_err(|_| {
        RuntimeError::Contract(format!(
            "bootstrap_required: platform Manifest is unavailable: {platform_id}"
        ))
    })?;
    if platform_metadata.file_type().is_symlink() || !platform_metadata.is_dir() {
        return contract(format!(
            "bootstrap_required: platform Manifest is unavailable: {platform_id}"
        ));
    }
    let manifest_path = platform_directory.join("manifest.json");
    let metadata = manifest_path.symlink_metadata().map_err(|_| {
        RuntimeError::Contract(format!(
            "bootstrap_required: platform Manifest is unavailable: {platform_id}"
        ))
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return contract(format!(
            "bootstrap_required: platform Manifest is unavailable: {platform_id}"
        ));
    }
    let canonical_root = root.canonicalize()?;
    let canonical_platform = platform_directory.canonicalize()?;
    let canonical_manifest = manifest_path.canonicalize()?;
    if canonical_platform.parent() != Some(canonical_root.as_path())
        || canonical_manifest.parent() != Some(canonical_platform.as_path())
    {
        return contract(format!(
            "bootstrap_required: platform Manifest escaped its trusted package: {platform_id}"
        ));
    }
    let manifest = load_json(&manifest_path)?;
    if manifest.get("id").and_then(Value::as_str) != Some(platform_id)
        || manifest.get("kind").and_then(Value::as_str) != Some("platform")
    {
        return contract(format!(
            "bootstrap_required: invalid platform package: {platform_id}"
        ));
    }
    let registered_platform = registry.by_id(platform_id).ok_or_else(|| {
        RuntimeError::Contract(format!(
            "bootstrap_required: invalid platform package: {platform_id}"
        ))
    })?;
    if registered_platform.path != canonical_manifest
        || registered_platform.digest != agent_contracts::canonical_sha256(&manifest)?
    {
        return contract(format!(
            "bootstrap_required: invalid platform package: {platform_id}"
        ));
    }
    Ok(manifest)
}

fn validated_provider_bindings<'a>(
    provider: &'a Value,
    package_id: &str,
    platform_id: &str,
) -> Result<&'a Map<String, Value>, RuntimeError> {
    if provider.get("id").and_then(Value::as_str) != Some(package_id)
        || provider.get("role").and_then(Value::as_str) != Some("provider")
        || !provider
            .get("targets")
            .and_then(Value::as_array)
            .is_some_and(|targets| {
                targets
                    .iter()
                    .any(|target| target.as_str() == Some(platform_id))
            })
    {
        return contract(format!(
            "bootstrap_required: platform Provider identity is invalid: {platform_id}"
        ));
    }
    let bindings = provider
        .get("bindings")
        .and_then(Value::as_object)
        .filter(|bindings| !bindings.is_empty())
        .ok_or_else(|| {
            RuntimeError::Contract(format!(
                "bootstrap_required: platform Provider binding closure is empty: {platform_id}"
            ))
        })?;
    let declared = provider
        .get("capabilities")
        .and_then(Value::as_array)
        .map(|capabilities| {
            capabilities
                .iter()
                .filter_map(|capability| capability.get("id").and_then(Value::as_str))
                .collect::<BTreeSet<_>>()
        })
        .unwrap_or_default();
    if bindings.keys().map(String::as_str).collect::<BTreeSet<_>>() != declared {
        return contract(format!(
            "bootstrap_required: platform Provider bindings differ from declared capabilities: {platform_id}"
        ));
    }
    Ok(bindings)
}

fn build_capability_closure(
    registry: &ManifestRegistry,
    contexts: &Map<String, Value>,
) -> Result<Map<String, Value>, RuntimeError> {
    let mut closure = Map::new();
    for context in contexts.values() {
        let provider_id = context
            .get("provider_id")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                RuntimeError::Contract("platform Provider identity is invalid".to_owned())
            })?;
        let bindings = context
            .get("bindings")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                RuntimeError::Contract("platform Provider binding closure is invalid".to_owned())
            })?;
        for (capability, binding) in bindings {
            if closure.contains_key(capability) {
                return contract(format!(
                    "ambiguous platform capability binding: {capability}"
                ));
            }
            closure.insert(
                capability.clone(),
                json!({"binding": binding, "provider_id": provider_id}),
            );
        }
    }
    for capability in ["review.independent", "verification.git.repository"] {
        let resolved = registry
            .resolve_binding(capability, None)
            .map_err(|error| RuntimeError::Contract(error.to_string()))?
            .ok_or_else(|| {
                RuntimeError::Contract(format!("{capability} capability closure is unavailable"))
            })?;
        closure.insert(
            capability.to_owned(),
            json!({
                "binding": resolved.binding,
                "provider_id": resolved.provider_id,
            }),
        );
    }
    Ok(closure)
}

fn validate_manifest_root(root: &Path) -> Result<(), RuntimeError> {
    let metadata = root.symlink_metadata().map_err(|_| {
        RuntimeError::Contract(
            "platform selection requires an explicit trusted Manifest root".to_owned(),
        )
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return contract("platform selection requires an explicit trusted Manifest root");
    }
    Ok(())
}

fn valid_platform(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.as_bytes()[0].is_ascii_lowercase()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

fn contract<T>(message: impl Into<String>) -> Result<T, RuntimeError> {
    Err(RuntimeError::Contract(message.into()))
}
