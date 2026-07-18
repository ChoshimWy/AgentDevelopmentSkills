use crate::{
    LifecycleError, MAX_CONTRACT_JSON_BYTES, configure_nofollow, open_child_directory,
    open_root_directory, same_content_state_cap, same_object_cap,
};
use agent_contracts::{canonical_json, canonical_sha256, parse_json};
use agent_registry::validate_manifest_syntax;
use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt as _};
use cap_std::fs::{Dir, OpenOptions};
use serde_json::{Map, Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Read as _;
use std::path::{Component, Path};

use super::packages::python_trim;
use super::source_install::SourceInstallSelection;

const MAX_SOURCE_TREE_ENTRIES: usize = 100_000;
const MAX_SOURCE_PATH_BYTES: usize = 4_096;
const MAX_RETAINED_SOURCE_BYTES: usize = MAX_CONTRACT_JSON_BYTES;

#[derive(Debug, Clone)]
pub(super) struct SourceSkill {
    pub(super) name: String,
    pub(super) files: Vec<Value>,
    pub(super) directories: Vec<Value>,
}

#[derive(Debug, Clone)]
pub(super) struct SourcePackage {
    pub(super) id: String,
    pub(super) manifest: Value,
    pub(super) manifest_digest: String,
    pub(super) provider: Option<Value>,
    pub(super) provider_digest: Option<String>,
    pub(super) files: Vec<Value>,
    pub(super) directories: Vec<Value>,
    pub(super) skills: Vec<SourceSkill>,
    pub(super) fragments: Vec<Value>,
}

impl SourcePackage {
    fn compatibility_projection(&self) -> Value {
        json!({
            "directories": self.directories,
            "files": self.files,
            "fragments": self.fragments,
            "id": self.id,
            "manifest": self.manifest,
            "manifest_sha256": self.manifest_digest,
            "provider": self.provider,
            "provider_manifest_sha256": self.provider_digest,
            "skills": self.skills.iter().map(|skill| json!({
                "directories": skill.directories,
                "files": skill.files,
                "name": skill.name,
            })).collect::<Vec<_>>(),
        })
    }
}

/// Immutable source package snapshots used by the native Install Bundle compiler.
///
/// This typed boundary freezes every declared package asset, provider Manifest,
/// instruction Fragment, and installable Skill without defining a new persisted
/// schema. Source roots remain private so compatibility output is host-independent.
#[derive(Debug, Clone)]
pub struct SourcePackageSet {
    pub(super) packages: Vec<SourcePackage>,
}

impl SourcePackageSet {
    /// Emit the temporary Python/Rust differential projection.
    ///
    /// This is not a persisted contract and intentionally has no schema version.
    #[must_use]
    pub fn compatibility_projection(&self) -> Value {
        json!({
            "packages": self.packages.iter()
                .map(SourcePackage::compatibility_projection)
                .collect::<Vec<_>>(),
        })
    }
}

/// Freeze the selected source package closure into immutable content records.
///
/// Directory and file traversal is capability-relative and never follows
/// symlinks. Source cache files are excluded exactly as in the Python compiler.
/// Every package is read twice and must produce the same snapshot before the
/// result is returned.
///
/// # Errors
/// Returns a fail-closed error for unsafe paths, missing declarations, malformed
/// Manifests, non-UTF-8 instruction content, excessive trees, or source mutation.
pub fn snapshot_source_packages(
    selection: &SourceInstallSelection,
) -> Result<SourcePackageSet, LifecycleError> {
    let mut packages = Vec::with_capacity(selection.package_roots().len());
    let mut retained_entries = 0_usize;
    let mut retained_bytes = 0_usize;
    let mut verification_entries = 0_usize;
    let mut verification_bytes = 0_usize;
    for (package_id, root) in selection.package_roots() {
        let package =
            load_source_package(root, package_id, &mut retained_entries, &mut retained_bytes)?;
        if selection.manifest_digest(package_id) != Some(package.manifest_digest.as_str()) {
            return invalid(format!(
                "platform manifest changed while building install plan: {package_id}"
            ));
        }
        let second = load_source_package(
            root,
            package_id,
            &mut verification_entries,
            &mut verification_bytes,
        )?;
        if package.compatibility_projection() != second.compatibility_projection() {
            return invalid(format!(
                "package files changed while building install plan: {package_id}"
            ));
        }
        packages.push(package);
    }
    Ok(SourcePackageSet { packages })
}

#[allow(clippy::too_many_lines)]
fn load_source_package(
    root_path: &Path,
    package_id: &str,
    traversal_entries: &mut usize,
    retained_bytes: &mut usize,
) -> Result<SourcePackage, LifecycleError> {
    if !safe_id(package_id) {
        return invalid(format!("platform package id is unsafe: {package_id}"));
    }
    let root = open_root_directory(root_path, None, &format!("package {package_id}"))?;
    let root_identity = root.dir_metadata()?;
    let manifest = load_json_child(&root, "manifest.json", "platform Manifest")?;
    validate_manifest_syntax(&manifest)?;
    reserve_retained_bytes(retained_bytes, canonical_json(&manifest)?.len())?;
    if manifest.get("id").and_then(Value::as_str) != Some(package_id) {
        return invalid(format!(
            "platform directory and manifest id differ: {package_id}"
        ));
    }
    let installation = manifest
        .get("installation")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            LifecycleError::Invalid(format!(
                "platform package has no installation contract: {package_id}"
            ))
        })?;

    let mut declared_roots = string_array(installation, "asset_roots")?;
    declared_roots.extend(string_array(installation, "skill_roots")?);

    let (provider, provider_digest) =
        if let Some(provider_relative) = optional_string(installation, "provider_manifest")? {
            let provider = load_json_relative(&root, provider_relative, "provider manifest")?;
            validate_manifest_syntax(&provider)?;
            reserve_retained_bytes(retained_bytes, canonical_json(&provider)?.len())?;
            if provider.get("role").and_then(Value::as_str) != Some("provider") {
                return invalid(format!(
                    "installation provider is not a provider manifest: {package_id}"
                ));
            }
            declared_roots.push(provider_relative.to_owned());
            let digest = canonical_sha256(&provider)?;
            (Some(provider), Some(digest))
        } else {
            (None, None)
        };

    let kind = required_string(&manifest, "kind", "package kind")?;
    let expected_scope = if package_id == "core" {
        "global".to_owned()
    } else {
        format!("{kind}:{package_id}")
    };
    let mut fragments = Vec::new();
    for raw in array_field(installation, "instruction_fragments")? {
        let raw = raw.as_object().ok_or_else(|| {
            LifecycleError::Invalid("instruction fragment must be an object".to_owned())
        })?;
        let path = required_string_map(raw, "path", "instruction fragment path")?;
        let scope = required_string_map(raw, "scope", "instruction fragment scope")?;
        if scope != expected_scope {
            return invalid(format!(
                "instruction fragment scope is invalid for {package_id}: {scope}"
            ));
        }
        let bytes = read_relative_file(&root, path, "instruction fragment")?;
        reserve_retained_bytes(retained_bytes, bytes.len())?;
        let text = String::from_utf8(bytes).map_err(|_| {
            LifecycleError::Invalid(format!("instruction fragment is not UTF-8: {path}"))
        })?;
        let content = format!("{}\n", python_trim(&text));
        let mut fragment = raw.clone();
        fragment.insert("content".to_owned(), Value::String(content.clone()));
        fragment.insert("package".to_owned(), Value::String(package_id.to_owned()));
        fragment.insert(
            "sha256".to_owned(),
            Value::String(bytes_sha256(content.as_bytes())),
        );
        fragments.push(Value::Object(fragment));
        declared_roots.push(path.to_owned());
    }

    let mut skills = Vec::new();
    for skill_root in string_array(installation, "skill_roots")? {
        let directory = open_relative_directory(&root, &skill_root, "skill root")
            .map_err(|_| LifecycleError::Invalid(format!("skill root is missing: {skill_root}")))?;
        for name in sorted_entry_names(&directory, traversal_entries, retained_bytes)? {
            let name_text = name.to_str().ok_or_else(|| {
                LifecycleError::Invalid("skill directory path is not UTF-8".to_owned())
            })?;
            let metadata = directory.symlink_metadata(&name)?;
            if metadata.file_type().is_symlink() {
                return invalid(format!(
                    "installation asset must not be a symlink: {skill_root}/{name_text}"
                ));
            }
            if !metadata.is_dir() {
                continue;
            }
            let candidate = open_child_directory(&directory, name_text, None, "skill directory")?;
            let skill_manifest = match candidate.symlink_metadata("SKILL.md") {
                Ok(metadata) if metadata.file_type().is_symlink() => {
                    return invalid(format!(
                        "installation asset must not be a symlink: \
                         {skill_root}/{name_text}/SKILL.md"
                    ));
                }
                Ok(metadata) if metadata.is_file() => true,
                Ok(_) | Err(_) => false,
            };
            if !skill_manifest {
                continue;
            }
            let (files, _) = snapshot_tree(&candidate, true, traversal_entries, retained_bytes)?;
            let directories = directories_for_files(&files, retained_bytes)?;
            skills.push(SourceSkill {
                name: name_text.to_owned(),
                files,
                directories,
            });
        }
    }

    let files = collect_package_files(&root, &declared_roots, traversal_entries, retained_bytes)?;
    let directories = directories_for_files(&files, retained_bytes)?;
    let current = open_root_directory(root_path, None, &format!("package {package_id}"))?;
    let current_identity = current.dir_metadata()?;
    if !same_object_cap(&root_identity, &current_identity)
        || !same_content_state_cap(&root_identity, &current_identity)
    {
        return invalid(format!(
            "package files changed while building install plan: {package_id}"
        ));
    }
    Ok(SourcePackage {
        id: package_id.to_owned(),
        manifest_digest: canonical_sha256(&manifest)?,
        manifest,
        provider,
        provider_digest,
        files,
        directories,
        skills,
        fragments,
    })
}

fn collect_package_files(
    root: &Dir,
    declared_roots: &[String],
    traversal_entries: &mut usize,
    retained_bytes: &mut usize,
) -> Result<Vec<Value>, LifecycleError> {
    let mut files = BTreeMap::new();
    files.insert(
        "manifest.json".to_owned(),
        file_record(root, "manifest.json", "manifest.json")?,
    );
    for metadata_name in ["migration-source.json", "migration-overrides.json"] {
        match root.symlink_metadata(metadata_name) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return invalid(format!(
                    "installation metadata must not be a symlink: {metadata_name}"
                ));
            }
            Ok(metadata) if metadata.is_file() => {
                files.insert(
                    metadata_name.to_owned(),
                    file_record(root, metadata_name, metadata_name)?,
                );
            }
            Ok(_) | Err(_) => {}
        }
    }
    for relative in declared_roots {
        increment_source_entry_count(traversal_entries)?;
        let components = portable_components(relative, "installation asset path")?;
        let (parent, name) = open_relative_parent(root, &components, "installation asset path")?;
        let metadata = parent.symlink_metadata(name).map_err(|_| {
            LifecycleError::Invalid(format!("installation asset path is missing: {relative}"))
        })?;
        if metadata.file_type().is_symlink() {
            return invalid(format!(
                "installation asset path must not be a symlink: {relative}"
            ));
        }
        let canonical = components.join("/");
        reserve_retained_bytes(retained_bytes, canonical.len())?;
        if metadata.is_file() {
            files.insert(canonical.clone(), file_record(&parent, name, &canonical)?);
        } else if metadata.is_dir() {
            let directory = open_child_directory(&parent, name, None, "installation asset path")?;
            collect_tree_files(
                &directory,
                &canonical,
                &mut files,
                traversal_entries,
                retained_bytes,
                true,
            )?;
        } else {
            return invalid(format!("installation asset path is missing: {relative}"));
        }
    }
    Ok(files.into_values().collect())
}

fn snapshot_tree(
    root: &Dir,
    ignore_source_cache: bool,
    traversal_entries: &mut usize,
    retained_bytes: &mut usize,
) -> Result<(Vec<Value>, Vec<Value>), LifecycleError> {
    let mut files = BTreeMap::new();
    collect_tree_files(
        root,
        "",
        &mut files,
        traversal_entries,
        retained_bytes,
        ignore_source_cache,
    )?;
    let files = files.into_values().collect::<Vec<_>>();
    let directories = directories_for_files(&files, retained_bytes)?;
    Ok((files, directories))
}

fn collect_tree_files(
    directory: &Dir,
    prefix: &str,
    files: &mut BTreeMap<String, Value>,
    entries: &mut usize,
    retained_bytes: &mut usize,
    ignore_source_cache: bool,
) -> Result<(), LifecycleError> {
    let mut pending = vec![(directory.try_clone()?, prefix.to_owned())];
    while let Some((current, current_prefix)) = pending.pop() {
        let mut children = Vec::new();
        for name in sorted_entry_names(&current, entries, retained_bytes)? {
            let name_text = name.to_str().ok_or_else(|| {
                LifecycleError::Invalid("install tree path is not UTF-8".to_owned())
            })?;
            let relative = if current_prefix.is_empty() {
                name_text.to_owned()
            } else {
                format!("{current_prefix}/{name_text}")
            };
            validate_source_path_size(&relative, "installation asset path")?;
            reserve_retained_bytes(retained_bytes, relative.len())?;
            if ignore_source_cache && ignored_source_cache(&relative) {
                continue;
            }
            let metadata = current.symlink_metadata(&name)?;
            if metadata.file_type().is_symlink() {
                return invalid(format!(
                    "installation asset must not be a symlink: {relative}"
                ));
            }
            if metadata.is_dir() {
                let child = open_child_directory(&current, name_text, None, "installation asset")?;
                children.push((child, relative));
            } else if metadata.is_file() {
                files.insert(
                    relative.clone(),
                    file_record(&current, name_text, &relative)?,
                );
            } else {
                return invalid(format!(
                    "install tree contains unsupported entry: {relative}"
                ));
            }
        }
        children.reverse();
        pending.extend(children);
    }
    Ok(())
}

fn file_record(parent: &Dir, name: &str, relative: &str) -> Result<Value, LifecycleError> {
    let metadata = parent.symlink_metadata(name)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return invalid(format!(
            "installation source changed or became unsafe: {name}"
        ));
    }
    let mut file = open_source_file(parent, name, "installation source file")?;
    let opened = file.metadata()?;
    let mode = canonical_source_mode(&opened, name);
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let after = file.metadata()?;
    let current = open_source_file(parent, name, "installation source file")?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
        || canonical_source_mode(&after, name) != mode
        || canonical_source_mode(&current, name) != mode
    {
        return invalid(format!("installation source changed while reading: {name}"));
    }
    Ok(json!({
        "mode": mode,
        "path": relative,
        "sha256": format!("{:x}", digest.finalize()),
    }))
}

fn read_relative_file(root: &Dir, relative: &str, label: &str) -> Result<Vec<u8>, LifecycleError> {
    let components = portable_components(relative, label)?;
    let (parent, name) = open_relative_parent(root, &components, label)?;
    let metadata = parent.symlink_metadata(name).map_err(|_| {
        LifecycleError::Invalid(format!("{label} is missing or unsafe: {relative}"))
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return invalid(format!("{label} is missing or unsafe: {relative}"));
    }
    let mut file = open_source_file(&parent, name, label)?;
    let opened = file.metadata()?;
    if opened.len() > MAX_CONTRACT_JSON_BYTES as u64 {
        return invalid(format!("{label} is too large: {relative}"));
    }
    let mut bytes = Vec::with_capacity(
        usize::try_from(opened.len())
            .unwrap_or(MAX_CONTRACT_JSON_BYTES)
            .min(MAX_CONTRACT_JSON_BYTES),
    );
    file.by_ref()
        .take((MAX_CONTRACT_JSON_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_CONTRACT_JSON_BYTES {
        return invalid(format!("{label} is too large: {relative}"));
    }
    let after = file.metadata()?;
    let current = open_source_file(&parent, name, label)?.metadata()?;
    if !same_object_cap(&opened, &after)
        || !same_object_cap(&opened, &current)
        || !same_content_state_cap(&opened, &after)
        || !same_content_state_cap(&opened, &current)
    {
        return invalid(format!("{label} changed while reading: {relative}"));
    }
    Ok(bytes)
}

fn load_json_child(root: &Dir, name: &str, label: &str) -> Result<Value, LifecycleError> {
    load_json_relative(root, name, label)
}

fn load_json_relative(root: &Dir, relative: &str, label: &str) -> Result<Value, LifecycleError> {
    Ok(parse_json(&read_relative_file(root, relative, label)?)?)
}

fn open_relative_directory(root: &Dir, relative: &str, label: &str) -> Result<Dir, LifecycleError> {
    let mut directory = root.try_clone()?;
    for component in portable_components(relative, label)? {
        directory = open_child_directory(&directory, component, None, label)?;
    }
    Ok(directory)
}

fn open_source_file(
    parent: &Dir,
    name: &str,
    label: &str,
) -> Result<cap_std::fs::File, LifecycleError> {
    let before = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    if before.file_type().is_symlink() || !before.is_file() {
        return invalid(format!("{label} is missing or unsafe"));
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    configure_nofollow(&mut options);
    let file = parent
        .open_with(name, &options)
        .map_err(|_| LifecycleError::Invalid(format!("{label} is missing or unsafe")))?;
    let opened = file.metadata()?;
    let after = parent
        .symlink_metadata(name)
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    let current = parent
        .open_with(name, &options)
        .and_then(|current| current.metadata())
        .map_err(|_| LifecycleError::Invalid(format!("{label} changed while opening")))?;
    if after.file_type().is_symlink() || !after.is_file() || !same_object_cap(&opened, &current) {
        return invalid(format!("{label} changed while opening"));
    }
    Ok(file)
}

fn open_relative_parent<'a>(
    root: &Dir,
    components: &'a [&'a str],
    label: &str,
) -> Result<(Dir, &'a str), LifecycleError> {
    let (name, parents) = components
        .split_last()
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} is invalid")))?;
    let mut directory = root.try_clone()?;
    for parent in parents {
        directory = open_child_directory(&directory, parent, None, label)?;
    }
    Ok((directory, name))
}

fn portable_components<'a>(relative: &'a str, label: &str) -> Result<Vec<&'a str>, LifecycleError> {
    if relative.is_empty() || relative.starts_with('/') || relative.contains('\\') {
        return invalid(format!("{label} must be a package-relative path"));
    }
    validate_source_path_size(relative, label)?;
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

fn validate_source_path_size(relative: &str, label: &str) -> Result<(), LifecycleError> {
    if relative.len() > MAX_SOURCE_PATH_BYTES {
        return invalid(format!(
            "{label} exceeds maximum path size of {MAX_SOURCE_PATH_BYTES} bytes"
        ));
    }
    Ok(())
}

fn directories_for_files(
    files: &[Value],
    retained_bytes: &mut usize,
) -> Result<Vec<Value>, LifecycleError> {
    let mut paths = BTreeSet::new();
    for file in files {
        let path = required_string(file, "path", "source file path")?;
        let mut parts = path.split('/').collect::<Vec<_>>();
        parts.pop();
        while !parts.is_empty() {
            let parent = parts.join("/");
            validate_source_path_size(&parent, "installation directory path")?;
            if paths.insert(parent.clone()) {
                reserve_retained_bytes(retained_bytes, parent.len())?;
            }
            parts.pop();
        }
    }
    Ok(paths
        .into_iter()
        .map(|path| json!({"mode": 0o755, "path": path}))
        .collect())
}

fn sorted_entry_names(
    directory: &Dir,
    entries: &mut usize,
    retained_bytes: &mut usize,
) -> Result<Vec<std::ffi::OsString>, LifecycleError> {
    let mut names = Vec::new();
    for entry in directory.entries()? {
        increment_source_entry_count(entries)?;
        let name = entry?.file_name();
        reserve_retained_bytes(retained_bytes, name.as_encoded_bytes().len())?;
        names.push(name);
    }
    names.sort();
    Ok(names)
}

fn increment_source_entry_count(entries: &mut usize) -> Result<(), LifecycleError> {
    *entries = entries
        .checked_add(1)
        .ok_or_else(|| LifecycleError::Invalid("source tree entry counter overflow".to_owned()))?;
    if *entries > MAX_SOURCE_TREE_ENTRIES {
        return invalid(format!(
            "source tree exceeds maximum of {MAX_SOURCE_TREE_ENTRIES} entries"
        ));
    }
    Ok(())
}

fn reserve_retained_bytes(total: &mut usize, additional: usize) -> Result<(), LifecycleError> {
    *total = total.checked_add(additional).ok_or_else(|| {
        LifecycleError::Invalid("source snapshot retained byte counter overflow".to_owned())
    })?;
    if *total > MAX_RETAINED_SOURCE_BYTES {
        return invalid(format!(
            "source snapshot exceeds maximum retained content of \
             {MAX_RETAINED_SOURCE_BYTES} bytes"
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn canonical_source_mode(metadata: &cap_std::fs::Metadata, _name: &str) -> u32 {
    use cap_std::fs::MetadataExt as _;
    if metadata.mode() & 0o111 == 0 {
        0o644
    } else {
        0o755
    }
}

#[cfg(not(unix))]
fn canonical_source_mode(_metadata: &cap_std::fs::Metadata, _name: &str) -> u32 {
    0o644
}

fn ignored_source_cache(relative: &str) -> bool {
    let path = Path::new(relative);
    path.components()
        .any(|component| component.as_os_str() == "__pycache__")
        || path.file_name().is_some_and(|name| name == ".DS_Store")
        || path.extension().is_some_and(|extension| extension == "pyc")
}

fn safe_id(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

fn string_array(object: &Map<String, Value>, field: &str) -> Result<Vec<String>, LifecycleError> {
    array_field(object, field)?
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| LifecycleError::Invalid(format!("{field} must contain strings")))
        })
        .collect()
}

fn array_field<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<&'a Vec<Value>, LifecycleError> {
    object
        .get(field)
        .and_then(Value::as_array)
        .ok_or_else(|| LifecycleError::Invalid(format!("{field} must be an array")))
}

fn optional_string<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<Option<&'a str>, LifecycleError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        Some(_) => invalid(format!("{field} must be a string or null")),
    }
}

fn required_string<'a>(
    value: &'a Value,
    field: &str,
    label: &str,
) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} field {field} must be a string")))
}

fn required_string_map<'a>(
    value: &'a Map<String, Value>,
    field: &str,
    label: &str,
) -> Result<&'a str, LifecycleError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| LifecycleError::Invalid(format!("{label} field {field} must be a string")))
}

fn bytes_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(LifecycleError::Invalid(message.into()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::resolve_source_install_selection;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct Fixture {
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "agent-source-packages-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            let package = root.join("platforms/core");
            std::fs::create_dir_all(&package).expect("create package");
            let manifest = json!({
                "bindings": {},
                "capabilities": [],
                "conflicts": [],
                "detection": {"medium": [], "strong": [], "weak": []},
                "id": "core",
                "installation": {
                    "asset_roots": [],
                    "instruction_fragments": [],
                    "skill_roots": [],
                },
                "kind": "adapter",
                "optional_requires": [],
                "package_requires": [],
                "permissions": {"detection": "repository-read-only"},
                "requires": [],
                "schema_version": "1.0",
                "targets": [],
                "version": "0.1.0",
            });
            std::fs::write(
                package.join("manifest.json"),
                agent_contracts::canonical_json(&manifest).expect("encode manifest"),
            )
            .expect("write manifest");
            Self { root }
        }

        fn platforms(&self) -> PathBuf {
            self.root.join("platforms")
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn snapshot_is_bound_to_the_selected_manifest_identity() {
        let fixture = Fixture::new();
        let selection = resolve_source_install_selection(fixture.platforms(), &[], &[], &[], true)
            .expect("resolve selection");
        let manifest_path = fixture.platforms().join("core/manifest.json");
        let mut manifest = agent_contracts::load_json(&manifest_path).expect("load manifest");
        manifest["version"] = Value::String("0.1.1".to_owned());
        std::fs::write(
            manifest_path,
            agent_contracts::canonical_json(&manifest).expect("encode changed manifest"),
        )
        .expect("change manifest");

        let error = snapshot_source_packages(&selection).expect_err("manifest drift fails closed");
        assert!(
            error
                .to_string()
                .contains("platform manifest changed while building install plan")
        );
    }

    #[cfg(unix)]
    #[test]
    fn noncanonical_source_mode_is_normalized_instead_of_rejected() {
        use std::os::unix::fs::PermissionsExt as _;

        let fixture = Fixture::new();
        let manifest_path = fixture.platforms().join("core/manifest.json");
        std::fs::set_permissions(&manifest_path, std::fs::Permissions::from_mode(0o600))
            .expect("set private source mode");
        let selection = resolve_source_install_selection(fixture.platforms(), &[], &[], &[], true)
            .expect("resolve selection");
        let snapshot = snapshot_source_packages(&selection).expect("snapshot private source");
        assert_eq!(
            snapshot.compatibility_projection()["packages"][0]["files"][0]["mode"],
            json!(0o644)
        );
    }

    #[test]
    fn directory_enumeration_enforces_the_limit_before_collecting() {
        let fixture = Fixture::new();
        let package = fixture.platforms().join("core");
        std::fs::write(package.join("a"), b"a").expect("write entry");
        std::fs::write(package.join("b"), b"b").expect("write entry");
        let directory = open_root_directory(&package, None, "fixture").expect("open fixture");
        let mut entries = MAX_SOURCE_TREE_ENTRIES - 1;
        let mut retained = 0;
        let error = sorted_entry_names(&directory, &mut entries, &mut retained)
            .expect_err("second entry exceeds limit");
        assert!(error.to_string().contains("source tree exceeds maximum"));
    }

    #[test]
    fn retained_content_budget_is_aggregate_and_overflow_safe() {
        let mut retained = MAX_RETAINED_SOURCE_BYTES - 1;
        reserve_retained_bytes(&mut retained, 1).expect("exact limit is accepted");
        assert!(reserve_retained_bytes(&mut retained, 1).is_err());
        let mut retained = usize::MAX;
        assert!(reserve_retained_bytes(&mut retained, 1).is_err());
    }

    #[test]
    fn discovered_paths_share_the_portable_path_limit() {
        let accepted = "x".repeat(MAX_SOURCE_PATH_BYTES);
        validate_source_path_size(&accepted, "fixture").expect("exact limit is accepted");
        let rejected = format!("{accepted}x");
        assert!(validate_source_path_size(&rejected, "fixture").is_err());
    }
}
