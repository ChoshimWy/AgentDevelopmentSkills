use crate::LifecycleError;
use agent_contracts::{canonical_sha256, load_json};
use agent_registry::{satisfies, validate_manifest_syntax};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

const PACKAGE_COLLECTIONS: [&str; 3] = ["disciplines", "stacks", "runtime-configs"];

#[derive(Debug, Clone)]
struct PackageCandidate {
    root: PathBuf,
    manifest: Value,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PackageDependency {
    consumer: String,
    provider: String,
    required_capabilities: Vec<String>,
    requirement: String,
    version: String,
}

impl PackageDependency {
    fn compatibility_projection(&self) -> Value {
        json!({
            "from": self.consumer,
            "required_capabilities": self.required_capabilities,
            "requirement": self.requirement,
            "to": self.provider,
            "version": self.version,
        })
    }
}

#[derive(Debug)]
struct VisitFrame {
    package_id: String,
    dependencies: Vec<PackageDependency>,
    next_dependency: usize,
}

/// Deterministic package closure selected from source package Manifests.
///
/// This is a typed migration boundary used by the future native Install Bundle
/// compiler. It deliberately carries source roots instead of defining another
/// persisted artifact schema.
#[derive(Debug, Clone)]
pub struct SourceInstallSelection {
    package_roots: Vec<(String, PathBuf)>,
    selected_platforms: Vec<String>,
    selected_disciplines: Vec<String>,
    selected_runtime_configs: Vec<String>,
    dependencies: Vec<PackageDependency>,
    selection_reasons: BTreeMap<String, Vec<String>>,
}

impl SourceInstallSelection {
    /// Package roots in deterministic provider-before-consumer order.
    #[must_use]
    pub fn package_roots(&self) -> &[(String, PathBuf)] {
        &self.package_roots
    }

    /// Selected installable platform IDs.
    #[must_use]
    pub fn selected_platforms(&self) -> &[String] {
        &self.selected_platforms
    }

    /// Selected discipline package IDs.
    #[must_use]
    pub fn selected_disciplines(&self) -> &[String] {
        &self.selected_disciplines
    }

    /// Selected runtime-config package IDs.
    #[must_use]
    pub fn selected_runtime_configs(&self) -> &[String] {
        &self.selected_runtime_configs
    }

    /// Emit the temporary compatibility projection used by differential tests.
    ///
    /// This projection is not a persisted contract and has no schema version.
    #[must_use]
    pub fn compatibility_projection(&self) -> Value {
        json!({
            "package_roots": self.package_roots.iter().map(|(identifier, root)| {
                json!({"id": identifier, "path": compatibility_path(root)})
            }).collect::<Vec<_>>(),
            "resolved_dependencies": self.dependencies.iter()
                .map(PackageDependency::compatibility_projection)
                .collect::<Vec<_>>(),
            "selected_disciplines": self.selected_disciplines,
            "selected_platforms": self.selected_platforms,
            "selected_runtime_configs": self.selected_runtime_configs,
            "selection_reasons": self.selection_reasons,
        })
    }
}

#[cfg(not(windows))]
fn compatibility_path(path: &Path) -> PathBuf {
    path.to_path_buf()
}

#[cfg(windows)]
fn compatibility_path(path: &Path) -> PathBuf {
    use std::ffi::OsString;
    use std::os::windows::ffi::{OsStrExt as _, OsStringExt as _};

    const VERBATIM: &[u16] = &[92, 92, 63, 92];
    const VERBATIM_UNC: &[u16] = &[92, 92, 63, 92, 85, 78, 67, 92];
    let encoded = path.as_os_str().encode_wide().collect::<Vec<_>>();
    let normalized = if encoded.starts_with(VERBATIM_UNC) {
        let mut result = vec![92_u16, 92];
        result.extend_from_slice(&encoded[VERBATIM_UNC.len()..]);
        Some(result)
    } else if encoded.starts_with(VERBATIM)
        && encoded
            .get(VERBATIM.len())
            .is_some_and(|unit| u8::try_from(*unit).is_ok_and(|byte| byte.is_ascii_alphabetic()))
        && encoded.get(VERBATIM.len() + 1) == Some(&58)
    {
        Some(encoded[VERBATIM.len()..].to_vec())
    } else {
        None
    };
    normalized.map_or_else(
        || path.to_path_buf(),
        |units| PathBuf::from(OsString::from_wide(&units)),
    )
}

/// Resolve source package selection, required/optional dependency closure, and
/// deterministic topological order without reading package assets or executing
/// package code.
///
/// The behavior mirrors the Python Install Bundle selection boundary:
/// `core` is always selected; platform selection is explicit; disciplines and
/// runtime configs may be selected without a platform; required dependencies
/// are pulled into the closure; optional dependencies only become edges when
/// independently selected.
///
/// # Errors
/// Returns a fail-closed error for unsafe package collections, invalid
/// selection, missing/incompatible dependencies, or dependency cycles.
pub fn resolve_source_install_selection(
    platform_root: impl AsRef<Path>,
    platforms: &[String],
    disciplines: &[String],
    runtime_configs: &[String],
    core_only: bool,
) -> Result<SourceInstallSelection, LifecycleError> {
    if core_only && !platforms.is_empty() {
        return invalid("--core-only cannot be combined with --platform");
    }
    if core_only && (!disciplines.is_empty() || !runtime_configs.is_empty()) {
        return invalid("--core-only cannot be combined with --discipline or --runtime-config");
    }

    let platform_root = std::fs::canonicalize(platform_root)?;
    if !platform_root.is_dir() {
        return invalid(format!(
            "platform root does not exist: {}",
            platform_root.display()
        ));
    }
    let catalog = load_package_catalog(&platform_root)?;
    let selected_platforms = if (!disciplines.is_empty() || !runtime_configs.is_empty())
        && platforms.is_empty()
        && !core_only
    {
        Vec::new()
    } else {
        resolve_platforms(&catalog, platforms, core_only)?
    };
    resolve_packages(&catalog, selected_platforms, disciplines, runtime_configs)
}

fn load_package_catalog(
    platform_root: &Path,
) -> Result<BTreeMap<String, PackageCandidate>, LifecycleError> {
    let mut collection_roots = BTreeSet::from([platform_root.to_path_buf()]);
    if platform_root
        .file_name()
        .is_some_and(|name| name == "platforms")
    {
        let parent = platform_root.parent().ok_or_else(|| {
            LifecycleError::Invalid("platforms collection has no parent".to_owned())
        })?;
        for name in PACKAGE_COLLECTIONS {
            let candidate = parent.join(name);
            let metadata = match candidate.symlink_metadata() {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            if metadata.file_type().is_symlink() {
                return invalid(format!(
                    "manifest package collection must not be a symlink: {name}"
                ));
            }
            if metadata.is_dir() {
                collection_roots.insert(std::fs::canonicalize(candidate)?);
            }
        }
    }

    let direct_manifests = collect_direct_manifest_paths(&collection_roots)?;
    let mut catalog = BTreeMap::new();
    for manifest_path in &direct_manifests {
        let manifest = load_json(manifest_path)?;
        validate_manifest_syntax(&manifest)?;
        if !manifest.get("installation").is_some_and(Value::is_object) {
            continue;
        }
        let package_root = manifest_path.parent().ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "package manifest has no parent: {}",
                manifest_path.display()
            ))
        })?;
        if package_root
            .parent()
            .is_none_or(|parent| !collection_roots.contains(parent))
        {
            continue;
        }
        let package_id = manifest_string(&manifest, "id")?.to_owned();
        if package_id.is_empty()
            || !package_id
                .chars()
                .all(|character| character.is_alphanumeric() || "._-".contains(character))
        {
            return invalid(format!("platform package id is unsafe: {package_id}"));
        }
        let directory_name = package_root
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| {
                LifecycleError::Invalid(format!(
                    "package directory name is not valid UTF-8: {}",
                    package_root.display()
                ))
            })?;
        if directory_name != package_id {
            return invalid(format!(
                "package directory and manifest id differ: {directory_name}"
            ));
        }
        let candidate = PackageCandidate {
            root: package_root.to_path_buf(),
            manifest,
        };
        if catalog.insert(package_id.clone(), candidate).is_some() {
            return invalid(format!("package id is ambiguous: {package_id}"));
        }
    }
    if collect_direct_manifest_paths(&collection_roots)? != direct_manifests {
        return invalid("source package catalog changed while resolving selection");
    }
    for (package_id, candidate) in &catalog {
        let current = load_json(candidate.root.join("manifest.json"))?;
        if canonical_sha256(&current)? != canonical_sha256(&candidate.manifest)? {
            return invalid(format!(
                "package manifest changed while resolving selection: {package_id}"
            ));
        }
    }
    Ok(catalog)
}

fn collect_direct_manifest_paths(
    collection_roots: &BTreeSet<PathBuf>,
) -> Result<BTreeSet<PathBuf>, LifecycleError> {
    let mut manifests = BTreeSet::new();
    for collection_root in collection_roots {
        let mut entries = std::fs::read_dir(collection_root)?.collect::<Result<Vec<_>, _>>()?;
        entries.sort_by_key(std::fs::DirEntry::file_name);
        for entry in entries {
            let candidate = entry.path();
            let candidate_metadata = candidate.symlink_metadata()?;
            let candidate_name = entry.file_name().to_string_lossy().into_owned();
            let manifest = candidate.join("manifest.json");
            let manifest_metadata = match manifest.symlink_metadata() {
                Ok(metadata) => Some(metadata),
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                    ) =>
                {
                    None
                }
                Err(error) => return Err(error.into()),
            };
            if candidate_metadata.file_type().is_symlink()
                || manifest_metadata
                    .as_ref()
                    .is_some_and(|metadata| metadata.file_type().is_symlink())
            {
                return invalid(format!("package candidate is unsafe: {candidate_name}"));
            }
            if !candidate_metadata.is_dir()
                || manifest_metadata
                    .as_ref()
                    .is_none_or(|metadata| !metadata.is_file())
            {
                continue;
            }
            let canonical_candidate = std::fs::canonicalize(&candidate)?;
            if canonical_candidate.parent() != Some(collection_root.as_path()) {
                return invalid(format!(
                    "package candidate escapes collection root: {candidate_name}"
                ));
            }
            manifests.insert(std::fs::canonicalize(manifest)?);
        }
    }
    Ok(manifests)
}

fn resolve_platforms(
    catalog: &BTreeMap<String, PackageCandidate>,
    requested: &[String],
    core_only: bool,
) -> Result<Vec<String>, LifecycleError> {
    if core_only {
        return Ok(Vec::new());
    }
    if requested.is_empty() {
        return invalid("select --core-only or at least one --platform");
    }
    let available = catalog
        .iter()
        .filter(|(_, candidate)| {
            manifest_string(&candidate.manifest, "kind").is_ok_and(|kind| kind == "platform")
        })
        .map(|(identifier, _)| identifier.clone())
        .collect::<BTreeSet<_>>();
    if requested.iter().any(|item| item == "all") {
        if requested.len() != 1 {
            return invalid("--platform all cannot be combined with another platform");
        }
        return Ok(available.into_iter().collect());
    }
    ensure_unique(requested, "selected platforms must be unique")?;
    let unknown = requested
        .iter()
        .filter(|item| !available.contains(*item))
        .cloned()
        .collect::<BTreeSet<_>>();
    if !unknown.is_empty() {
        return invalid(format!(
            "platform package is not installable: {}",
            unknown.into_iter().collect::<Vec<_>>().join(", ")
        ));
    }
    let mut selected = requested.to_vec();
    selected.sort();
    Ok(selected)
}

fn resolve_packages(
    catalog: &BTreeMap<String, PackageCandidate>,
    selected_platforms: Vec<String>,
    disciplines: &[String],
    runtime_configs: &[String],
) -> Result<SourceInstallSelection, LifecycleError> {
    if !catalog.contains_key("core") {
        return invalid("core package is not installable");
    }
    ensure_package_kind(
        catalog,
        disciplines,
        "discipline",
        "selected disciplines must be unique",
    )?;
    ensure_package_kind(
        catalog,
        runtime_configs,
        "runtime-config",
        "selected runtime configs must be unique",
    )?;

    let mut reasons = BTreeMap::<String, BTreeSet<String>>::new();
    reasons.insert("core".to_owned(), BTreeSet::from(["core".to_owned()]));
    for identifier in &selected_platforms {
        reasons
            .entry(identifier.clone())
            .or_default()
            .insert(format!("platform:{identifier}"));
    }
    for identifier in disciplines {
        reasons
            .entry(identifier.clone())
            .or_default()
            .insert(format!("discipline:{identifier}"));
    }
    for identifier in runtime_configs {
        reasons
            .entry(identifier.clone())
            .or_default()
            .insert(format!("runtime-config:{identifier}"));
    }
    let explicit = std::iter::once("core".to_owned())
        .chain(selected_platforms.iter().cloned())
        .chain(disciplines.iter().cloned())
        .chain(runtime_configs.iter().cloned())
        .collect::<BTreeSet<_>>();

    let (resolved, required_dependencies) =
        resolve_dependency_closure(catalog, explicit, &mut reasons)?;

    let ordered = topological_order(catalog, &resolved, &required_dependencies)?;
    let package_roots = ordered
        .iter()
        .map(|identifier| {
            (
                identifier.clone(),
                catalog
                    .get(identifier)
                    .expect("ordered package is cataloged")
                    .root
                    .clone(),
            )
        })
        .collect();
    let selection_reasons = ordered
        .iter()
        .map(|identifier| {
            (
                identifier.clone(),
                reasons
                    .remove(identifier)
                    .expect("selected package has a reason")
                    .into_iter()
                    .collect(),
            )
        })
        .collect();
    let mut selected_disciplines = disciplines.to_vec();
    selected_disciplines.sort();
    let mut selected_runtime_configs = runtime_configs.to_vec();
    selected_runtime_configs.sort();
    Ok(SourceInstallSelection {
        package_roots,
        selected_platforms,
        selected_disciplines,
        selected_runtime_configs,
        dependencies: required_dependencies,
        selection_reasons,
    })
}

fn resolve_dependency_closure(
    catalog: &BTreeMap<String, PackageCandidate>,
    explicit: BTreeSet<String>,
    reasons: &mut BTreeMap<String, BTreeSet<String>>,
) -> Result<(BTreeSet<String>, Vec<PackageDependency>), LifecycleError> {
    let mut resolved = BTreeSet::new();
    let mut visiting = Vec::new();
    let mut required_dependencies = Vec::new();
    let mut optional_dependencies = Vec::new();
    for package_id in explicit {
        if resolved.contains(&package_id) {
            continue;
        }
        push_visit_frame(catalog, &mut visiting, package_id)?;
        while !visiting.is_empty() {
            if let Some(dependency) = next_dependency(&mut visiting) {
                if dependency.requirement == "optional" {
                    optional_dependencies.push(dependency);
                    continue;
                }
                validate_dependency(catalog, &dependency, false)?;
                reasons
                    .entry(dependency.provider.clone())
                    .or_default()
                    .insert(format!("dependency:{}", dependency.consumer));
                required_dependencies.push(dependency.clone());
                if let Some(start) = visiting
                    .iter()
                    .position(|frame| frame.package_id == dependency.provider)
                {
                    let mut cycle = visiting[start..]
                        .iter()
                        .map(|frame| frame.package_id.clone())
                        .collect::<Vec<_>>();
                    cycle.push(dependency.provider);
                    return invalid(format!("package dependency cycle: {}", cycle.join(" -> ")));
                }
                if !resolved.contains(&dependency.provider) {
                    push_visit_frame(catalog, &mut visiting, dependency.provider)?;
                }
            } else {
                let completed = visiting.pop().expect("visit stack is not empty");
                resolved.insert(completed.package_id);
            }
        }
    }
    for dependency in optional_dependencies {
        if resolved.contains(&dependency.provider) {
            validate_dependency(catalog, &dependency, true)?;
            required_dependencies.push(dependency);
        }
    }
    required_dependencies.sort_by(|left, right| {
        (&left.consumer, &left.provider).cmp(&(&right.consumer, &right.provider))
    });
    Ok((resolved, required_dependencies))
}

fn next_dependency(visiting: &mut [VisitFrame]) -> Option<PackageDependency> {
    let frame = visiting.last_mut().expect("visit stack is not empty");
    if frame.next_dependency < frame.dependencies.len() {
        let dependency = frame.dependencies[frame.next_dependency].clone();
        frame.next_dependency += 1;
        Some(dependency)
    } else {
        None
    }
}

fn push_visit_frame(
    catalog: &BTreeMap<String, PackageCandidate>,
    visiting: &mut Vec<VisitFrame>,
    package_id: String,
) -> Result<(), LifecycleError> {
    let candidate = catalog.get(&package_id).ok_or_else(|| {
        LifecycleError::Invalid(format!("required package is not installable: {package_id}"))
    })?;
    let raw_dependencies = match candidate.manifest.get("package_requires") {
        None => &[],
        Some(Value::Array(dependencies)) => dependencies.as_slice(),
        Some(_) => return invalid("plugin-manifest package_requires must be an array"),
    };
    let mut dependencies = raw_dependencies
        .iter()
        .map(|value| parse_dependency(&package_id, value))
        .collect::<Result<Vec<_>, _>>()?;
    dependencies.sort_by(|left, right| left.provider.cmp(&right.provider));
    visiting.push(VisitFrame {
        package_id,
        dependencies,
        next_dependency: 0,
    });
    Ok(())
}

fn parse_dependency(consumer: &str, value: &Value) -> Result<PackageDependency, LifecycleError> {
    let dependency = value
        .as_object()
        .expect("validated package dependency is an object");
    let required_capabilities = dependency
        .get("required_capabilities")
        .and_then(Value::as_array)
        .expect("validated required_capabilities is an array")
        .iter()
        .map(|value| {
            value.as_str().map(str::to_owned).ok_or_else(|| {
                LifecycleError::Invalid(
                    "package dependency required_capabilities must contain strings".to_owned(),
                )
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(PackageDependency {
        consumer: consumer.to_owned(),
        provider: value_string(dependency.get("id"), "package dependency id")?,
        required_capabilities,
        requirement: value_string(
            dependency.get("requirement"),
            "package dependency requirement",
        )?,
        version: value_string(dependency.get("version"), "package dependency version")?,
    })
}

fn validate_dependency(
    catalog: &BTreeMap<String, PackageCandidate>,
    dependency: &PackageDependency,
    optional: bool,
) -> Result<(), LifecycleError> {
    let target = catalog.get(&dependency.provider).ok_or_else(|| {
        LifecycleError::Invalid(format!(
            "package {} requires missing package: {}",
            dependency.consumer, dependency.provider
        ))
    })?;
    let target_version = manifest_string(&target.manifest, "version")?;
    if !satisfies(target_version, &dependency.version)? {
        let qualifier = if optional { " optionally" } else { "" };
        return invalid(format!(
            "package {}{qualifier} requires {} {}, found {target_version}",
            dependency.consumer, dependency.provider, dependency.version
        ));
    }
    Ok(())
}

fn topological_order(
    catalog: &BTreeMap<String, PackageCandidate>,
    resolved: &BTreeSet<String>,
    dependencies: &[PackageDependency],
) -> Result<Vec<String>, LifecycleError> {
    let mut incoming = resolved
        .iter()
        .map(|identifier| (identifier.clone(), 0_usize))
        .collect::<BTreeMap<_, _>>();
    let mut outgoing = resolved
        .iter()
        .map(|identifier| (identifier.clone(), BTreeSet::new()))
        .collect::<BTreeMap<_, _>>();
    for dependency in dependencies {
        if outgoing
            .get_mut(&dependency.provider)
            .expect("dependency provider is selected")
            .insert(dependency.consumer.clone())
        {
            *incoming
                .get_mut(&dependency.consumer)
                .expect("dependency consumer is selected") += 1;
        }
    }
    let mut queue = incoming
        .iter()
        .filter(|(_, count)| **count == 0)
        .map(|(identifier, _)| identifier.clone())
        .collect::<Vec<_>>();
    sort_package_ids(&mut queue, catalog)?;
    let mut ordered = Vec::with_capacity(resolved.len());
    while !queue.is_empty() {
        let package_id = queue.remove(0);
        ordered.push(package_id.clone());
        let consumers = outgoing
            .get(&package_id)
            .expect("selected package has outgoing entry")
            .iter()
            .cloned()
            .collect::<Vec<_>>();
        for consumer in consumers {
            let count = incoming
                .get_mut(&consumer)
                .expect("dependency consumer is selected");
            *count -= 1;
            if *count == 0 {
                queue.push(consumer);
                sort_package_ids(&mut queue, catalog)?;
            }
        }
    }
    if ordered.len() != resolved.len() {
        return invalid("package dependency cycle includes selected optional packages");
    }
    Ok(ordered)
}

fn sort_package_ids(
    identifiers: &mut [String],
    catalog: &BTreeMap<String, PackageCandidate>,
) -> Result<(), LifecycleError> {
    let mut ranks = BTreeMap::new();
    for identifier in identifiers.iter() {
        ranks.insert(identifier.clone(), package_rank(identifier, catalog)?);
    }
    identifiers.sort_by_key(|identifier| {
        (
            *ranks.get(identifier).expect("package rank was precomputed"),
            identifier.clone(),
        )
    });
    Ok(())
}

fn package_rank(
    identifier: &str,
    catalog: &BTreeMap<String, PackageCandidate>,
) -> Result<u8, LifecycleError> {
    if identifier == "core" {
        return Ok(0);
    }
    let kind = manifest_string(
        &catalog
            .get(identifier)
            .expect("selected package is cataloged")
            .manifest,
        "kind",
    )?;
    Ok(match kind {
        "discipline" => 1,
        "platform" => 2,
        "stack" => 3,
        "adapter" => 4,
        "runtime-config" => 5,
        _ => 9,
    })
}

fn ensure_package_kind(
    catalog: &BTreeMap<String, PackageCandidate>,
    requested: &[String],
    expected_kind: &str,
    duplicate_error: &str,
) -> Result<(), LifecycleError> {
    ensure_unique(requested, duplicate_error)?;
    let unknown = requested
        .iter()
        .filter(|identifier| {
            catalog.get(*identifier).is_none_or(|candidate| {
                !manifest_string(&candidate.manifest, "kind")
                    .is_ok_and(|kind| kind == expected_kind)
            })
        })
        .cloned()
        .collect::<BTreeSet<_>>();
    if !unknown.is_empty() {
        return invalid(format!(
            "{expected_kind} package is not installable: {}",
            unknown.into_iter().collect::<Vec<_>>().join(", ")
        ));
    }
    Ok(())
}

fn ensure_unique(values: &[String], message: &str) -> Result<(), LifecycleError> {
    if values.iter().collect::<BTreeSet<_>>().len() != values.len() {
        return invalid(message);
    }
    Ok(())
}

fn manifest_string<'a>(manifest: &'a Value, field: &str) -> Result<&'a str, LifecycleError> {
    manifest
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| LifecycleError::Invalid(format!("installable package {field} is required")))
}

fn value_string(value: Option<&Value>, label: &str) -> Result<String, LifecycleError> {
    value
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} must be a non-empty string")))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static FIXTURE_COUNTER: AtomicU64 = AtomicU64::new(0);

    struct PackageFixture {
        root: PathBuf,
    }

    impl PackageFixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "agent-source-install-{}-{}",
                std::process::id(),
                FIXTURE_COUNTER.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(root.join("platforms")).expect("create fixture");
            Self { root }
        }

        fn platforms(&self) -> PathBuf {
            self.root.join("platforms")
        }

        fn package(
            &self,
            collection: &str,
            identifier: &str,
            kind: &str,
            version: &str,
            dependencies: &Value,
        ) {
            let package = self.root.join(collection).join(identifier);
            std::fs::create_dir_all(&package).expect("create package");
            let manifest = json!({
                "bindings": {},
                "capabilities": [],
                "conflicts": [],
                "detection": {"medium": [], "strong": [], "weak": []},
                "id": identifier,
                "installation": {
                    "asset_roots": [],
                    "instruction_fragments": [],
                    "skill_roots": [],
                },
                "kind": kind,
                "optional_requires": [],
                "package_requires": dependencies,
                "permissions": {"detection": "repository-read-only"},
                "requires": [],
                "schema_version": "1.0",
                "targets": [],
                "version": version,
            });
            std::fs::write(
                package.join("manifest.json"),
                agent_contracts::canonical_json(&manifest).expect("encode manifest"),
            )
            .expect("write manifest");
        }
    }

    impl Drop for PackageFixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    fn repository_platforms() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join("platforms")
            .canonicalize()
            .expect("repository platforms")
    }

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn real_repository_all_selection_matches_python_order_and_reasons() {
        let selection = resolve_source_install_selection(
            repository_platforms(),
            &strings(&["all"]),
            &[],
            &[],
            false,
        )
        .expect("resolve source selection");
        assert_eq!(
            selection.selected_platforms(),
            strings(&["apple", "desktop"])
        );
        assert_eq!(
            selection
                .package_roots()
                .iter()
                .map(|(identifier, _)| identifier.as_str())
                .collect::<Vec<_>>(),
            [
                "core",
                "design",
                "documentation",
                "git",
                "qa",
                "review",
                "workflow",
                "apple",
                "desktop",
            ]
        );
        assert_eq!(
            selection.compatibility_projection()["selection_reasons"]["git"],
            json!(["dependency:apple", "dependency:workflow"])
        );
    }

    #[test]
    fn disciplines_and_runtime_configs_are_explicit_without_platforms() {
        let discipline = resolve_source_install_selection(
            repository_platforms(),
            &[],
            &strings(&["qa"]),
            &[],
            false,
        )
        .expect("resolve discipline");
        assert!(discipline.selected_platforms().is_empty());
        assert_eq!(discipline.selected_disciplines(), strings(&["qa"]));
        assert_eq!(
            discipline
                .package_roots()
                .iter()
                .map(|(identifier, _)| identifier.as_str())
                .collect::<Vec<_>>(),
            ["core", "qa"]
        );

        let runtime = resolve_source_install_selection(
            repository_platforms(),
            &[],
            &[],
            &strings(&["codex"]),
            false,
        )
        .expect("resolve runtime config");
        assert!(runtime.selected_platforms().is_empty());
        assert_eq!(runtime.selected_runtime_configs(), strings(&["codex"]));
        assert_eq!(
            runtime
                .package_roots()
                .iter()
                .map(|(identifier, _)| identifier.as_str())
                .collect::<Vec<_>>(),
            ["core", "codex"]
        );
    }

    #[test]
    fn invalid_explicit_selection_fails_closed() {
        let platforms = repository_platforms();
        let missing = resolve_source_install_selection(&platforms, &[], &[], &[], false)
            .expect_err("platform selection must be explicit");
        assert!(missing.to_string().contains("select --core-only"));

        let bootstrap_only =
            resolve_source_install_selection(&platforms, &strings(&["web"]), &[], &[], false)
                .expect_err("bootstrap-only package is not installable");
        assert!(bootstrap_only.to_string().contains("not installable"));

        let duplicate = resolve_source_install_selection(
            &platforms,
            &strings(&["apple", "apple"]),
            &[],
            &[],
            false,
        )
        .expect_err("duplicates fail closed");
        assert!(duplicate.to_string().contains("must be unique"));

        let mixed_all = resolve_source_install_selection(
            &platforms,
            &strings(&["all", "apple"]),
            &[],
            &[],
            false,
        )
        .expect_err("all is exclusive");
        assert!(mixed_all.to_string().contains("cannot be combined"));
    }

    #[test]
    fn omitted_package_requires_is_an_empty_dependency_set() {
        let fixture = PackageFixture::new();
        fixture.package("platforms", "core", "adapter", "1.0.0", &json!([]));
        let manifest_path = fixture.platforms().join("core/manifest.json");
        let mut manifest =
            agent_contracts::load_json(&manifest_path).expect("load fixture manifest");
        manifest
            .as_object_mut()
            .expect("manifest is an object")
            .remove("package_requires");
        std::fs::write(
            &manifest_path,
            agent_contracts::canonical_json(&manifest).expect("encode manifest"),
        )
        .expect("write manifest");

        let selection = resolve_source_install_selection(fixture.platforms(), &[], &[], &[], true)
            .expect("missing package_requires is valid");
        assert_eq!(selection.package_roots()[0].0, "core");
    }

    #[test]
    fn nested_manifest_is_not_part_of_the_shallow_package_catalog() {
        let fixture = PackageFixture::new();
        fixture.package("platforms", "core", "adapter", "1.0.0", &json!([]));
        let nested = fixture.platforms().join("core/assets/nested");
        std::fs::create_dir_all(&nested).expect("create nested asset");
        std::fs::write(nested.join("manifest.json"), b"{}").expect("write invalid nested manifest");

        let selection = resolve_source_install_selection(fixture.platforms(), &[], &[], &[], true)
            .expect("nested manifest is outside package catalog");
        assert_eq!(selection.package_roots()[0].0, "core");
    }

    #[cfg(windows)]
    #[test]
    fn compatibility_projection_removes_windows_verbatim_prefixes() {
        assert_eq!(
            compatibility_path(Path::new(r"\\?\C:\repo\platforms\core")),
            PathBuf::from(r"C:\repo\platforms\core")
        );
        assert_eq!(
            compatibility_path(Path::new(r"\\?\UNC\server\share\core")),
            PathBuf::from(r"\\server\share\core")
        );
    }

    #[cfg(unix)]
    #[test]
    fn direct_candidate_and_manifest_symlinks_fail_closed() {
        use std::os::unix::fs::symlink;

        let fixture = PackageFixture::new();
        fixture.package("platforms", "core", "adapter", "1.0.0", &json!([]));
        let external = fixture.root.join("external");
        std::fs::create_dir(&external).expect("create external directory");
        let unsafe_candidate = fixture.platforms().join("unsafe");
        symlink(&external, &unsafe_candidate).expect("create candidate symlink");
        let candidate_error =
            resolve_source_install_selection(fixture.platforms(), &[], &[], &[], true)
                .expect_err("candidate symlink fails closed");
        assert!(
            candidate_error
                .to_string()
                .contains("package candidate is unsafe")
        );

        std::fs::remove_file(&unsafe_candidate).expect("remove candidate symlink");
        std::fs::create_dir(&unsafe_candidate).expect("create candidate directory");
        symlink(
            fixture.platforms().join("core/manifest.json"),
            unsafe_candidate.join("manifest.json"),
        )
        .expect("create manifest symlink");
        let manifest_error =
            resolve_source_install_selection(fixture.platforms(), &[], &[], &[], true)
                .expect_err("manifest symlink fails closed");
        assert!(
            manifest_error
                .to_string()
                .contains("package candidate is unsafe")
        );
    }

    #[test]
    fn required_dependency_missing_version_and_cycle_fail_closed() {
        let fixture = PackageFixture::new();
        fixture.package("platforms", "core", "adapter", "1.0.0", &json!([]));
        fixture.package(
            "platforms",
            "apple",
            "platform",
            "1.0.0",
            &json!([{
                "id": "documentation",
                "required_capabilities": ["fixture.documentation"],
                "requirement": "required",
                "version": ">=1.0.0 <2.0.0",
            }]),
        );
        let missing = resolve_source_install_selection(
            fixture.platforms(),
            &strings(&["apple"]),
            &[],
            &[],
            false,
        )
        .expect_err("missing dependency fails");
        assert!(missing.to_string().contains("requires missing package"));

        fixture.package(
            "disciplines",
            "documentation",
            "discipline",
            "2.0.0",
            &json!([]),
        );
        let incompatible = resolve_source_install_selection(
            fixture.platforms(),
            &strings(&["apple"]),
            &[],
            &[],
            false,
        )
        .expect_err("incompatible dependency fails");
        assert!(incompatible.to_string().contains("found 2.0.0"));

        fixture.package(
            "platforms",
            "core",
            "adapter",
            "1.0.0",
            &json!([{
                "id": "apple",
                "required_capabilities": ["fixture.apple"],
                "requirement": "required",
                "version": ">=1.0.0 <2.0.0",
            }]),
        );
        fixture.package(
            "platforms",
            "apple",
            "platform",
            "1.0.0",
            &json!([{
                "id": "core",
                "required_capabilities": ["fixture.core"],
                "requirement": "required",
                "version": ">=1.0.0 <2.0.0",
            }]),
        );
        let cycle = resolve_source_install_selection(
            fixture.platforms(),
            &strings(&["apple"]),
            &[],
            &[],
            false,
        )
        .expect_err("dependency cycle fails");
        assert!(
            cycle.to_string().contains("apple -> core -> apple"),
            "{cycle}"
        );
    }

    #[test]
    fn optional_dependency_is_only_checked_when_independently_selected() {
        let fixture = PackageFixture::new();
        fixture.package("platforms", "core", "adapter", "1.0.0", &json!([]));
        fixture.package(
            "platforms",
            "apple",
            "platform",
            "1.0.0",
            &json!([{
                "id": "documentation",
                "required_capabilities": ["fixture.documentation"],
                "requirement": "optional",
                "version": ">=1.0.0 <2.0.0",
            }]),
        );
        let without_optional = resolve_source_install_selection(
            fixture.platforms(),
            &strings(&["apple"]),
            &[],
            &[],
            false,
        )
        .expect("missing optional dependency is ignored");
        assert_eq!(
            without_optional
                .package_roots()
                .iter()
                .map(|(identifier, _)| identifier.as_str())
                .collect::<Vec<_>>(),
            ["core", "apple"]
        );

        fixture.package(
            "disciplines",
            "documentation",
            "discipline",
            "2.0.0",
            &json!([]),
        );
        let selected = resolve_source_install_selection(
            fixture.platforms(),
            &strings(&["apple"]),
            &strings(&["documentation"]),
            &[],
            false,
        )
        .expect_err("selected incompatible optional dependency fails");
        assert!(
            selected
                .to_string()
                .contains("apple optionally requires documentation")
        );
    }
}
