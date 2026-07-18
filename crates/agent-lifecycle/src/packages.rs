use super::{
    LifecycleError, MANAGED_DIRECTORY_MODE, open_child_directory, open_child_file,
    same_content_state_cap, same_object_cap,
};
use agent_contracts::{canonical_sha256, json_integer, parse_json};
use agent_registry::{ManifestRegistry, RegisteredManifest, validate_manifest_syntax};
use cap_fs_ext::MetadataExt as _;
use cap_std::fs::Dir;
use serde_json::{Map, Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::io::Read as _;
use std::path::{Component, Path, PathBuf};

pub(super) const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");
const MAX_PACKAGE_TREE_ENTRIES: usize = 100_000;

pub(super) struct PackageInspection {
    pub(super) details: Value,
    pub(super) semantics: Value,
}

#[derive(Clone)]
struct ExpectedFile {
    mode: u32,
    sha256: String,
}

#[derive(Clone)]
pub(super) struct SemanticPackage {
    pub(super) fragments: Vec<Value>,
    pub(super) id: String,
    pub(super) manifest: Value,
    pub(super) provider: Option<Value>,
    pub(super) files: Vec<Value>,
}

struct CheckedPackage {
    id: String,
    manifest: Value,
    provider: Option<Value>,
}

#[allow(clippy::too_many_lines)]
pub(super) fn check_package_integrity(
    target: &Dir,
    install_lock: &Value,
    package_lock: &Value,
    retained_semantics: &mut Option<Value>,
) -> Result<PackageInspection, LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let packages_root = open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "installed package directory",
    )?;
    let packages_identity = packages_root.dir_metadata()?;
    let install_packages = array_field(install_lock, "packages", "Install Lock packages")?;
    let expected_ids = install_packages
        .iter()
        .map(|record| string_field(record, "id", "Install Lock package id"))
        .collect::<Result<Vec<_>, _>>()?;
    let mut actual_ids = Vec::new();
    for entry in packages_root.entries()? {
        let entry = entry?;
        if super::ignored_os_metadata(&packages_root, &entry.file_name())? {
            continue;
        }
        actual_ids.push(entry.file_name().to_string_lossy().into_owned());
    }
    actual_ids.sort();
    let mut sorted_expected = expected_ids.clone();
    sorted_expected.sort_unstable();
    if actual_ids != sorted_expected {
        return invalid("installed package set differs from Install Lock");
    }

    let locked_packages = array_field(package_lock, "packages", "package Lock packages")?;
    let locked_ids = locked_packages
        .iter()
        .map(|record| string_field(record, "id", "package Lock package id"))
        .collect::<Result<Vec<_>, _>>()?;
    if locked_ids != expected_ids {
        return invalid("persistent Lockfile package closure or order differs from Install Lock");
    }
    let locked_by_id = value_map_by_id(locked_packages)?;
    let selected_by_id = value_map_by_id(array_field(
        install_lock,
        "selected_packages",
        "Install Lock selected packages",
    )?)?;

    let mut checked_packages = Vec::with_capacity(install_packages.len());
    let mut package_identities = Vec::with_capacity(install_packages.len());
    let mut registry_entries = Vec::new();
    for record in install_packages {
        let package_id = string_field(record, "id", "Install Lock package id")?;
        let root_mode = u32_field(record, "root_mode", "package root mode")?;
        let root = open_child_directory(
            &packages_root,
            package_id,
            Some(root_mode),
            &format!("package {package_id}"),
        )?;
        package_identities.push((package_id.to_owned(), root.dir_metadata()?, record.clone()));
        validate_package_tree(&root, record, package_id)?;
        let manifest = load_recorded_json(&root, record, "manifest.json", "installed Manifest")?;
        validate_manifest_syntax(&manifest)?;
        let manifest_digest = canonical_sha256(&manifest)?;
        if manifest.get("id").and_then(Value::as_str) != Some(package_id)
            || record.get("manifest_sha256").and_then(Value::as_str)
                != Some(manifest_digest.as_str())
        {
            return invalid(format!("installed package Manifest differs: {package_id}"));
        }
        registry_entries.push(RegisteredManifest {
            path: PathBuf::from(package_id).join("manifest.json"),
            value: manifest.clone(),
            digest: manifest_digest,
        });
        validate_package_identity(
            package_id,
            record,
            locked_by_id.get(package_id).copied(),
            selected_by_id.get(package_id).copied(),
        )?;

        let expected_provider = record
            .get("provider_manifest_sha256")
            .unwrap_or(&Value::Null);
        let provider_relative = manifest
            .get("installation")
            .and_then(Value::as_object)
            .and_then(|installation| installation.get("provider_manifest"));
        let provider = match expected_provider {
            Value::Null => {
                if provider_relative.is_some_and(|value| !value.is_null()) {
                    return invalid(format!(
                        "package unexpectedly declares a Provider: {package_id}"
                    ));
                }
                None
            }
            Value::String(expected_digest) => {
                let relative = provider_relative.and_then(Value::as_str).ok_or_else(|| {
                    LifecycleError::Invalid(format!(
                        "installed Provider Manifest differs: {package_id}"
                    ))
                })?;
                let value =
                    load_recorded_json(&root, record, relative, "installed Provider Manifest")?;
                validate_manifest_syntax(&value)?;
                let digest = canonical_sha256(&value)?;
                if digest != *expected_digest {
                    return invalid(format!("installed Provider Manifest differs: {package_id}"));
                }
                let canonical_relative =
                    canonical_relative_path(relative, "installed Provider Manifest")?;
                registry_entries.push(RegisteredManifest {
                    path: PathBuf::from(package_id).join(canonical_relative),
                    value: value.clone(),
                    digest,
                });
                Some(value)
            }
            _ => return invalid("package provider Manifest digest is invalid"),
        };
        checked_packages.push(CheckedPackage {
            id: package_id.to_owned(),
            manifest,
            provider,
        });
    }

    let mut installed = Vec::with_capacity(checked_packages.len());
    for ((package, record), (_, original_metadata, _)) in checked_packages
        .into_iter()
        .zip(install_packages)
        .zip(&package_identities)
    {
        if package.provider.as_ref().is_some_and(|provider| {
            provider.get("role").and_then(Value::as_str) != Some("provider")
        }) {
            return invalid(format!(
                "installation provider is not a provider manifest: {}",
                package.id
            ));
        }
        let root = open_child_directory(
            &packages_root,
            &package.id,
            Some(u32_field(record, "root_mode", "package root mode")?),
            &format!("package {}", package.id),
        )?;
        let current_metadata = root.dir_metadata()?;
        if !same_object_cap(original_metadata, &current_metadata)
            || !same_content_state_cap(original_metadata, &current_metadata)
        {
            return invalid(format!(
                "installed package changed while inspecting: {}",
                package.id
            ));
        }
        let fragments = load_instruction_fragments(&root, record, &package.id, &package.manifest)?;
        let declared_files = validate_installation_roots(record, &package.id, &package.manifest)?;
        validate_package_tree(&root, record, &package.id)?;
        installed.push(SemanticPackage {
            fragments,
            id: package.id,
            manifest: package.manifest,
            provider: package.provider,
            files: declared_files,
        });
    }
    ManifestRegistry::new(registry_entries, CORE_VERSION)?;

    let semantics = derive_package_semantics(&installed, install_packages)?;
    *retained_semantics = Some(semantics.clone());
    validate_semantics(
        install_lock,
        package_lock,
        &selected_by_id,
        &locked_by_id,
        &semantics,
    )?;
    revalidate_package_paths(
        target,
        &packages_identity,
        &package_identities,
        &sorted_expected,
    )?;
    Ok(PackageInspection {
        details: json!({
            "package_count": expected_ids.len(),
            "packages": sorted_expected,
        }),
        semantics,
    })
}

#[allow(clippy::too_many_lines)]
pub(super) fn derive_rollback_package_semantics(
    packages_root: &Dir,
    install_lock: &Value,
) -> Result<Value, LifecycleError> {
    let records = array_field(install_lock, "packages", "Install Lock packages")?;
    let expected_ids = records
        .iter()
        .map(|record| string_field(record, "id", "Install Lock package id"))
        .collect::<Result<Vec<_>, _>>()?;
    let mut actual_ids = packages_root
        .entries()?
        .map(|entry| entry.map(|entry| entry.file_name().to_string_lossy().into_owned()))
        .collect::<Result<Vec<_>, _>>()?;
    actual_ids.sort();
    let mut sorted_expected = expected_ids.clone();
    sorted_expected.sort_unstable();
    if actual_ids != sorted_expected {
        return invalid("rollback point package set differs from Install Lock");
    }

    let mut checked = Vec::with_capacity(records.len());
    let mut registry_entries = Vec::new();
    for record in records {
        let package_id = string_field(record, "id", "Install Lock package id")?;
        let root = open_child_directory(
            packages_root,
            package_id,
            Some(u32_field(record, "root_mode", "package root mode")?),
            &format!("rollback point package {package_id}"),
        )
        .map_err(|_| {
            LifecycleError::Invalid(format!(
                "rollback point package is missing or unsafe: {package_id}"
            ))
        })?;
        validate_recorded_tree(
            &root,
            record,
            "files_sha256",
            &format!("rollback point package differs from Install Lock: {package_id}"),
        )?;
        let manifest = load_recorded_json(&root, record, "manifest.json", "installed Manifest")?;
        validate_manifest_syntax(&manifest)?;
        let manifest_digest = canonical_sha256(&manifest)?;
        if manifest.get("id").and_then(Value::as_str) != Some(package_id)
            || record.get("manifest_sha256").and_then(Value::as_str)
                != Some(manifest_digest.as_str())
        {
            return invalid(format!(
                "rollback point package differs from Install Lock: {package_id}"
            ));
        }
        registry_entries.push(RegisteredManifest {
            path: PathBuf::from(package_id).join("manifest.json"),
            value: manifest.clone(),
            digest: manifest_digest,
        });

        let provider_relative = manifest
            .get("installation")
            .and_then(Value::as_object)
            .and_then(|installation| installation.get("provider_manifest"));
        let provider = match record
            .get("provider_manifest_sha256")
            .unwrap_or(&Value::Null)
        {
            Value::Null => {
                if provider_relative.is_some_and(|value| !value.is_null()) {
                    return invalid(format!(
                        "package unexpectedly declares a Provider: {package_id}"
                    ));
                }
                None
            }
            Value::String(expected_digest) => {
                let relative = provider_relative.and_then(Value::as_str).ok_or_else(|| {
                    LifecycleError::Invalid(format!(
                        "installed Provider Manifest differs: {package_id}"
                    ))
                })?;
                let value =
                    load_recorded_json(&root, record, relative, "installed Provider Manifest")?;
                validate_manifest_syntax(&value)?;
                let digest = canonical_sha256(&value)?;
                if digest != *expected_digest {
                    return invalid(format!("installed Provider Manifest differs: {package_id}"));
                }
                registry_entries.push(RegisteredManifest {
                    path: PathBuf::from(package_id).join(canonical_relative_path(
                        relative,
                        "installed Provider Manifest",
                    )?),
                    value: value.clone(),
                    digest,
                });
                Some(value)
            }
            _ => return invalid("package provider Manifest digest is invalid"),
        };
        checked.push((package_id.to_owned(), record, root, manifest, provider));
    }
    ManifestRegistry::new(registry_entries, CORE_VERSION)?;

    let mut installed = Vec::with_capacity(checked.len());
    for (package_id, record, root, manifest, provider) in checked {
        if provider
            .as_ref()
            .is_some_and(|value| value.get("role").and_then(Value::as_str) != Some("provider"))
        {
            return invalid(format!(
                "installation provider is not a provider manifest: {package_id}"
            ));
        }
        let fragments = load_instruction_fragments(&root, record, &package_id, &manifest)?;
        let files = validate_installation_roots(record, &package_id, &manifest)?;
        validate_recorded_tree(
            &root,
            record,
            "files_sha256",
            &format!("rollback point package differs from Install Lock: {package_id}"),
        )?;
        installed.push(SemanticPackage {
            fragments,
            id: package_id,
            manifest,
            provider,
            files,
        });
    }
    derive_package_semantics(&installed, records)
}

fn revalidate_package_paths(
    target: &Dir,
    original_packages: &cap_std::fs::Metadata,
    original_roots: &[(String, cap_std::fs::Metadata, Value)],
    expected_ids: &[&str],
) -> Result<(), LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let current_packages = open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "installed package directory",
    )?;
    let current_metadata = current_packages.dir_metadata()?;
    if !same_object_cap(original_packages, &current_metadata)
        || !same_content_state_cap(original_packages, &current_metadata)
    {
        return invalid("installed package directory changed while inspecting");
    }
    let mut current_ids = package_entry_names(&current_packages)?
        .into_iter()
        .map(|name| name.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    current_ids.sort();
    if current_ids
        .iter()
        .map(String::as_str)
        .ne(expected_ids.iter().copied())
    {
        return invalid("installed package set differs from Install Lock");
    }
    for (package_id, original_metadata, record) in original_roots {
        let current = open_child_directory(
            &current_packages,
            package_id,
            Some(u32_field(record, "root_mode", "package root mode")?),
            &format!("package {package_id}"),
        )?;
        let current_metadata = current.dir_metadata()?;
        if !same_object_cap(original_metadata, &current_metadata)
            || !same_content_state_cap(original_metadata, &current_metadata)
        {
            return invalid(format!(
                "installed package changed while inspecting: {package_id}"
            ));
        }
        validate_package_tree(&current, record, package_id)?;
    }
    let final_packages = reopen_packages_snapshot(target, original_packages, expected_ids)?;
    for (package_id, original_metadata, _) in original_roots {
        let current = open_child_directory(
            &final_packages,
            package_id,
            None,
            &format!("package {package_id}"),
        )?;
        let current_metadata = current.dir_metadata()?;
        if !same_object_cap(original_metadata, &current_metadata)
            || !same_content_state_cap(original_metadata, &current_metadata)
        {
            return invalid(format!(
                "installed package changed while inspecting: {package_id}"
            ));
        }
    }
    reopen_packages_snapshot(target, original_packages, expected_ids)?;
    Ok(())
}

fn reopen_packages_snapshot(
    target: &Dir,
    original_packages: &cap_std::fs::Metadata,
    expected_ids: &[&str],
) -> Result<Dir, LifecycleError> {
    let managed = open_child_directory(
        target,
        ".agent-skills",
        Some(MANAGED_DIRECTORY_MODE),
        "managed metadata directory",
    )?;
    let packages = open_child_directory(
        &managed,
        "packages",
        Some(MANAGED_DIRECTORY_MODE),
        "installed package directory",
    )?;
    let metadata = packages.dir_metadata()?;
    if !same_object_cap(original_packages, &metadata)
        || !same_content_state_cap(original_packages, &metadata)
    {
        return invalid("installed package directory changed while inspecting");
    }
    let mut current_ids = package_entry_names(&packages)?
        .into_iter()
        .map(|name| name.to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    current_ids.sort();
    if current_ids
        .iter()
        .map(String::as_str)
        .ne(expected_ids.iter().copied())
    {
        return invalid("installed package set differs from Install Lock");
    }
    Ok(packages)
}

fn validate_package_tree(
    root: &Dir,
    record: &Value,
    package_id: &str,
) -> Result<(), LifecycleError> {
    validate_recorded_tree(
        root,
        record,
        "files_sha256",
        &format!("installed package content differs: {package_id}"),
    )
}

pub(super) fn validate_recorded_tree(
    root: &Dir,
    record: &Value,
    digest_field: &str,
    difference_message: &str,
) -> Result<(), LifecycleError> {
    validate_recorded_tree_with_metadata_policy(
        root,
        record,
        digest_field,
        difference_message,
        true,
    )
}

pub(super) fn validate_recorded_tree_strict(
    root: &Dir,
    record: &Value,
    digest_field: &str,
    difference_message: &str,
) -> Result<(), LifecycleError> {
    validate_recorded_tree_with_metadata_policy(
        root,
        record,
        digest_field,
        difference_message,
        false,
    )
}

fn validate_recorded_tree_with_metadata_policy(
    root: &Dir,
    record: &Value,
    digest_field: &str,
    difference_message: &str,
    ignore_os_metadata: bool,
) -> Result<(), LifecycleError> {
    let expected_files = array_field(record, "files", "package files")?
        .iter()
        .map(|entry| {
            Ok((
                string_field(entry, "path", "package file path")?.to_owned(),
                ExpectedFile {
                    mode: u32_field(entry, "mode", "package file mode")?,
                    sha256: string_field(entry, "sha256", "package file hash")?.to_owned(),
                },
            ))
        })
        .collect::<Result<BTreeMap<_, _>, LifecycleError>>()?;
    let expected_directories = array_field(record, "directories", "package directories")?
        .iter()
        .map(|entry| {
            Ok((
                string_field(entry, "path", "package directory path")?.to_owned(),
                u32_field(entry, "mode", "package directory mode")?,
            ))
        })
        .collect::<Result<BTreeMap<_, _>, LifecycleError>>()?;
    let mut seen_files = BTreeSet::new();
    let mut seen_directories = BTreeSet::new();
    let mut file_entry_count = 0_usize;
    let mut directory_entry_count = 0_usize;
    walk_package_tree(
        root,
        "",
        &expected_files,
        &expected_directories,
        &mut seen_files,
        &mut seen_directories,
        &mut file_entry_count,
        &mut directory_entry_count,
        ignore_os_metadata,
    )
    .map_err(|error| match &error {
        LifecycleError::Invalid(message)
            if message.starts_with("install tree must not contain symlinks:")
                || message.starts_with("install tree contains unsupported entry:") =>
        {
            error
        }
        _ => LifecycleError::Invalid(difference_message.to_owned()),
    })?;
    if seen_files.len() != expected_files.len()
        || seen_directories.len() != expected_directories.len()
    {
        return invalid(difference_message);
    }
    let actual_files = expected_files
        .iter()
        .map(|(path, entry)| json!({"mode": entry.mode, "path": path, "sha256": entry.sha256}))
        .collect::<Vec<_>>();
    if record.get("file_count").and_then(Value::as_u64) != u64::try_from(actual_files.len()).ok()
        || record.get(digest_field).and_then(Value::as_str)
            != Some(canonical_sha256(&Value::Array(actual_files))?.as_str())
    {
        return invalid(difference_message);
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn walk_package_tree(
    directory: &Dir,
    prefix: &str,
    expected_files: &BTreeMap<String, ExpectedFile>,
    expected_directories: &BTreeMap<String, u32>,
    seen_files: &mut BTreeSet<String>,
    seen_directories: &mut BTreeSet<String>,
    file_entry_count: &mut usize,
    directory_entry_count: &mut usize,
    ignore_os_metadata: bool,
) -> Result<(), LifecycleError> {
    let mut pending = BTreeSet::new();
    for name in tree_entry_names(directory, ignore_os_metadata)? {
        let name = name
            .to_str()
            .ok_or_else(|| LifecycleError::Invalid("install tree path is not UTF-8".to_owned()))?;
        let relative = if prefix.is_empty() {
            name.to_owned()
        } else {
            format!("{prefix}/{name}")
        };
        pending.insert(relative);
    }
    while let Some(relative) = pending.pop_first() {
        let (parent, name) = open_relative_parent(directory, &relative, "installed package entry")?;
        let metadata = parent.symlink_metadata(&name)?;
        if metadata.file_type().is_symlink() {
            return invalid(format!(
                "install tree must not contain symlinks: {relative}"
            ));
        }
        if metadata.is_dir() {
            increment_tree_entry_count(directory_entry_count, "directory")?;
            let mode = expected_directories
                .get(&relative)
                .copied()
                .ok_or_else(|| {
                    LifecycleError::Invalid(format!("unknown package directory: {relative}"))
                })?;
            let child =
                open_child_directory(&parent, &name, Some(mode), "installed package directory")?;
            seen_directories.insert(relative.clone());
            for child_name in tree_entry_names(&child, ignore_os_metadata)? {
                let child_name = child_name.to_str().ok_or_else(|| {
                    LifecycleError::Invalid("install tree path is not UTF-8".to_owned())
                })?;
                pending.insert(format!("{relative}/{child_name}"));
            }
        } else if metadata.is_file() {
            increment_tree_entry_count(file_entry_count, "file")?;
            if !ignore_os_metadata && metadata.nlink() != 1 {
                return invalid(format!(
                    "install tree file has an unsafe hard-link alias: {relative}"
                ));
            }
            let expected = expected_files.get(&relative).ok_or_else(|| {
                LifecycleError::Invalid(format!("unknown package file: {relative}"))
            })?;
            let actual = hash_child_file(&parent, &name, expected.mode, "installed package file")?;
            if actual != expected.sha256 {
                return invalid(format!("package file hash differs: {relative}"));
            }
            seen_files.insert(relative);
        } else {
            return invalid(format!(
                "install tree contains unsupported entry: {relative}"
            ));
        }
    }
    Ok(())
}

fn increment_tree_entry_count(counter: &mut usize, kind: &str) -> Result<(), LifecycleError> {
    *counter = counter
        .checked_add(1)
        .ok_or_else(|| LifecycleError::Invalid("package tree entry counter overflow".to_owned()))?;
    if *counter > MAX_PACKAGE_TREE_ENTRIES {
        return invalid(format!(
            "package tree exceeds maximum of {MAX_PACKAGE_TREE_ENTRIES} {kind} entries"
        ));
    }
    Ok(())
}

fn open_relative_parent(
    root: &Dir,
    relative: &str,
    label: &str,
) -> Result<(Dir, String), LifecycleError> {
    let components = portable_components(relative, label)?;
    let (name, parents) = components.split_last().ok_or_else(|| {
        LifecycleError::Invalid(format!("{label} must be a package-relative path"))
    })?;
    let mut directory = root.try_clone()?;
    for parent in parents {
        directory = open_child_directory(&directory, parent, None, label)?;
    }
    Ok((directory, (*name).to_owned()))
}

fn package_entry_names(directory: &Dir) -> Result<Vec<std::ffi::OsString>, LifecycleError> {
    tree_entry_names(directory, true)
}

fn tree_entry_names(
    directory: &Dir,
    ignore_os_metadata: bool,
) -> Result<Vec<std::ffi::OsString>, LifecycleError> {
    let mut names = Vec::new();
    for entry in directory.entries()? {
        let entry = entry?;
        if ignore_os_metadata && super::ignored_os_metadata(directory, &entry.file_name())? {
            continue;
        }
        names.push(entry.file_name());
    }
    names.sort();
    Ok(names)
}

pub(super) fn hash_child_file(
    parent: &Dir,
    name: &str,
    mode: u32,
    label: &str,
) -> Result<String, LifecycleError> {
    let mut file = open_child_file(parent, name, mode, label)?;
    let opened = file.metadata()?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let after = file.metadata()?;
    let current = open_child_file(parent, name, mode, label)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
    {
        return invalid("installed package file changed while reading");
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn load_recorded_json(
    root: &Dir,
    record: &Value,
    relative: &str,
    label: &str,
) -> Result<Value, LifecycleError> {
    Ok(parse_json(&read_recorded_bytes(
        root, record, relative, label,
    )?)?)
}

pub(super) fn read_recorded_bytes(
    root: &Dir,
    record: &Value,
    relative: &str,
    label: &str,
) -> Result<Vec<u8>, LifecycleError> {
    let canonical_relative = canonical_relative_path(relative, label)?;
    let mode = array_field(record, "files", "package files")?
        .iter()
        .find(|entry| {
            entry.get("path").and_then(Value::as_str) == Some(canonical_relative.as_str())
        })
        .map(|entry| u32_field(entry, "mode", "package file mode"))
        .transpose()?
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    let mut directory = root.try_clone()?;
    let components = portable_components(&canonical_relative, label)?;
    let (name, parents) = components.split_last().ok_or_else(|| {
        LifecycleError::Invalid(format!("{label} must be a package-relative path"))
    })?;
    for parent in parents {
        directory = open_child_directory(&directory, parent, None, label)?;
    }
    let mut file = open_child_file(&directory, name, mode, label)?;
    let opened = file.metadata()?;
    if opened.len() > super::MAX_CONTRACT_JSON_BYTES as u64 {
        return invalid(format!("{label} is too large"));
    }
    let mut bytes = Vec::with_capacity(
        usize::try_from(opened.len())
            .unwrap_or(super::MAX_CONTRACT_JSON_BYTES)
            .min(super::MAX_CONTRACT_JSON_BYTES),
    );
    file.by_ref()
        .take((super::MAX_CONTRACT_JSON_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > super::MAX_CONTRACT_JSON_BYTES {
        return invalid(format!("{label} is too large"));
    }
    let after = file.metadata()?;
    let current = open_child_file(&directory, name, mode, label)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
    {
        return invalid(format!("{label} changed while reading"));
    }
    Ok(bytes)
}

fn portable_components<'a>(relative: &'a str, label: &str) -> Result<Vec<&'a str>, LifecycleError> {
    if relative.is_empty() || relative.starts_with('/') || relative.contains('\\') {
        return invalid(format!("{label} must be a package-relative path"));
    }
    let mut parts = Vec::new();
    for component in Path::new(relative).components() {
        match component {
            Component::Normal(part) => parts
                .push(part.to_str().ok_or_else(|| {
                    LifecycleError::Invalid(format!("{label} path is not UTF-8"))
                })?),
            Component::CurDir => {}
            Component::ParentDir | Component::Prefix(_) | Component::RootDir => {
                return invalid(format!("{label} must be a package-relative path"));
            }
        }
    }
    if parts.is_empty() {
        return invalid(format!("{label} must be a package-relative path"));
    }
    Ok(parts)
}

fn canonical_relative_path(relative: &str, label: &str) -> Result<String, LifecycleError> {
    Ok(portable_components(relative, label)?.join("/"))
}

fn load_instruction_fragments(
    root: &Dir,
    record: &Value,
    package_id: &str,
    manifest: &Value,
) -> Result<Vec<Value>, LifecycleError> {
    let installation = object_field(manifest, "installation", "package installation")?;
    let raw_fragments = installation
        .get("instruction_fragments")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            LifecycleError::Invalid("package instruction_fragments is invalid".to_owned())
        })?;
    let kind = string_field(manifest, "kind", "package kind")?;
    let expected_scope = if package_id == "core" {
        "global".to_owned()
    } else {
        format!("{kind}:{package_id}")
    };
    let mut fragments = Vec::with_capacity(raw_fragments.len());
    for raw in raw_fragments {
        let path = string_field(raw, "path", "instruction fragment path")?;
        if raw.get("scope").and_then(Value::as_str) != Some(expected_scope.as_str()) {
            return invalid(format!(
                "instruction fragment scope is invalid for {package_id}: {}",
                raw.get("scope").and_then(Value::as_str).unwrap_or("")
            ));
        }
        let bytes = read_recorded_bytes(root, record, path, "instruction fragment")?;
        let text = String::from_utf8(bytes).map_err(|_| {
            LifecycleError::Invalid(format!("instruction fragment is not UTF-8: {path}"))
        })?;
        let content = format!("{}\n", python_trim(&text));
        let mut fragment = raw.as_object().cloned().ok_or_else(|| {
            LifecycleError::Invalid("instruction fragment must be an object".to_owned())
        })?;
        fragment.insert("content".to_owned(), Value::String(content.clone()));
        fragment.insert("package".to_owned(), Value::String(package_id.to_owned()));
        fragment.insert(
            "sha256".to_owned(),
            Value::String(bytes_sha256(content.as_bytes())),
        );
        fragments.push(Value::Object(fragment));
    }
    Ok(fragments)
}

fn validate_installation_roots(
    record: &Value,
    package_id: &str,
    manifest: &Value,
) -> Result<Vec<Value>, LifecycleError> {
    let files = array_field(record, "files", "package files")?;
    let directories = array_field(record, "directories", "package directories")?;
    let file_by_path = files
        .iter()
        .map(|entry| {
            Ok((
                string_field(entry, "path", "package file path")?.to_owned(),
                entry,
            ))
        })
        .collect::<Result<BTreeMap<_, _>, LifecycleError>>()?;
    let directory_paths = directories
        .iter()
        .map(|entry| string_field(entry, "path", "package directory path").map(str::to_owned))
        .collect::<Result<BTreeSet<_>, _>>()?;
    let installation = object_field(manifest, "installation", "package installation")?;
    let skill_roots = string_array_map_field(installation, "skill_roots", "package skill_roots")?;
    let canonical_skill_roots = skill_roots
        .iter()
        .map(|root| {
            let canonical = canonical_relative_path(root, "skill root")?;
            if !directory_paths.contains(&canonical) {
                return invalid(format!("skill root is missing: {root}"));
            }
            Ok(canonical)
        })
        .collect::<Result<Vec<_>, LifecycleError>>()?;
    let mut declared = BTreeSet::from(["manifest.json".to_owned()]);
    for metadata in ["migration-source.json", "migration-overrides.json"] {
        if file_by_path.contains_key(metadata) {
            declared.insert(metadata.to_owned());
        }
    }
    for root in string_array_map_field(installation, "asset_roots", "package asset_roots")? {
        collect_declared_root(
            root,
            "installation asset path",
            &file_by_path,
            &directory_paths,
            &mut declared,
        )?;
    }
    for canonical_root in canonical_skill_roots {
        collect_directory_files(&canonical_root, &file_by_path, &mut declared);
    }
    if let Some(provider) = installation
        .get("provider_manifest")
        .and_then(Value::as_str)
    {
        require_declared_file(provider, "provider manifest", &file_by_path, &mut declared)?;
    }
    for fragment in map_array_field(
        installation,
        "instruction_fragments",
        "package instruction fragments",
    )? {
        let path = fragment
            .as_object()
            .and_then(|value| value.get("path"))
            .and_then(Value::as_str)
            .ok_or_else(|| {
                LifecycleError::Invalid("instruction fragment path is invalid".to_owned())
            })?;
        require_declared_file(path, "instruction fragment", &file_by_path, &mut declared)?;
    }
    declared
        .into_iter()
        .map(|path| {
            let entry = file_by_path.get(&path).copied().ok_or_else(|| {
                LifecycleError::Invalid(format!(
                    "declared package file is missing: {package_id}:{path}"
                ))
            })?;
            canonical_source_file_entry(entry)
        })
        .collect()
}

fn collect_declared_root(
    root: &str,
    label: &str,
    files: &BTreeMap<String, &Value>,
    directories: &BTreeSet<String>,
    declared: &mut BTreeSet<String>,
) -> Result<(), LifecycleError> {
    let canonical_root = canonical_relative_path(root, label)?;
    if files.contains_key(&canonical_root) {
        declared.insert(canonical_root);
        return Ok(());
    }
    if directories.contains(&canonical_root) {
        collect_directory_files(&canonical_root, files, declared);
        return Ok(());
    }
    invalid(format!("{label} is missing: {root}"))
}

fn collect_directory_files(
    root: &str,
    files: &BTreeMap<String, &Value>,
    declared: &mut BTreeSet<String>,
) {
    let prefix = format!("{}/", root.trim_end_matches('/'));
    declared.extend(
        files
            .keys()
            .filter(|path| path.starts_with(&prefix) && !ignored_source_cache(path))
            .cloned(),
    );
}

fn require_declared_file(
    path: &str,
    label: &str,
    files: &BTreeMap<String, &Value>,
    declared: &mut BTreeSet<String>,
) -> Result<(), LifecycleError> {
    let canonical_path = canonical_relative_path(path, label)?;
    if !files.contains_key(&canonical_path) {
        return invalid(format!("{label} is missing: {path}"));
    }
    declared.insert(canonical_path);
    Ok(())
}

fn ignored_source_cache(path: &str) -> bool {
    let mut components = path.split('/');
    let mut last = "";
    for component in &mut components {
        if component == "__pycache__" {
            return true;
        }
        last = component;
    }
    last == ".DS_Store" || last.strip_suffix(".pyc").is_some()
}

fn canonical_source_file_entry(entry: &Value) -> Result<Value, LifecycleError> {
    let mut normalized = entry
        .as_object()
        .cloned()
        .ok_or_else(|| LifecycleError::Invalid("package file entry is invalid".to_owned()))?;
    let mode = u32_field(entry, "mode", "package file mode")?;
    normalized.insert(
        "mode".to_owned(),
        Value::from(if mode & 0o111 == 0 { 0o644 } else { 0o755 }),
    );
    Ok(Value::Object(normalized))
}

fn validate_package_identity(
    package_id: &str,
    record: &Value,
    locked: Option<&Value>,
    selected: Option<&Value>,
) -> Result<(), LifecycleError> {
    let Some(locked) = locked else {
        return invalid(format!(
            "installed package identity differs from persistent Lockfile: {package_id}"
        ));
    };
    let Some(selected) = selected else {
        return invalid(format!(
            "installed package identity differs from persistent Lockfile: {package_id}"
        ));
    };
    let semantic_fields = [
        "core_compatibility",
        "kind",
        "provider_compatibility",
        "provider_version",
        "version",
    ];
    for field in semantic_fields {
        if locked.get(field) != selected.get(field) {
            return invalid(format!(
                "installed package identity differs from persistent Lockfile: {package_id}"
            ));
        }
    }
    if locked.get("manifest_sha256") != record.get("manifest_sha256")
        || locked.get("provider_manifest_sha256") != record.get("provider_manifest_sha256")
        || locked
            .get("source")
            .and_then(Value::as_object)
            .and_then(|source| source.get("sha256"))
            != record.get("files_sha256")
    {
        return invalid(format!(
            "installed package identity differs from persistent Lockfile: {package_id}"
        ));
    }
    Ok(())
}

fn selected_identity(
    package_id: &str,
    manifest: &Value,
    provider: Option<&Value>,
) -> Result<Value, LifecycleError> {
    let version = string_field(manifest, "version", "installable package version")?;
    let kind = if package_id == "core" {
        "core"
    } else {
        string_field(manifest, "kind", "package kind")?
    };
    let (core_compatibility, provider_compatibility, provider_version) = if let Some(provider) =
        provider
    {
        let package = object_field(provider, "package", "provider package")?;
        let provider_contract = object_field(manifest, "provider_contract", "provider contract")?;
        (
            Value::String(
                string_map_field(package, "core_compatibility", "provider compatibility")?
                    .to_owned(),
            ),
            Value::String(
                string_map_field(
                    provider_contract,
                    "package_compatibility",
                    "package provider compatibility",
                )?
                .to_owned(),
            ),
            Value::String(string_map_field(package, "version", "provider version")?.to_owned()),
        )
    } else {
        (
            Value::String(format!("=={CORE_VERSION}")),
            Value::Null,
            Value::Null,
        )
    };
    Ok(json!({
        "core_compatibility": core_compatibility,
        "kind": kind,
        "provider_compatibility": provider_compatibility,
        "provider_version": provider_version,
        "version": version,
    }))
}

#[allow(clippy::too_many_lines)]
pub(super) fn derive_package_semantics(
    packages: &[SemanticPackage],
    package_records: &[Value],
) -> Result<Value, LifecycleError> {
    let package_ids = packages
        .iter()
        .map(|package| package.id.as_str())
        .collect::<BTreeSet<_>>();
    let mut identities = Map::new();
    let mut dependencies = Vec::new();
    let mut side_effects = BTreeSet::new();
    let mut permission_profiles = BTreeSet::new();
    let mut bindings = Map::new();
    let mut capability_permissions = BTreeMap::new();
    let mut skill_names = BTreeSet::new();
    let package_skills = packages
        .iter()
        .map(derive_skills)
        .collect::<Result<Vec<_>, _>>()?;
    for skill in package_skills.iter().flatten() {
        skill_names.insert(string_field(skill, "name", "installed Skill name")?.to_owned());
    }

    for package in packages {
        let source = package.provider.as_ref().unwrap_or(&package.manifest);
        let source_bindings = source
            .get("bindings")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        for (capability_id, binding) in source_bindings {
            if bindings.contains_key(&capability_id) {
                return invalid(format!("installation binding conflict: {capability_id}"));
            }
            let normalized = if binding.is_object() {
                binding.clone()
            } else {
                json!({"kind": "skill", "name": binding})
            };
            validate_binding_target(&capability_id, &normalized, packages, &skill_names)?;
            bindings.insert(
                capability_id,
                json!({"binding": binding, "package": package.id}),
            );
        }
        for capability in array_field(source, "capabilities", "manifest capabilities")? {
            let capability_id = string_field(capability, "id", "capability id")?;
            let (permission, effects) = capability_effects(source, capability, capability_id)?;
            capability_permissions.insert(capability_id.to_owned(), permission.to_owned());
            permission_profiles.insert(permission.to_owned());
            side_effects.extend(effects);
        }
        identities.insert(
            package.id.clone(),
            selected_identity(&package.id, &package.manifest, package.provider.as_ref())?,
        );
        for dependency in package
            .manifest
            .get("package_requires")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let target = string_field(dependency, "id", "package dependency id")?;
            let requirement =
                string_field(dependency, "requirement", "package dependency requirement")?;
            if requirement == "optional" && !package_ids.contains(target) {
                continue;
            }
            if !package_ids.contains(target) {
                return invalid(format!(
                    "installed package {} requires missing package {target}",
                    package.id
                ));
            }
            dependencies.push(json!({
                "from": package.id,
                "required_capabilities": dependency.get("required_capabilities").cloned()
                    .unwrap_or_else(|| json!([])),
                "requirement": requirement,
                "to": target,
                "version": string_field(dependency, "version", "package dependency version")?,
            }));
        }
    }
    dependencies.sort_by(|left, right| {
        let left_key = (
            left.get("from").and_then(Value::as_str),
            left.get("to").and_then(Value::as_str),
        );
        let right_key = (
            right.get("from").and_then(Value::as_str),
            right.get("to").and_then(Value::as_str),
        );
        left_key.cmp(&right_key)
    });
    let record_by_id = value_map_by_id(package_records)?;
    let mut capability_providers = Map::new();
    for (capability_id, binding) in &bindings {
        let package_id = string_field(binding, "package", "binding package")?;
        let identity = identities.get(package_id).ok_or_else(|| {
            LifecycleError::Invalid("binding package identity is missing".to_owned())
        })?;
        let record = record_by_id.get(package_id).copied().ok_or_else(|| {
            LifecycleError::Invalid("binding package record is missing".to_owned())
        })?;
        capability_providers.insert(
            capability_id.clone(),
            json!({
                "binding": binding.get("binding").cloned().unwrap_or(Value::Null),
                "package": package_id,
                "package_version": identity.get("version").cloned().unwrap_or(Value::Null),
                "permission_profile": capability_permissions.get(capability_id).cloned()
                    .ok_or_else(|| LifecycleError::Invalid(
                        format!("binding capability is not declared: {capability_id}")
                    ))?,
                "source_sha256": record.get("files_sha256").cloned().unwrap_or(Value::Null),
            }),
        );
    }
    let instructions = compose_instructions(packages)?;
    let skills = package_skills.into_iter().flatten().collect::<Vec<_>>();
    Ok(json!({
        "bindings": bindings,
        "capability_providers": capability_providers,
        "dependencies": dependencies,
        "instructions": instructions,
        "permission_profiles": permission_profiles.into_iter().collect::<Vec<_>>(),
        "selected_package_identities": identities,
        "side_effects": side_effects.into_iter().collect::<Vec<_>>(),
        "skills": skills,
    }))
}

#[derive(Clone)]
struct ResolvedRule {
    content: String,
    content_sha256: String,
    effect: String,
    id: String,
    locked: bool,
    order: usize,
    package: String,
    scope: String,
}

fn compose_instructions(packages: &[SemanticPackage]) -> Result<Value, LifecycleError> {
    let positions = packages
        .iter()
        .enumerate()
        .map(|(index, package)| (package.id.as_str(), index))
        .collect::<BTreeMap<_, _>>();
    let mut fragments = packages
        .iter()
        .flat_map(|package| package.fragments.iter().cloned())
        .collect::<Vec<_>>();
    fragments.sort_by(|left, right| {
        (
            positions
                .get(left.get("package").and_then(Value::as_str).unwrap_or(""))
                .copied()
                .unwrap_or(usize::MAX),
            left.get("order").and_then(json_integer),
            left.get("id").and_then(Value::as_str),
        )
            .cmp(&(
                positions
                    .get(right.get("package").and_then(Value::as_str).unwrap_or(""))
                    .copied()
                    .unwrap_or(usize::MAX),
                right.get("order").and_then(json_integer),
                right.get("id").and_then(Value::as_str),
            ))
    });
    let ids = fragments
        .iter()
        .map(|fragment| string_field(fragment, "id", "instruction fragment id"))
        .collect::<Result<Vec<_>, _>>()?;
    if ids.iter().collect::<BTreeSet<_>>().len() != ids.len() {
        return invalid("instruction fragment ids conflict");
    }
    let (trace, effective, fragment_text) = resolve_instruction_rules(&fragments)?;
    let mut content = concat!(
        "<!-- agent-development-skills:managed instructions-v1 -->\n",
        "# 全局 Agent Instructions\n\n",
        "> 此文件由 `agent-skills install` 确定性生成；请在源 Fragment 中修改。\n\n"
    )
    .to_owned();
    for fragment in &fragments {
        let id = string_field(fragment, "id", "instruction fragment id")?;
        let rendered = fragment_text.get(id).map_or("", String::as_str);
        if rendered.is_empty() {
            continue;
        }
        write!(
            &mut content,
            "<!-- fragment:{id} scope={} sha256={} -->\n{rendered}\n\n",
            string_field(fragment, "scope", "instruction fragment scope")?,
            string_field(fragment, "sha256", "instruction fragment hash")?,
        )
        .expect("writing to a String cannot fail");
    }
    if !effective.is_empty() {
        content.push_str("## Effective Rules\n\n");
        for rule in effective {
            write!(
                &mut content,
                "<!-- rule:{} effect={} -->\n{}\n",
                rule.id, rule.effect, rule.content
            )
            .expect("writing to a String cannot fail");
        }
        content.push('\n');
    }
    let frozen_fragments = fragments
        .iter()
        .map(|fragment| {
            let mut frozen = Map::new();
            for field in [
                "id",
                "merge_strategy",
                "order",
                "package",
                "path",
                "scope",
                "sha256",
            ] {
                frozen.insert(
                    field.to_owned(),
                    fragment.get(field).cloned().unwrap_or(Value::Null),
                );
            }
            Value::Object(frozen)
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "content": content,
        "fragments": frozen_fragments,
        "rule_trace": trace,
        "sha256": bytes_sha256(content.as_bytes()),
    }))
}

type RuleResolution = (Vec<Value>, Vec<ResolvedRule>, BTreeMap<String, String>);

#[allow(clippy::too_many_lines)]
fn resolve_instruction_rules(fragments: &[Value]) -> Result<RuleResolution, LifecycleError> {
    let mut resolved = BTreeMap::<String, ResolvedRule>::new();
    let mut trace = Vec::new();
    let mut fragment_text = BTreeMap::new();
    let mut next_order = 0_usize;
    for fragment in fragments {
        let content = string_field(fragment, "content", "instruction fragment content")?;
        let lines = python_splitlines(content);
        let mut passthrough = Vec::new();
        let mut index = 0_usize;
        while index < lines.len() {
            let Some((rule_id, effect)) = parse_rule_marker(python_trim(lines[index])) else {
                passthrough.push(lines[index]);
                index += 1;
                continue;
            };
            if index + 1 >= lines.len() || !python_trim_start(lines[index + 1]).starts_with('-') {
                return invalid(format!("instruction rule marker has no bullet: {rule_id}"));
            }
            let rule_content = python_trim(lines[index + 1]).to_owned();
            let content_sha256 = bytes_sha256(format!("{effect}\0{rule_content}").as_bytes());
            let previous = resolved.get(&rule_id).cloned();
            let candidate = ResolvedRule {
                content: rule_content,
                content_sha256,
                effect,
                id: rule_id.clone(),
                locked: string_field(fragment, "merge_strategy", "instruction merge strategy")?
                    == "locked",
                order: previous.as_ref().map_or(next_order, |rule| rule.order),
                package: string_field(fragment, "package", "instruction package")?.to_owned(),
                scope: string_field(fragment, "scope", "instruction scope")?.to_owned(),
            };
            let mut decision = "accepted";
            let winner = if let Some(previous) = previous {
                if previous.locked && previous.content_sha256 != candidate.content_sha256 {
                    return invalid(format!("locked instruction rule conflict: {rule_id}"));
                }
                if previous.content_sha256 == candidate.content_sha256 {
                    if candidate.locked && !previous.locked {
                        candidate
                    } else {
                        previous
                    }
                } else if previous.effect == "deny" || candidate.effect == "deny" {
                    decision = "deny-wins";
                    if previous.effect == "deny" {
                        previous
                    } else {
                        candidate
                    }
                } else {
                    decision = "replaced";
                    candidate
                }
            } else {
                next_order += 1;
                candidate
            };
            trace.push(json!({
                "content_sha256": winner.content_sha256,
                "decision": decision,
                "effect": winner.effect,
                "id": winner.id,
                "locked": winner.locked,
                "package": winner.package,
                "scope": winner.scope,
            }));
            resolved.insert(rule_id, winner);
            index += 2;
        }
        fragment_text.insert(
            string_field(fragment, "id", "instruction fragment id")?.to_owned(),
            python_trim(&passthrough.join("\n")).to_owned(),
        );
    }
    let mut effective = resolved.into_values().collect::<Vec<_>>();
    effective.sort_by(|left, right| (left.order, &left.id).cmp(&(right.order, &right.id)));
    Ok((trace, effective, fragment_text))
}

fn python_splitlines(value: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0_usize;
    let mut characters = value.char_indices().peekable();
    while let Some((index, character)) = characters.next() {
        if !matches!(
            character,
            '\n' | '\r'
                | '\u{000b}'
                | '\u{000c}'
                | '\u{001c}'
                | '\u{001d}'
                | '\u{001e}'
                | '\u{0085}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            continue;
        }
        lines.push(&value[start..index]);
        start = index + character.len_utf8();
        if character == '\r' && characters.peek().is_some_and(|(_, next)| *next == '\n') {
            let (newline_index, newline) = characters.next().expect("peeked newline exists");
            start = newline_index + newline.len_utf8();
        }
    }
    if start < value.len() {
        lines.push(&value[start..]);
    }
    lines
}

fn python_is_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{001c}'..='\u{001f}')
}

pub(super) fn python_trim(value: &str) -> &str {
    value.trim_matches(python_is_whitespace)
}

fn python_trim_start(value: &str) -> &str {
    value.trim_start_matches(python_is_whitespace)
}

fn parse_rule_marker(line: &str) -> Option<(String, String)> {
    let body = python_trim(line.strip_prefix("<!--")?.strip_suffix("-->")?);
    let body = body.strip_prefix("rule:")?;
    if body.chars().next().is_some_and(python_is_whitespace) {
        return None;
    }
    let mut fields = body
        .split(python_is_whitespace)
        .filter(|field| !field.is_empty());
    let identifier = fields.next()?;
    let effect = fields.next()?.strip_prefix("effect=")?;
    if fields.next().is_some() {
        return None;
    }
    if identifier.is_empty()
        || !identifier.as_bytes()[0].is_ascii_alphanumeric()
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
        || !matches!(effect, "allow" | "deny")
    {
        return None;
    }
    Some((identifier.to_owned(), effect.to_owned()))
}

fn bytes_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn derive_skills(package: &SemanticPackage) -> Result<Vec<Value>, LifecycleError> {
    let installation = object_field(&package.manifest, "installation", "package installation")?;
    let skill_roots = installation
        .get("skill_roots")
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid("package skill_roots is invalid".to_owned()))?;
    let mut skills = Vec::new();
    for root in skill_roots {
        let raw_root = root
            .as_str()
            .ok_or_else(|| LifecycleError::Invalid("package skill root is invalid".to_owned()))?;
        let root = canonical_relative_path(raw_root, "skill root")?;
        let prefix = format!("{root}/");
        let mut candidate_names = BTreeSet::new();
        for entry in &package.files {
            let path = string_field(entry, "path", "package file path")?;
            if let Some(remainder) = path.strip_prefix(&prefix)
                && let Some((candidate, suffix)) = remainder.split_once('/')
                && suffix == "SKILL.md"
            {
                candidate_names.insert(candidate.to_owned());
            }
        }
        for name in candidate_names {
            let skill_prefix = format!("{prefix}{name}/");
            let mut files = package
                .files
                .iter()
                .filter_map(|entry| {
                    let path = entry.get("path")?.as_str()?;
                    let relative = path.strip_prefix(&skill_prefix)?;
                    Some(json!({
                        "mode": entry.get("mode").cloned().unwrap_or(Value::Null),
                        "path": relative,
                        "sha256": entry.get("sha256").cloned().unwrap_or(Value::Null),
                    }))
                })
                .collect::<Vec<_>>();
            files.sort_by(|left, right| {
                left.get("path")
                    .and_then(Value::as_str)
                    .cmp(&right.get("path").and_then(Value::as_str))
            });
            let directories = directories_for_files(&files);
            skills.push(json!({
                "directories": directories,
                "file_count": files.len(),
                "files": files,
                "name": name,
                "package": package.id,
                "root_mode": MANAGED_DIRECTORY_MODE,
                "sha256": canonical_sha256(&Value::Array(files.clone()))?,
            }));
        }
    }
    Ok(skills)
}

fn directories_for_files(files: &[Value]) -> Vec<Value> {
    let mut directories = BTreeSet::new();
    for file in files {
        let Some(path) = file.get("path").and_then(Value::as_str) else {
            continue;
        };
        let mut parent = Path::new(path).parent();
        while let Some(path) = parent {
            if path.as_os_str().is_empty() {
                break;
            }
            directories.insert(path.to_string_lossy().replace('\\', "/"));
            parent = path.parent();
        }
    }
    directories
        .into_iter()
        .map(|path| json!({"mode": MANAGED_DIRECTORY_MODE, "path": path}))
        .collect()
}

fn capability_effects<'a>(
    manifest: &'a Value,
    capability: &'a Value,
    capability_id: &str,
) -> Result<(&'a str, Vec<String>), LifecycleError> {
    let prefix = capability_id
        .split_once('.')
        .map_or(capability_id, |(prefix, _)| prefix);
    let permission_key = if prefix == "implementation" {
        "implementation"
    } else if prefix == "verification" {
        "verification"
    } else {
        "detection"
    };
    let permission = capability
        .get("permission_profile")
        .and_then(Value::as_str)
        .filter(|permission| !permission.is_empty())
        .or_else(|| {
            manifest
                .get("permissions")
                .and_then(Value::as_object)
                .and_then(|permissions| permissions.get(permission_key))
                .and_then(Value::as_str)
        })
        .unwrap_or("repository-read-only");
    let effects = if let Some(effects) = capability
        .get("side_effects")
        .filter(|effects| !effects.is_null())
    {
        effects
            .as_array()
            .ok_or_else(|| {
                LifecycleError::Invalid("capability side_effects is invalid".to_owned())
            })?
            .iter()
            .map(|effect| {
                effect.as_str().map(str::to_owned).ok_or_else(|| {
                    LifecycleError::Invalid("capability side effect is invalid".to_owned())
                })
            })
            .collect::<Result<Vec<_>, _>>()?
    } else if prefix == "implementation" {
        vec!["project-files".to_owned()]
    } else if prefix == "verification" {
        vec!["validation-artifacts".to_owned()]
    } else {
        Vec::new()
    };
    Ok((permission, effects))
}

fn validate_binding_target(
    capability_id: &str,
    normalized: &Value,
    packages: &[SemanticPackage],
    skill_names: &BTreeSet<String>,
) -> Result<(), LifecycleError> {
    let kind = normalized
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("skill");
    let name = normalized.get("name").and_then(Value::as_str).unwrap_or("");
    let exists = if kind == "skill" {
        skill_names.contains(name)
    } else if kind == "tool" && name == "core.intent-lock" {
        true
    } else {
        let mut candidates = BTreeSet::new();
        for package in packages {
            for file in &package.files {
                let Some(path) = file.get("path").and_then(Value::as_str) else {
                    continue;
                };
                let path_value = Path::new(path);
                if name == path
                    || path_value.file_name().and_then(|value| value.to_str()) == Some(name)
                    || path_value.file_stem().and_then(|value| value.to_str()) == Some(name)
                {
                    candidates.insert((package.id.clone(), path.to_owned()));
                }
            }
        }
        candidates.len() == 1
    };
    if !exists {
        return invalid(format!(
            "installation binding target is missing from dependency closure: \
             {capability_id} -> {kind}:{name}"
        ));
    }
    Ok(())
}

fn validate_semantics(
    install_lock: &Value,
    package_lock: &Value,
    selected: &BTreeMap<String, &Value>,
    locked: &BTreeMap<String, &Value>,
    semantics: &Value,
) -> Result<(), LifecycleError> {
    let identities = object_field(
        semantics,
        "selected_package_identities",
        "installed package identities",
    )?;
    let fields = [
        "core_compatibility",
        "kind",
        "provider_compatibility",
        "provider_version",
        "version",
    ];
    for (package_id, expected) in identities {
        let selected_identity = selected.get(package_id).copied().ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "Lockfile package semantics differ from installed Manifests: {package_id}"
            ))
        })?;
        let locked_identity = locked.get(package_id).copied().ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "Lockfile package semantics differ from installed Manifests: {package_id}"
            ))
        })?;
        if fields.iter().any(|field| {
            selected_identity.get(field) != expected.get(field)
                || locked_identity.get(field) != expected.get(field)
        }) {
            return invalid(format!(
                "Lockfile package semantics differ from installed Manifests: {package_id}"
            ));
        }
    }
    if package_lock.get("assets_sha256")
        != Some(&Value::String(canonical_sha256(
            install_lock.get("assets").unwrap_or(&Value::Null),
        )?))
    {
        return invalid("installed asset allowlist differs from persistent Lockfile");
    }
    if package_lock.get("dependencies") != install_lock.get("resolved_dependencies") {
        return invalid("persistent Lockfile dependency closure differs from Install Lock");
    }
    if semantics.get("dependencies") != install_lock.get("resolved_dependencies") {
        return invalid("locked dependency closure differs from installed Manifests");
    }
    let expected_selection = json!({
        "disciplines": install_lock.get("selected_disciplines").cloned().unwrap_or(Value::Null),
        "platforms": install_lock.get("selected_platforms").cloned().unwrap_or(Value::Null),
        "runtime_configs": install_lock.get("selected_runtime_configs").cloned()
            .unwrap_or(Value::Null),
    });
    if package_lock.get("selection") != Some(&expected_selection) {
        return invalid("persistent Lockfile selection differs from Install Lock");
    }
    if package_lock.get("side_effects") != install_lock.get("side_effects") {
        return invalid("persistent Lockfile side effects differ from Install Lock");
    }
    if semantics.get("side_effects") != install_lock.get("side_effects") {
        return invalid("locked side effects differ from installed Manifests");
    }
    Ok(())
}

fn value_map_by_id(values: &[Value]) -> Result<BTreeMap<String, &Value>, LifecycleError> {
    values
        .iter()
        .map(|value| Ok((string_field(value, "id", "record id")?.to_owned(), value)))
        .collect()
}

fn array_field<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a [Value], LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn object_field<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a Map<String, Value>, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_object)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn map_array_field<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a [Value], LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn string_array_map_field<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<Vec<&'a str>, LifecycleError> {
    map_array_field(value, field, label)?
        .iter()
        .map(|item| {
            item.as_str()
                .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
        })
        .collect()
}

fn string_field<'a>(value: &'a Value, field: &str, label: &str) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn string_map_field<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn u32_field(value: &Value, field: &str, label: &str) -> Result<u32, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn instruction_order_and_rule_markers_match_python_contract() {
        assert_eq!(
            python_splitlines("a\r\nb\u{0085}c\u{2028}"),
            ["a", "b", "c"]
        );
        assert_eq!(python_trim("\u{001c} value \u{001f}"), "value");
        assert_eq!(
            parse_rule_marker("<!--   rule:alpha.one effect=deny   -->"),
            Some(("alpha.one".to_owned(), "deny".to_owned()))
        );
        assert_eq!(
            parse_rule_marker("<!--\u{001c}rule:alpha.one\u{001f}effect=deny\u{001d}-->"),
            Some(("alpha.one".to_owned(), "deny".to_owned()))
        );
        assert_eq!(
            parse_rule_marker("<!-- rule:_hidden effect=allow -->"),
            None
        );
        assert_eq!(
            parse_rule_marker("<!-- rule: alpha.one effect=deny -->"),
            None
        );
        let package = SemanticPackage {
            fragments: vec![
                json!({
                    "content": "Zulu\n",
                    "id": "zulu",
                    "merge_strategy": "append",
                    "order": -2,
                    "package": "core",
                    "path": "zulu.md",
                    "scope": "global",
                    "sha256": "a".repeat(64),
                }),
                json!({
                    "content": "Alpha\n",
                    "id": "alpha",
                    "merge_strategy": "append",
                    "order": -1,
                    "package": "core",
                    "path": "alpha.md",
                    "scope": "global",
                    "sha256": "b".repeat(64),
                }),
            ],
            id: "core".to_owned(),
            manifest: json!({}),
            provider: None,
            files: Vec::new(),
        };
        let instructions = compose_instructions(&[package]).expect("compose instructions");
        assert_eq!(
            instructions["fragments"]
                .as_array()
                .expect("frozen fragments")
                .iter()
                .map(|item| item["id"].as_str().expect("fragment id"))
                .collect::<Vec<_>>(),
            ["zulu", "alpha"]
        );
    }

    #[test]
    fn capability_defaults_use_prefix_null_and_truthiness_semantics() {
        let manifest = json!({
            "permissions": {"implementation": "project-write"}
        });
        let capability = json!({
            "id": "implementation",
            "permission_profile": "",
            "side_effects": null,
        });
        let (permission, effects) = capability_effects(&manifest, &capability, "implementation")
            .expect("capability defaults");
        assert_eq!(permission, "project-write");
        assert_eq!(effects, ["project-files"]);

        let manifest_empty = json!({
            "permissions": {"implementation": ""}
        });
        let (permission, _) = capability_effects(&manifest_empty, &capability, "implementation")
            .expect("manifest permission preserves Python truthiness boundary");
        assert_eq!(permission, "");
    }

    #[test]
    fn file_and_directory_walk_limits_are_independent() {
        let mut files = MAX_PACKAGE_TREE_ENTRIES - 1;
        let mut directories = MAX_PACKAGE_TREE_ENTRIES - 1;
        increment_tree_entry_count(&mut files, "file").expect("accept maximum file count");
        increment_tree_entry_count(&mut directories, "directory")
            .expect("accept maximum directory count independently");
        assert!(increment_tree_entry_count(&mut files, "file").is_err());
        assert!(increment_tree_entry_count(&mut directories, "directory").is_err());
    }

    #[test]
    fn installed_files_use_canonical_paths_modes_and_source_cache_filter() {
        let record = json!({
            "directories": [
                {"mode": 0o755, "path": "skills"},
                {"mode": 0o755, "path": "skills/example"},
                {"mode": 0o755, "path": "skills/example/__pycache__"},
            ],
            "files": [
                {"mode": 0o600, "path": "manifest.json", "sha256": "a".repeat(64)},
                {"mode": 0o600, "path": "skills/example/SKILL.md", "sha256": "b".repeat(64)},
                {"mode": 0o700, "path": "skills/example/tool", "sha256": "c".repeat(64)},
                {"mode": 0o644, "path": "skills/example/cache.pyc", "sha256": "d".repeat(64)},
                {
                    "mode": 0o644,
                    "path": "skills/example/__pycache__/cache",
                    "sha256": "e".repeat(64),
                },
                {"mode": 0o644, "path": "skills/example/.DS_Store", "sha256": "f".repeat(64)},
            ],
        });
        let manifest = json!({
            "installation": {
                "asset_roots": [],
                "instruction_fragments": [],
                "skill_roots": ["./skills//"],
            }
        });
        let files =
            validate_installation_roots(&record, "core", &manifest).expect("installed files");
        assert_eq!(
            files
                .iter()
                .map(|entry| (
                    entry["path"].as_str().expect("path"),
                    entry["mode"].as_u64().expect("mode"),
                ))
                .collect::<Vec<_>>(),
            [
                ("manifest.json", 0o644),
                ("skills/example/SKILL.md", 0o644),
                ("skills/example/tool", 0o755),
            ]
        );
        assert_eq!(
            canonical_relative_path("./provider//manifest.json/", "provider").expect("canonical"),
            "provider/manifest.json"
        );
    }
}
