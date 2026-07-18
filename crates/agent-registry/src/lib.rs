//! Read-only manifest registry used by the native migration path.

use agent_contracts::{ContractError, canonical_sha256, load_json};
use serde::Serialize;
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use thiserror::Error;

/// Version of the Python core whose registry behavior is mirrored here.
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");

const MAX_MANIFEST_DIRECTORY_DEPTH: usize = 128;
const MAX_MANIFEST_DIRECTORY_ENTRIES: usize = 100_000;
const MAX_MANIFESTS: usize = 4_096;
const MAX_CAPABILITY_NODES: usize = 16_384;
const MAX_CAPABILITY_EDGES: usize = 65_536;

/// Registry loading, validation, and resolution failures.
#[derive(Debug, Error)]
pub enum RegistryError {
    #[error(transparent)]
    Contract(#[from] ContractError),
    #[error("manifest input cannot be read: {0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Invalid(String),
}

/// One validated manifest and its canonical identity.
#[derive(Debug, Clone)]
pub struct RegisteredManifest {
    pub path: PathBuf,
    pub value: Value,
    pub digest: String,
}

/// One resolved capability binding.
#[derive(Debug, Clone, Serialize)]
pub struct ResolvedBinding {
    pub capability_id: String,
    pub provider_id: String,
    pub binding: Value,
    pub contract: Value,
    pub manifest_digest: String,
}

/// Deterministic, read-only collection of package and provider manifests.
#[derive(Debug, Clone)]
pub struct ManifestRegistry {
    items: Vec<RegisteredManifest>,
    core_version: String,
}

/// Validate one Manifest's local syntax without resolving registry-wide graph
/// relationships.
///
/// # Errors
/// Returns the same fail-closed syntax error used while constructing a
/// [`ManifestRegistry`].
pub fn validate_manifest_syntax(value: &Value) -> Result<(), RegistryError> {
    validate_manifest(value)
}

impl ManifestRegistry {
    /// Load a standard repository collection rooted at `platforms/`.
    ///
    /// Sibling `disciplines`, `stacks`, and `runtime-configs` collections are
    /// included exactly as in the Python registry.
    ///
    /// # Errors
    /// Returns a fail-closed error for unsafe paths, malformed manifests, or an
    /// invalid registry graph.
    pub fn from_directory(
        root: impl AsRef<Path>,
        disabled_providers: &BTreeSet<String>,
        core_version: &str,
    ) -> Result<Self, RegistryError> {
        Self::from_directory_with_provider_roots(root, &[], disabled_providers, core_version)
    }

    /// Load a standard repository collection plus explicit external providers.
    ///
    /// # Errors
    /// Returns a fail-closed error for unsafe paths, malformed manifests, or an
    /// invalid registry graph.
    pub fn from_directory_with_provider_roots(
        root: impl AsRef<Path>,
        provider_roots: &[PathBuf],
        disabled_providers: &BTreeSet<String>,
        core_version: &str,
    ) -> Result<Self, RegistryError> {
        let root = root.as_ref();
        let mut roots = vec![root.to_path_buf()];
        if root.is_dir() && root.file_name().is_some_and(|name| name == "platforms") {
            let parent = root.parent().ok_or_else(|| {
                RegistryError::Invalid("platforms collection has no parent".to_owned())
            })?;
            for name in ["disciplines", "stacks", "runtime-configs"] {
                let candidate = parent.join(name);
                if candidate
                    .symlink_metadata()
                    .is_ok_and(|metadata| metadata.file_type().is_symlink())
                {
                    return Err(RegistryError::Invalid(format!(
                        "manifest package collection must not be a symlink: {name}"
                    )));
                }
                if candidate.is_dir() {
                    roots.push(candidate);
                }
            }
        }
        roots.extend_from_slice(provider_roots);
        Self::from_directories(&roots, disabled_providers, core_version)
    }

    /// Load explicit manifest roots.
    ///
    /// # Errors
    /// Returns a fail-closed error for malformed inputs or graph violations.
    pub fn from_directories(
        roots: &[PathBuf],
        disabled_providers: &BTreeSet<String>,
        core_version: &str,
    ) -> Result<Self, RegistryError> {
        if roots.len() > MAX_MANIFESTS {
            return invalid(format!(
                "manifest collection exceeds maximum of {MAX_MANIFESTS} roots"
            ));
        }
        let mut paths = BTreeSet::new();
        let mut directory_entry_count = 0_usize;
        for root in roots {
            if !root.exists() {
                continue;
            }
            let canonical = std::fs::canonicalize(root)?;
            if canonical.is_file() {
                paths.insert(canonical);
            } else {
                collect_manifest_paths(&canonical, &mut paths, &mut directory_entry_count)?;
            }
            if paths.len() > MAX_MANIFESTS {
                return invalid(format!(
                    "manifest collection exceeds maximum of {MAX_MANIFESTS} manifests"
                ));
            }
        }

        let mut manifests = Vec::new();
        for path in paths {
            let value = load_json(&path)?;
            validate_manifest(&value)?;
            if string_field(&value, "role") == Some("provider")
                && string_field(&value, "id")
                    .is_some_and(|identifier| disabled_providers.contains(identifier))
            {
                continue;
            }
            let digest = canonical_sha256(&value)?;
            manifests.push(RegisteredManifest {
                path,
                value,
                digest,
            });
        }
        Self::new(manifests, core_version)
    }

    /// Build a registry from already loaded manifests.
    ///
    /// # Errors
    /// Returns a fail-closed error for duplicate IDs or graph violations.
    pub fn new(
        mut manifests: Vec<RegisteredManifest>,
        core_version: &str,
    ) -> Result<Self, RegistryError> {
        for manifest in &manifests {
            validate_manifest(&manifest.value)?;
            if canonical_sha256(&manifest.value)? != manifest.digest {
                return invalid(format!(
                    "manifest {} digest does not match its canonical content",
                    manifest_id(&manifest.value)
                ));
            }
        }
        manifests.sort_by(|left, right| manifest_id(&left.value).cmp(manifest_id(&right.value)));
        let ids = manifests
            .iter()
            .map(|item| manifest_id(&item.value).to_owned())
            .collect::<Vec<_>>();
        if ids.iter().collect::<BTreeSet<_>>().len() != ids.len() {
            return invalid("manifest ids must be unique");
        }
        let registry = Self {
            items: manifests,
            core_version: core_version.to_owned(),
        };
        registry.validate_graph()?;
        Ok(registry)
    }

    #[must_use]
    pub fn manifests(&self) -> &[RegisteredManifest] {
        &self.items
    }

    #[must_use]
    pub fn by_id(&self, identifier: &str) -> Option<&RegisteredManifest> {
        self.items
            .iter()
            .find(|item| manifest_id(&item.value) == identifier)
    }

    #[must_use]
    pub fn capability_providers(&self, capability_id: &str) -> Vec<&RegisteredManifest> {
        self.items
            .iter()
            .filter(|item| {
                capabilities(&item.value).is_ok_and(|entries| {
                    entries
                        .iter()
                        .any(|entry| string_field(entry, "id") == Some(capability_id))
                })
            })
            .collect()
    }

    /// Resolve the normalized contract for an unambiguous capability.
    ///
    /// # Errors
    /// Returns an ambiguity or malformed-contract error.
    pub fn capability_contract(&self, capability_id: &str) -> Result<Option<Value>, RegistryError> {
        let providers = self.capability_providers(capability_id);
        if providers.is_empty() {
            return Ok(None);
        }
        if providers.len() > 1 {
            return invalid(format!("ambiguous capability provider: {capability_id}"));
        }
        let manifest = &providers[0].value;
        let entry = capability_entry(manifest, capability_id)?;
        Ok(Some(normalized_contract(manifest, entry)?))
    }

    /// Return an unresolved bootstrap requirement when no provider is installed.
    ///
    /// # Errors
    /// Returns an ambiguity or malformed-contract error.
    pub fn bootstrap_requirement(&self, platform: &str) -> Result<Option<Value>, RegistryError> {
        let candidates = self
            .items
            .iter()
            .filter(|item| {
                string_field(&item.value, "role") == Some("bootstrap")
                    && (manifest_id(&item.value) == platform
                        || string_array_field(&item.value, "targets")
                            .is_ok_and(|targets| targets.iter().any(|target| target == platform)))
            })
            .collect::<Vec<_>>();
        if candidates.len() > 1 {
            return invalid(format!("ambiguous platform bootstrap contract: {platform}"));
        }
        let Some(candidate) = candidates.first() else {
            return Ok(None);
        };
        let contract = object_field(&candidate.value, "provider_contract")?;
        let package_id = required_string(contract, "package_id")?;
        if self
            .by_id(package_id)
            .is_some_and(|provider| string_field(&provider.value, "role") == Some("provider"))
        {
            return Ok(None);
        }
        let mut required = required_string_array(contract, "required_capabilities")?;
        required.sort();
        Ok(Some(json!({
            "package_compatibility": required_string(contract, "package_compatibility")?,
            "platform": platform,
            "provider": package_id,
            "required_capabilities": required,
        })))
    }

    /// Resolve an implementation binding, optionally constrained to a platform.
    ///
    /// # Errors
    /// Returns an ambiguity, missing binding, or invalid contract error.
    pub fn resolve_binding(
        &self,
        capability_id: &str,
        platform: Option<&str>,
    ) -> Result<Option<ResolvedBinding>, RegistryError> {
        let mut providers = self.capability_providers(capability_id);
        if let Some(platform) = platform {
            providers.retain(|item| supports_platform(&item.value, platform));
        }
        if providers.is_empty() {
            return Ok(None);
        }
        if providers.len() > 1 {
            return invalid(format!("ambiguous capability provider: {capability_id}"));
        }
        let registered = providers[0];
        let bindings = object_field(&registered.value, "bindings")?;
        let raw = bindings.get(capability_id).ok_or_else(|| {
            RegistryError::Invalid(format!("capability has no binding: {capability_id}"))
        })?;
        let binding = normalize_binding(raw)?;
        let entry = capability_entry(&registered.value, capability_id)?;
        let contract = normalized_contract(&registered.value, entry)?;
        Ok(Some(ResolvedBinding {
            capability_id: capability_id.to_owned(),
            provider_id: manifest_id(&registered.value).to_owned(),
            binding,
            contract,
            manifest_digest: registered.digest.clone(),
        }))
    }

    /// Hash the sorted `(manifest id, manifest hash)` closure.
    ///
    /// # Errors
    /// Returns a canonical JSON encoding error.
    pub fn digest(&self) -> Result<String, RegistryError> {
        let closure = self
            .items
            .iter()
            .map(|item| json!({"id": manifest_id(&item.value), "sha256": item.digest}))
            .collect::<Vec<_>>();
        Ok(canonical_sha256(&Value::Array(closure))?)
    }

    /// Produce a deterministic differential-test snapshot of the full registry.
    ///
    /// # Errors
    /// Returns a resolution error when any source capability is ambiguous.
    pub fn snapshot(&self) -> Result<Value, RegistryError> {
        let capability_ids = self
            .items
            .iter()
            .flat_map(|item| {
                capabilities(&item.value).map_or_else(
                    |_| Vec::new(),
                    |entries| {
                        entries
                            .iter()
                            .filter_map(|entry| string_field(entry, "id").map(str::to_owned))
                            .collect::<Vec<_>>()
                    },
                )
            })
            .collect::<BTreeSet<_>>();
        let mut bindings = Map::new();
        for capability_id in &capability_ids {
            if let Some(resolved) = self.resolve_binding(capability_id, None)? {
                bindings.insert(
                    capability_id.clone(),
                    serde_json::to_value(resolved)
                        .map_err(|error| RegistryError::Invalid(error.to_string()))?,
                );
            }
        }

        let mut bootstrap_platforms = BTreeSet::new();
        for item in &self.items {
            if string_field(&item.value, "role") == Some("bootstrap") {
                bootstrap_platforms.insert(manifest_id(&item.value).to_owned());
            }
        }
        let mut bootstrap_requirements = Map::new();
        for platform in bootstrap_platforms {
            if let Some(requirement) = self.bootstrap_requirement(&platform)? {
                bootstrap_requirements.insert(platform, requirement);
            }
        }

        let manifests = self
            .items
            .iter()
            .map(|item| json!({"id": manifest_id(&item.value), "sha256": item.digest}))
            .collect::<Vec<_>>();
        Ok(json!({
            "bindings": bindings,
            "bootstrap_requirements": bootstrap_requirements,
            "digest": self.digest()?,
            "manifests": manifests,
            "schema_version": "1.0",
        }))
    }

    fn validate_graph(&self) -> Result<(), RegistryError> {
        let installed = self
            .items
            .iter()
            .map(|item| manifest_id(&item.value))
            .collect::<BTreeSet<_>>();
        for item in &self.items {
            let manifest = &item.value;
            if string_field(manifest, "role") == Some("provider") {
                self.validate_provider(item)?;
            }
            let conflicts = optional_string_array_field(manifest, "conflicts")?;
            let active = conflicts
                .iter()
                .filter(|identifier| installed.contains(identifier.as_str()))
                .cloned()
                .collect::<BTreeSet<_>>();
            if !active.is_empty() {
                return invalid(format!(
                    "manifest {} conflicts with: {}",
                    manifest_id(manifest),
                    active.into_iter().collect::<Vec<_>>().join(", ")
                ));
            }
            for required in optional_string_array_field(manifest, "requires")? {
                if self.capability_providers(&required).is_empty() {
                    return invalid(format!(
                        "manifest {} requires missing capability: {required}",
                        manifest_id(manifest)
                    ));
                }
            }
            for capability in capabilities(manifest)? {
                let normalized = normalized_contract(manifest, capability)?;
                if string_field(manifest, "role") != Some("bootstrap") {
                    let capability_id = required_string(capability, "id")?;
                    let bindings = object_field(manifest, "bindings")?;
                    let raw = bindings.get(capability_id).ok_or_else(|| {
                        RegistryError::Invalid(format!(
                            "manifest {} capability has no binding: {capability_id}",
                            manifest_id(manifest)
                        ))
                    })?;
                    let binding = normalize_binding(raw)?;
                    if let Some(modes) = capability.get("supported_modes") {
                        let supported = unique_string_array(modes, "supported_modes")?;
                        if supported.is_empty() {
                            return invalid(format!(
                                "manifest {} capability supported_modes are invalid: {capability_id}",
                                manifest_id(manifest)
                            ));
                        }
                        let selected = string_field(&binding, "mode").unwrap_or("default");
                        if !supported.iter().any(|mode| mode == selected) {
                            return invalid(format!(
                                "manifest {} binding mode is unsupported for {capability_id}",
                                manifest_id(manifest)
                            ));
                        }
                    }
                    if let Some(permission) = capability.get("binding_permission_profile")
                        && permission.as_str() != string_field(&normalized, "permission_profile")
                    {
                        return invalid(format!(
                            "manifest {} binding permission is incompatible for {capability_id}",
                            manifest_id(manifest)
                        ));
                    }
                }
            }
        }
        self.validate_requirement_cycles()
    }

    #[allow(clippy::too_many_lines)]
    fn validate_provider(&self, item: &RegisteredManifest) -> Result<(), RegistryError> {
        let manifest = &item.value;
        let identifier = manifest_id(manifest);
        let package = object_field(manifest, "package").map_err(|_| {
            RegistryError::Invalid(format!(
                "provider {identifier} package metadata is required"
            ))
        })?;
        let version = required_string(package, "version")?;
        let compatibility = required_string(package, "core_compatibility")?;
        if !satisfies(&self.core_version, compatibility)? {
            return invalid(format!(
                "provider {identifier} is incompatible with core {}: {compatibility}",
                self.core_version
            ));
        }

        let bootstraps = self
            .items
            .iter()
            .filter(|candidate| {
                candidate
                    .value
                    .get("provider_contract")
                    .and_then(Value::as_object)
                    .and_then(|contract| contract.get("package_id"))
                    .and_then(Value::as_str)
                    == Some(identifier)
            })
            .collect::<Vec<_>>();
        if bootstraps.len() != 1 {
            return invalid(format!(
                "provider {identifier} requires exactly one bootstrap contract"
            ));
        }
        let bootstrap = &bootstraps[0].value;
        let contract = object_field(bootstrap, "provider_contract")?;
        let mut allowed_targets = optional_string_array_field(bootstrap, "targets")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        allowed_targets.insert(manifest_id(bootstrap).to_owned());
        let provider_targets = string_array_field(manifest, "targets")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        if provider_targets.is_empty() || !provider_targets.is_subset(&allowed_targets) {
            return invalid(format!(
                "provider {identifier} targets are outside its bootstrap contract"
            ));
        }
        let package_range = required_string(contract, "package_compatibility")?;
        if !satisfies(version, package_range)? {
            return invalid(format!(
                "provider {identifier} version {version} is outside {package_range}"
            ));
        }

        let required = required_string_array(contract, "required_capabilities")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        let optional = required_string_array(contract, "optional_capabilities")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        let advisory = required_string_array(contract, "advisory_capabilities")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        let mut provided = BTreeMap::new();
        for entry in capabilities(manifest)? {
            provided.insert(required_string(entry, "id")?.to_owned(), entry);
        }

        let invalid_reachability = provided
            .iter()
            .filter_map(|(capability_id, entry)| {
                (!matches!(
                    string_field(entry, "reachability"),
                    Some("recipe" | "manual-only")
                ))
                .then_some(capability_id.clone())
            })
            .collect::<Vec<_>>();
        if !invalid_reachability.is_empty() {
            return invalid(format!(
                "provider {identifier} capabilities lack reachability: {}",
                invalid_reachability.join(", ")
            ));
        }
        let manual_entries = provided
            .iter()
            .filter_map(|(capability_id, entry)| {
                (string_field(entry, "reachability") == Some("manual-only"))
                    .then_some(capability_id.clone())
            })
            .collect::<BTreeSet<_>>();
        let automatic = automatic_recipe_capabilities(&provider_targets);
        let forged = provided
            .iter()
            .filter_map(|(capability_id, entry)| {
                (string_field(entry, "reachability") == Some("recipe")
                    && !automatic.contains(capability_id))
                .then_some(capability_id.clone())
            })
            .collect::<Vec<_>>();
        if !forged.is_empty() {
            return invalid(format!(
                "provider {identifier} capabilities are not reachable from a recipe: {}",
                forged.join(", ")
            ));
        }

        let manual_declared = string_array_field(manifest, "manual_only_capabilities")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        if manual_declared != manual_entries {
            return invalid(format!(
                "provider {identifier} manual-only capability list is inconsistent"
            ));
        }
        let manual_metadata = object_field(manifest, "manual_only_metadata")?;
        if manual_metadata.keys().collect::<BTreeSet<_>>()
            != manual_entries.iter().collect::<BTreeSet<_>>()
        {
            return invalid(format!(
                "provider {identifier} manual-only metadata is inconsistent"
            ));
        }
        for (capability_id, metadata) in manual_metadata {
            let metadata = metadata.as_object().ok_or_else(|| {
                RegistryError::Invalid(format!(
                    "provider {identifier} manual-only metadata is invalid: {capability_id}"
                ))
            })?;
            if metadata.keys().map(String::as_str).collect::<BTreeSet<_>>()
                != BTreeSet::from(["entrypoint", "reason", "review_by"])
            {
                return invalid(format!(
                    "provider {identifier} manual-only metadata is invalid: {capability_id}"
                ));
            }
            let binding = normalize_binding(
                object_field(manifest, "bindings")?
                    .get(capability_id)
                    .ok_or_else(|| {
                        RegistryError::Invalid(format!(
                            "provider {identifier} manual-only metadata is invalid: {capability_id}"
                        ))
                    })?,
            )?;
            let valid = string_field(metadata, "entrypoint") == string_field(&binding, "name")
                && string_field(metadata, "reason").is_some_and(|reason| !reason.trim().is_empty())
                && string_field(metadata, "review_by").is_some_and(valid_date_shape);
            if !valid {
                return invalid(format!(
                    "provider {identifier} manual-only metadata is invalid: {capability_id}"
                ));
            }
        }

        let provided_ids = provided.keys().cloned().collect::<BTreeSet<_>>();
        let missing = required
            .difference(&provided_ids)
            .cloned()
            .collect::<Vec<_>>();
        if !missing.is_empty() {
            return invalid(format!(
                "provider {identifier} is missing required capabilities: {}",
                missing.join(", ")
            ));
        }
        let declared = required
            .union(&optional)
            .cloned()
            .collect::<BTreeSet<_>>()
            .union(&advisory)
            .cloned()
            .collect::<BTreeSet<_>>();
        let undeclared = provided_ids
            .difference(&declared)
            .cloned()
            .collect::<Vec<_>>();
        if !undeclared.is_empty() {
            return invalid(format!(
                "provider {identifier} has undeclared capabilities: {}",
                undeclared.join(", ")
            ));
        }

        let allowed_permissions = required_string_array(contract, "allowed_permission_profiles")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        let allowed_effects = required_string_array(contract, "allowed_side_effects")?
            .into_iter()
            .collect::<BTreeSet<_>>();
        let capability_permissions = object_field(contract, "capability_permissions")?;
        let capability_effects = object_field(contract, "capability_side_effects")?;
        for (capability_id, entry) in provided {
            let normalized = normalized_contract(manifest, entry)?;
            let permission = required_string(
                normalized
                    .as_object()
                    .expect("normalized contract is object"),
                "permission_profile",
            )?;
            if !allowed_permissions.contains(permission) {
                return invalid(format!(
                    "provider {identifier} expands permission for {capability_id}"
                ));
            }
            if capability_permissions
                .get(&capability_id)
                .and_then(Value::as_str)
                != Some(permission)
            {
                return invalid(format!(
                    "provider {identifier} expands capability permission for {capability_id}"
                ));
            }
            let effects = string_array_field(&normalized, "side_effects")?
                .into_iter()
                .collect::<BTreeSet<_>>();
            if !effects.is_subset(&allowed_effects) {
                return invalid(format!(
                    "provider {identifier} expands side effects for {capability_id}"
                ));
            }
            let capability_allowed = capability_effects
                .get(&capability_id)
                .ok_or_else(|| {
                    RegistryError::Invalid(format!(
                        "provider {identifier} expands capability side effects for {capability_id}"
                    ))
                })
                .and_then(|value| {
                    nonempty_string_array(value, "capability_side_effects")
                        .map(|items| items.into_iter().collect::<BTreeSet<_>>())
                })?;
            if !effects.is_subset(&capability_allowed) {
                return invalid(format!(
                    "provider {identifier} expands capability side effects for {capability_id}"
                ));
            }
        }
        Ok(())
    }

    fn validate_requirement_cycles(&self) -> Result<(), RegistryError> {
        let mut graph = BTreeMap::<String, BTreeSet<String>>::new();
        for item in &self.items {
            let required = optional_string_array_field(&item.value, "requires")?
                .into_iter()
                .collect::<BTreeSet<_>>();
            for capability in capabilities(&item.value)? {
                graph
                    .entry(required_string(capability, "id")?.to_owned())
                    .or_default()
                    .extend(required.clone());
            }
        }
        validate_capability_graph(&graph)
    }
}

/// Parse a numeric `SemVer` core with an optional patch component.
///
/// # Errors
/// Returns an error for unsupported version syntax.
pub fn parse_version(value: &str) -> Result<[String; 3], RegistryError> {
    let parts = value.split('.').collect::<Vec<_>>();
    if !(2..=3).contains(&parts.len())
        || parts
            .iter()
            .any(|part| part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()))
        || parts
            .iter()
            .any(|part| part.len() > 1 && part.starts_with('0'))
    {
        return invalid(format!("unsupported version: {value:?}"));
    }
    Ok([
        parts[0].to_owned(),
        parts[1].to_owned(),
        parts.get(2).unwrap_or(&"0").to_string(),
    ])
}

/// Evaluate the Python registry's space-separated numeric version range.
///
/// # Errors
/// Returns an error for an empty or unsupported constraint.
pub fn satisfies(version: &str, expression: &str) -> Result<bool, RegistryError> {
    let actual = parse_version(version)?;
    if expression.trim().is_empty() {
        return invalid("compatibility range must be a non-empty string");
    }
    for raw in expression.split_whitespace() {
        let (operator, expected_text) = [">=", "<=", "==", ">", "<"]
            .into_iter()
            .find_map(|operator| raw.strip_prefix(operator).map(|value| (operator, value)))
            .ok_or_else(|| {
                RegistryError::Invalid(format!("unsupported compatibility constraint: {raw:?}"))
            })?;
        let expected = parse_version(expected_text)?;
        let ordering = compare_versions(&actual, &expected);
        let passed = match operator {
            ">=" => !ordering.is_lt(),
            "<=" => !ordering.is_gt(),
            ">" => ordering.is_gt(),
            "<" => ordering.is_lt(),
            "==" => ordering.is_eq(),
            _ => unreachable!(),
        };
        if !passed {
            return Ok(false);
        }
    }
    Ok(true)
}

fn compare_versions(left: &[String; 3], right: &[String; 3]) -> std::cmp::Ordering {
    for (left, right) in left.iter().zip(right) {
        let ordering = left.len().cmp(&right.len()).then_with(|| left.cmp(right));
        if !ordering.is_eq() {
            return ordering;
        }
    }
    std::cmp::Ordering::Equal
}

fn collect_manifest_paths(
    root: &Path,
    paths: &mut BTreeSet<PathBuf>,
    entry_count: &mut usize,
) -> Result<(), RegistryError> {
    let mut stack = vec![(root.to_path_buf(), 0_usize)];
    while let Some((directory, depth)) = stack.pop() {
        if depth > MAX_MANIFEST_DIRECTORY_DEPTH {
            return invalid(format!(
                "manifest collection exceeds maximum directory depth of {MAX_MANIFEST_DIRECTORY_DEPTH}"
            ));
        }
        for entry in std::fs::read_dir(directory)? {
            let entry = entry?;
            *entry_count += 1;
            if *entry_count > MAX_MANIFEST_DIRECTORY_ENTRIES {
                return invalid(format!(
                    "manifest collection exceeds maximum of {MAX_MANIFEST_DIRECTORY_ENTRIES} directory entries"
                ));
            }
            let file_type = entry.file_type()?;
            let path = entry.path();
            let is_manifest = path.file_name().is_some_and(|name| name == "manifest.json");
            if file_type.is_symlink() {
                if is_manifest {
                    let resolved = std::fs::canonicalize(path)?;
                    if resolved.is_file() {
                        paths.insert(resolved);
                    } else {
                        return invalid("manifest path named manifest.json must resolve to a file");
                    }
                }
                continue;
            }
            if file_type.is_dir() {
                if is_manifest {
                    return invalid("manifest path named manifest.json must be a file");
                }
                stack.push((path, depth + 1));
            } else if file_type.is_file() && is_manifest {
                paths.insert(std::fs::canonicalize(path)?);
            }
            if paths.len() > MAX_MANIFESTS {
                return invalid(format!(
                    "manifest collection exceeds maximum of {MAX_MANIFESTS} manifests"
                ));
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn validate_manifest(value: &Value) -> Result<(), RegistryError> {
    let object = value
        .as_object()
        .ok_or_else(|| RegistryError::Invalid("plugin-manifest must be an object".to_owned()))?;
    require_fields(
        object,
        &["schema_version", "id", "kind", "detection", "capabilities"],
        "plugin-manifest",
    )?;
    if required_string(object, "schema_version")? != "1.0" {
        return invalid("unsupported schema_version");
    }
    required_string(object, "id")?;
    if !matches!(
        required_string(object, "kind")?,
        "core" | "platform" | "stack" | "discipline" | "adapter" | "runtime-config"
    ) {
        return invalid("plugin-manifest kind is invalid");
    }
    if let Some(status) = string_field(value, "implementation_status") {
        if !matches!(status, "implemented" | "bootstrap-only") {
            return invalid("plugin-manifest implementation_status is invalid");
        }
        if required_string(object, "kind")? != "platform" {
            return invalid(
                "plugin-manifest implementation_status is only valid for platform packages",
            );
        }
    } else if object.contains_key("implementation_status") {
        return invalid("plugin-manifest implementation_status is invalid");
    }
    let detection = object_field(value, "detection")?;
    require_fields(
        detection,
        &["strong", "medium", "weak"],
        "plugin-manifest.detection",
    )?;
    for field in ["strong", "medium", "weak"] {
        string_array(
            detection
                .get(field)
                .ok_or_else(|| RegistryError::Invalid(format!("missing {field}")))?,
            field,
        )?;
    }
    let entries = capabilities(value)?;
    let capability_ids = entries
        .iter()
        .map(|entry| required_string(entry, "id"))
        .collect::<Result<Vec<_>, _>>()?;
    if capability_ids.iter().collect::<BTreeSet<_>>().len() != capability_ids.len() {
        return invalid("plugin-manifest capability ids must be present and unique");
    }
    let role = match object.get("role") {
        None => "builtin",
        Some(Value::String(role)) => role.as_str(),
        Some(_) => return invalid("plugin-manifest role is invalid"),
    };
    if !matches!(role, "builtin" | "bootstrap" | "provider") {
        return invalid("plugin-manifest role is invalid");
    }
    let bindings = value
        .get("bindings")
        .map(|item| {
            item.as_object().ok_or_else(|| {
                RegistryError::Invalid("plugin-manifest bindings must be an object".to_owned())
            })
        })
        .transpose()?
        .cloned()
        .unwrap_or_default();
    let implementation_status = string_field(value, "implementation_status");
    if implementation_status == Some("bootstrap-only")
        && (role != "bootstrap"
            || !entries.is_empty()
            || !bindings.is_empty()
            || object.contains_key("installation"))
    {
        return invalid(
            "bootstrap-only platform must use a bootstrap role without capabilities, bindings, or installation",
        );
    }
    if implementation_status == Some("implemented") && !object.contains_key("installation") {
        return invalid("implemented platform must provide an installation contract");
    }
    if let Some(installation) = object.get("installation") {
        let installation = installation.as_object().ok_or_else(|| {
            RegistryError::Invalid("plugin-manifest installation must be an object".to_owned())
        })?;
        require_fields(
            installation,
            &["asset_roots", "instruction_fragments", "skill_roots"],
            "plugin-manifest.installation",
        )?;
        for field in ["asset_roots", "skill_roots"] {
            unique_string_array(
                installation
                    .get(field)
                    .ok_or_else(|| RegistryError::Invalid(format!("missing {field}")))?,
                field,
            )?;
        }
        let fragments = installation
            .get("instruction_fragments")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                RegistryError::Invalid(
                    "plugin-manifest.installation instruction_fragments must be an array"
                        .to_owned(),
                )
            })?;
        let mut fragment_ids = BTreeSet::new();
        for fragment in fragments {
            let fragment = fragment.as_object().ok_or_else(|| {
                RegistryError::Invalid(
                    "plugin-manifest.installation instruction fragment must be an object"
                        .to_owned(),
                )
            })?;
            require_fields(
                fragment,
                &["id", "path", "scope", "order", "merge_strategy"],
                "plugin-manifest.installation.instruction-fragment",
            )?;
            let identifier = required_string(fragment, "id")?;
            required_string(fragment, "path")?;
            required_string(fragment, "scope")?;
            if !fragment.get("order").is_some_and(Value::is_i64)
                || !matches!(
                    string_field(fragment, "merge_strategy"),
                    Some("append" | "locked")
                )
            {
                return invalid("plugin-manifest.installation instruction fragment is invalid");
            }
            if !fragment_ids.insert(identifier) {
                return invalid(
                    "plugin-manifest.installation instruction fragment ids must be unique",
                );
            }
        }
        if installation.contains_key("provider_manifest")
            && string_field(installation, "provider_manifest").is_none()
        {
            return invalid("plugin-manifest.installation provider_manifest is invalid");
        }
        required_string(object, "version").map_err(|_| {
            RegistryError::Invalid("installable plugin-manifest version is required".to_owned())
        })?;
    }
    if role == "bootstrap" {
        validate_provider_contract(value)?;
    }
    if role == "provider" {
        let package = object_field(value, "package").map_err(|_| {
            RegistryError::Invalid("provider manifest package metadata is required".to_owned())
        })?;
        require_fields(
            package,
            &["version", "core_compatibility"],
            "plugin-manifest.package",
        )?;
    }
    validate_package_dependencies(value)
}

fn validate_provider_contract(value: &Value) -> Result<(), RegistryError> {
    let contract = object_field(value, "provider_contract").map_err(|_| {
        RegistryError::Invalid("bootstrap manifest provider_contract is required".to_owned())
    })?;
    let fields = [
        "package_id",
        "package_compatibility",
        "required_capabilities",
        "optional_capabilities",
        "advisory_capabilities",
        "allowed_permission_profiles",
        "allowed_side_effects",
        "capability_permissions",
        "capability_side_effects",
    ];
    require_fields(contract, &fields, "plugin-manifest.provider_contract")?;
    required_string(contract, "package_id")?;
    required_string(contract, "package_compatibility")?;
    let group_names = [
        "required_capabilities",
        "optional_capabilities",
        "advisory_capabilities",
    ];
    let groups = group_names
        .iter()
        .map(|field| {
            required_string_array(contract, field)
                .map(|items| items.into_iter().collect::<BTreeSet<_>>())
        })
        .collect::<Result<Vec<_>, _>>()?;
    if !groups[0].is_disjoint(&groups[1])
        || !groups[0].is_disjoint(&groups[2])
        || !groups[1].is_disjoint(&groups[2])
    {
        return invalid("plugin-manifest.provider_contract capability groups must not overlap");
    }
    let declared = groups.into_iter().flatten().collect::<BTreeSet<_>>();
    required_string_array(contract, "allowed_permission_profiles")?;
    required_string_array(contract, "allowed_side_effects")?;
    let permissions = object_field(contract, "capability_permissions")?;
    let effects = object_field(contract, "capability_side_effects")?;
    if permissions.keys().collect::<BTreeSet<_>>() != declared.iter().collect::<BTreeSet<_>>() {
        return invalid(
            "plugin-manifest.provider_contract capability_permissions must cover every declared capability",
        );
    }
    if effects.keys().collect::<BTreeSet<_>>() != declared.iter().collect::<BTreeSet<_>>() {
        return invalid(
            "plugin-manifest.provider_contract capability_side_effects must cover every declared capability",
        );
    }
    for value in permissions.values() {
        if value.as_str().is_none_or(str::is_empty) {
            return invalid(
                "plugin-manifest.provider_contract capability_permissions values must be strings",
            );
        }
    }
    for value in effects.values() {
        nonempty_string_array(value, "capability_side_effects")?;
    }
    Ok(())
}

fn validate_package_dependencies(value: &Value) -> Result<(), RegistryError> {
    let dependencies = value
        .get("package_requires")
        .map(|item| {
            item.as_array().ok_or_else(|| {
                RegistryError::Invalid(
                    "plugin-manifest package_requires must be an array".to_owned(),
                )
            })
        })
        .transpose()?
        .cloned()
        .unwrap_or_default();
    let mut ids = BTreeSet::new();
    for dependency in dependencies {
        let dependency = dependency.as_object().ok_or_else(|| {
            RegistryError::Invalid(
                "plugin-manifest package dependency must be an object".to_owned(),
            )
        })?;
        require_fields(
            dependency,
            &["id", "version", "requirement", "required_capabilities"],
            "plugin-manifest.package-dependency",
        )?;
        let identifier = required_string(dependency, "id")?.to_owned();
        required_string(dependency, "version")?;
        if !matches!(
            string_field(dependency, "requirement"),
            Some("required" | "optional")
        ) || required_string_array(dependency, "required_capabilities")?.is_empty()
        {
            return invalid("plugin-manifest package dependency is invalid");
        }
        if !ids.insert(identifier) {
            return invalid("plugin-manifest package dependency ids must be unique");
        }
    }
    Ok(())
}

fn normalized_contract(manifest: &Value, entry: &Value) -> Result<Value, RegistryError> {
    let capability_id = required_string(
        entry
            .as_object()
            .ok_or_else(|| RegistryError::Invalid("capability must be an object".to_owned()))?,
        "id",
    )?;
    let prefix = capability_id
        .split_once('.')
        .map_or(capability_id, |pair| pair.0);
    let permission_key = match prefix {
        "implementation" => "implementation",
        "verification" => "verification",
        _ => "detection",
    };
    let default_permission = Value::String("repository-read-only".to_owned());
    let permission = entry
        .get("permission_profile")
        .filter(|value| python_truthy(value))
        .cloned()
        .map_or_else(
            || {
                manifest.get("permissions").map_or_else(
                    || Ok(default_permission.clone()),
                    |permissions| {
                        permissions
                            .as_object()
                            .ok_or_else(|| {
                                RegistryError::Invalid(
                                    "plugin-manifest permissions must be an object".to_owned(),
                                )
                            })
                            .map(|permissions| {
                                permissions
                                    .get(permission_key)
                                    .cloned()
                                    .unwrap_or_else(|| default_permission.clone())
                            })
                    },
                )
            },
            Ok,
        )?;
    let side_effects = entry
        .get("side_effects")
        .filter(|value| !value.is_null())
        .cloned()
        .unwrap_or_else(|| {
            json!(match prefix {
                "implementation" => vec!["project-files"],
                "verification" => vec!["validation-artifacts"],
                _ => Vec::new(),
            })
        });
    let concurrency_keys = entry
        .get("concurrency_keys")
        .filter(|value| !value.is_null())
        .cloned()
        .unwrap_or_else(|| {
            json!(match prefix {
                "implementation" => vec![format!("repository-write:{}", manifest_id(manifest))],
                "verification" => vec![format!("build-queue:{}", manifest_id(manifest))],
                _ => Vec::new(),
            })
        });
    let contract = json!({
        "concurrency_keys": concurrency_keys,
        "failure_codes": entry.get("failure_codes").cloned().unwrap_or_else(|| json!([
            "tool-unavailable", "environment-blocked", "contract-violation"
        ])),
        "id": capability_id,
        "idempotent": entry.get("idempotent").cloned().unwrap_or_else(|| json!(prefix != "implementation")),
        "input_schema": entry.get("input_schema").cloned().unwrap_or_else(|| json!("generic-request-v1")),
        "output_schema": entry.get("output_schema").cloned().unwrap_or_else(|| json!("generic-result-v1")),
        "permission_profile": permission,
        "schema_version": "1.0",
        "side_effects": side_effects,
        "version": entry.get("version").cloned().unwrap_or_else(|| json!("1.0")),
    });
    validate_capability_contract(&contract)?;
    Ok(contract)
}

fn validate_capability_contract(contract: &Value) -> Result<(), RegistryError> {
    let object = contract.as_object().ok_or_else(|| {
        RegistryError::Invalid("capability-contract must be an object".to_owned())
    })?;
    for field in [
        "id",
        "version",
        "input_schema",
        "output_schema",
        "permission_profile",
    ] {
        if !object.get(field).is_some_and(Value::is_string) {
            return invalid(format!("capability-contract {field} must be a string"));
        }
    }
    if string_field(object, "id").is_none_or(|identifier| identifier.chars().count() < 3) {
        return invalid("capability-contract id is invalid");
    }
    if !object.get("idempotent").is_some_and(Value::is_boolean) {
        return invalid("capability-contract idempotent must be a boolean");
    }
    for field in ["side_effects", "concurrency_keys", "failure_codes"] {
        let values = object.get(field).and_then(Value::as_array).ok_or_else(|| {
            RegistryError::Invalid(format!("capability-contract {field} must be an array"))
        })?;
        if values.iter().any(|value| !value.is_string()) {
            return invalid(format!("capability-contract {field} must contain strings"));
        }
    }
    Ok(())
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn validate_capability_graph(
    graph: &BTreeMap<String, BTreeSet<String>>,
) -> Result<(), RegistryError> {
    if graph.len() > MAX_CAPABILITY_NODES {
        return invalid(format!(
            "manifest capability graph exceeds maximum of {MAX_CAPABILITY_NODES} nodes"
        ));
    }
    let edge_count = graph.values().map(BTreeSet::len).sum::<usize>();
    if edge_count > MAX_CAPABILITY_EDGES {
        return invalid(format!(
            "manifest capability graph exceeds maximum of {MAX_CAPABILITY_EDGES} edges"
        ));
    }

    let mut states = BTreeMap::<String, u8>::new();
    let mut path = Vec::<String>::new();
    for capability in graph.keys() {
        if states.get(capability) == Some(&2) {
            continue;
        }
        let mut stack = vec![(capability.clone(), false)];
        while let Some((current, exiting)) = stack.pop() {
            if exiting {
                states.insert(current, 2);
                path.pop();
                continue;
            }
            match states.get(&current).copied().unwrap_or(0) {
                2 => continue,
                1 => {
                    let start = path.iter().position(|item| item == &current).unwrap_or(0);
                    let mut cycle = path[start..].to_vec();
                    cycle.push(current);
                    return invalid(format!(
                        "manifest capability dependency cycle: {}",
                        cycle.join(" -> ")
                    ));
                }
                _ => {}
            }
            states.insert(current.clone(), 1);
            path.push(current.clone());
            stack.push((current.clone(), true));
            if let Some(dependencies) = graph.get(&current) {
                for dependency in dependencies.iter().rev() {
                    stack.push((dependency.clone(), false));
                }
            }
        }
    }
    Ok(())
}

fn normalize_binding(value: &Value) -> Result<Value, RegistryError> {
    if let Some(name) = value.as_str().filter(|name| !name.is_empty()) {
        return Ok(json!({"kind": "skill", "name": name}));
    }
    let object = value
        .as_object()
        .ok_or_else(|| RegistryError::Invalid("binding must be a string or object".to_owned()))?;
    let unknown = object
        .keys()
        .filter(|key| !matches!(key.as_str(), "kind" | "name" | "mode"))
        .cloned()
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        return invalid(format!(
            "binding has unknown fields: {}",
            unknown.join(", ")
        ));
    }
    if !matches!(
        string_field(object, "kind"),
        Some("skill" | "agent" | "script" | "tool")
    ) {
        return invalid("binding kind is invalid");
    }
    required_string(object, "name")
        .map_err(|_| RegistryError::Invalid("binding name is invalid".to_owned()))?;
    if object.contains_key("mode") && string_field(object, "mode").is_none() {
        return invalid("binding mode is invalid");
    }
    Ok(Value::Object(object.clone()))
}

fn supports_platform(manifest: &Value, platform: &str) -> bool {
    if string_field(manifest, "role") != Some("provider") {
        return true;
    }
    let targets = optional_string_array_field(manifest, "targets").unwrap_or_default();
    if platform == "*" {
        targets.is_empty()
    } else {
        targets.iter().any(|target| target == platform)
    }
}

/// Enumerate every capability reachable through automatic recipes.
#[must_use]
pub fn automatic_recipe_capabilities(targets: &BTreeSet<String>) -> BTreeSet<String> {
    let mut result = [
        "core.intent-lock",
        "qa.contract.validate",
        "qa.coverage.compile",
        "qa.plan.compile",
        "qa.report.aggregate",
        "report.apple.delivery",
        "reporting.delivery",
        "review.independent",
        "workflow.analysis",
        "workflow.orchestration",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect::<BTreeSet<_>>();
    let disciplines = [
        "automation",
        "build",
        "debug",
        "design",
        "documentation",
        "performance",
    ];
    let task_types = [
        "code-small",
        "doc-only",
        "investigation",
        "qa-only",
        "review-only",
    ];
    for target in targets {
        result.insert(format!("review.{target}.static"));
        for mask in 0_u8..(1 << disciplines.len()) {
            let selected = disciplines
                .iter()
                .enumerate()
                .filter_map(|(index, discipline)| (mask & (1 << index) != 0).then_some(*discipline))
                .collect::<BTreeSet<_>>();
            for task_type in task_types {
                result.extend(required_platform_capabilities(target, task_type, &selected));
            }
        }
    }
    result
}

/// Return the ordered capability recipe for one platform task.
#[must_use]
pub fn required_platform_capabilities(
    platform: &str,
    task_type: &str,
    disciplines: &BTreeSet<&str>,
) -> Vec<String> {
    if task_type == "review-only" {
        return Vec::new();
    }
    if task_type == "doc-only" {
        let mut capabilities = vec![format!("analysis.{platform}")];
        if disciplines.contains("documentation") {
            capabilities.push("documentation.html".to_owned());
        }
        return capabilities;
    }
    if task_type == "investigation" {
        if platform == "apple" {
            for (discipline, capability) in [
                ("debug", "debugging.apple.analysis"),
                ("performance", "performance.apple"),
                ("automation", "automation.apple"),
            ] {
                if disciplines.contains(discipline) {
                    return vec![capability.to_owned()];
                }
            }
        }
        return vec![format!("analysis.{platform}")];
    }
    if task_type == "qa-only" {
        let mut capabilities = vec![format!("verification.{platform}.affected-tests")];
        if platform == "apple" {
            capabilities.push("verification.apple.auto".to_owned());
        }
        return capabilities;
    }
    let mut capabilities = if platform == "apple" && disciplines.contains("build") {
        vec![
            "build.apple.configuration".to_owned(),
            "verification.apple.affected-tests".to_owned(),
        ]
    } else if platform == "apple" && disciplines.contains("debug") {
        vec![
            "debugging.apple.execute".to_owned(),
            "verification.apple.affected-tests".to_owned(),
        ]
    } else if platform == "apple" && disciplines.contains("performance") {
        vec!["performance.apple".to_owned()]
    } else if platform == "apple" && disciplines.contains("automation") {
        vec!["automation.apple".to_owned()]
    } else {
        vec![
            format!("implementation.{platform}"),
            format!("verification.{platform}.affected-tests"),
        ]
    };
    if disciplines.contains("design") {
        let mut design = if platform == "apple" {
            vec!["design.apple.source".to_owned()]
        } else {
            Vec::new()
        };
        design.extend(
            [
                "design.evidence.normalize",
                "design.system",
                "design.ir.compile",
                "design.registry.resolve",
                "design.packet.slice",
            ]
            .into_iter()
            .map(str::to_owned),
        );
        if platform == "apple" {
            design.push("design.apple.binding".to_owned());
        }
        design.extend(capabilities);
        capabilities = design;
    }
    if platform == "apple"
        && let Some(index) = capabilities
            .iter()
            .position(|capability| capability == "verification.apple.affected-tests")
    {
        capabilities.insert(index + 1, "verification.apple.auto".to_owned());
    }
    capabilities
}

fn valid_date_shape(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 4 | 7) || byte.is_ascii_digit())
}

fn manifest_id(value: &Value) -> &str {
    string_field(value, "id").unwrap_or("")
}

fn capability_entry<'a>(
    manifest: &'a Value,
    capability_id: &str,
) -> Result<&'a Value, RegistryError> {
    capabilities(manifest)?
        .iter()
        .find(|entry| string_field(entry, "id") == Some(capability_id))
        .ok_or_else(|| RegistryError::Invalid(format!("capability is missing: {capability_id}")))
}

fn capabilities(value: &Value) -> Result<&Vec<Value>, RegistryError> {
    value
        .get("capabilities")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            RegistryError::Invalid("plugin-manifest capabilities must be an array".to_owned())
        })
}

fn object_field<'a, O>(value: &'a O, field: &str) -> Result<&'a Map<String, Value>, RegistryError>
where
    O: ObjectAccess + ?Sized,
{
    value
        .get_value(field)
        .and_then(Value::as_object)
        .ok_or_else(|| RegistryError::Invalid(format!("{field} must be an object")))
}

fn string_field<'a, O>(value: &'a O, field: &str) -> Option<&'a str>
where
    O: ObjectAccess + ?Sized,
{
    value.get_value(field).and_then(Value::as_str)
}

trait ObjectAccess {
    fn get_value(&self, field: &str) -> Option<&Value>;
}

impl ObjectAccess for Value {
    fn get_value(&self, field: &str) -> Option<&Value> {
        self.get(field)
    }
}

impl ObjectAccess for Map<String, Value> {
    fn get_value(&self, field: &str) -> Option<&Value> {
        self.get(field)
    }
}

impl<T> ObjectAccess for &T
where
    T: ObjectAccess + ?Sized,
{
    fn get_value(&self, field: &str) -> Option<&Value> {
        (*self).get_value(field)
    }
}

fn required_string<'a, O>(object: &'a O, field: &str) -> Result<&'a str, RegistryError>
where
    O: ObjectAccess + ?Sized,
{
    string_field(object, field)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| RegistryError::Invalid(format!("{field} must be a non-empty string")))
}

fn string_array_field(value: &Value, field: &str) -> Result<Vec<String>, RegistryError> {
    string_array(
        value
            .get(field)
            .ok_or_else(|| RegistryError::Invalid(format!("{field} is required")))?,
        field,
    )
}

fn optional_string_array_field(value: &Value, field: &str) -> Result<Vec<String>, RegistryError> {
    value
        .get(field)
        .map_or_else(|| Ok(Vec::new()), |items| string_array(items, field))
}

fn required_string_array(
    object: &Map<String, Value>,
    field: &str,
) -> Result<Vec<String>, RegistryError> {
    unique_string_array(
        object
            .get(field)
            .ok_or_else(|| RegistryError::Invalid(format!("{field} is required")))?,
        field,
    )
}

fn unique_string_array(value: &Value, label: &str) -> Result<Vec<String>, RegistryError> {
    let strings = nonempty_string_array(value, label)?;
    if strings.iter().collect::<BTreeSet<_>>().len() != strings.len() {
        return invalid(format!("{label} must contain unique strings"));
    }
    Ok(strings)
}

fn nonempty_string_array(value: &Value, label: &str) -> Result<Vec<String>, RegistryError> {
    let items = value
        .as_array()
        .ok_or_else(|| RegistryError::Invalid(format!("{label} must be an array")))?;
    let strings = items
        .iter()
        .map(|item| {
            item.as_str()
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| {
                    RegistryError::Invalid(format!("{label} must contain non-empty strings"))
                })
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(strings)
}

fn string_array(value: &Value, label: &str) -> Result<Vec<String>, RegistryError> {
    value
        .as_array()
        .ok_or_else(|| RegistryError::Invalid(format!("{label} must be an array")))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| RegistryError::Invalid(format!("{label} must contain strings")))
        })
        .collect()
}

fn require_fields(
    object: &Map<String, Value>,
    fields: &[&str],
    label: &str,
) -> Result<(), RegistryError> {
    let missing = fields
        .iter()
        .filter(|field| !object.contains_key(**field))
        .copied()
        .collect::<Vec<_>>();
    if missing.is_empty() {
        Ok(())
    } else {
        invalid(format!("{label} missing fields: {}", missing.join(", ")))
    }
}

fn invalid<T>(message: impl Into<String>) -> Result<T, RegistryError> {
    Err(RegistryError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::{
        CORE_VERSION, MAX_CAPABILITY_NODES, ManifestRegistry, RegisteredManifest,
        normalize_binding, parse_version, satisfies, validate_capability_graph,
    };
    use agent_contracts::canonical_sha256;
    use serde_json::json;
    use std::collections::{BTreeMap, BTreeSet};
    use std::path::PathBuf;

    #[test]
    fn version_range_matches_python_contract() {
        assert_eq!(
            parse_version("0.2").unwrap(),
            ["0".to_owned(), "2".to_owned(), "0".to_owned()]
        );
        assert_eq!(
            parse_version("10.20.30").unwrap(),
            ["10".to_owned(), "20".to_owned(), "30".to_owned()]
        );
        assert!(parse_version("01.2.3").is_err());
        assert!(satisfies("0.2.0", ">=0.1.0 <0.3.0").unwrap());
        assert!(!satisfies("0.3.0", ">=0.1.0 <0.3.0").unwrap());
        assert!(satisfies("0.2.0", "==0.2").unwrap());
        assert!(satisfies("0.2.0", "<=0.2.0").unwrap());
        let huge = "999999999999999999999999999999.2.3";
        assert!(satisfies(huge, ">=999999999999999999999999999998.0").unwrap());
        assert!(satisfies(huge, "<1000000000000000000000000000000.0").unwrap());
        assert_eq!(CORE_VERSION, env!("CARGO_PKG_VERSION"));
    }

    #[test]
    fn capability_graph_limits_fail_without_recursive_descent() {
        let deep_graph = (0..10_000)
            .map(|index| {
                let dependencies = if index == 9_999 {
                    BTreeSet::new()
                } else {
                    BTreeSet::from([format!("capability.{}", index + 1)])
                };
                (format!("capability.{index}"), dependencies)
            })
            .collect::<BTreeMap<_, _>>();
        validate_capability_graph(&deep_graph).unwrap();

        let graph = (0..=MAX_CAPABILITY_NODES)
            .map(|index| (format!("capability.{index}"), BTreeSet::new()))
            .collect::<BTreeMap<_, _>>();
        assert!(
            validate_capability_graph(&graph)
                .unwrap_err()
                .to_string()
                .contains("maximum")
        );

        let graph = BTreeMap::from([
            (
                "capability.a".to_owned(),
                BTreeSet::from(["capability.b".to_owned()]),
            ),
            (
                "capability.b".to_owned(),
                BTreeSet::from(["capability.a".to_owned()]),
            ),
        ]);
        assert!(
            validate_capability_graph(&graph)
                .unwrap_err()
                .to_string()
                .contains("dependency cycle")
        );
    }

    #[test]
    fn binding_normalization_is_fail_closed() {
        assert_eq!(
            normalize_binding(&json!("fixture")).unwrap(),
            json!({"kind": "skill", "name": "fixture"})
        );
        assert!(normalize_binding(&json!({"kind": "shell", "name": "fixture"})).is_err());
        assert!(
            normalize_binding(&json!({"kind": "skill", "name": "fixture", "extra": true})).is_err()
        );
    }

    #[test]
    fn direct_registry_construction_revalidates_content_and_digest() {
        let value = json!({
            "bindings": {},
            "capabilities": [],
            "detection": {"medium": [], "strong": [], "weak": []},
            "id": "fixture",
            "kind": "adapter",
            "role": null,
            "schema_version": "1.0",
        });
        let invalid_role = RegisteredManifest {
            path: PathBuf::from("fixture/manifest.json"),
            digest: canonical_sha256(&value).unwrap(),
            value,
        };
        assert!(ManifestRegistry::new(vec![invalid_role], CORE_VERSION).is_err());

        let value = json!({
            "bindings": {},
            "capabilities": [],
            "detection": {"medium": [], "strong": [], "weak": []},
            "id": "fixture",
            "kind": "adapter",
            "schema_version": "1.0",
        });
        let forged_digest = RegisteredManifest {
            path: PathBuf::from("fixture/manifest.json"),
            digest: "0".repeat(64),
            value,
        };
        assert!(ManifestRegistry::new(vec![forged_digest], CORE_VERSION).is_err());
    }

    #[test]
    fn repository_registry_identity_and_resolution_are_frozen() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join("platforms");
        let registry =
            ManifestRegistry::from_directory(root, &BTreeSet::new(), CORE_VERSION).unwrap();
        assert_eq!(registry.manifests().len(), 17);
        assert_eq!(
            registry.digest().unwrap(),
            "3ab71c533e9fa63de1f1cc929099a79683a38fc035674f056e6e85371d706579"
        );
        let implementation = registry
            .resolve_binding("implementation.apple", Some("apple"))
            .unwrap()
            .unwrap();
        assert_eq!(implementation.provider_id, "ios-agent-skills");
        assert_eq!(
            implementation.binding,
            json!({
                "kind": "skill",
                "mode": "auto",
                "name": "ios-feature-implementation",
            })
        );
        assert!(registry.bootstrap_requirement("apple").unwrap().is_none());
        assert_eq!(
            registry.bootstrap_requirement("android").unwrap().unwrap()["provider"],
            "android-agent-skills"
        );
    }
}
